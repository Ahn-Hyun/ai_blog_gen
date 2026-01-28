from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
sys.path.append(str(SRC_DIR))

from collectors.trendspyg_collector import collect_trending_searches  # noqa: E402
from config.settings import (  # noqa: E402
    DEFAULT_CSV_HOURS,
    DEFAULT_CSV_SORT_BY,
    DEFAULT_LIMIT,
    DEFAULT_REGIONS,
    DEFAULT_RSS_CACHE,
    DEFAULT_RSS_INCLUDE_ARTICLES,
    DEFAULT_RSS_INCLUDE_IMAGES,
    DEFAULT_RSS_MAX_ARTICLES_PER_TREND,
    DEFAULT_SLEEP_SEC,
    DEFAULT_TREND_METHOD,
    DEFAULT_TREND_SOURCE,
)
from store.local_store import write_json  # noqa: E402


def _default_output_path() -> Path:
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return ROOT_DIR / "data" / "trends" / f"{date_str}-trends.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect trending keywords via Google Trends RSS (trendspyg)."
    )
    parser.add_argument(
        "--regions",
        nargs="*",
        default=DEFAULT_REGIONS,
        help="Region codes (e.g. KR, US) or legacy names (e.g. south_korea)",
    )
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Max keywords per region")
    parser.add_argument("--sleep-sec", type=float, default=DEFAULT_SLEEP_SEC, help="Delay between calls")
    parser.add_argument(
        "--method",
        default=DEFAULT_TREND_METHOD,
        choices=["today_searches", "trending_searches", "realtime_trending_searches"],
        help="Trend method (today_searches maps to trending RSS)",
    )
    parser.add_argument(
        "--source",
        default=DEFAULT_TREND_SOURCE,
        choices=["rss", "csv"],
        help="Data source (rss or csv)",
    )
    parser.add_argument(
        "--window-hours",
        type=int,
        default=DEFAULT_CSV_HOURS,
        help="CSV time window in hours (4, 24, 48, 168)",
    )
    parser.add_argument(
        "--csv-sort-by",
        default=DEFAULT_CSV_SORT_BY,
        choices=["relevance", "title", "traffic", "started"],
        help="CSV sort order",
    )
    parser.add_argument(
        "--include-images",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_RSS_INCLUDE_IMAGES,
        help="Include image metadata in RSS results",
    )
    parser.add_argument(
        "--include-articles",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_RSS_INCLUDE_ARTICLES,
        help="Include news articles in RSS results",
    )
    parser.add_argument(
        "--max-articles-per-trend",
        type=int,
        default=DEFAULT_RSS_MAX_ARTICLES_PER_TREND,
        help="Max news articles per trend",
    )
    parser.add_argument(
        "--cache",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_RSS_CACHE,
        help="Use RSS cache",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level",
    )
    parser.add_argument("--output", type=str, default=None, help="Output JSON path")
    return parser.parse_args()


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def main() -> int:
    args = parse_args()
    _configure_logging(args.log_level)
    logging.getLogger(__name__).info("Collect trends start")
    payload = collect_trending_searches(
        args.regions,
        limit=args.limit,
        sleep_sec=args.sleep_sec,
        method=args.method,
        source=args.source,
        window_hours=args.window_hours,
        csv_sort_by=args.csv_sort_by,
        include_images=args.include_images,
        include_articles=args.include_articles,
        max_articles_per_trend=args.max_articles_per_trend,
        cache=args.cache,
    )
    output_path = Path(args.output) if args.output else _default_output_path()
    write_json(output_path, payload)
    print(f"Saved {len(payload['items'])} items to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
