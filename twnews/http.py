"""Polite HTTP fetching: robots.txt, per-host spacing, conditional GET, retry with backoff."""

from __future__ import annotations

import asyncio
import logging
import random
import time
import urllib.robotparser
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx

from .storage import Storage

log = logging.getLogger("twnews.http")

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class FetchError(Exception):
    pass


class RobotsDisallowed(FetchError):
    pass


class _Retryable(Exception):
    def __init__(self, msg: str, retry_after: float | None = None):
        super().__init__(msg)
        self.retry_after = retry_after


@dataclass
class FetchResult:
    url: str
    status: int
    content: bytes
    text: str
    headers: httpx.Headers

    @property
    def not_modified(self) -> bool:
        return self.status == 304


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError):
            return None


class HttpFetcher:
    def __init__(self, fetch_cfg: dict, storage: Storage, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = fetch_cfg
        self.storage = storage
        self.client = httpx.AsyncClient(
            headers={
                "User-Agent": fetch_cfg["user_agent"],
                "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.6",
                "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/html;q=0.8, */*;q=0.5",
            },
            timeout=httpx.Timeout(fetch_cfg["timeout_seconds"]),
            follow_redirects=True,
            transport=transport,
        )
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._host_last: dict[str, float] = {}
        self._robots: dict[str, tuple[urllib.robotparser.RobotFileParser, float]] = {}
        self._robots_unreachable: dict[str, str] = {}
        self._robots_locks: dict[str, asyncio.Lock] = {}

    async def aclose(self) -> None:
        await self.client.aclose()

    # -- per-host rate limiting -------------------------------------------
    async def _wait_for_host(self, host: str) -> None:
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        async with lock:
            gap = self.cfg["host_min_gap_seconds"]
            wait = self._host_last.get(host, 0.0) + gap - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._host_last[host] = time.monotonic()

    # -- robots.txt (RFC 9309 semantics) ----------------------------------
    async def _robots_for(self, url: str) -> str | None:
        """Return None if `url` may be fetched, otherwise the reason it may not."""
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        lock = self._robots_locks.setdefault(origin, asyncio.Lock())
        async with lock:
            cached = self._robots.get(origin)
            if cached is None or cached[1] < time.monotonic():
                cached = await self._load_robots(origin)
                self._robots[origin] = cached
        if cached[0].can_fetch(self.cfg["robots_user_agent"], url):
            return None
        if origin in self._robots_unreachable:
            return f"robots.txt unreachable for {origin} ({self._robots_unreachable[origin]}); treated as disallow"
        return f"robots.txt disallows {url}"

    async def _load_robots(self, origin: str) -> tuple[urllib.robotparser.RobotFileParser, float]:
        rp = urllib.robotparser.RobotFileParser()
        ttl = self.cfg["robots_cache_seconds"]
        self._robots_unreachable.pop(origin, None)
        try:
            resp = await self._request(f"{origin}/robots.txt", {})
        except FetchError as exc:
            # Unreachable robots.txt: assume full disallow, re-check soon (RFC 9309 §2.3.1.4).
            log.warning("robots.txt unreachable; treating host as disallowed", extra={"origin": origin, "error": str(exc)})
            rp.disallow_all = True
            self._robots_unreachable[origin] = str(exc).split(": ", 1)[-1]
            return rp, time.monotonic() + 600
        if resp.status_code >= 500:
            rp.disallow_all = True
            self._robots_unreachable[origin] = f"HTTP {resp.status_code}"
            ttl = 600
        elif resp.status_code >= 400:
            rp.allow_all = True  # 4xx: no restrictions
        else:
            rp.parse(resp.text.splitlines())
        log.debug("robots.txt loaded", extra={"origin": origin, "status": resp.status_code})
        return rp, time.monotonic() + ttl

    # -- core request with retries -----------------------------------------
    async def _request(self, url: str, headers: dict[str, str]) -> httpx.Response:
        host = urlsplit(url).netloc
        retries = self.cfg["retries"]
        base = self.cfg["backoff_base_seconds"]
        cap = self.cfg["backoff_max_seconds"]
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            await self._wait_for_host(host)
            try:
                resp = await self.client.get(url, headers=headers)
                if resp.status_code in RETRYABLE_STATUS:
                    raise _Retryable(f"HTTP {resp.status_code}", _retry_after_seconds(resp.headers.get("Retry-After")))
                return resp
            except (httpx.TransportError, _Retryable) as exc:
                last_exc = exc
                if attempt >= retries:
                    break
                delay = min(cap, base * (2 ** attempt)) + random.uniform(0, base)
                if isinstance(exc, _Retryable) and exc.retry_after is not None:
                    delay = min(cap, max(delay, exc.retry_after))
                log.debug("retrying fetch", extra={"url": url, "attempt": attempt + 1, "delay": round(delay, 2),
                                                   "error": str(exc)})
                await asyncio.sleep(delay)
        raise FetchError(f"{url}: {last_exc}")

    async def get(self, url: str, *, conditional: bool = True) -> FetchResult:
        if self.cfg["respect_robots"]:
            reason = await self._robots_for(url)
            if reason:
                raise RobotsDisallowed(reason)
        headers: dict[str, str] = {}
        if conditional:
            etag, last_modified = self.storage.get_validators(url)
            if etag:
                headers["If-None-Match"] = etag
            if last_modified:
                headers["If-Modified-Since"] = last_modified
        resp = await self._request(url, headers)
        if resp.status_code == 304:
            return FetchResult(url, 304, b"", "", resp.headers)
        if resp.status_code >= 400:
            raise FetchError(f"{url}: HTTP {resp.status_code}")
        if conditional:
            self.storage.set_validators(url, resp.headers.get("ETag"), resp.headers.get("Last-Modified"))
        return FetchResult(url, resp.status_code, resp.content, resp.text, resp.headers)
