from __future__ import annotations

import argparse
import base64
import http.client
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
sys.path.append(str(SRC_DIR))

from collectors.trendspyg_collector import collect_trending_searches  # noqa: E402
from config.settings import (  # noqa: E402
    DEFAULT_CSV_HOURS,
    DEFAULT_CSV_SORT_BY,
    DEFAULT_FALLBACK_CATEGORY,
    DEFAULT_FALLBACK_TAGS,
    DEFAULT_LIMIT,
    DEFAULT_RSS_CACHE,
    DEFAULT_RSS_INCLUDE_ARTICLES,
    DEFAULT_RSS_INCLUDE_IMAGES,
    DEFAULT_RSS_MAX_ARTICLES_PER_TREND,
    DEFAULT_SLEEP_SEC,
    DEFAULT_TREND_METHOD,
    DEFAULT_TREND_SOURCE,
)
from store.local_store import read_json, write_json  # noqa: E402

DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; TrendBlogBot/1.0)"
DEFAULT_MAX_SOURCE_CHARS = 2000
DEFAULT_MAX_TOTAL_SOURCE_CHARS = 12000
DEFAULT_SCRAPE_TIMEOUT = 12
DEFAULT_SCRAPE_DELAY_SEC = 1.0
DEFAULT_SCRAPE_MAX_RETRIES = 2
DEFAULT_SCRAPE_BACKOFF_SEC = 5.0
DEFAULT_GEMINI_MODEL = "gemini-3-pro-preview"
DEFAULT_GEMINI_MODEL_CONTENT = DEFAULT_GEMINI_MODEL
DEFAULT_GEMINI_MODEL_META = DEFAULT_GEMINI_MODEL
DEFAULT_GEMINI_TEMPERATURE = 0.6
DEFAULT_GEMINI_MAX_TOKENS = 2200
DEFAULT_BLOG_DOMAIN = "https://blog.ship-write.com"
DEFAULT_CONTENT_LANGUAGE = "English"
DEFAULT_CONTENT_TONE = "neutral, informative"
DEFAULT_USE_MULTI_AGENT = True
DEFAULT_SEARCH_RSS_ENABLED = True
DEFAULT_SEARCH_RSS_MAX_RESULTS = 6
DEFAULT_SEARCH_RSS_MAX_PER_QUERY = 3
DEFAULT_MAX_EVIDENCE_SOURCES = 6
DEFAULT_QUALITY_GATE_REVISIONS = 1
DEFAULT_SEARCH_WEB_ENABLED = True
DEFAULT_SEARCH_WEB_MAX_RESULTS = 5
DEFAULT_SEARCH_WEB_MAX_PER_QUERY = 2
DEFAULT_SEARCH_WEB_DEPTH = "basic"
DEFAULT_SEARCH_WEB_INCLUDE_ANSWER = True
DEFAULT_SEARCH_WEB_INCLUDE_DOMAINS: tuple[str, ...] = ()
DEFAULT_SEARCH_WEB_EXCLUDE_DOMAINS: tuple[str, ...] = ()
DEFAULT_YOUTUBE_SEARCH_ENABLED = True
DEFAULT_YOUTUBE_MAX_RESULTS = 4
DEFAULT_YOUTUBE_MAX_PER_QUERY = 2
DEFAULT_GOOGLE_IMAGE_ENABLED = True
DEFAULT_GOOGLE_IMAGE_MODEL = "imagen-4.0-generate-001"
DEFAULT_GOOGLE_IMAGE_ASPECT_RATIO = "16:9"
CONTENT_JSON_SCHEMA = (
    "Required JSON keys: summary (2-3 sentences), key_points (array of 3-5 strings), "
    "body_markdown (string, MDX-friendly, ~1200-1800 words), "
    "image_prompt_hint (short string)."
)
FRONTMATTER_ZOD_SCHEMA = """
z.object({
  title: z.string().min(5).max(90),
  description: z.string().min(30).max(160),
  category: z.array(z.string()).min(1).max(2),
  tags: z.array(z.string()).min(1).max(3),
  hero_alt: z.string().min(3).max(120),
  image_prompt: z.string().min(5).max(160),
})
""".strip()

STATE_PATH = ROOT_DIR / "data" / "state" / "published.json"
DOMAIN_LAST_FETCH: dict[str, float] = {}
REGION_CODE_MAP = {
    "south_korea": "KR",
    "korea": "KR",
    "kr": "KR",
    "united_states": "US",
    "usa": "US",
    "us": "US",
    "japan": "JP",
    "jp": "JP",
}
GOOGLE_NEWS_LANGUAGE_MAP = {
    "KR": "ko",
    "US": "en",
    "JP": "ja",
}


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._title_chunks: list[str] = []
        self._skip = False
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip = True
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip = False
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_chunks.append(data)
            return
        if self._skip:
            return
        text = data.strip()
        if text:
            self._chunks.append(text)

    def get_text(self) -> str:
        return " ".join(self._chunks)

    def get_title(self) -> str:
        return " ".join(self._title_chunks).strip()


@dataclass(frozen=True)
class AutomationConfig:
    regions: list[str]
    interval_hours: float
    max_topic_rank: int
    trend_source: str
    trend_method: str
    trend_window_hours: int
    csv_sort_by: str
    trend_limit: int
    trend_sleep_sec: float
    rss_include_images: bool
    rss_include_articles: bool
    rss_max_articles_per_trend: int
    rss_cache: bool
    gemini_api_key: str
    gemini_model: str
    gemini_model_content: str
    gemini_model_meta: str
    gemini_temperature: float
    gemini_max_tokens: int
    blog_domain: str
    content_language: str
    content_tone: str
    use_multi_agent: bool
    google_api_key: str
    youtube_api_key: str
    tavily_api_key: str
    fallback_category: str
    fallback_tags: list[str]
    post_draft: bool
    astro_root: Path
    content_dir: Path
    hero_base_dir: Path
    nanobanana_cmd: str | None
    nanobanana_args: str | None
    nanobanana_required: bool
    user_agent: str
    scrape_timeout: int
    scrape_delay_sec: float
    scrape_max_retries: int
    scrape_backoff_sec: float
    max_source_chars: int
    max_total_source_chars: int
    search_rss_enabled: bool
    search_rss_max_results: int
    search_rss_max_per_query: int
    search_web_enabled: bool
    search_web_max_results: int
    search_web_max_per_query: int
    search_web_depth: str
    search_web_include_answer: bool
    search_web_include_domains: list[str]
    search_web_exclude_domains: list[str]
    youtube_search_enabled: bool
    youtube_max_results: int
    youtube_max_per_query: int
    max_evidence_sources: int
    quality_gate_revisions: int
    google_image_enabled: bool
    google_image_model: str
    google_image_aspect_ratio: str


def _parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_list(value: str | None, default: Iterable[str]) -> list[str]:
    if not value:
        return list(default)
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_int(value: str | None, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _parse_float(value: str | None, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def _resolve_env() -> dict[str, str]:
    env = dict(os.environ)
    env_path = ROOT_DIR / ".env"
    file_env = _load_env_file(env_path)
    if not file_env:
        file_env = _load_env_file(ROOT_DIR / "env.template")
    file_env.update(env)
    return file_env


def _build_config() -> AutomationConfig:
    env = _resolve_env()
    regions = _parse_list(env.get("TREND_REGIONS"), ["KR"])
    interval_hours = _parse_float(env.get("POST_INTERVAL_HOURS"), 6.0)
    max_topic_rank = _parse_int(env.get("MAX_TOPIC_RANK"), 3)

    trend_source = env.get("TREND_SOURCE", DEFAULT_TREND_SOURCE)
    trend_method = env.get("TREND_METHOD", DEFAULT_TREND_METHOD)
    trend_window_hours = _parse_int(env.get("TREND_WINDOW_HOURS"), DEFAULT_CSV_HOURS)
    csv_sort_by = env.get("CSV_SORT_BY", DEFAULT_CSV_SORT_BY)
    trend_limit = _parse_int(env.get("TREND_LIMIT"), DEFAULT_LIMIT)
    trend_sleep_sec = _parse_float(env.get("TREND_SLEEP_SEC"), DEFAULT_SLEEP_SEC)

    rss_include_images = _parse_bool(env.get("RSS_INCLUDE_IMAGES"), DEFAULT_RSS_INCLUDE_IMAGES)
    rss_include_articles = _parse_bool(env.get("RSS_INCLUDE_ARTICLES"), DEFAULT_RSS_INCLUDE_ARTICLES)
    rss_max_articles = _parse_int(env.get("RSS_MAX_ARTICLES_PER_TREND"), DEFAULT_RSS_MAX_ARTICLES_PER_TREND)
    rss_cache = _parse_bool(env.get("RSS_CACHE"), DEFAULT_RSS_CACHE)

    gemini_api_key = env.get("GEMINI_API_KEY", "").strip()
    google_api_key = env.get("GOOGLE_API_KEY", "").strip()
    if not gemini_api_key and google_api_key:
        gemini_api_key = google_api_key
    if not google_api_key and gemini_api_key:
        google_api_key = gemini_api_key
    gemini_model = env.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
    gemini_model_content = env.get("GEMINI_MODEL_CONTENT", gemini_model)
    gemini_model_meta = env.get("GEMINI_MODEL_META", gemini_model)
    gemini_temperature = _parse_float(env.get("GEMINI_TEMPERATURE"), DEFAULT_GEMINI_TEMPERATURE)
    gemini_max_tokens = _parse_int(env.get("GEMINI_MAX_TOKENS"), DEFAULT_GEMINI_MAX_TOKENS)

    blog_domain = env.get("BLOG_DOMAIN", DEFAULT_BLOG_DOMAIN)
    if not blog_domain.startswith("http"):
        blog_domain = f"https://{blog_domain}"
    content_language = env.get("CONTENT_LANGUAGE", DEFAULT_CONTENT_LANGUAGE).strip() or DEFAULT_CONTENT_LANGUAGE
    content_tone = env.get("CONTENT_TONE", DEFAULT_CONTENT_TONE).strip() or DEFAULT_CONTENT_TONE
    use_multi_agent = _parse_bool(env.get("USE_MULTI_AGENT"), DEFAULT_USE_MULTI_AGENT)
    youtube_api_key = env.get("YOUTUBE_API_KEY", "").strip() or google_api_key
    tavily_api_key = env.get("TAVILY_API_KEY", "").strip()
    search_web_enabled = _parse_bool(
        env.get("SEARCH_WEB_ENABLED"),
        DEFAULT_SEARCH_WEB_ENABLED,
    )
    search_web_max_results = _parse_int(
        env.get("SEARCH_WEB_MAX_RESULTS"),
        DEFAULT_SEARCH_WEB_MAX_RESULTS,
    )
    search_web_max_per_query = _parse_int(
        env.get("SEARCH_WEB_MAX_PER_QUERY"),
        DEFAULT_SEARCH_WEB_MAX_PER_QUERY,
    )
    search_web_depth = env.get("SEARCH_WEB_DEPTH", DEFAULT_SEARCH_WEB_DEPTH).strip().lower()
    if search_web_depth not in {"basic", "advanced"}:
        search_web_depth = DEFAULT_SEARCH_WEB_DEPTH
    search_web_include_answer = _parse_bool(
        env.get("SEARCH_WEB_INCLUDE_ANSWER"),
        DEFAULT_SEARCH_WEB_INCLUDE_ANSWER,
    )
    search_web_include_domains = _parse_list(
        env.get("SEARCH_WEB_INCLUDE_DOMAINS"),
        DEFAULT_SEARCH_WEB_INCLUDE_DOMAINS,
    )
    search_web_exclude_domains = _parse_list(
        env.get("SEARCH_WEB_EXCLUDE_DOMAINS"),
        DEFAULT_SEARCH_WEB_EXCLUDE_DOMAINS,
    )
    if search_web_enabled and not tavily_api_key:
        logging.warning(
            "SEARCH_WEB_ENABLED is true but TAVILY_API_KEY is missing. Web search will be skipped."
        )
    youtube_search_enabled = _parse_bool(
        env.get("YOUTUBE_SEARCH_ENABLED"),
        DEFAULT_YOUTUBE_SEARCH_ENABLED,
    )
    youtube_max_results = _parse_int(
        env.get("YOUTUBE_MAX_RESULTS"),
        DEFAULT_YOUTUBE_MAX_RESULTS,
    )
    youtube_max_per_query = _parse_int(
        env.get("YOUTUBE_MAX_PER_QUERY"),
        DEFAULT_YOUTUBE_MAX_PER_QUERY,
    )
    search_rss_enabled = _parse_bool(env.get("SEARCH_RSS_ENABLED"), DEFAULT_SEARCH_RSS_ENABLED)
    search_rss_max_results = _parse_int(
        env.get("SEARCH_RSS_MAX_RESULTS"),
        DEFAULT_SEARCH_RSS_MAX_RESULTS,
    )
    search_rss_max_per_query = _parse_int(
        env.get("SEARCH_RSS_MAX_PER_QUERY"),
        DEFAULT_SEARCH_RSS_MAX_PER_QUERY,
    )
    max_evidence_sources = _parse_int(
        env.get("MAX_EVIDENCE_SOURCES"),
        DEFAULT_MAX_EVIDENCE_SOURCES,
    )
    quality_gate_revisions = _parse_int(
        env.get("QUALITY_GATE_REVISIONS"),
        DEFAULT_QUALITY_GATE_REVISIONS,
    )
    google_image_enabled = _parse_bool(
        env.get("GOOGLE_IMAGE_ENABLED"),
        DEFAULT_GOOGLE_IMAGE_ENABLED,
    )
    google_image_model = env.get("GOOGLE_IMAGE_MODEL", DEFAULT_GOOGLE_IMAGE_MODEL).strip()
    google_image_aspect_ratio = env.get(
        "GOOGLE_IMAGE_ASPECT_RATIO",
        DEFAULT_GOOGLE_IMAGE_ASPECT_RATIO,
    ).strip()
    fallback_category = env.get("FALLBACK_CATEGORY", DEFAULT_FALLBACK_CATEGORY)
    fallback_tags = _parse_list(env.get("FALLBACK_TAGS"), DEFAULT_FALLBACK_TAGS)
    if not fallback_tags:
        fallback_tags = ["topic"]
    fallback_tags = fallback_tags[:3]
    post_draft = _parse_bool(env.get("POST_DRAFT"), True)

    astro_root = Path(env.get("ASTRO_ROOT", "../ai_blog_v1_astro")).resolve()
    content_dir = astro_root / "src" / "content" / "blog"
    hero_base_dir = astro_root / "public" / "images" / "posts"

    nanobanana_cmd = env.get("NANOBANANA_CMD")
    nanobanana_args = env.get("NANOBANANA_ARGS")
    nanobanana_required = _parse_bool(env.get("NANOBANANA_REQUIRED"), True)

    user_agent = env.get("SCRAPE_USER_AGENT", DEFAULT_USER_AGENT)
    scrape_timeout = _parse_int(env.get("SCRAPE_TIMEOUT_SEC"), DEFAULT_SCRAPE_TIMEOUT)
    scrape_delay_sec = _parse_float(env.get("SCRAPE_DELAY_SEC"), DEFAULT_SCRAPE_DELAY_SEC)
    scrape_max_retries = _parse_int(env.get("SCRAPE_MAX_RETRIES"), DEFAULT_SCRAPE_MAX_RETRIES)
    scrape_backoff_sec = _parse_float(env.get("SCRAPE_BACKOFF_SEC"), DEFAULT_SCRAPE_BACKOFF_SEC)
    max_source_chars = _parse_int(env.get("MAX_SOURCE_CHARS"), DEFAULT_MAX_SOURCE_CHARS)
    max_total_source_chars = _parse_int(env.get("MAX_TOTAL_SOURCE_CHARS"), DEFAULT_MAX_TOTAL_SOURCE_CHARS)

    return AutomationConfig(
        regions=regions,
        interval_hours=interval_hours,
        max_topic_rank=max_topic_rank,
        trend_source=trend_source,
        trend_method=trend_method,
        trend_window_hours=trend_window_hours,
        csv_sort_by=csv_sort_by,
        trend_limit=trend_limit,
        trend_sleep_sec=trend_sleep_sec,
        rss_include_images=rss_include_images,
        rss_include_articles=rss_include_articles,
        rss_max_articles_per_trend=rss_max_articles,
        rss_cache=rss_cache,
        gemini_api_key=gemini_api_key,
        gemini_model=gemini_model,
        gemini_model_content=gemini_model_content,
        gemini_model_meta=gemini_model_meta,
        gemini_temperature=gemini_temperature,
        gemini_max_tokens=gemini_max_tokens,
        blog_domain=blog_domain.rstrip("/"),
        content_language=content_language,
        content_tone=content_tone,
        use_multi_agent=use_multi_agent,
        google_api_key=google_api_key,
        youtube_api_key=youtube_api_key,
        tavily_api_key=tavily_api_key,
        fallback_category=fallback_category,
        fallback_tags=fallback_tags,
        post_draft=post_draft,
        astro_root=astro_root,
        content_dir=content_dir,
        hero_base_dir=hero_base_dir,
        nanobanana_cmd=nanobanana_cmd.strip() if nanobanana_cmd else None,
        nanobanana_args=nanobanana_args.strip() if nanobanana_args else None,
        nanobanana_required=nanobanana_required,
        user_agent=user_agent,
        scrape_timeout=scrape_timeout,
        scrape_delay_sec=scrape_delay_sec,
        scrape_max_retries=scrape_max_retries,
        scrape_backoff_sec=scrape_backoff_sec,
        max_source_chars=max_source_chars,
        max_total_source_chars=max_total_source_chars,
        search_rss_enabled=search_rss_enabled,
        search_rss_max_results=search_rss_max_results,
        search_rss_max_per_query=search_rss_max_per_query,
        search_web_enabled=search_web_enabled,
        search_web_max_results=search_web_max_results,
        search_web_max_per_query=search_web_max_per_query,
        search_web_depth=search_web_depth,
        search_web_include_answer=search_web_include_answer,
        search_web_include_domains=search_web_include_domains,
        search_web_exclude_domains=search_web_exclude_domains,
        youtube_search_enabled=youtube_search_enabled,
        youtube_max_results=youtube_max_results,
        youtube_max_per_query=youtube_max_per_query,
        max_evidence_sources=max_evidence_sources,
        quality_gate_revisions=quality_gate_revisions,
        google_image_enabled=google_image_enabled,
        google_image_model=google_image_model or DEFAULT_GOOGLE_IMAGE_MODEL,
        google_image_aspect_ratio=google_image_aspect_ratio or DEFAULT_GOOGLE_IMAGE_ASPECT_RATIO,
    )


def _normalize_keyword(keyword: str) -> str:
    return " ".join(keyword.strip().lower().split())


def _is_ascii(text: str) -> bool:
    return all(ord(char) < 128 for char in text)


def _force_ascii(text: str) -> str:
    return text.encode("ascii", "ignore").decode("ascii")


def _ensure_ascii_text(text: str | None, fallback: str) -> str:
    if not text:
        return fallback
    if _is_ascii(text):
        return text
    sanitized = _force_ascii(text).strip()
    return sanitized if sanitized else fallback


def _ensure_ascii_body(body: str, fallback: str) -> str:
    sanitized = _force_ascii(body).strip()
    if len(re.sub(r"\s+", "", sanitized)) < 600:
        return fallback
    return sanitized


def _normalize_category_list(value, fallback: list[str]) -> list[str]:
    if not isinstance(value, list):
        return fallback
    cleaned: list[str] = []
    for item in value:
        text = _force_ascii(str(item)).strip()
        if text and len(text) <= 40:
            cleaned.append(text)
    return cleaned[:2] if cleaned else fallback


def _normalize_tag_list(value, fallback: list[str]) -> list[str]:
    if not isinstance(value, list):
        return fallback
    cleaned: list[str] = []
    for item in value:
        text = _force_ascii(str(item)).strip()
        if not text:
            continue
        if len(text) > 30:
            continue
        if len(text.split()) > 3:
            continue
        cleaned.append(text)
    if len(cleaned) < 1:
        return fallback
    return cleaned[:3]


def _normalize_key_points(value) -> list[str]:
    if not isinstance(value, list):
        return []
    points: list[str] = []
    for item in value:
        text = _force_ascii(str(item)).strip()
        if text:
            points.append(text)
    return points[:6]


def _ensure_list_of_strings(value) -> list[str]:
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            items.append(text)
    return items


def _slugify(text: str) -> str:
    cleaned = _force_ascii(text)
    cleaned = re.sub(r"[^\w\s-]", "", cleaned, flags=re.UNICODE)
    cleaned = re.sub(r"[\s_-]+", "-", cleaned.strip())
    return cleaned.lower()


def _sanitize_url(url: str) -> str:
    return quote(url, safe=":/?&=#%")


def _throttle_domain(url: str, delay_sec: float) -> None:
    if delay_sec <= 0:
        return
    parsed = urlparse(url)
    domain = parsed.netloc
    if not domain:
        return
    now = time.monotonic()
    last = DOMAIN_LAST_FETCH.get(domain)
    if last is not None:
        wait = delay_sec - (now - last)
        if wait > 0:
            time.sleep(wait)
    DOMAIN_LAST_FETCH[domain] = time.monotonic()


def _fetch_url_text(
    url: str,
    user_agent: str,
    timeout: int,
    delay_sec: float,
    max_retries: int,
    backoff_sec: float,
) -> tuple[str | None, str | None]:
    sanitized_url = _sanitize_url(url)
    for attempt in range(max_retries + 1):
        _throttle_domain(sanitized_url, delay_sec)
        request = Request(sanitized_url, headers={"User-Agent": user_agent})
        try:
            with urlopen(request, timeout=timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                html = response.read().decode(charset, errors="ignore")
            break
        except HTTPError as exc:
            if exc.code == 429 and attempt < max_retries:
                sleep_for = backoff_sec * (2**attempt)
                logging.warning(
                    "429 rate limit for %s, retrying in %.1fs",
                    sanitized_url,
                    sleep_for,
                )
                time.sleep(sleep_for)
                continue
            logging.warning("Failed to fetch %s: %s", sanitized_url, exc)
            return None, None
        except (
            URLError,
            TimeoutError,
            http.client.IncompleteRead,
            http.client.RemoteDisconnected,
        ) as exc:
            if attempt < max_retries:
                sleep_for = backoff_sec * (2**attempt)
                logging.warning(
                    "Fetch failed for %s, retrying in %.1fs (%s)",
                    sanitized_url,
                    sleep_for,
                    exc,
                )
                time.sleep(sleep_for)
                continue
            logging.warning("Failed to fetch %s: %s", sanitized_url, exc)
            return None, None

    parser = _HTMLTextExtractor()
    parser.feed(html)
    title = parser.get_title() or None
    text = parser.get_text()
    return title, text


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _truncate_plain(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip()


def _extract_urls(item: dict) -> list[str]:
    urls: list[str] = []
    explore_link = item.get("explore_link")
    if _is_valid_url(explore_link) and explore_link.startswith("http"):
        urls.append(explore_link)

    articles = item.get("news_articles") or []
    if isinstance(articles, list):
        for article in articles:
            if isinstance(article, dict):
                link = article.get("url")
                if _is_valid_url(link) and link.startswith("http"):
                    urls.append(link)

    metadata = item.get("metadata")
    if isinstance(metadata, dict):
        for value in metadata.values():
            if _is_valid_url(value) and value.startswith("http"):
                urls.append(value)

    return list(dict.fromkeys(urls))


def _extract_image_urls(item: dict) -> list[str]:
    urls: list[str] = []
    image = item.get("image")
    if isinstance(image, dict):
        image_url = image.get("url")
        if _is_valid_url(image_url) and image_url.startswith("http"):
            urls.append(image_url)

    articles = item.get("news_articles") or []
    if isinstance(articles, list):
        for article in articles:
            if isinstance(article, dict):
                image_url = article.get("image")
                if _is_valid_url(image_url) and image_url.startswith("http"):
                    urls.append(image_url)
    return list(dict.fromkeys(urls))


def _is_valid_url(url: str | None) -> bool:
    if not isinstance(url, str) or not url:
        return False
    return re.search(r"\s", url) is None


def _format_reference(url: str) -> str:
    if not _is_valid_url(url):
        return ""
    parsed = urlparse(url)
    label = parsed.netloc or "Source"
    return f"[{label}]({url})"


def _linkify_urls(text: str) -> str:
    pattern = re.compile(r"(?<!\()https?://[^\s)]+")

    def replacer(match: re.Match) -> str:
        url = match.group(0)
        return _format_reference(url)

    return pattern.sub(replacer, text)


def _strip_raw_urls(text: str) -> str:
    return re.sub(r"(?<!\()https?://\S+", "", text)


def _remove_ellipsis_sentences(line: str) -> str:
    if "..." not in line and "\u2026" not in line:
        return line
    sentences = re.split(r"(?<=[.!?])\s+", line)
    kept = [s for s in sentences if "..." not in s and "\u2026" not in s]
    return " ".join(kept).strip()


def _clean_body_text(body: str) -> str:
    cleaned_lines: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            cleaned_lines.append("")
            continue
        if stripped.startswith(("#", "![", "-", ">")):
            if "..." in stripped or "\u2026" in stripped:
                continue
            cleaned_lines.append(_strip_raw_urls(stripped))
            continue
        sanitized = _remove_ellipsis_sentences(stripped)
        sanitized = _strip_raw_urls(sanitized)
        if sanitized:
            cleaned_lines.append(sanitized)
    return "\n".join(cleaned_lines).strip()


def _strip_markdown(text: str) -> str:
    cleaned = re.sub(r"!\[[^\]]*\]\([^\)]*\)", "", text)
    cleaned = re.sub(r"\[([^\]]+)\]\([^\)]*\)", r"\1", cleaned)
    cleaned = re.sub(r"`{1,3}[^`]+`{1,3}", "", cleaned)
    cleaned = re.sub(r"^#{1,6}\s+", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"^>\s+", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"^-+\s+", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def _first_sentences(text: str, count: int = 2, max_len: int = 360) -> str:
    cleaned = re.sub(r"\s+", " ", _force_ascii(text)).strip()
    if not cleaned:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    selected = [s.strip() for s in sentences if s.strip()]
    summary = " ".join(selected[:count]).strip()
    return _truncate_plain(summary, max_len)


def _first_sentence(text: str, max_len: int = 240) -> str:
    cleaned = re.sub(r"\s+", " ", _force_ascii(text)).strip()
    if not cleaned:
        return ""
    for sep in (". ", "? ", "! "):
        if sep in cleaned:
            sentence = cleaned.split(sep, 1)[0].strip()
            if sentence:
                return _truncate_plain(sentence, max_len)
    return _truncate_plain(cleaned, max_len)


def _source_snippets(sources: list[dict], limit: int = 3) -> list[str]:
    snippets: list[str] = []
    for source in sources:
        text = source.get("text") or ""
        sentence = _first_sentence(text)
        if not sentence:
            sentence = _first_sentence(source.get("title") or "")
        if not sentence:
            continue
        url = source.get("url") or ""
        if _is_valid_url(url):
            sentence = f"{sentence} ({_format_reference(url)})"
        snippets.append(sentence)
        if len(snippets) >= limit:
            break
    return snippets


def _build_evidence_summary(evidence: dict, keyword: str) -> str:
    if not isinstance(evidence, dict):
        return f"Evidence summary for {keyword} is limited."
    lines: list[str] = []
    timeline = evidence.get("timeline") or []
    if isinstance(timeline, list):
        for item in timeline[:3]:
            if not isinstance(item, dict):
                continue
            date = str(item.get("date") or "").strip()
            event = str(item.get("event") or "").strip()
            source = str(item.get("source") or "").strip()
            if event:
                lines.append(f"- {date} {event} ({source})".strip())
    claims = evidence.get("claims") or []
    if isinstance(claims, list):
        for item in claims[:3]:
            if not isinstance(item, dict):
                continue
            claim = str(item.get("claim") or "").strip()
            source = str(item.get("source") or "").strip()
            if claim:
                lines.append(f"- Claim: {claim} ({source})".strip())
    conflicts = evidence.get("conflicts") or []
    if isinstance(conflicts, list) and conflicts:
        lines.append("- Conflicts exist between sources; treat with caution.")
    if not lines:
        return f"Evidence summary for {keyword} is limited."
    return " ".join(lines)


def _select_topics_by_region(
    payload: dict,
    regions: list[str],
    max_per_region: int,
) -> list[dict]:
    items = payload.get("items", [])
    region_items: dict[str, list[dict]] = {region: [] for region in regions}
    for item in items:
        region = item.get("region")
        if region in region_items:
            region_items[region].append(item)

    selected: list[dict] = []
    for region in regions:
        region_list = sorted(region_items.get(region, []), key=lambda x: x.get("rank", 999))
        seen: set[str] = set()
        for item in region_list:
            keyword = item.get("keyword")
            if not keyword:
                continue
            normalized = _normalize_keyword(str(keyword))
            if normalized in seen:
                continue
            seen.add(normalized)
            selected.append(item)
            if len(seen) >= max_per_region:
                break
    return selected


def _load_state() -> dict:
    return read_json(STATE_PATH, default={"topics": [], "slugs": []}) or {"topics": [], "slugs": []}


def _save_state(state: dict) -> None:
    write_json(STATE_PATH, state)


class GeminiClient:
    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = "https://generativelanguage.googleapis.com/v1beta"

    def generate(self, prompt: str, *, temperature: float, max_tokens: int) -> str:
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY is not set.")
        url = f"{self.base_url}/models/{self.model}:generateContent?key={self.api_key}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
        candidates = data.get("candidates", [])
        if not candidates:
            raise RuntimeError("Gemini returned no candidates.")
        parts = candidates[0].get("content", {}).get("parts", [])
        text_parts = [part.get("text", "") for part in parts if isinstance(part, dict)]
        return "\n".join(text_parts).strip()


def _extract_json_block(text: str) -> dict | None:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    snippet = text[start : end + 1]
    try:
        return json.loads(snippet)
    except json.JSONDecodeError:
        return None


def _compose_prompt(system: str, user: str) -> str:
    system_block = system.strip()
    user_block = user.strip()
    return f"SYSTEM:\n{system_block}\n\nUSER:\n{user_block}"


def _infer_language_code(language: str | None) -> str | None:
    if not language:
        return None
    normalized = language.strip().lower()
    if normalized in {"en", "english"}:
        return "en"
    if normalized in {"ko", "korean"}:
        return "ko"
    if normalized in {"ja", "japanese"}:
        return "ja"
    return None


def _normalize_region_code(region: str | None) -> str | None:
    if not region:
        return None
    normalized = region.strip().lower().replace(" ", "_")
    if len(normalized) == 2 and normalized.isalpha():
        return normalized.upper()
    return REGION_CODE_MAP.get(normalized)


def _google_news_params(region_code: str | None, language_code: str | None) -> str:
    if not region_code or not language_code:
        return ""
    return f"&hl={language_code}&gl={region_code}&ceid={region_code}:{language_code}"


def _fetch_url_raw(
    url: str,
    user_agent: str,
    timeout: int,
    delay_sec: float,
    max_retries: int,
    backoff_sec: float,
) -> str | None:
    sanitized_url = _sanitize_url(url)
    for attempt in range(max_retries + 1):
        _throttle_domain(sanitized_url, delay_sec)
        request = Request(sanitized_url, headers={"User-Agent": user_agent})
        try:
            with urlopen(request, timeout=timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, errors="ignore")
        except HTTPError as exc:
            if exc.code == 429 and attempt < max_retries:
                sleep_for = backoff_sec * (2**attempt)
                logging.warning(
                    "429 rate limit for %s, retrying in %.1fs",
                    sanitized_url,
                    sleep_for,
                )
                time.sleep(sleep_for)
                continue
            logging.warning("Failed to fetch %s: %s", sanitized_url, exc)
            return None
        except (
            URLError,
            TimeoutError,
            http.client.IncompleteRead,
            http.client.RemoteDisconnected,
        ) as exc:
            if attempt < max_retries:
                sleep_for = backoff_sec * (2**attempt)
                logging.warning(
                    "Fetch failed for %s, retrying in %.1fs (%s)",
                    sanitized_url,
                    sleep_for,
                    exc,
                )
                time.sleep(sleep_for)
                continue
            logging.warning("Failed to fetch %s: %s", sanitized_url, exc)
            return None
    return None


def _fetch_url_raw_with_status(
    url: str,
    user_agent: str,
    timeout: int,
    delay_sec: float,
    max_retries: int,
    backoff_sec: float,
) -> tuple[str | None, int | None]:
    sanitized_url = _sanitize_url(url)
    for attempt in range(max_retries + 1):
        _throttle_domain(sanitized_url, delay_sec)
        request = Request(sanitized_url, headers={"User-Agent": user_agent})
        try:
            with urlopen(request, timeout=timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, errors="ignore"), response.status
        except HTTPError as exc:
            if exc.code == 429 and attempt < max_retries:
                sleep_for = backoff_sec * (2**attempt)
                logging.warning(
                    "429 rate limit for %s, retrying in %.1fs",
                    sanitized_url,
                    sleep_for,
                )
                time.sleep(sleep_for)
                continue
            logging.warning("Failed to fetch %s: %s", sanitized_url, exc)
            return None, exc.code
        except (
            URLError,
            TimeoutError,
            http.client.IncompleteRead,
            http.client.RemoteDisconnected,
        ) as exc:
            if attempt < max_retries:
                sleep_for = backoff_sec * (2**attempt)
                logging.warning(
                    "Fetch failed for %s, retrying in %.1fs (%s)",
                    sanitized_url,
                    sleep_for,
                    exc,
                )
                time.sleep(sleep_for)
                continue
            logging.warning("Failed to fetch %s: %s", sanitized_url, exc)
            return None, None
    return None, None


def _post_json_with_status(
    url: str,
    payload: dict,
    user_agent: str,
    timeout: int,
    delay_sec: float,
    max_retries: int,
    backoff_sec: float,
) -> tuple[str | None, int | None]:
    sanitized_url = _sanitize_url(url)
    body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    headers = {
        "User-Agent": user_agent,
        "Content-Type": "application/json",
    }
    for attempt in range(max_retries + 1):
        _throttle_domain(sanitized_url, delay_sec)
        request = Request(sanitized_url, data=body, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, errors="ignore"), response.status
        except HTTPError as exc:
            if exc.code == 429 and attempt < max_retries:
                sleep_for = backoff_sec * (2**attempt)
                logging.warning(
                    "429 rate limit for %s, retrying in %.1fs",
                    sanitized_url,
                    sleep_for,
                )
                time.sleep(sleep_for)
                continue
            logging.warning("Failed to POST %s: %s", sanitized_url, exc)
            return None, exc.code
        except (
            URLError,
            TimeoutError,
            http.client.IncompleteRead,
            http.client.RemoteDisconnected,
        ) as exc:
            if attempt < max_retries:
                sleep_for = backoff_sec * (2**attempt)
                logging.warning(
                    "POST failed for %s, retrying in %.1fs (%s)",
                    sanitized_url,
                    sleep_for,
                    exc,
                )
                time.sleep(sleep_for)
                continue
            logging.warning("Failed to POST %s: %s", sanitized_url, exc)
            return None, None
    return None, None


def _parse_pub_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except Exception:
        return None


def _search_news_rss(
    query: str,
    *,
    region: str | None,
    language: str | None,
    max_results: int,
    config: AutomationConfig,
) -> list[dict]:
    if not query:
        return []
    region_code = _normalize_region_code(region)
    language_code = _infer_language_code(language)
    if not language_code and region_code:
        language_code = GOOGLE_NEWS_LANGUAGE_MAP.get(region_code)
    params = _google_news_params(region_code, language_code)
    url = f"https://news.google.com/rss/search?q={quote(query)}{params}"
    xml_text = _fetch_url_raw(
        url,
        config.user_agent,
        config.scrape_timeout,
        config.scrape_delay_sec,
        config.scrape_max_retries,
        config.scrape_backoff_sec,
    )
    if not xml_text:
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        logging.warning("Failed to parse RSS for query: %s", query)
        return []
    results: list[dict] = []
    for item in root.findall("./channel/item"):
        link = item.findtext("link") or ""
        title = item.findtext("title") or ""
        source = item.findtext("source") or ""
        pub_date = _parse_pub_date(item.findtext("pubDate"))
        if not _is_valid_url(link):
            continue
        results.append(
            {
                "url": link.strip(),
                "title": title.strip(),
                "publisher": source.strip() or urlparse(link).netloc,
                "published_at": pub_date,
                "origin": "google_news_rss",
            }
        )
        if len(results) >= max_results:
            break
    return results


def _search_web_tavily(
    query: str,
    *,
    max_results: int,
    config: AutomationConfig,
) -> list[dict]:
    if not query or max_results <= 0:
        return []
    if not config.tavily_api_key:
        return []
    payload: dict[str, object] = {
        "api_key": config.tavily_api_key,
        "query": query,
        "search_depth": config.search_web_depth,
        "max_results": min(max_results, 10),
        "include_answer": config.search_web_include_answer,
    }
    if config.search_web_include_domains:
        payload["include_domains"] = config.search_web_include_domains
    if config.search_web_exclude_domains:
        payload["exclude_domains"] = config.search_web_exclude_domains
    response, status = _post_json_with_status(
        "https://api.tavily.com/search",
        payload,
        config.user_agent,
        config.scrape_timeout,
        config.scrape_delay_sec,
        config.scrape_max_retries,
        config.scrape_backoff_sec,
    )
    if not response:
        if status in {401, 403}:
            logging.warning("Tavily search auth failed (status %s). Check TAVILY_API_KEY.", status)
        return []
    try:
        data = json.loads(response)
    except json.JSONDecodeError:
        return []
    items = data.get("results")
    if not isinstance(items, list):
        return []
    results: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not _is_valid_url(url):
            continue
        title = str(item.get("title") or "").strip()
        snippet = str(item.get("content") or item.get("snippet") or "").strip()
        published_at = str(item.get("published_date") or item.get("published_at") or "").strip()
        results.append(
            {
                "url": url,
                "title": title or "Untitled",
                "publisher": urlparse(url).netloc,
                "published_at": published_at or None,
                "origin": "tavily_search",
                "snippet": snippet,
            }
        )
    return results


def _extract_web_content_tavily(
    urls: list[str],
    *,
    config: AutomationConfig,
) -> list[dict]:
    if not urls or not config.tavily_api_key:
        return []
    payload = {
        "api_key": config.tavily_api_key,
        "urls": urls,
        "max_characters": config.max_source_chars,
    }
    response, status = _post_json_with_status(
        "https://api.tavily.com/extract",
        payload,
        config.user_agent,
        config.scrape_timeout,
        config.scrape_delay_sec,
        config.scrape_max_retries,
        config.scrape_backoff_sec,
    )
    if not response:
        if status in {401, 403}:
            logging.warning("Tavily extract auth failed (status %s). Check TAVILY_API_KEY.", status)
        return []
    try:
        data = json.loads(response)
    except json.JSONDecodeError:
        return []
    items = data.get("results")
    if not isinstance(items, list):
        return []
    results: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not _is_valid_url(url):
            continue
        title = str(item.get("title") or "").strip()
        content = str(item.get("content") or item.get("raw_content") or "").strip()
        results.append(
            {
                "url": url,
                "title": title,
                "content": content,
            }
        )
    return results


def _search_youtube(
    query: str,
    *,
    region: str | None,
    language: str | None,
    max_results: int,
    config: AutomationConfig,
) -> list[dict]:
    if not query or max_results <= 0:
        return []
    api_key = config.youtube_api_key or config.google_api_key or config.gemini_api_key
    if not api_key:
        return []
    region_code = _normalize_region_code(region)
    language_code = _infer_language_code(language)
    params = {
        "key": api_key,
        "part": "snippet",
        "q": query,
        "type": "video",
        "maxResults": min(max_results, 10),
        "safeSearch": "moderate",
        "order": "relevance",
    }
    if region_code:
        params["regionCode"] = region_code
    if language_code:
        params["relevanceLanguage"] = language_code
    url = f"https://www.googleapis.com/youtube/v3/search?{urlencode(params)}"
    response, status = _fetch_url_raw_with_status(
        url,
        config.user_agent,
        config.scrape_timeout,
        config.scrape_delay_sec,
        config.scrape_max_retries,
        config.scrape_backoff_sec,
    )
    if not response:
        if status == 403:
            logging.warning(
                "YouTube API 403. Check YOUTUBE_API_KEY or GOOGLE_API_KEY and "
                "ensure YouTube Data API v3 is enabled."
            )
        return []
    try:
        data = json.loads(response)
    except json.JSONDecodeError:
        return []
    items = data.get("items")
    if not isinstance(items, list):
        return []
    results: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        video_id = item.get("id", {}).get("videoId")
        if not video_id:
            continue
        snippet = item.get("snippet", {}) if isinstance(item.get("snippet"), dict) else {}
        title = str(snippet.get("title") or "").strip()
        channel = str(snippet.get("channelTitle") or "").strip()
        published_at = str(snippet.get("publishedAt") or "").strip() or None
        url = f"https://www.youtube.com/watch?v={video_id}"
        results.append(
            {
                "url": url,
                "title": title or "YouTube video",
                "publisher": channel or "YouTube",
                "published_at": published_at,
                "origin": "youtube_search",
            }
        )
    return results


def _normalize_url_for_dedupe(url: str) -> str:
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    return base.rstrip("/")


def _candidate_sources_from_topic(topic: dict) -> list[dict]:
    candidates: list[dict] = []
    explore_link = topic.get("explore_link")
    if _is_valid_url(explore_link):
        candidates.append(
            {
                "url": explore_link,
                "title": "Google Trends",
                "publisher": "trends.google.com",
                "published_at": topic.get("published_at"),
                "origin": "trend_explore",
            }
        )
    articles = topic.get("news_articles") or []
    if isinstance(articles, list):
        for article in articles:
            if not isinstance(article, dict):
                continue
            url = article.get("url")
            if not _is_valid_url(url):
                continue
            candidates.append(
                {
                    "url": url,
                    "title": str(article.get("title") or "").strip(),
                    "publisher": str(article.get("source") or "").strip()
                    or urlparse(url).netloc,
                    "published_at": article.get("published_at"),
                    "origin": "trend_article",
                }
            )
    metadata = topic.get("metadata")
    if isinstance(metadata, dict):
        for value in metadata.values():
            if _is_valid_url(value):
                url = str(value).strip()
                candidates.append(
                    {
                        "url": url,
                        "title": "",
                        "publisher": urlparse(url).netloc,
                        "published_at": None,
                        "origin": "trend_metadata",
                    }
                )
    return candidates


def _dedupe_candidates(candidates: list[dict]) -> list[dict]:
    seen: set[str] = set()
    unique: list[dict] = []
    for candidate in candidates:
        url = candidate.get("url")
        if not _is_valid_url(url):
            continue
        normalized = _normalize_url_for_dedupe(url)
        if normalized in seen:
            continue
        seen.add(normalized)
        unique.append(candidate)
    return unique


def _fetch_sources_from_candidates(
    candidates: list[dict],
    config: AutomationConfig,
    *,
    max_sources: int | None = None,
) -> list[dict]:
    if not config.tavily_api_key:
        return []
    sources: list[dict] = []
    total_chars = 0
    normalized_candidates: list[dict] = []
    candidate_by_url: dict[str, dict] = {}
    for candidate in candidates:
        url = candidate.get("url")
        if not _is_valid_url(url):
            continue
        normalized = _normalize_url_for_dedupe(str(url))
        candidate_by_url[normalized] = candidate
        normalized_candidates.append(candidate)
    limit = max_sources
    batch_size = 5 if limit is None else max(1, min(5, limit))
    index = 0
    while index < len(normalized_candidates):
        if limit is not None and len(sources) >= limit:
            break
        batch_urls: list[str] = []
        while index < len(normalized_candidates) and len(batch_urls) < batch_size:
            url = str(normalized_candidates[index].get("url") or "").strip()
            index += 1
            if _is_valid_url(url):
                batch_urls.append(url)
        if not batch_urls:
            continue
        extracted = _extract_web_content_tavily(batch_urls, config=config)
        extracted_map = {
            _normalize_url_for_dedupe(item.get("url", "")): item for item in extracted
        }
        for url in batch_urls:
            if limit is not None and len(sources) >= limit:
                break
            normalized = _normalize_url_for_dedupe(url)
            item = extracted_map.get(normalized)
            if not item:
                continue
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            cleaned = _truncate(re.sub(r"\s+", " ", content), config.max_source_chars)
            if total_chars >= config.max_total_source_chars:
                cleaned = ""
            else:
                remaining = config.max_total_source_chars - total_chars
                if len(cleaned) > remaining:
                    cleaned = _truncate(cleaned, remaining)
                total_chars += len(cleaned)
            if not cleaned:
                continue
            candidate = candidate_by_url.get(normalized, {})
            sources.append(
                {
                    "url": url,
                    "title": item.get("title") or candidate.get("title") or "Untitled",
                    "publisher": candidate.get("publisher") or urlparse(url).netloc,
                    "published_at": candidate.get("published_at") or None,
                    "text": cleaned,
                    "origin": candidate.get("origin"),
                }
            )
    return sources


def _build_content_prompt(
    keyword: str,
    region: str,
    traffic: str | None,
    sources: list[dict],
    image_urls: list[str],
    references: list[str],
    language: str = "English",
) -> str:
    sources_text = "\n".join(
        f"- {source['url']} | {source.get('title') or 'Untitled'}\n  {source['text']}"
        for source in sources
    )
    images_text = "\n".join(f"- {url}" for url in image_urls) or "- None"
    refs_text = "\n".join(f"- {url}" for url in references) or "- None"
    image_requirement = (
        "Embed at least 2 images from the provided list in the middle of the article."
        if image_urls
        else "No images are available. Do not add image markdown."
    )

    return f"""
Role: Columnist and investigative writer.
Write in {language} only. ASCII characters only.

Primary keyword: {keyword}
Region: {region}
Traffic: {traffic or "unknown"}

Source notes (use these to build original analysis, not a list of links):
{sources_text}

Image URLs you MUST embed in the body (use Markdown images):
{images_text}

Reference URLs (include in a final reference list):
{refs_text}

Output JSON only. {CONTENT_JSON_SCHEMA}

SEO requirements:
- Use the primary keyword in the first paragraph, one H2 heading, and the conclusion.
- Use 2-4 secondary keywords derived from the sources (natural phrasing, no stuffing).
- Keep paragraphs short (2-4 sentences).
- Include one FAQ section with 2-4 Q/A items.

Editorial requirements:
- Write a full topic column, not a summary or bullet digest.
- Provide depth: background, recent trigger, stakeholder impact, and forward-looking analysis.
- Include at least 3 inline citations with Markdown links inside paragraphs.
- Do NOT use the headings "Overview", "Key Points", or "Implications".
- Use 3-6 meaningful section headings tailored to the story.
- Include an opening paragraph with a clear angle, and a closing paragraph with a takeaway.
- Integrate at least 2 source links inline in the body (e.g., "According to [Source](url)...").
- {image_requirement}
- Avoid listing raw URLs; all URLs must be Markdown links.
- Do not tell readers to click the links for details; include the details in the column.
- Never include ellipses or truncated fragments. Rewrite into complete sentences.
- Do not include frontmatter.
""".strip()


def _build_topic_ranker_prompt(trend_items_json: str) -> str:
    system = """
You are the editorial director for a trend-driven, evidence-first blog.
Your mission is to choose topics that are provably true, useful to readers,
and safe for the brand.

CORE PRINCIPLES
1) Evidence > hype: prioritize topics with multiple credible sources.
2) Reader value > virality: focus on practical impact and clarity.
3) Brand safety: avoid defamation, medical/legal/financial claims without strong proof.
4) SEO durability: prefer topics with sustained search intent and clear angles.

DECISION RUBRIC
- Evidence depth (primary/official + reputable secondary)
- Why now (time relevance or new development)
- Angle clarity (specific audience + scope)
- Differentiation (room for original analysis)
- Risk level (misinfo, legal, medical, privacy)

REJECTION RULES
- Single-source or unverifiable claims
- Sensational, speculative, or rumor-driven topics
- Topics lacking a concrete angle or reader benefit

OUTPUT REQUIREMENTS
- Use only provided inputs; do not browse.
- Return JSON only and follow the exact schema.
""".strip()
    user = f"""
You will receive a list of trend items. For each item decide:
1) select or reject
2) recommended angle (who + what + why now)
3) risk level (low|medium|high) with a short reason and risk type
4) research needs to verify key claims

Rules:
- Use only the provided inputs. Do not browse.
- Be conservative: if evidence seems thin, reject or mark high risk.
- "why_now" must be grounded in the input signal (trend data).
- "research_needs" must be specific verification tasks, not generic.
- Return JSON only and do not add extra keys.

Input:
{trend_items_json}

Output JSON:
{{
  "selected": [
    {{
      "keyword": "...",
      "angle": "...",
      "why_now": "...",
      "risk": "low|medium|high",
      "research_needs": ["..."]
    }}
  ],
  "rejected": [
    {{"keyword": "...", "reason": "..."}}
  ]
}}
""".strip()
    return _compose_prompt(system, user)


def _build_research_planner_prompt(
    *,
    keyword: str,
    angle: str,
    language: str,
    region: str,
) -> str:
    system = """
You are a research strategist specializing in rapid, verifiable reporting.
Design search queries and verification points that build a factual,
well-sourced article from credible evidence.

PRIORITIES
1) Primary/official sources first (government, company, regulator, court, dataset)
2) Reputable secondary sources for context and synthesis
3) Explicit verification of dates, numbers, and core claims
4) Balanced viewpoints when controversy exists

QUALITY RULES
- Queries must be specific, testable, and time-aware
- Include the current year in at least half of the queries
- Avoid vague or clickbait phrasing
""".strip()
    user = f"""
Topic: {keyword}
Angle: {angle}
Language: {language}
Region: {region}

Tasks:
1) Generate 5-8 search queries (natural language, include current year)
2) Propose priority sources or domains to check first
3) List must-verify claims or questions

Rules:
- Queries should be specific and testable
- Include at least one query for official statements or data
- Include at least one query for statistics or datasets
- Include at least one query for reputable local/regional coverage (if relevant)
- Include one query aimed at verification or debunking if claims are contentious
- Avoid vague or clickbait wording
- Output JSON only with the keys below

Output JSON:
{{
  "queries": ["..."],
  "priority_sources": ["..."],
  "must_verify": ["..."]
}}
""".strip()
    return _compose_prompt(system, user)


def _build_research_rescue_prompt(
    *,
    keyword: str,
    angle: str,
    language: str,
    region: str,
    failed_domains: list[str],
) -> str:
    system = """
You are a research rescue agent.
When sources are blocked or missing, propose alternative queries and reliable domains.
Prioritize official sources, reputable outlets, and accessible references.
Avoid domains that are likely paywalled or blocked.
""".strip()
    user = f"""
Topic: {keyword}
Angle: {angle}
Language: {language}
Region: {region}
Failed domains: {json.dumps(failed_domains, ensure_ascii=True)}

Tasks:
1) Generate 4-6 alternative search queries (include current year in at least half)
2) Provide 3-6 priority sources or domains to try next

Rules:
- Avoid the failed domains list when possible
- Prefer official statements, regulators, or primary sources
- Output JSON only with the keys below

Output JSON:
{{
  "queries": ["..."],
  "priority_sources": ["..."]
}}
""".strip()
    return _compose_prompt(system, user)


def _build_web_research_prompt(
    *,
    queries: list[str],
    priority_sources: list[str],
    raw_sources_json: str,
) -> str:
    system = """
You are a web evidence collector with forensic standards.
Extract verifiable facts from primary or reputable secondary sources.
Never rely on snippets alone. You must use the provided source extracts.
Capture dates, numbers, and direct quotes when available.
Separate facts from interpretation and avoid speculation.
""".strip()
    user = f"""
Inputs:
queries: {json.dumps(queries, ensure_ascii=True)}
priority_sources: {json.dumps(priority_sources, ensure_ascii=True)}

Raw sources (only use these sources):
{raw_sources_json}

Output JSON:
{{
  "sources": [
    {{
      "title": "...",
      "url": "...",
      "publisher": "...",
      "published_at": "...",
      "key_facts": ["..."],
      "direct_quotes": ["..."]
    }}
  ]
}}

Constraints:
- Include 3-8 sources if possible (at least 1 primary source when feasible).
- key_facts should be specific, attributable, and include dates/numbers.
- direct_quotes should be short and exact.
- If a field is unknown, use "unknown".
- If sources conflict, include both and note the conflict in key_facts.
""".strip()
    return _compose_prompt(system, user)


def _build_evidence_builder_prompt(*, sources_json: str) -> str:
    system = """
You are the evidence synthesizer.
Structure facts into timelines, claims, and unresolved questions.
Separate verified facts from uncertainty and highlight conflicts between sources.
Do not add new information beyond the provided sources.
Only promote information to "claims" if the sources explicitly support it.
""".strip()
    user = f"""
Input:
{sources_json}

Output JSON:
{{
  "timeline": [
    {{"date": "...", "event": "...", "source": "..."}}
  ],
  "claims": [
    {{"claim": "...", "evidence": ["..."], "source": "..."}}
  ],
  "open_questions": ["..."],
  "conflicts": [
    {{"issue": "...", "source_a": "...", "source_b": "..."}}
  ]
}}

Rules:
- Use source URLs or publisher names in source fields.
- Claims must be backed by explicit evidence.
- If no conflicts exist, return an empty conflicts array.
- Use clear, specific dates (ISO when possible) in timeline.
- If evidence is weak or ambiguous, put it in open_questions instead of claims.
""".strip()
    return _compose_prompt(system, user)


def _build_outline_prompt(*, keyword: str, angle: str, evidence_summary: str) -> str:
    system = """
You are the article architect.
Create a logical, reader-friendly structure that supports the chosen angle.
Balance context, evidence, impact, and forward-looking analysis.
Use section headings that are specific, concrete, and SEO-aware.
Anchor sections in evidence and reader intent (what they came to learn).
""".strip()
    user = f"""
Topic: {keyword}
Angle: {angle}
Evidence summary: {evidence_summary}

Output JSON:
{{
  "title_direction": "...",
  "sections": [
    {{"heading": "...", "goal": "...", "evidence_refs": ["..."]}}
  ],
  "faq": ["...","..."]
}}

Rules:
- Provide 4-7 sections.
- Avoid generic headings like "Overview" or "Conclusion".
- evidence_refs should point to source URLs or IDs.
- Ensure at least one section addresses "what changed/why now".
- Ensure at least one section addresses "impact / what it means for readers".
- Include the primary keyword (or close variation) in at least 2 headings.
- FAQ should target high-intent reader questions, not trivia.
""".strip()
    return _compose_prompt(system, user)


def _build_resource_allocation_prompt(*, outline_json: str, sources_json: str) -> str:
    system = """
You are the resource editor.
Assign images and YouTube resources that improve understanding and trust.
Avoid copyright or brand risk. Prefer original illustrations or licensed stock.
Quality over quantity: it is better to assign no resource than a weak one.
Avoid logos, brand marks, and identifiable faces unless essential.
For time-sensitive topics, prioritize recent and credible sources.
""".strip()
    user = f"""
Inputs:
{outline_json}
{sources_json}

Output JSON:
{{
  "inline_images": [
    {{"section_heading": "...", "image_type": "generated|licensed", "prompt_or_query": "..."}}
  ],
  "hero_image": {{"style_prompt": "...", "alt_text": "..."}},
  "youtube_queries": ["..."]
}}

Rules:
- Provide 1-3 inline image suggestions.
- Generated images should be clean, minimal, and text free.
- Licensed images should be described as search queries for stock sites.
- YouTube queries must be specific and educational.
- If no strong match exists, return empty arrays instead of forcing matches.
- For time-sensitive topics, include the current year in YouTube queries.
- Alt text must be concrete and descriptive, not generic.
""".strip()
    return _compose_prompt(system, user)


def _build_section_writer_prompt(
    *,
    section_heading: str,
    section_goal: str,
    evidence_subset: str,
    sources_subset: str,
    language: str,
) -> str:
    system = """
You are a section writer for a single part of the article.
Write with precision and evidence. Do not invent facts.
Every factual statement must be supported by a citation.
Use Markdown links for citations and avoid raw URLs.
If evidence is thin, be cautious and state uncertainty explicitly.
""".strip()
    user = f"""
Section title: {section_heading}
Section goal: {section_goal}
Evidence/facts: {evidence_subset}
Relevant sources: {sources_subset}
Language: {language}

Writing rules:
- 3-6 paragraphs, 2-4 sentences each
- Include at least 2 inline citations with Markdown links
- Do not make claims without citations
- Avoid hype or sensational wording
- Do not add a heading; the assembler will add it
- Prefer clear cause -> evidence -> implication flow
- If a key claim lacks evidence, mark it as uncertain rather than assert it
- Keep terminology consistent with sources (avoid re-labeling entities)

Output (MDX):
{{section_mdx}}
""".strip()
    return _compose_prompt(system, user)


def _build_assembler_prompt(
    *,
    section_mdx_list: list[str],
    faq_list: list[str],
    tone: str,
    keyword: str,
) -> str:
    system = """
You are the editor in chief.
Assemble sections into a coherent article with smooth transitions.
Add an intro, conclusion, and FAQ without adding new facts.
Preserve all citations and do not invent sources.
Maintain a consistent voice and avoid redundancy across sections.
""".strip()
    user = f"""
Inputs:
sections: {json.dumps(section_mdx_list, ensure_ascii=True)}
faq: {json.dumps(faq_list, ensure_ascii=True)}
tone: {tone}
primary_keyword: {keyword}

Requirements:
- Include the primary keyword in the first paragraph and conclusion
- Keep paragraphs short and readable
- Do not add new claims or sources
- Intro should set scope and "why now" context using existing evidence
- Conclusion should summarize evidence and note remaining uncertainties
- FAQ answers must be concise and evidence-based

Output (MDX):
full article body
""".strip()
    return _compose_prompt(system, user)


def _build_quality_gate_prompt(*, full_mdx: str) -> str:
    system = """
You are a world-class content quality auditor.
Evaluate factual support, structure, SEO, readability, and risk.
Be strict: if any critical issue exists, require revision.
Return only JSON with issues when revisions are needed.
""".strip()
    user = f"""
Input:
{full_mdx}

Checklist:
- Every factual claim has a citation
- Primary keyword appears in title, first paragraph, and conclusion
- Sections are specific and non generic
- Paragraphs are not overly long
- Tone is neutral and informative
- No unsupported statistics, dates, or direct quotes
- No sensational or speculative language
- No repeated or redundant paragraphs
- FAQ answers are concise and evidence-based

Output JSON:
{{
  "status": "pass|revise",
  "issues": [
    {{"type": "missing_citation|factual_risk|seo|structure|style", "detail": "...", "fix_hint": "..."}}
  ]
}}
""".strip()
    return _compose_prompt(system, user)


def _build_revision_prompt(*, full_mdx: str, issues_json: str, keyword: str) -> str:
    system = """
You are a senior editor revising an article to address quality issues.
You must fix the issues without adding new facts or sources.
Preserve existing citations and only adjust wording or structure.
""".strip()
    user = f"""
Article:
{full_mdx}

Issues JSON:
{issues_json}

Rules:
- Do not add new claims or sources.
- Keep the primary keyword "{keyword}" in the first paragraph and conclusion.
- Keep paragraphs short and avoid redundancy.
- Preserve all Markdown links.

Output (MDX):
revised article body
""".strip()
    return _compose_prompt(system, user)


def _build_meta_prompt(
    *,
    keyword: str,
    summary: str,
    key_points: list[str],
    body_excerpt: str,
    image_prompt_hint: str,
    language: str = "English",
) -> str:
    key_points_text = "\n".join(f"- {point}" for point in key_points) or "- None"
    return f"""
SYSTEM:
You are an SEO frontmatter generator for an Astro blog.
Create concise, accurate metadata aligned with the article.
Avoid clickbait and never claim facts not supported by the article.

USER:
Inputs:
keyword: {keyword}
summary: {summary}
body_excerpt: {body_excerpt}
language: {language}

Key points:
{key_points_text}

Image prompt hint (use or improve): {image_prompt_hint}

Rules:
- title length 50-65 characters
- description length 140-160 characters
- 1-2 categories, 1-3 tags
- hero_alt must be concrete and descriptive
- English only unless language specifies otherwise
- Include the primary keyword naturally in title and description
- Avoid sensational wording or absolute claims
- Output JSON only with the keys below

Output JSON:
{{
  "title": "...",
  "description": "...",
  "category": ["..."],
  "tags": ["..."],
  "hero_alt": "...",
  "image_prompt": "..."
}}
""".strip()


def _ensure_images_in_body(body: str, image_urls: list[str], alt_text: str) -> str:
    if not image_urls:
        return body
    existing = re.findall(r"!\[.*?\]\((.*?)\)", body)
    needed = 2 if len(image_urls) >= 2 else 1
    if len(existing) >= needed:
        return body
    existing_set = {url for url in existing if isinstance(url, str)}
    add_urls = [url for url in image_urls if url not in existing_set]
    safe_alt = _ensure_ascii_text(alt_text, "Related image")
    blocks = [f"![{safe_alt}]({url})" for url in add_urls[:needed]]
    parts = body.split("\n\n")
    insert_at = 2 if len(parts) > 2 else len(parts)
    parts[insert_at:insert_at] = blocks
    return "\n\n".join(parts)


def _generate_hero_image_google(prompt: str, output_path: Path, config: AutomationConfig) -> bool:
    if not config.google_image_enabled:
        return False
    api_key = config.google_api_key or config.gemini_api_key
    if not api_key:
        return False
    model = config.google_image_model or DEFAULT_GOOGLE_IMAGE_MODEL
    safe_prompt = _force_ascii(prompt).strip() or "Abstract tech illustration"
    payload = {
        "instances": [{"prompt": safe_prompt}],
        "parameters": {
            "sampleCount": 1,
            "aspectRatio": config.google_image_aspect_ratio or DEFAULT_GOOGLE_IMAGE_ASPECT_RATIO,
        },
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:predict"
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        logging.warning("Google image generation failed: %s", exc)
        return False
    predictions = data.get("predictions")
    if not isinstance(predictions, list):
        return False
    for prediction in predictions:
        if not isinstance(prediction, dict):
            continue
        b64 = prediction.get("bytesBase64Encoded")
        if not b64 and isinstance(prediction.get("image"), dict):
            b64 = prediction["image"].get("bytesBase64Encoded")
        if not b64 and isinstance(prediction.get("imageBytes"), dict):
            b64 = prediction["imageBytes"].get("bytesBase64Encoded")
        if not b64:
            continue
        try:
            image_bytes = base64.b64decode(b64)
        except Exception:
            continue
        output_path.write_bytes(image_bytes)
        return True
    return False


def _generate_hero_image(prompt: str, output_path: Path, config: AutomationConfig) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if _generate_hero_image_google(prompt, output_path, config):
        logging.info("Hero image generated via Google Imagen.")
        return
    if config.nanobanana_cmd:
        cmd = [config.nanobanana_cmd]
        if config.nanobanana_args:
            cmd += shlex.split(config.nanobanana_args)
        cmd += ["--prompt", prompt, "--output", str(output_path)]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0 and output_path.exists():
            logging.info("Hero image generated via nanobanana.")
            return
        if config.nanobanana_required:
            raise RuntimeError(
                f"Nanobanana failed (required): {result.stderr.strip() or 'unknown error'}"
            )
        logging.warning("Nanobanana failed: %s", result.stderr.strip())
    elif config.nanobanana_required:
        raise RuntimeError("Nanobanana command is required but not configured.")

    placeholder = config.astro_root / "src" / "assets" / "blog-placeholder-5.jpg"
    if placeholder.exists():
        output_path.write_bytes(placeholder.read_bytes())
        logging.info("Hero image fallback used.")
    else:
        logging.warning("Hero image placeholder missing: %s", placeholder)


def _build_frontmatter(
    *,
    title: str,
    description: str,
    date_str: str,
    slug: str,
    category: list[str],
    tags: list[str],
    draft: bool,
    hero_alt: str,
    domain: str,
) -> str:
    safe_title = title.replace('"', '\\"')
    safe_desc = description.replace('"', '\\"')
    safe_alt = hero_alt.replace('"', '\\"')
    category_list = ", ".join(f'"{item}"' for item in category)
    tag_list = ", ".join(f'"{tag}"' for tag in tags)
    canonical = f"{domain}/blog/{date_str}-{slug}"
    return (
        "---\n"
        f'title: "{safe_title}"\n'
        f'description: "{safe_desc}"\n'
        f"pubDate: {date_str}\n"
        f"updatedDate: {date_str}\n"
        f"category: [{category_list}]\n"
        f"tags: [{tag_list}]\n"
        f"draft: {str(draft).lower()}\n"
        "heroImage:\n"
        f'  src: "/images/posts/{date_str}-{slug}/hero.jpg"\n'
        f'  alt: "{safe_alt}"\n'
        "seo:\n"
        f'  canonical: "{canonical}"\n'
        f'  ogTitle: "{safe_title}"\n'
        f'  ogDescription: "{safe_desc}"\n'
        "---\n"
    )


def _write_post(
    config: AutomationConfig,
    *,
    title: str,
    description: str,
    category: list[str],
    tags: list[str],
    body: str,
    hero_alt: str,
    image_prompt: str,
    reference_urls: list[str],
    slug_hint: str,
) -> Path:
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    slug = _slugify(slug_hint) or f"topic-{int(time.time())}"
    post_path = config.content_dir / f"{date_str}-{slug}.mdx"
    if post_path.exists():
        slug = f"{slug}-{int(time.time())}"
        post_path = config.content_dir / f"{date_str}-{slug}.mdx"

    frontmatter = _build_frontmatter(
        title=title,
        description=description,
        date_str=date_str,
        slug=slug,
        category=category,
        tags=tags,
        draft=config.post_draft,
        hero_alt=hero_alt or title,
        domain=config.blog_domain,
    )

    unique_refs = [url for url in dict.fromkeys(reference_urls) if _is_valid_url(url)]
    references = "\n".join(
        f"- {_format_reference(url)}" for url in unique_refs if _format_reference(url)
    )
    content = body.strip()
    if references:
        content = f"{content}\n\n## References\n\n{references}\n"

    config.content_dir.mkdir(parents=True, exist_ok=True)
    post_path.write_text(frontmatter + "\n" + content + "\n", encoding="utf-8")

    hero_path = config.hero_base_dir / f"{date_str}-{slug}" / "hero.jpg"
    _generate_hero_image(image_prompt or title, hero_path, config)
    return post_path


def _build_fallback_body(
    keyword: str,
    sources: list[dict],
    image_urls: list[str],
    alt_text: str,
) -> str:
    titles = [source.get("title") or "Untitled" for source in sources]
    links = [source.get("url") for source in sources if source.get("url")]
    title_snippet = ", ".join(titles[:3])
    link_snippet = [_format_reference(link) for link in links[:3]]
    detail_snippets = _source_snippets(sources, limit=3)
    detail_text = " ".join(detail_snippets)

    body = (
        f"## Why {keyword} suddenly matters\n\n"
        f"{keyword} is drawing renewed attention, and not just because of a single headline. "
        f"Recent coverage points to overlapping signals that frame the story: "
        f"{title_snippet or 'recent developments across multiple sources'}. "
        f"This column pulls those signals into a single narrative.\n\n"
        f"## What the reporting reveals\n\n"
        f"The coverage suggests a broader shift around {keyword} that goes beyond a one-day spike. "
        f"If you read the reporting closely, the throughline is about momentum and what it implies "
        f"for the next 6-12 months. "
        f"{'Key reporting can be found at ' + ', '.join(link_snippet) + '.' if link_snippet else ''}\n\n"
        f"{detail_text}\n\n"
        f"## The real takeaway\n\n"
        f"The headline may be new, but the underlying forces are not. The most useful way to read "
        f"this moment is to track how decision-makers respond and where the next constraints appear. "
        f"That is the clearest signal to watch as {keyword} evolves.\n\n"
        f"## What to watch next\n\n"
        f"- Look for follow-up coverage that clarifies scope and timeline.\n"
        f"- Watch whether adjacent markets or policies respond quickly.\n"
        f"- Track whether the conversation shifts from novelty to execution.\n"
    )
    return _ensure_images_in_body(body, image_urls, alt_text)


def _rank_topics_with_llm(
    config: AutomationConfig,
    writer: GeminiClient,
    topics: list[dict],
) -> list[dict]:
    if not topics:
        return []
    trend_items = []
    for topic in topics:
        trend_items.append(
            {
                "keyword": topic.get("keyword"),
                "region": topic.get("region"),
                "rank": topic.get("rank"),
                "traffic": topic.get("traffic"),
                "published_at": topic.get("published_at"),
                "explore_link": topic.get("explore_link"),
                "news_articles": topic.get("news_articles"),
                "metadata": topic.get("metadata"),
            }
        )
    prompt = _build_topic_ranker_prompt(
        json.dumps(trend_items, ensure_ascii=True),
    )
    try:
        response = writer.generate(
            prompt,
            temperature=config.gemini_temperature,
            max_tokens=config.gemini_max_tokens,
        )
        data = _extract_json_block(response)
    except Exception as exc:
        logging.warning("Topic ranker failed: %s", exc)
        return topics
    if not isinstance(data, dict):
        return topics
    selected = data.get("selected")
    if not isinstance(selected, list) or not selected:
        return topics
    topic_map = {_normalize_keyword(str(t.get("keyword") or "")): t for t in topics}
    ranked: list[dict] = []
    for item in selected:
        if not isinstance(item, dict):
            continue
        keyword = str(item.get("keyword") or "").strip()
        if not keyword:
            continue
        normalized = _normalize_keyword(keyword)
        topic = topic_map.get(normalized)
        if not topic:
            continue
        topic = dict(topic)
        topic["angle"] = str(item.get("angle") or "").strip()
        topic["why_now"] = str(item.get("why_now") or "").strip()
        topic["risk"] = str(item.get("risk") or "").strip()
        topic["research_needs"] = _ensure_list_of_strings(item.get("research_needs"))
        ranked.append(topic)
    return ranked or topics


def _plan_research(
    config: AutomationConfig,
    writer: GeminiClient,
    *,
    keyword: str,
    angle: str,
    region: str,
) -> dict:
    prompt = _build_research_planner_prompt(
        keyword=keyword,
        angle=angle,
        language=config.content_language,
        region=region or "",
    )
    try:
        response = writer.generate(
            prompt,
            temperature=config.gemini_temperature,
            max_tokens=config.gemini_max_tokens,
        )
        data = _extract_json_block(response)
    except Exception as exc:
        logging.warning("Research planner failed for %s: %s", keyword, exc)
        data = None
    if not isinstance(data, dict):
        data = {}
    queries = _ensure_list_of_strings(data.get("queries"))
    priority_sources = _ensure_list_of_strings(data.get("priority_sources"))
    must_verify = _ensure_list_of_strings(data.get("must_verify"))
    if not queries:
        year = datetime.now(timezone.utc).year
        queries = [
            f"{keyword} official statement {year}",
            f"{keyword} latest updates {year}",
            f"{keyword} data report {year}",
            f"{keyword} policy or regulation {year}",
            f"{keyword} impact analysis {year}",
        ]
    return {
        "queries": queries[:8],
        "priority_sources": priority_sources[:6],
        "must_verify": must_verify[:8],
    }


def _rescue_research_plan(
    config: AutomationConfig,
    writer: GeminiClient,
    *,
    keyword: str,
    angle: str,
    region: str,
    failed_domains: list[str] | None = None,
) -> dict:
    failed_domains = failed_domains or []
    prompt = _build_research_rescue_prompt(
        keyword=keyword,
        angle=angle,
        language=config.content_language,
        region=region or "",
        failed_domains=failed_domains,
    )
    try:
        response = writer.generate(
            prompt,
            temperature=config.gemini_temperature,
            max_tokens=config.gemini_max_tokens,
        )
        data = _extract_json_block(response)
    except Exception as exc:
        logging.warning("Research rescue failed for %s: %s", keyword, exc)
        data = None
    if not isinstance(data, dict):
        return {"queries": [], "priority_sources": []}
    return {
        "queries": _ensure_list_of_strings(data.get("queries"))[:6],
        "priority_sources": _ensure_list_of_strings(data.get("priority_sources"))[:6],
    }


def _gather_sources_for_topic(
    config: AutomationConfig,
    writer: GeminiClient,
    topic: dict,
    research_plan: dict,
) -> list[dict]:
    candidates = _candidate_sources_from_topic(topic)
    if config.search_web_enabled and config.tavily_api_key:
        queries = _ensure_list_of_strings(research_plan.get("queries"))[:8]
        added = 0
        for query in queries:
            results = _search_web_tavily(
                query,
                max_results=config.search_web_max_per_query,
                config=config,
            )
            candidates.extend(results)
            added += len(results)
            if added >= config.search_web_max_results:
                break
    if config.search_rss_enabled:
        queries = _ensure_list_of_strings(research_plan.get("queries"))[:8]
        added = 0
        for query in queries:
            results = _search_news_rss(
                query,
                region=str(topic.get("region") or ""),
                language=config.content_language,
                max_results=config.search_rss_max_per_query,
                config=config,
            )
            candidates.extend(results)
            added += len(results)
            if added >= config.search_rss_max_results:
                break
    candidates = _dedupe_candidates(candidates)
    sources = _fetch_sources_from_candidates(
        candidates,
        config,
        max_sources=config.max_evidence_sources,
    )
    min_sources = min(3, config.max_evidence_sources)
    if len(sources) >= min_sources:
        return sources
    rescue = _rescue_research_plan(
        config,
        writer,
        keyword=str(topic.get("keyword") or ""),
        angle=str(topic.get("angle") or ""),
        region=str(topic.get("region") or ""),
    )
    rescue_queries = _ensure_list_of_strings(rescue.get("queries"))[:6]
    if rescue_queries:
        if config.search_web_enabled and config.tavily_api_key:
            added = 0
            for query in rescue_queries:
                results = _search_web_tavily(
                    query,
                    max_results=config.search_web_max_per_query,
                    config=config,
                )
                candidates.extend(results)
                added += len(results)
                if added >= config.search_web_max_results:
                    break
        if config.search_rss_enabled:
            added = 0
            for query in rescue_queries:
                results = _search_news_rss(
                    query,
                    region=str(topic.get("region") or ""),
                    language=config.content_language,
                    max_results=config.search_rss_max_per_query,
                    config=config,
                )
                candidates.extend(results)
                added += len(results)
                if added >= config.search_rss_max_results:
                    break
        candidates = _dedupe_candidates(candidates)
        sources = _fetch_sources_from_candidates(
            candidates,
            config,
            max_sources=config.max_evidence_sources,
        )
    return sources


def _extract_structured_sources(
    config: AutomationConfig,
    writer: GeminiClient,
    *,
    raw_sources: list[dict],
    queries: list[str],
    priority_sources: list[str],
) -> list[dict]:
    if not raw_sources:
        return []
    raw_sources_json = json.dumps(raw_sources, ensure_ascii=True)
    prompt = _build_web_research_prompt(
        queries=queries,
        priority_sources=priority_sources,
        raw_sources_json=raw_sources_json,
    )
    try:
        response = writer.generate(
            prompt,
            temperature=config.gemini_temperature,
            max_tokens=config.gemini_max_tokens,
        )
        data = _extract_json_block(response)
    except Exception as exc:
        logging.warning("Web researcher failed: %s", exc)
        data = None
    sources = []
    if isinstance(data, dict) and isinstance(data.get("sources"), list):
        for item in data.get("sources"):
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not _is_valid_url(url):
                continue
            sources.append(
                {
                    "title": str(item.get("title") or "").strip() or "Untitled",
                    "url": url,
                    "publisher": str(item.get("publisher") or "").strip() or urlparse(url).netloc,
                    "published_at": str(item.get("published_at") or "unknown").strip(),
                    "key_facts": _ensure_list_of_strings(item.get("key_facts")),
                    "direct_quotes": _ensure_list_of_strings(item.get("direct_quotes")),
                }
            )
    if sources:
        return sources
    fallback_sources = []
    for raw in raw_sources:
        url = str(raw.get("url") or "").strip()
        if not _is_valid_url(url):
            continue
        first_fact = _first_sentence(str(raw.get("text") or ""))
        fallback_sources.append(
            {
                "title": str(raw.get("title") or "").strip() or "Untitled",
                "url": url,
                "publisher": str(raw.get("publisher") or "").strip() or urlparse(url).netloc,
                "published_at": str(raw.get("published_at") or "unknown").strip(),
                "key_facts": [first_fact] if first_fact else [],
                "direct_quotes": [],
            }
        )
    return fallback_sources


def _build_evidence_from_sources(
    config: AutomationConfig,
    writer: GeminiClient,
    sources: list[dict],
) -> dict:
    sources_json = json.dumps({"sources": sources}, ensure_ascii=True)
    prompt = _build_evidence_builder_prompt(sources_json=sources_json)
    try:
        response = writer.generate(
            prompt,
            temperature=config.gemini_temperature,
            max_tokens=config.gemini_max_tokens,
        )
        data = _extract_json_block(response)
    except Exception as exc:
        logging.warning("Evidence builder failed: %s", exc)
        data = None
    if isinstance(data, dict):
        return data
    return {"timeline": [], "claims": [], "open_questions": [], "conflicts": []}


def _build_outline(
    config: AutomationConfig,
    writer: GeminiClient,
    *,
    keyword: str,
    angle: str,
    evidence_summary: str,
) -> dict:
    prompt = _build_outline_prompt(
        keyword=keyword,
        angle=angle,
        evidence_summary=evidence_summary,
    )
    try:
        response = writer.generate(
            prompt,
            temperature=config.gemini_temperature,
            max_tokens=config.gemini_max_tokens,
        )
        data = _extract_json_block(response)
    except Exception as exc:
        logging.warning("Outline architect failed: %s", exc)
        data = None
    if isinstance(data, dict) and isinstance(data.get("sections"), list):
        return data
    fallback_sections = [
        {
            "heading": f"Why {keyword} is rising now",
            "goal": "Explain the recent trigger and why the topic matters now.",
            "evidence_refs": [],
        },
        {
            "heading": f"Key facts shaping {keyword}",
            "goal": "Summarize verified facts and data points.",
            "evidence_refs": [],
        },
        {
            "heading": f"What {keyword} means for readers",
            "goal": "Translate the evidence into reader impact and implications.",
            "evidence_refs": [],
        },
        {
            "heading": f"What to watch next for {keyword}",
            "goal": "Highlight open questions and forward-looking signals.",
            "evidence_refs": [],
        },
    ]
    return {"title_direction": "", "sections": fallback_sections, "faq": []}


def _allocate_resources(
    config: AutomationConfig,
    writer: GeminiClient,
    *,
    outline: dict,
    sources: list[dict],
) -> dict:
    outline_json = json.dumps(outline, ensure_ascii=True)
    sources_json = json.dumps({"sources": sources}, ensure_ascii=True)
    prompt = _build_resource_allocation_prompt(
        outline_json=outline_json,
        sources_json=sources_json,
    )
    try:
        response = writer.generate(
            prompt,
            temperature=config.gemini_temperature,
            max_tokens=config.gemini_max_tokens,
        )
        data = _extract_json_block(response)
    except Exception as exc:
        logging.warning("Resource allocation failed: %s", exc)
        data = None
    return data if isinstance(data, dict) else {}


def _collect_youtube_videos(
    config: AutomationConfig,
    *,
    topic: dict,
    resources: dict,
) -> list[dict]:
    if not config.youtube_search_enabled or not isinstance(resources, dict):
        return []
    queries = _ensure_list_of_strings(resources.get("youtube_queries"))
    if not queries:
        return []
    results: list[dict] = []
    seen: set[str] = set()
    added = 0
    for query in queries:
        videos = _search_youtube(
            query,
            region=str(topic.get("region") or ""),
            language=config.content_language,
            max_results=config.youtube_max_per_query,
            config=config,
        )
        for video in videos:
            url = video.get("url")
            if not _is_valid_url(url):
                continue
            normalized = _normalize_url_for_dedupe(url)
            if normalized in seen:
                continue
            seen.add(normalized)
            results.append(video)
            added += 1
            if added >= config.youtube_max_results:
                break
        if added >= config.youtube_max_results:
            break
    return results


def _filter_sources_for_section(sources: list[dict], refs: list[str]) -> list[dict]:
    if not refs:
        return sources
    selected: list[dict] = []
    for source in sources:
        url = str(source.get("url") or "")
        publisher = str(source.get("publisher") or "")
        title = str(source.get("title") or "")
        for ref in refs:
            if not ref:
                continue
            ref_text = str(ref)
            if ref_text.startswith("http") and ref_text in url:
                selected.append(source)
                break
            if ref_text.lower() in publisher.lower() or ref_text.lower() in title.lower():
                selected.append(source)
                break
    return selected or sources


def _filter_evidence_for_sources(evidence: dict, sources: list[dict]) -> dict:
    if not isinstance(evidence, dict):
        return {"timeline": [], "claims": [], "open_questions": [], "conflicts": []}
    source_keys = {str(s.get("url") or "") for s in sources}
    source_keys.update(str(s.get("publisher") or "") for s in sources)
    filtered_claims = []
    for item in evidence.get("claims") or []:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or "")
        if source and source not in source_keys:
            continue
        filtered_claims.append(item)
    filtered_timeline = []
    for item in evidence.get("timeline") or []:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or "")
        if source and source not in source_keys:
            continue
        filtered_timeline.append(item)
    return {
        "timeline": filtered_timeline,
        "claims": filtered_claims,
        "open_questions": evidence.get("open_questions") or [],
        "conflicts": evidence.get("conflicts") or [],
    }


def _write_sections(
    config: AutomationConfig,
    writer: GeminiClient,
    *,
    outline: dict,
    evidence: dict,
    sources: list[dict],
) -> list[str] | None:
    sections_data = outline.get("sections") if isinstance(outline, dict) else None
    if not isinstance(sections_data, list) or not sections_data:
        return None
    section_mdx_list: list[str] = []
    for section in sections_data:
        if not isinstance(section, dict):
            continue
        heading = str(section.get("heading") or "").strip()
        goal = str(section.get("goal") or "").strip()
        refs = _ensure_list_of_strings(section.get("evidence_refs"))
        sources_subset = _filter_sources_for_section(sources, refs)
        evidence_subset = _filter_evidence_for_sources(evidence, sources_subset)
        prompt = _build_section_writer_prompt(
            section_heading=heading,
            section_goal=goal,
            evidence_subset=json.dumps(evidence_subset, ensure_ascii=True),
            sources_subset=json.dumps(sources_subset, ensure_ascii=True),
            language=config.content_language,
        )
        try:
            response = writer.generate(
                prompt,
                temperature=config.gemini_temperature,
                max_tokens=config.gemini_max_tokens,
            )
            section_body = response.strip()
        except Exception as exc:
            logging.warning("Section writer failed (%s): %s", heading, exc)
            return None
        if not section_body:
            return None
        section_mdx_list.append(section_body)
    return section_mdx_list


def _assemble_article(
    config: AutomationConfig,
    writer: GeminiClient,
    *,
    section_mdx_list: list[str],
    faq_list: list[str],
    keyword: str,
) -> str | None:
    prompt = _build_assembler_prompt(
        section_mdx_list=section_mdx_list,
        faq_list=faq_list,
        tone=config.content_tone,
        keyword=keyword,
    )
    try:
        response = writer.generate(
            prompt,
            temperature=config.gemini_temperature,
            max_tokens=config.gemini_max_tokens,
        )
        return response.strip()
    except Exception as exc:
        logging.warning("Assembler failed: %s", exc)
        return None


def _apply_quality_gate(
    config: AutomationConfig,
    writer: GeminiClient,
    *,
    full_mdx: str,
    keyword: str,
) -> str:
    if not full_mdx:
        return full_mdx
    content = full_mdx
    for _ in range(max(config.quality_gate_revisions, 0) + 1):
        prompt = _build_quality_gate_prompt(full_mdx=content)
        try:
            response = writer.generate(
                prompt,
                temperature=config.gemini_temperature,
                max_tokens=config.gemini_max_tokens,
            )
            data = _extract_json_block(response)
        except Exception as exc:
            logging.warning("Quality gate failed: %s", exc)
            return content
        if not isinstance(data, dict):
            return content
        if data.get("status") == "pass":
            return content
        if config.quality_gate_revisions <= 0:
            return content
        issues_json = json.dumps(data, ensure_ascii=True)
        revise_prompt = _build_revision_prompt(
            full_mdx=content,
            issues_json=issues_json,
            keyword=keyword,
        )
        try:
            revised = writer.generate(
                revise_prompt,
                temperature=config.gemini_temperature,
                max_tokens=config.gemini_max_tokens,
            )
            content = revised.strip() or content
        except Exception as exc:
            logging.warning("Revision failed: %s", exc)
            return content
    return content


def _key_points_from_evidence(evidence: dict) -> list[str]:
    points: list[str] = []
    claims = evidence.get("claims") if isinstance(evidence, dict) else None
    if isinstance(claims, list):
        for item in claims:
            if not isinstance(item, dict):
                continue
            claim = str(item.get("claim") or "").strip()
            if claim:
                points.append(claim)
    return points[:5]


def _generate_article_multi_agent(
    config: AutomationConfig,
    writer: GeminiClient,
    *,
    topic: dict,
) -> dict | None:
    keyword = str(topic.get("keyword") or "").strip()
    if not keyword:
        return None
    angle = str(topic.get("angle") or "").strip() or f"latest developments and impact for {keyword}"
    research_plan = _plan_research(
        config,
        writer,
        keyword=keyword,
        angle=angle,
        region=str(topic.get("region") or ""),
    )
    raw_sources = _gather_sources_for_topic(config, writer, topic, research_plan)
    if not raw_sources:
        return None
    structured_sources = _extract_structured_sources(
        config,
        writer,
        raw_sources=raw_sources,
        queries=_ensure_list_of_strings(research_plan.get("queries")),
        priority_sources=_ensure_list_of_strings(research_plan.get("priority_sources")),
    )
    if not structured_sources:
        return None
    evidence = _build_evidence_from_sources(config, writer, structured_sources)
    evidence_summary = _build_evidence_summary(evidence, keyword)
    outline = _build_outline(
        config,
        writer,
        keyword=keyword,
        angle=angle,
        evidence_summary=evidence_summary,
    )
    resources = _allocate_resources(config, writer, outline=outline, sources=structured_sources)
    youtube_videos = _collect_youtube_videos(config, topic=topic, resources=resources)
    section_mdx_list = _write_sections(
        config,
        writer,
        outline=outline,
        evidence=evidence,
        sources=structured_sources,
    )
    if not section_mdx_list:
        return None
    faq_list = _ensure_list_of_strings(outline.get("faq") if isinstance(outline, dict) else [])
    full_body = _assemble_article(
        config,
        writer,
        section_mdx_list=section_mdx_list,
        faq_list=faq_list,
        keyword=keyword,
    )
    if not full_body:
        return None
    full_body = _apply_quality_gate(
        config,
        writer,
        full_mdx=full_body,
        keyword=keyword,
    )
    summary = _first_sentences(_strip_markdown(full_body), count=2, max_len=320)
    key_points = _key_points_from_evidence(evidence)
    hero_hint = ""
    hero_alt = ""
    if isinstance(resources, dict):
        hero = resources.get("hero_image")
        if isinstance(hero, dict):
            hero_hint = str(hero.get("style_prompt") or "").strip()
            hero_alt = str(hero.get("alt_text") or "").strip()
    image_prompt_hint = hero_hint or keyword
    reference_urls = [s.get("url") for s in structured_sources if _is_valid_url(s.get("url"))]
    for video in youtube_videos:
        url = video.get("url")
        if _is_valid_url(url):
            reference_urls.append(url)
    return {
        "body": full_body,
        "summary": summary,
        "key_points": key_points,
        "image_prompt_hint": image_prompt_hint,
        "hero_alt_hint": hero_alt,
        "reference_urls": reference_urls,
    }


def _generate_post_for_topic(
    config: AutomationConfig,
    writer: GeminiClient,
    meta_writer: GeminiClient,
    topic: dict,
) -> Path | None:
    keyword = topic.get("keyword")
    if not keyword:
        return None

    image_urls = _extract_image_urls(topic)
    urls = _extract_urls(topic)
    alt_text = _ensure_ascii_text(f"{keyword} related image", "Related image")
    fallback_body = ""
    summary = ""
    key_points: list[str] = []
    image_prompt_hint = keyword
    body = ""
    hero_alt_hint = ""
    reference_urls = list(urls)

    if config.use_multi_agent:
        article = _generate_article_multi_agent(config, writer, topic=topic)
        if article:
            body = str(article.get("body") or "").strip()
            summary = _ensure_ascii_text(str(article.get("summary") or "").strip(), "")
            key_points = _normalize_key_points(article.get("key_points"))
            image_prompt_hint = _ensure_ascii_text(
                str(article.get("image_prompt_hint") or keyword).strip(),
                keyword,
            )
            hero_alt_hint = _ensure_ascii_text(
                str(article.get("hero_alt_hint") or "").strip(),
                "",
            )
            reference_urls = list(article.get("reference_urls") or reference_urls)

    if not body:
        candidates = [
            {
                "url": url,
                "title": "",
                "publisher": urlparse(url).netloc,
                "published_at": None,
                "origin": "topic_url",
            }
            for url in urls
            if _is_valid_url(url)
        ]
        sources = _fetch_sources_from_candidates(candidates, config, max_sources=None)

        content_prompt = _build_content_prompt(
            keyword=keyword,
            region=topic.get("region", ""),
            traffic=topic.get("traffic"),
            sources=sources,
            image_urls=image_urls,
            references=urls,
            language=config.content_language,
        )

        try:
            response = writer.generate(
                content_prompt,
                temperature=config.gemini_temperature,
                max_tokens=config.gemini_max_tokens,
            )
            content_data = _extract_json_block(response)
        except Exception as exc:
            logging.warning("Content LLM failed for %s: %s", keyword, exc)
            content_data = None

        fallback_body = _build_fallback_body(keyword, sources, image_urls, alt_text)

        if content_data:
            summary = _ensure_ascii_text(
                str(content_data.get("summary") or "").strip(),
                "",
            )
            key_points = _normalize_key_points(content_data.get("key_points"))
            body = str(content_data.get("body_markdown") or "").strip()
            image_prompt_hint = _ensure_ascii_text(
                str(content_data.get("image_prompt_hint") or keyword).strip(),
                keyword,
            )

    if not body:
        body = fallback_body

    body = _ensure_images_in_body(body, image_urls, alt_text)
    body = _linkify_urls(body)
    body = _clean_body_text(body)
    body = _ensure_ascii_body(body, fallback_body or body)

    if not summary:
        summary = _ensure_ascii_text(
            _first_sentences(_strip_markdown(body), count=2, max_len=200),
            "Trend summary of the topic.",
        )

    meta_prompt = _build_meta_prompt(
        keyword=keyword,
        summary=summary,
        key_points=key_points,
        body_excerpt=_truncate(body, 1400),
        image_prompt_hint=image_prompt_hint,
        language=config.content_language,
    )

    try:
        meta_response = meta_writer.generate(
            meta_prompt,
            temperature=config.gemini_temperature,
            max_tokens=config.gemini_max_tokens,
        )
        meta_data = _extract_json_block(meta_response)
    except Exception as exc:
        logging.warning("Frontmatter LLM failed for %s: %s", keyword, exc)
        meta_data = None

    fallback_category = _ensure_ascii_text(config.fallback_category, "trend")
    fallback_tags = [
        _ensure_ascii_text(tag, "topic") for tag in config.fallback_tags if tag
    ] or ["topic"]
    fallback_tags = fallback_tags[:3]

    if meta_data:
        title = _ensure_ascii_text(
            str(meta_data.get("title") or keyword).strip(),
            "Trend summary",
        )
        description = _ensure_ascii_text(
            str(meta_data.get("description") or summary).strip(),
            "Key updates and context around the topic.",
        )
        category_list = _normalize_category_list(
            meta_data.get("category"),
            [fallback_category],
        )
        tags_list = _normalize_tag_list(meta_data.get("tags"), fallback_tags)
        hero_alt = _ensure_ascii_text(
            str(meta_data.get("hero_alt") or hero_alt_hint or title).strip(),
            hero_alt_hint or title,
        )
        image_prompt = _ensure_ascii_text(
            str(meta_data.get("image_prompt") or image_prompt_hint).strip(),
            image_prompt_hint,
        )
    else:
        title = _ensure_ascii_text(f"{keyword} trend summary", "Trend summary")
        description = _ensure_ascii_text(
            summary or f"Key updates and context around {keyword}.",
            "Key updates and context around the topic.",
        )
        category_list = [fallback_category]
        tags_list = fallback_tags
        hero_alt = _ensure_ascii_text(f"{keyword} hero image", "Hero image")
        image_prompt = _ensure_ascii_text(
            image_prompt_hint or f"{keyword} concept illustration",
            "Concept illustration",
        )

    return _write_post(
        config,
        title=title,
        description=description,
        category=category_list,
        tags=tags_list,
        body=body,
        hero_alt=hero_alt,
        image_prompt=image_prompt,
        reference_urls=reference_urls,
        slug_hint=title,
    )


def _save_trends_snapshot(payload: dict) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")
    path = ROOT_DIR / "data" / "trends" / f"{timestamp}-trends.json"
    write_json(path, payload)


def run_once(config: AutomationConfig) -> None:
    try:
        payload = collect_trending_searches(
            config.regions,
            limit=config.trend_limit,
            sleep_sec=config.trend_sleep_sec,
            method=config.trend_method,
            source=config.trend_source,
            window_hours=config.trend_window_hours,
            csv_sort_by=config.csv_sort_by,
            include_images=config.rss_include_images,
            include_articles=config.rss_include_articles,
            max_articles_per_trend=config.rss_max_articles_per_trend,
            cache=config.rss_cache,
        )
    except RuntimeError as exc:
        if config.trend_source == "csv":
            logging.warning("CSV source failed; falling back to RSS. Error: %s", exc)
            payload = collect_trending_searches(
                config.regions,
                limit=config.trend_limit,
                sleep_sec=config.trend_sleep_sec,
                method=config.trend_method,
                source="rss",
                window_hours=config.trend_window_hours,
                csv_sort_by=config.csv_sort_by,
                include_images=config.rss_include_images,
                include_articles=config.rss_include_articles,
                max_articles_per_trend=config.rss_max_articles_per_trend,
                cache=config.rss_cache,
            )
        else:
            raise
    _save_trends_snapshot(payload)

    topics = _select_topics_by_region(payload, config.regions, config.max_topic_rank)
    if not topics:
        logging.info("No topics found.")
        return

    state = _load_state()
    used = set(state.get("topics") or state.get("keywords") or [])
    slugs = set(state.get("slugs", []))

    writer = GeminiClient(config.gemini_api_key, config.gemini_model_content)
    meta_writer = GeminiClient(config.gemini_api_key, config.gemini_model_meta)
    if config.use_multi_agent:
        topics = _rank_topics_with_llm(config, writer, topics)
    for topic in topics:
        keyword = topic.get("keyword")
        if not keyword:
            continue
        normalized = _normalize_keyword(str(keyword))
        topic_key = f"{topic.get('region', '')}:{normalized}"
        if topic_key in used:
            logging.info("Skip already processed topic: %s", topic_key)
            continue
        post_path = _generate_post_for_topic(config, writer, meta_writer, topic)
        if post_path:
            used.add(topic_key)
            slugs.add(post_path.stem)
            logging.info("Post created: %s", post_path)

    state["topics"] = sorted(used)
    state["slugs"] = sorted(slugs)
    _save_state(state)


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Automate trend-based blog posts.")
    parser.add_argument("--once", action="store_true", help="Run only once")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    _configure_logging(args.log_level)
    config = _build_config()
    logging.info("Automation start (interval=%s hours)", config.interval_hours)

    if args.once:
        run_once(config)
        return 0

    while True:
        run_once(config)
        sleep_seconds = max(config.interval_hours, 0.1) * 3600
        logging.info("Sleeping for %s seconds", sleep_seconds)
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
