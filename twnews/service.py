"""Orchestration: polling, flash/regular briefings, push, daemon lifecycle and --once mode."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .briefing import BriefItem, build_briefing, prune_briefings
from .fileserver import FileServer
from .http import FetchError, HttpFetcher
from .push import PushMessage, Pusher, build_channels
from .sources import RawItem, Source, collect, enrich
from .storage import NewItem, Storage
from .textutil import (TAIPEI, classify_topic, content_hash, iso, match_keywords, now_taipei, parse_iso,
                       summary_is_redundant, summarize)

log = logging.getLogger("twnews.service")


@dataclass
class SourceReport:
    source_id: str
    name: str
    ok: bool
    via: str
    fetched: int
    new: int
    flash: int
    errors: list[str]


class Monitor:
    def __init__(self, cfg: dict, transport: httpx.AsyncBaseTransport | None = None,
                 push_transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self.storage = Storage(cfg["paths"]["database"])
        self.fetcher = HttpFetcher(cfg["fetch"], self.storage, transport=transport)
        self.sources = [Source.from_config(s, cfg["fetch"]) for s in cfg["sources"] if s.get("enabled", True)]
        self.source_names = {s.id: s.name for s in self.sources}
        self.keywords: list[str] = [str(k) for k in cfg["watch_keywords"]]
        self.topics: dict[str, list[str]] = {k: [str(x) for x in v] for k, v in (cfg["topics"] or {}).items()}
        self.pusher = Pusher(build_channels(cfg["push"]["channels"]), cfg["fetch"]["user_agent"],
                             transport=push_transport)
        self.brief_lock = asyncio.Lock()
        self.poll_lock = asyncio.Lock()
        self.last_flash_at = 0.0
        self._tasks: set[asyncio.Task] = set()
        self.file_server: FileServer | None = None
        self.last_push_results: list[tuple[str, bool, str]] = []

    # ------------------------------------------------------------------ polling
    async def poll_cycle(self, force: bool = False) -> list[SourceReport]:
        if self.poll_lock.locked():
            log.debug("previous poll still running; skipping")
            return []
        async with self.poll_lock:
            now = time.monotonic()
            due = [s for s in self.sources if force or s.is_due(now)]
            if not due:
                return []
            sem = asyncio.Semaphore(self.cfg["fetch"]["max_concurrency"])

            async def run(src: Source) -> SourceReport:
                async with sem:
                    return await self._poll_source(src)

            reports = list(await asyncio.gather(*(run(s) for s in due)))
            new_total = sum(r.new for r in reports)
            if new_total:
                log.info("poll cycle complete", extra={"sources": len(due), "new_items": new_total,
                                                        "flash_candidates": sum(r.flash for r in reports)})
            await self.maybe_flash()
            return reports

    async def _poll_source(self, src: Source) -> SourceReport:
        started = time.monotonic()
        try:
            result = await collect(src, self.fetcher)
        except Exception as exc:  # parser bugs etc. must not kill the loop
            log.exception("source crashed", extra={"source": src.id})
            src.record(False, time.monotonic(), self.cfg["fetch"]["source_backoff_max_seconds"])
            return SourceReport(src.id, src.name, False, "", 0, 0, 0, [repr(exc)])

        src.record(result.ok, time.monotonic(), self.cfg["fetch"]["source_backoff_max_seconds"])
        if not result.ok:
            log.warning("source unavailable; backing off", extra={"source": src.id, "failures": src.failures,
                                                                   "retry_in_s": round(src.backoff_until - time.monotonic())})
            return SourceReport(src.id, src.name, False, "", 0, 0, 0, result.errors)

        bootstrap_key = f"bootstrapped:{src.id}"
        bootstrapped = self.storage.get_state(bootstrap_key) is not None
        new_ids, flash_count, to_enrich = [], 0, []
        for raw in result.items:
            item_id, is_flash = self._store(src, raw, flash_allowed=bootstrapped or not self.cfg["flash"]["skip_initial_backlog"])
            if item_id is not None:
                new_ids.append(item_id)
                flash_count += is_flash
                if src.enrich and raw.via == "html" and not raw.summary:
                    to_enrich.append((item_id, raw.link))
        if not bootstrapped and result.items:
            self.storage.set_state(bootstrap_key, iso(now_taipei()))

        await self._enrich(src, to_enrich)
        log.debug("source polled", extra={"source": src.id, "via": result.via, "fetched": len(result.items),
                                          "new": len(new_ids), "ms": int((time.monotonic() - started) * 1000)})
        return SourceReport(src.id, src.name, True, result.via, len(result.items), len(new_ids), flash_count,
                            result.errors)

    def _store(self, src: Source, raw: RawItem, flash_allowed: bool) -> tuple[int | None, bool]:
        summary = "" if summary_is_redundant(raw.title, raw.summary) else summarize(raw.summary)
        text = f"{raw.title} {summary}"
        matched = match_keywords(text, self.keywords)
        max_age = timedelta(minutes=self.cfg["flash"]["max_age_minutes"])
        fresh = raw.published is None or raw.published >= now_taipei() - max_age
        is_flash = bool(self.cfg["flash"]["enabled"] and matched and flash_allowed and fresh)
        item = NewItem(
            source=src.id, source_name=src.name, guid=raw.guid, title=raw.title, link=raw.link,
            published_at=raw.published, summary=summary, hash=content_hash(raw.title, summary),
            topic=classify_topic(text, self.topics), keywords=matched, via=raw.via,
            flash_state=1 if is_flash else 0,
        )
        item_id = self.storage.insert_item(item)
        return item_id, (item_id is not None and is_flash)

    async def _enrich(self, src: Source, pending: list[tuple[int, str]]) -> None:
        limit = self.cfg["fetch"]["enrich_max_per_cycle"]
        for item_id, link in pending[:limit]:
            try:
                text, published = await enrich(self.fetcher, link)
            except FetchError as exc:
                log.debug("enrichment failed", extra={"source": src.id, "url": link, "error": str(exc)})
                continue
            except Exception:
                log.exception("enrichment crashed", extra={"source": src.id, "url": link})
                continue
            self.storage.update_item_enrichment(item_id, summarize(text), published)

    # ------------------------------------------------------------------ briefings
    def _row_to_item(self, row) -> BriefItem:
        return BriefItem(
            source_name=row["source_name"], title=row["title"], link=row["link"],
            published=parse_iso(row["published_at"]), fetched=parse_iso(row["fetched_at"]),
            summary=row["summary"], topic=row["topic"],
            keywords=[k for k in row["keywords"].split(",") if k],
        )

    async def maybe_flash(self) -> None:
        if not self.cfg["flash"]["enabled"]:
            return
        if time.monotonic() - self.last_flash_at < self.cfg["flash"]["cooldown_seconds"]:
            return  # still cooling down: pending items accumulate into the next flash
        rows = self.storage.pending_flash()
        if not rows:
            return
        self.last_flash_at = time.monotonic()
        items = [self._row_to_item(r) for r in rows]
        triggers = sorted({k for it in items for k in it.keywords})
        log.info("compiling flash briefing", extra={"items": len(items), "keywords": triggers})
        await self.compile_and_push("flash", items, triggers=triggers)
        self.storage.mark_flash_sent(r["id"] for r in rows)

    async def regular_briefing(self) -> Path | None:
        b = self.cfg["briefing"]
        now = now_taipei()
        last = parse_iso(self.storage.get_state("last_regular_at")) or now - timedelta(minutes=b["interval_minutes"])
        rows = self.storage.items_for_window(last, last - timedelta(minutes=b["late_grace_minutes"]), b["max_items"])
        self.storage.set_state("last_regular_at", iso(now))
        if not rows and b["skip_if_empty"]:
            log.info("no new items; regular briefing skipped", extra={"since": iso(last)})
            return None
        items = [self._row_to_item(r) for r in rows]
        log.info("compiling regular briefing", extra={"items": len(items), "since": iso(last)})
        return await self.compile_and_push("regular", items, window=(last, now))

    async def compile_and_push(self, kind: str, items: list[BriefItem], *, triggers: list[str] | None = None,
                               window: tuple[datetime, datetime] | None = None,
                               title_prefix: str = "") -> Path | None:
        b = self.cfg["briefing"]
        async with self.brief_lock:
            generated_at = now_taipei()
            try:
                path = await asyncio.to_thread(
                    build_briefing, items, kind=kind, generated_at=generated_at,
                    out_dir=self.cfg["paths"]["briefings"], title=b["title"], group_by=b["group_by"],
                    font=b["font"], source_order=[s.name for s in self.sources],
                    topic_order=list(self.topics), triggers=triggers, window=window)
            except Exception:
                log.exception("briefing generation failed", extra={"kind": kind})
                return None
            removed = prune_briefings(self.cfg["paths"]["briefings"], b["keep_latest"])
            log.info("briefing written", extra={"file": str(path), "items": len(items), "pruned": len(removed)})
            self.last_push_results = await self.pusher.push(
                self._push_message(kind, items, path, generated_at, triggers, title_prefix))
            return path

    def _file_url(self, path: Path) -> str | None:
        links = self.cfg["links"]
        base = (links.get("public_base_url") or "").rstrip("/")
        if not base:
            return None
        fs = links["file_server"]
        if fs.get("enabled"):
            return f"{base}/{quote(fs['token'])}/{quote(path.name)}"
        return f"{base}/{quote(path.name)}"

    def _push_message(self, kind: str, items: list[BriefItem], path: Path, generated_at: datetime,
                      triggers: list[str] | None, title_prefix: str) -> PushMessage:
        label = {"flash": "【快讯】", "sample": "【测试】"}.get(kind, "")
        title = f"{title_prefix}{label}{self.cfg['briefing']['title']} {generated_at:%m-%d %H:%M} · {len(items)}条"
        lines = []
        if triggers:
            lines.append("触发关键词：" + "、".join(triggers))
        n = self.cfg["push"]["headline_count"]
        ordered = sorted(items, key=lambda i: i.published or i.fetched, reverse=True)
        for it in ordered[:n]:
            lines.append(f"• {it.title}（{it.source_name}）")
        if len(items) > n:
            lines.append(f"……另有 {len(items) - n} 条，详见完整简报")
        return PushMessage(title=title, body="\n".join(lines) or "本时段无新闻", kind=kind,
                           file_path=path, file_url=self._file_url(path))

    async def housekeeping(self) -> None:
        removed = self.storage.prune_items(self.cfg["storage"]["retain_days"])
        if removed:
            log.info("pruned old items", extra={"removed": removed})

    # ------------------------------------------------------------------ lifecycle
    def _start_file_server(self) -> None:
        fs = self.cfg["links"]["file_server"]
        if fs.get("enabled"):
            self.file_server = FileServer(self.cfg["paths"]["briefings"], fs["bind"], int(fs["port"]), fs["token"])
            self.file_server.start()

    async def close(self) -> None:
        if self.file_server:
            self.file_server.stop()
        await self.fetcher.aclose()
        await self.pusher.aclose()
        self.storage.close()

    def _tracked(self, coro_fn):
        async def wrapper():
            task = asyncio.current_task()
            self._tasks.add(task)
            try:
                await coro_fn()
            except Exception:
                log.exception("scheduled job failed", extra={"job": coro_fn.__name__})
            finally:
                self._tasks.discard(task)
        wrapper.__name__ = coro_fn.__name__
        return wrapper

    def _regular_trigger(self):
        n = self.cfg["briefing"]["interval_minutes"]
        if self.cfg["briefing"]["align_to_clock"]:
            if n < 60 and 60 % n == 0:
                return CronTrigger(minute=f"*/{n}", timezone=TAIPEI)
            if n % 60 == 0 and 1440 % n == 0:
                return CronTrigger(hour=f"*/{n // 60}", minute=0, timezone=TAIPEI)
        return IntervalTrigger(minutes=n, timezone=TAIPEI)

    async def run_forever(self) -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        _install_signal_handlers(loop, stop)
        self._start_file_server()

        scheduler = AsyncIOScheduler(timezone=TAIPEI, job_defaults={"coalesce": True, "max_instances": 1,
                                                                     "misfire_grace_time": 60})
        scheduler.add_job(self._tracked(self.poll_cycle), IntervalTrigger(
            seconds=self.cfg["fetch"]["poll_interval_seconds"], timezone=TAIPEI),
            id="poll", next_run_time=datetime.now(TAIPEI))
        scheduler.add_job(self._tracked(self.regular_briefing), self._regular_trigger(), id="regular")
        scheduler.add_job(self._tracked(self.housekeeping), IntervalTrigger(hours=24, timezone=TAIPEI),
                          id="housekeeping", next_run_time=datetime.now(TAIPEI) + timedelta(minutes=5))
        scheduler.start()
        log.info("monitor started", extra={
            "sources": len(self.sources), "keywords": len(self.keywords),
            "poll_s": self.cfg["fetch"]["poll_interval_seconds"],
            "briefing_min": self.cfg["briefing"]["interval_minutes"],
            "push_channels": [c.name for c in self.pusher.channels],
            "next_briefing": iso(scheduler.get_job("regular").next_run_time)})
        try:
            await stop.wait()
        finally:
            log.info("shutting down")
            scheduler.shutdown(wait=False)
            if self._tasks:
                done, pending = await asyncio.wait(set(self._tasks), timeout=25)
                for t in pending:
                    t.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
            await self.close()
            log.info("shutdown complete")

    async def run_once(self, push: bool = True) -> int:
        """Smoke test: poll every source once, print a report, build one sample briefing, push once."""
        print(f"Polling {len(self.sources)} sources once ({now_taipei():%Y-%m-%d %H:%M:%S} Asia/Taipei)...\n")
        reports = await self.poll_cycle(force=True)
        width = max([len(r.name) for r in reports] + [6])
        print(f"{'source':<{width}}  {'status':<6} {'via':<5} {'fetched':>7} {'new':>5}  notes")
        print("-" * (width + 40))
        for r in reports:
            note = " | ".join(e[:120] for e in r.errors[:2])
            print(f"{r.name:<{width}}  {'OK' if r.ok else 'FAIL':<6} {r.via or '-':<5} {r.fetched:>7} {r.new:>5}  {note}")
        ok = sum(r.ok for r in reports)
        print(f"\n{ok}/{len(reports)} sources OK, {sum(r.fetched for r in reports)} items fetched, "
              f"{sum(r.new for r in reports)} new, {self.storage.count_items()} in database.")

        rows = self.storage.latest_items(min(60, self.cfg["briefing"]["max_items"]))
        items = [self._row_to_item(r) for r in rows]
        if push:
            path = await self.compile_and_push("sample", items)
        else:
            path = await asyncio.to_thread(
                build_briefing, items, kind="sample", generated_at=now_taipei(),
                out_dir=self.cfg["paths"]["briefings"], title=self.cfg["briefing"]["title"],
                group_by=self.cfg["briefing"]["group_by"], font=self.cfg["briefing"]["font"],
                source_order=[s.name for s in self.sources], topic_order=list(self.topics))
        print(f"\nSample briefing: {path}")
        if push:
            if not self.last_push_results:
                print("Test push: no push channels enabled in config.yaml")
            for name, success, detail in self.last_push_results:
                print(f"Test push via {name}: {'OK (' + detail + ')' if success else 'FAILED: ' + detail}")
        await self.close()
        return 0 if ok else 1


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    def _request_stop(signame: str) -> None:
        log.info("signal received", extra={"signal": signame})
        stop.set()

    sigs = [signal.SIGINT, signal.SIGTERM]
    if sys.platform == "win32" and hasattr(signal, "SIGBREAK"):
        sigs.append(signal.SIGBREAK)
    for sig in sigs:
        try:
            loop.add_signal_handler(sig, _request_stop, sig.name)
        except (NotImplementedError, RuntimeError):  # Windows event loops
            signal.signal(sig, lambda s, _f: loop.call_soon_threadsafe(_request_stop, signal.Signals(s).name))
