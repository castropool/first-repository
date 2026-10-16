"""Configuration loading: YAML file merged onto defaults, with ${ENV_VAR} expansion."""

from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "paths": {
        "database": "data/news.sqlite3",
        "briefings": "briefings",
        "logs": "logs",
    },
    "logging": {
        "level": "INFO",
        "max_bytes": 10 * 1024 * 1024,
        "backup_count": 5,
        "console": True,
    },
    "fetch": {
        "poll_interval_seconds": 30,
        "user_agent": "TwNewsMonitor/1.0 (personal news briefing bot; +https://github.com/castropool/first-repository)",
        "robots_user_agent": "TwNewsMonitor",
        "respect_robots": True,
        "robots_cache_seconds": 86400,
        "timeout_seconds": 15,
        "max_concurrency": 6,
        "host_min_gap_seconds": 1.0,
        "retries": 3,
        "backoff_base_seconds": 1.0,
        "backoff_max_seconds": 30.0,
        "source_backoff_max_seconds": 900,
        "default_min_interval_seconds": 30,
        "default_html_min_interval_seconds": 120,
        "enrich_max_per_cycle": 5,
    },
    "sources": [],
    "watch_keywords": [],
    "topics": {},
    "briefing": {
        "interval_minutes": 60,
        "align_to_clock": True,
        "group_by": "source",  # source | topic
        "skip_if_empty": True,
        "max_items": 300,
        "late_grace_minutes": 15,
        "keep_latest": 48,
        "title": "台湾新闻简报",
        "font": "Microsoft YaHei",
    },
    "flash": {
        "enabled": True,
        "cooldown_seconds": 120,
        "max_age_minutes": 180,
        "skip_initial_backlog": True,
    },
    "storage": {
        "retain_days": 30,
    },
    "links": {
        "public_base_url": "",
        "file_server": {
            "enabled": False,
            "bind": "0.0.0.0",
            "port": 8765,
            "token": "",
        },
    },
    "push": {
        "headline_count": 8,
        "channels": [],
    },
}

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    return value


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, val in (override or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], val)
        else:
            out[key] = val
    return out


class ConfigError(ValueError):
    pass


def load_config(path: str | os.PathLike) -> dict[str, Any]:
    path = Path(path).resolve()
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    cfg = _merge(DEFAULTS, _expand_env(raw))
    cfg["base_dir"] = str(path.parent)

    # Resolve relative paths against the config file's directory.
    for key, rel in cfg["paths"].items():
        p = Path(rel)
        cfg["paths"][key] = str(p if p.is_absolute() else path.parent / p)

    _validate(cfg)
    return cfg


def _validate(cfg: dict[str, Any]) -> None:
    fetch = cfg["fetch"]
    if fetch["poll_interval_seconds"] < 10:
        raise ConfigError("fetch.poll_interval_seconds must be >= 10")
    if cfg["briefing"]["interval_minutes"] < 1:
        raise ConfigError("briefing.interval_minutes must be >= 1")
    if cfg["briefing"]["group_by"] not in ("source", "topic"):
        raise ConfigError("briefing.group_by must be 'source' or 'topic'")
    seen = set()
    for src in cfg["sources"]:
        sid = src.get("id")
        if not sid:
            raise ConfigError(f"source without id: {src}")
        if sid in seen:
            raise ConfigError(f"duplicate source id: {sid}")
        seen.add(sid)
        if not src.get("feeds") and not src.get("html"):
            raise ConfigError(f"source {sid}: needs 'feeds' and/or 'html'")
        html = src.get("html")
        if html and (not html.get("url") or not html.get("link_pattern")):
            raise ConfigError(f"source {sid}: html needs 'url' and 'link_pattern'")
    for ch in cfg["push"]["channels"]:
        if "type" not in ch:
            raise ConfigError(f"push channel without type: {ch}")
