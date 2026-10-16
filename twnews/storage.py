"""SQLite persistence: news items, HTTP validators (ETag/Last-Modified), key/value state."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

from .textutil import iso, now_taipei

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,
    source_name   TEXT NOT NULL,
    guid          TEXT NOT NULL UNIQUE,
    title         TEXT NOT NULL,
    link          TEXT NOT NULL UNIQUE,
    published_at  TEXT,
    fetched_at    TEXT NOT NULL,
    summary       TEXT NOT NULL DEFAULT '',
    hash          TEXT NOT NULL UNIQUE,
    topic         TEXT NOT NULL DEFAULT '',
    keywords      TEXT NOT NULL DEFAULT '',
    via           TEXT NOT NULL DEFAULT 'rss',
    flash_state   INTEGER NOT NULL DEFAULT 0   -- 0 = not flash, 1 = pending, 2 = sent
);
CREATE INDEX IF NOT EXISTS idx_items_fetched ON items(fetched_at);
CREATE INDEX IF NOT EXISTS idx_items_flash ON items(flash_state);
CREATE INDEX IF NOT EXISTS idx_items_source ON items(source);

CREATE TABLE IF NOT EXISTS http_cache (
    url            TEXT PRIMARY KEY,
    etag           TEXT,
    last_modified  TEXT,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS state (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
"""


@dataclass
class NewItem:
    source: str
    source_name: str
    guid: str
    title: str
    link: str
    published_at: datetime | None
    summary: str
    hash: str
    topic: str
    keywords: list[str]
    via: str
    flash_state: int = 0


class Storage:
    """Thin synchronous wrapper; every call is short, so it is used directly from the event loop."""

    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # -- items -------------------------------------------------------------
    def insert_item(self, item: NewItem) -> int | None:
        """Insert unless the GUID, link or content hash is already known. Returns new row id or None."""
        with self._lock:
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO items
                   (source, source_name, guid, title, link, published_at, fetched_at, summary, hash,
                    topic, keywords, via, flash_state)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (item.source, item.source_name, item.guid, item.title, item.link, iso(item.published_at),
                 iso(now_taipei()), item.summary, item.hash, item.topic, ",".join(item.keywords),
                 item.via, item.flash_state),
            )
            return cur.lastrowid if cur.rowcount == 1 else None

    def update_item_enrichment(self, item_id: int, summary: str, published_at: datetime | None) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE items SET summary = CASE WHEN summary = '' THEN ? ELSE summary END, "
                "published_at = COALESCE(published_at, ?) WHERE id = ?",
                (summary, iso(published_at), item_id),
            )

    def items_for_window(self, fetched_after: datetime, published_after: datetime, limit: int) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(
                """SELECT * FROM items
                   WHERE fetched_at > ? AND COALESCE(published_at, fetched_at) >= ?
                   ORDER BY COALESCE(published_at, fetched_at) DESC LIMIT ?""",
                (iso(fetched_after), iso(published_after), limit),
            ).fetchall()

    def latest_items(self, limit: int) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM items ORDER BY COALESCE(published_at, fetched_at) DESC LIMIT ?", (limit,)
            ).fetchall()

    def pending_flash(self) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM items WHERE flash_state = 1 ORDER BY COALESCE(published_at, fetched_at) DESC"
            ).fetchall()

    def mark_flash_sent(self, ids: Iterable[int]) -> None:
        ids = list(ids)
        if not ids:
            return
        with self._lock:
            self.conn.executemany("UPDATE items SET flash_state = 2 WHERE id = ?", [(i,) for i in ids])

    def prune_items(self, retain_days: int) -> int:
        cutoff = iso(now_taipei() - timedelta(days=retain_days))
        with self._lock:
            return self.conn.execute("DELETE FROM items WHERE fetched_at < ?", (cutoff,)).rowcount

    def count_items(self, source: str | None = None) -> int:
        with self._lock:
            if source:
                return self.conn.execute("SELECT COUNT(*) FROM items WHERE source = ?", (source,)).fetchone()[0]
            return self.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]

    # -- HTTP validators ---------------------------------------------------
    def get_validators(self, url: str) -> tuple[str | None, str | None]:
        with self._lock:
            row = self.conn.execute("SELECT etag, last_modified FROM http_cache WHERE url = ?", (url,)).fetchone()
        return (row["etag"], row["last_modified"]) if row else (None, None)

    def set_validators(self, url: str, etag: str | None, last_modified: str | None) -> None:
        with self._lock:
            if not etag and not last_modified:
                self.conn.execute("DELETE FROM http_cache WHERE url = ?", (url,))
                return
            self.conn.execute(
                "INSERT INTO http_cache(url, etag, last_modified, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(url) DO UPDATE SET etag=excluded.etag, last_modified=excluded.last_modified, "
                "updated_at=excluded.updated_at",
                (url, etag, last_modified, iso(now_taipei())),
            )

    # -- key/value state ---------------------------------------------------
    def get_state(self, key: str) -> str | None:
        with self._lock:
            row = self.conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_state(self, key: str, value: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO state(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
