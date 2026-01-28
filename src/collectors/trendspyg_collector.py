from __future__ import annotations

import csv
import json
import logging
import math
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from trendspyg import download_google_trends_csv, download_google_trends_rss

from config.settings import (
    DEFAULT_CSV_HOURS,
    DEFAULT_CSV_SORT_BY,
    DEFAULT_LIMIT,
    DEFAULT_RSS_CACHE,
    DEFAULT_RSS_INCLUDE_ARTICLES,
    DEFAULT_RSS_INCLUDE_IMAGES,
    DEFAULT_RSS_MAX_ARTICLES_PER_TREND,
    DEFAULT_SLEEP_SEC,
    DEFAULT_TREND_METHOD,
    DEFAULT_TREND_SOURCE,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TrendItem:
    keyword: str
    region: str
    rank: int
    source: str
    traffic: str | None = None
    published_at: str | None = None
    explore_link: str | None = None
    image: dict | None = None
    news_articles: list[dict] | None = None
    metadata: dict | None = None


RSS_REGION_MAP = {
    "united_states": "US",
    "south_korea": "KR",
    "korea": "KR",
    "japan": "JP",
}

CSV_ALLOWED_HOURS = (4, 24, 48, 168)


def _normalize_keyword(keyword: str) -> str:
    return " ".join(keyword.strip().lower().split())


def _parse_title_value(value) -> str | None:
    if isinstance(value, dict):
        for key in ("trend", "query", "title", "name"):
            candidate = value.get(key)
            if candidate:
                return str(candidate).strip()
        return None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _jsonify_value(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        return None if math.isnan(value) else value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc).isoformat()
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonify_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonify_value(item) for item in value]
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    return str(value)


def _coerce_csv_hours(hours: int) -> tuple[int, bool]:
    if hours in CSV_ALLOWED_HOURS:
        return hours, False
    closest = min(CSV_ALLOWED_HOURS, key=lambda x: (abs(x - hours), x))
    return closest, True


def _extract_first(record: dict, keys: tuple[str, ...]):
    for key in keys:
        if key in record:
            value = record[key]
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            return value
    return None


def _normalize_csv_record(record: dict) -> dict | None:
    if not record:
        return None
    metadata = {str(k): _jsonify_value(v) for k, v in record.items()}
    lower = {k.lower(): v for k, v in metadata.items()}

    keyword_value = _extract_first(
        lower,
        ("trend", "title", "query", "keyword", "search term", "search_term", "name"),
    )
    keyword = _parse_title_value(keyword_value)
    if not keyword:
        return None

    traffic = _extract_first(
        lower,
        ("traffic", "search volume", "search_volume", "volume"),
    )
    started = _extract_first(
        lower,
        ("started", "published", "date", "time", "timestamp"),
    )
    explore_link = _extract_first(
        lower,
        ("explore_link", "explore link", "link"),
    )

    return {
        "keyword": keyword,
        "traffic": traffic,
        "published_at": _normalize_published(started),
        "explore_link": explore_link,
        "image": None,
        "news_articles": None,
        "metadata": metadata,
    }


def _records_from_csv_output(output) -> list[dict]:
    if output is None:
        return []
    if hasattr(output, "to_dict"):
        try:
            return output.to_dict(orient="records")
        except TypeError:
            return output.to_dict()
    if isinstance(output, str):
        text = output.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return []
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, dict):
                data = parsed.get("data")
                return data if isinstance(data, list) else []
        path = Path(text)
        if path.exists():
            if path.suffix.lower() in {".json"}:
                try:
                    parsed = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    return []
                return parsed if isinstance(parsed, list) else []
            try:
                with path.open(encoding="utf-8", newline="") as handle:
                    return list(csv.DictReader(handle))
            except Exception:
                return []
    return []


def _normalize_csv_entries(records: list[dict]) -> list[dict]:
    if not records:
        return []
    normalized_entries: list[dict] = []
    for record in records:
        normalized = _normalize_csv_record(record)
        if normalized:
            normalized_entries.append(normalized)
    return normalized_entries


def _normalize_published(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc).isoformat()
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _normalize_rss_entry(entry) -> dict | None:
    keyword = _parse_title_value(entry)
    if not keyword:
        return None
    if not isinstance(entry, dict):
        return {"keyword": keyword}
    return {
        "keyword": keyword,
        "traffic": entry.get("traffic"),
        "published_at": _normalize_published(entry.get("published")),
        "explore_link": entry.get("explore_link"),
        "image": entry.get("image"),
        "news_articles": entry.get("news_articles"),
    }


def _unique_keywords(items: list[TrendItem]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for item in items:
        key = _normalize_keyword(item.keyword)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item.keyword)
    return unique


def _normalize_region_key(region: str) -> str:
    return region.strip().lower().replace(" ", "_")


def _map_rss_region(region: str) -> str:
    normalized = region.strip()
    if len(normalized) == 2 and normalized.isalpha():
        return normalized.upper()
    key = _normalize_region_key(normalized)
    mapped = RSS_REGION_MAP.get(key)
    return mapped or normalized


def _extract_rss_entries(entries) -> list[dict]:
    if not entries:
        return []
    normalized_entries: list[dict] = []
    for entry in entries:
        normalized = _normalize_rss_entry(entry)
        if normalized:
            normalized_entries.append(normalized)
    return normalized_entries


def _dedupe_entries(entries: list[dict]) -> list[dict]:
    seen: set[str] = set()
    unique: list[dict] = []
    for entry in entries:
        keyword = entry.get("keyword")
        if not keyword:
            continue
        normalized = _normalize_keyword(keyword)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique.append(entry)
    return unique


def _fetch_trends_rss(
    region: str,
    method: str,
    *,
    include_images: bool,
    include_articles: bool,
    max_articles_per_trend: int,
    cache: bool,
) -> tuple[list[dict], str]:
    region_code = _map_rss_region(region)
    logger.debug(
        "Fetching RSS trends (region=%s, mapped=%s, method=%s, images=%s, articles=%s, max_articles=%s, cache=%s)",
        region,
        region_code,
        method,
        include_images,
        include_articles,
        max_articles_per_trend,
        cache,
    )
    entries = download_google_trends_rss(
        geo=region_code,
        include_images=include_images,
        include_articles=include_articles,
        max_articles_per_trend=max_articles_per_trend,
        cache=cache,
    )
    logger.debug("RSS trends fetched: %s entries", len(entries) if entries else 0)
    return _extract_rss_entries(entries), f"{method}_rss"


def _fetch_trends_csv(
    region: str,
    *,
    hours: int,
    sort_by: str,
    category: str = "all",
    active_only: bool = False,
    download_dir: str | None = None,
    max_retries: int = 1,
    retry_delay_sec: float = 1.0,
) -> tuple[list[dict], str]:
    region_code = _map_rss_region(region)
    logger.debug(
        "Fetching CSV trends (region=%s, mapped=%s, hours=%s, sort_by=%s, category=%s)",
        region,
        region_code,
        hours,
        sort_by,
        category,
    )
    max_retries = max(1, int(max_retries))
    attempt = 0
    while True:
        attempt += 1
        try:
            output = download_google_trends_csv(
                geo=region_code,
                hours=hours,
                category=category,
                sort_by=sort_by,
                active_only=active_only,
                download_dir=download_dir,
                output_format="dataframe",
            )
            break
        except Exception as exc:
            if attempt >= max_retries:
                raise
            logger.warning(
                "CSV download failed (attempt %s/%s, region=%s, category=%s): %s",
                attempt,
                max_retries,
                region,
                category,
                exc,
            )
            if retry_delay_sec > 0:
                time.sleep(retry_delay_sec)
    records = _records_from_csv_output(output)
    entries = _normalize_csv_entries(records)
    logger.debug("CSV trends fetched: %s entries", len(entries))
    return entries, f"csv_{hours}h"


def _fetch_trends(
    *,
    region: str,
    method: str,
    include_images: bool,
    include_articles: bool,
    max_articles_per_trend: int,
    cache: bool,
) -> tuple[list[dict], str]:
    effective_method = method
    if method == "today_searches":
        logger.warning(
            "today_searches is not supported by trendspyg RSS; using trending_searches instead."
        )
        effective_method = "trending_searches"
    if effective_method not in ("trending_searches", "realtime_trending_searches"):
        raise ValueError(f"Unsupported trend method: {method}")
    logger.debug("Fetching %s via RSS (region=%s)", effective_method, region)
    entries, method_used = _fetch_trends_rss(
        region,
        effective_method,
        include_images=include_images,
        include_articles=include_articles,
        max_articles_per_trend=max_articles_per_trend,
        cache=cache,
    )
    return entries, method_used


def collect_trending_searches(
    regions: Iterable[str],
    *,
    limit: int = DEFAULT_LIMIT,
    sleep_sec: float = DEFAULT_SLEEP_SEC,
    method: str = DEFAULT_TREND_METHOD,
    source: str = DEFAULT_TREND_SOURCE,
    window_hours: int = DEFAULT_CSV_HOURS,
    csv_sort_by: str = DEFAULT_CSV_SORT_BY,
    categories: Iterable[str] | None = None,
    csv_active_only: bool = False,
    csv_download_dir: str | None = None,
    csv_max_retries: int = 1,
    csv_retry_delay_sec: float = 1.0,
    include_images: bool = DEFAULT_RSS_INCLUDE_IMAGES,
    include_articles: bool = DEFAULT_RSS_INCLUDE_ARTICLES,
    max_articles_per_trend: int = DEFAULT_RSS_MAX_ARTICLES_PER_TREND,
    cache: bool = DEFAULT_RSS_CACHE,
) -> dict:
    regions_list = list(regions)
    items: list[TrendItem] = []
    source_mode = source.strip().lower()
    if source_mode not in ("rss", "csv"):
        raise ValueError(f"Unsupported trend source: {source}")

    csv_hours_used: int | None = None
    categories_list = [c.strip() for c in categories or [] if c and str(c).strip()]
    if source_mode == "csv":
        csv_hours_used, coerced = _coerce_csv_hours(window_hours)
        if coerced:
            logger.warning(
                "CSV hours %s not supported; using %s instead.",
                window_hours,
                csv_hours_used,
            )

    if source_mode == "csv":
        logger.info(
            "Collect trends (source=%s, regions=%s, limit=%s, hours=%s, sort_by=%s, categories=%s)",
            source_mode,
            regions_list,
            limit,
            csv_hours_used,
            csv_sort_by,
            categories_list or ["all"],
        )
    else:
        if categories_list:
            logger.info("Categories are ignored for RSS source: %s", categories_list)
        logger.info(
            "Collect trends (source=%s, method=%s, regions=%s, limit=%s, images=%s, articles=%s, max_articles=%s, cache=%s)",
            source_mode,
            method,
            regions_list,
            limit,
            include_images,
            include_articles,
            max_articles_per_trend,
            cache,
        )
    for region in regions_list:
        try:
            if source_mode == "rss":
                entries, method_used = _fetch_trends(
                    region=region,
                    method=method,
                    include_images=include_images,
                    include_articles=include_articles,
                    max_articles_per_trend=max_articles_per_trend,
                    cache=cache,
                )
            else:
                if method != DEFAULT_TREND_METHOD:
                    logger.debug("CSV source ignores method=%s", method)
                if not categories_list:
                    categories_list = ["all"]
                entries = []
                method_used = "csv"
                errors: list[Exception] = []
                for category in categories_list:
                    try:
                        category_entries, method_used = _fetch_trends_csv(
                            region,
                            hours=csv_hours_used or DEFAULT_CSV_HOURS,
                            sort_by=csv_sort_by,
                            category=category or "all",
                            active_only=csv_active_only,
                            download_dir=csv_download_dir,
                            max_retries=csv_max_retries,
                            retry_delay_sec=csv_retry_delay_sec,
                        )
                    except Exception as exc:
                        errors.append(exc)
                        logger.warning(
                            "CSV fetch failed (region=%s, category=%s): %s",
                            region,
                            category,
                            exc,
                        )
                        continue
                    if category and category != "all":
                        for entry in category_entries:
                            metadata = entry.get("metadata") or {}
                            metadata = dict(metadata)
                            metadata["category_filter"] = category
                            entry["metadata"] = metadata
                    entries.extend(category_entries)
                if not entries and errors:
                    raise RuntimeError(
                        f"CSV fetch failed for all categories (region={region})"
                    ) from errors[-1]
        except Exception as exc:
            raise RuntimeError(
                f"trend fetch failed (source={source_mode}, method={method}, region={region})"
            ) from exc

        source_for_region = "trendspyg"
        unique_entries = _dedupe_entries(entries)[:limit]
        logger.info(
            "Region %s: %s keywords collected (source=%s, method=%s)",
            region,
            len(unique_entries),
            source_for_region,
            method_used,
        )
        for rank, entry in enumerate(unique_entries, start=1):
            items.append(
                TrendItem(
                    keyword=entry["keyword"],
                    region=region,
                    rank=rank,
                    source=f"{source_for_region}:{method_used}",
                    traffic=entry.get("traffic"),
                    published_at=entry.get("published_at"),
                    explore_link=entry.get("explore_link"),
                    image=entry.get("image"),
                    news_articles=entry.get("news_articles"),
                    metadata=entry.get("metadata"),
                )
            )

        if sleep_sec > 0:
            time.sleep(sleep_sec)

    unique_keywords = _unique_keywords(items)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "trendspyg",
        "method": method,
        "source_mode": source_mode,
        "window_hours_requested": window_hours if source_mode == "csv" else None,
        "window_hours_used": csv_hours_used if source_mode == "csv" else None,
        "regions": regions_list,
        "rss_options": {
            "include_images": include_images,
            "include_articles": include_articles,
            "max_articles_per_trend": max_articles_per_trend,
            "cache": cache,
        }
        if source_mode == "rss"
        else None,
        "csv_options": {
            "hours": csv_hours_used,
            "sort_by": csv_sort_by,
            "categories": categories_list or ["all"],
            "active_only": csv_active_only if source_mode == "csv" else None,
            "download_dir": csv_download_dir if source_mode == "csv" else None,
            "max_retries": csv_max_retries if source_mode == "csv" else None,
            "retry_delay_sec": csv_retry_delay_sec if source_mode == "csv" else None,
        }
        if source_mode == "csv"
        else None,
        "items": [asdict(item) for item in items],
        "unique_keywords": unique_keywords,
        "related_topics_included": False,
        "related_topics_source": None,
        "related_topics": {},
        "related_topics_errors": [],
    }
    logger.info(
        "Collection done (items=%s, unique_keywords=%s)",
        len(items),
        len(unique_keywords),
    )
    return payload
