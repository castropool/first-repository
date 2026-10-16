#!/usr/bin/env python3
"""Taiwan news monitor: polls Taiwan news sources, compiles .docx briefings, pushes them to your phone.

    python main.py                  # run as a daemon (Ctrl+C / SIGTERM for graceful shutdown)
    python main.py --once           # smoke test: poll once, print counts, sample briefing, test push
    python main.py --once --no-push
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

if sys.version_info < (3, 12):
    sys.exit("TwNewsMonitor requires Python 3.12 or newer")

from twnews.config import ConfigError, load_config
from twnews.logging_setup import setup_logging
from twnews.service import Monitor


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Taiwan news monitor and briefing generator")
    p.add_argument("-c", "--config", default=str(Path(__file__).with_name("config.yaml")),
                   help="path to config.yaml (default: next to main.py)")
    p.add_argument("--once", action="store_true",
                   help="poll all sources once, print per-source counts, build one sample briefing, send one test push")
    p.add_argument("--no-push", action="store_true", help="with --once: skip the test push")
    p.add_argument("--log-level", help="override logging.level from config (DEBUG, INFO, ...)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        cfg = load_config(args.config)
    except (ConfigError, OSError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    lc = cfg["logging"]
    setup_logging(cfg["paths"]["logs"], args.log_level or lc["level"], lc["max_bytes"], lc["backup_count"],
                  console=lc["console"] or args.once)
    log = logging.getLogger("twnews")

    async def runner() -> int:
        monitor = Monitor(cfg)
        if args.once:
            return await monitor.run_once(push=not args.no_push)
        await monitor.run_forever()
        return 0

    try:
        return asyncio.run(runner())
    except KeyboardInterrupt:  # Ctrl+C before handlers were installed, or on Windows consoles
        log.info("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
