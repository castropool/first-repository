"""News sources: RSS/Atom feeds first, HTML listing-page scraping as fallback."""

from __future__ import annotations

import asyncio
import calendar
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import feedparser
import lxml.html
import trafilatura

from .http import FetchError, HttpFetcher, RobotsDisallowed
from .textutil import clean_xml_text, normalize_link, parse_iso, strip_html, to_taipei

log = logging.getLogger("twnews.sources")


@dataclass
class RawItem:
    guid: str
    title: str
    link: str
    published: datetime | None
    summary: str
    via: str  # rss | html


@dataclass
class CollectResult:
    items: list[RawItem] = field(default_factory=list)
    ok: bool = False
    via: str = ""
    not_modified: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class Source:
    id: str
    name: str
    feeds: list[str]
    html: dict | None
    min_interval: float
    enrich: bool
    # runtime state
    next_due: float = 0.0
    failures: int = 0
    backoff_until: float = 0.0

    @classmethod
    def from_config(cls, cfg: dict, fetch_cfg: dict) -> "Source":
        feeds = list(cfg.get("feeds") or [])
        default_interval = (fetch_cfg["default_min_interval_seconds"] if feeds
                            else fetch_cfg["default_html_min_interval_seconds"])
        return cls(
            id=cfg["id"],
            name=cfg.get("name", cfg["id"]),
            feeds=feeds,
            html=cfg.get("html"),
            min_interval=float(cfg.get("min_interval", default_interval)),
            enrich=bool(cfg.get("enrich", True)),  # fetch article pages for HTML-scraped items
        )

    def is_due(self, now: float) -> bool:
        return now >= self.next_due and now >= self.backoff_until

    def record(self, ok: bool, now: float, max_backoff: float) -> None:
        self.next_due = now + self.min_interval
        if ok:
            self.failures = 0
            self.backoff_until = 0.0
        else:
            self.failures += 1
            self.backoff_until = now + min(max_backoff, self.min_interval * (2 ** self.failures))


# --------------------------------------------------------------------------- feeds
def _entry_time(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        st = entry.get(key)
        if st:
            return to_taipei(datetime.fromtimestamp(calendar.timegm(st), tz=timezone.utc))
    return None


def parse_feed(content: bytes, base_url: str) -> list[RawItem]:
    parsed = feedparser.parse(content, response_headers={"content-location": base_url})
    if parsed.bozo and not parsed.entries:
        raise FetchError(f"unparseable feed {base_url}: {parsed.get('bozo_exception')}")
    items = []
    for e in parsed.entries:
        link = (e.get("link") or "").strip()
        title = strip_html(e.get("title", ""))
        if not link or not title:
            continue
        summary = e.get("summary") or ""
        if not summary and e.get("content"):
            summary = e.content[0].get("value", "")
        items.append(RawItem(
            guid=(e.get("id") or link).strip(),
            title=title,
            link=normalize_link(link),
            published=_entry_time(e),
            summary=strip_html(summary),
            via="rss",
        ))
    return items


# --------------------------------------------------------------------------- HTML
def parse_listing(html: str, base_url: str, spec: dict) -> list[RawItem]:
    doc = lxml.html.fromstring(html)
    doc.make_links_absolute(base_url, resolve_base_href=True)
    roots = doc.xpath(spec["container_xpath"]) if spec.get("container_xpath") else [doc]
    pattern = re.compile(spec["link_pattern"])
    max_items = int(spec.get("max_items", 60))
    found: dict[str, str] = {}
    for root in roots:
        for a in root.iter("a"):
            href = a.get("href") or ""
            if not pattern.search(href):
                continue
            text = re.sub(r"\s+", " ", a.text_content() or "").strip() or (a.get("title") or "").strip()
            link = normalize_link(href)
            if len(text) > len(found.get(link, "")):
                found[link] = text
    items = []
    for link, title in found.items():
        if len(title) < 6:  # thumbnails, "more" links, etc.
            continue
        items.append(RawItem(guid=link, title=clean_xml_text(title), link=link, published=None, summary="", via="html"))
        if len(items) >= max_items:
            break
    return items


def extract_article(html: str, url: str) -> tuple[str, datetime | None]:
    """Use trafilatura to pull the description/lead text and publish date from an article page."""
    raw = trafilatura.extract(html, url=url, output_format="json", with_metadata=True,
                              include_comments=False, include_tables=False, favor_precision=True)
    if not raw:
        return "", None
    data = json.loads(raw)
    text = data.get("description") or data.get("excerpt") or data.get("text") or ""
    published = parse_iso(data.get("date")) if data.get("date") else None
    return strip_html(text), published


# --------------------------------------------------------------------------- collection
async def collect(source: Source, fetcher: HttpFetcher) -> CollectResult:
    res = CollectResult()
    for url in source.feeds:
        try:
            fr = await fetcher.get(url)
            if fr.not_modified:
                res.not_modified += 1
                res.ok = True
                continue
            items = parse_feed(fr.content, url)
            res.items.extend(items)
            res.ok = True
            res.via = "rss"
        except FetchError as exc:  # includes RobotsDisallowed
            res.errors.append(str(exc))
            log.warning("feed failed", extra={"source": source.id, "url": url, "error": str(exc)})

    if not res.ok and source.html:
        if source.feeds:
            log.info("falling back to HTML scraping", extra={"source": source.id})
        try:
            fr = await fetcher.get(source.html["url"])
            res.ok = True
            res.via = "html"
            if fr.not_modified:
                res.not_modified += 1
            else:
                res.items.extend(parse_listing(fr.text, str(fr.url), source.html))
        except FetchError as exc:
            res.errors.append(str(exc))
            log.warning("html fallback failed", extra={"source": source.id, "error": str(exc)})
    if res.ok and not res.via:
        res.via = "304"
    return res


async def enrich(fetcher: HttpFetcher, url: str) -> tuple[str, datetime | None]:
    try:
        fr = await fetcher.get(url, conditional=False)
    except RobotsDisallowed:
        return "", None
    return await asyncio.to_thread(extract_article, fr.text, url)

