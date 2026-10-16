"""Text helpers: time zone, HTML stripping, Simplified Chinese conversion, summaries, hashing."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import lxml.html
import opencc

TAIPEI = ZoneInfo("Asia/Taipei")

_converter = opencc.OpenCC("tw2sp")  # Taiwan Traditional -> Simplified, incl. phrase conversion
_INVALID_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
_WS = re.compile(r"\s+")
_CJK = re.compile(r"[㐀-鿿]")
_SENT_SPLIT = re.compile(r"(?<=[。！？!?])|(?<=[.])\s+")
_TRACKING_PARAMS = re.compile(r"^(utm_|fbclid$|gclid$|from$|ref$|cid$|ch$|feature$)", re.I)


def now_taipei() -> datetime:
    return datetime.now(TAIPEI)


def to_taipei(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TAIPEI)


def iso(dt: datetime | None) -> str | None:
    return to_taipei(dt).isoformat(timespec="seconds") if dt else None


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return to_taipei(datetime.fromisoformat(value))
    except ValueError:
        return None


def clean_xml_text(text: str | None) -> str:
    """Remove characters that are illegal in XML (python-docx rejects them)."""
    return _INVALID_XML.sub("", text or "")


def strip_html(text: str | None) -> str:
    if not text:
        return ""
    if "<" in text and ">" in text:
        try:
            text = lxml.html.fromstring(f"<div>{text}</div>").text_content()
        except Exception:  # malformed markup: fall back to a crude strip
            text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("　", " ").replace("\xa0", " ")
    return _WS.sub(" ", clean_xml_text(text)).strip()


@lru_cache(maxsize=4096)
def to_simplified(text: str) -> str:
    return _converter.convert(text) if text else ""


def is_chinese(text: str) -> bool:
    return bool(text) and len(_CJK.findall(text)) >= max(2, len(text) // 10)


def summarize(text: str, max_sentences: int = 2, max_chars: int = 140) -> str:
    """Return the first 1-2 sentences of `text`, capped at `max_chars`."""
    text = strip_html(text)
    if not text:
        return ""
    out = ""
    count = 0
    for sent in _SENT_SPLIT.split(text):
        sent = sent.strip()
        if not sent:
            continue
        if out and len(out) + len(sent) > max_chars:
            break
        out = f"{out} {sent}".strip() if out and not is_chinese(sent) else out + sent
        count += 1
        if count >= max_sentences:
            break
    if len(out) > max_chars:
        out = out[: max_chars - 1].rstrip() + "…"
    return out


def normalize_for_match(text: str) -> str:
    return to_simplified(text or "").lower()


def _fingerprint_text(text: str) -> str:
    text = normalize_for_match(text)
    return "".join(ch for ch in text if ch.isalnum())


def content_hash(title: str, summary: str) -> str:
    basis = _fingerprint_text(title) + "|" + _fingerprint_text(summary)[:300]
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def summary_is_redundant(title: str, summary: str) -> bool:
    """Google News etc. put the headline (plus source) in the description."""
    t, s = _fingerprint_text(title), _fingerprint_text(summary)
    return not s or s.startswith(t) or t.startswith(s)


def normalize_link(url: str) -> str:
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING_PARAMS.match(k)]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", urlencode(query), ""))


def match_keywords(text: str, keywords: list[str]) -> list[str]:
    hay = normalize_for_match(text)
    return [kw for kw in keywords if kw and normalize_for_match(kw) in hay]


def classify_topic(text: str, topics: dict[str, list[str]], default: str = "其他") -> str:
    for name, kws in topics.items():
        if match_keywords(text, kws):
            return name
    return default
