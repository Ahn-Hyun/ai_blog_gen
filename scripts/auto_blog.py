from __future__ import annotations

import argparse
import base64
import binascii
import colorsys
import http.client
import ipaddress
import json
import logging
import math
import mimetypes
import os
import random
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover - dependency may be absent in local dev until installed
    OpenAI = None

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
sys.path.append(str(SRC_DIR))

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
DEFAULT_MAX_SOURCE_CHARS = 10000
DEFAULT_MAX_TOTAL_SOURCE_CHARS = 40000
DEFAULT_SCRAPE_TIMEOUT = 12
DEFAULT_SCRAPE_DELAY_SEC = 1.0
DEFAULT_SCRAPE_MAX_RETRIES = 2
DEFAULT_SCRAPE_BACKOFF_SEC = 5.0
DEFAULT_ANTHROPIC_MODEL = "gemini-3.1-pro"
DEFAULT_ANTHROPIC_MODEL_CONTENT = DEFAULT_ANTHROPIC_MODEL
DEFAULT_ANTHROPIC_MODEL_META = DEFAULT_ANTHROPIC_MODEL
DEFAULT_ANTHROPIC_TEMPERATURE = 0.6
DEFAULT_ANTHROPIC_MAX_TOKENS = 60000
DEFAULT_ANTHROPIC_TIMEOUT_SEC = 900
DEFAULT_BLOG_DOMAIN = "blog.ship-write.com"
DEFAULT_CONTENT_LANGUAGE = "English"
DEFAULT_CONTENT_TONE = "neutral, informative, US-market-focused"
DEFAULT_CONTENT_TIMEZONE = "America/New_York"
DEFAULT_USE_MULTI_AGENT = True
DEFAULT_SEARCH_RSS_ENABLED = True
DEFAULT_SEARCH_RSS_MAX_RESULTS = 4
DEFAULT_SEARCH_RSS_MAX_PER_QUERY = 3
DEFAULT_MAX_EVIDENCE_SOURCES = 4
DEFAULT_QUALITY_GATE_REVISIONS = 0
DEFAULT_FINAL_REVIEW_ENABLED = True
DEFAULT_FINAL_REVIEW_REVISIONS = 2
DEFAULT_MDX_RENDER_GUARD_ENABLED = True
DEFAULT_MDX_RENDER_GUARD_REVISIONS = 1
DEFAULT_MDX_RENDER_AUTO_FIX = True
DEFAULT_SEARCH_WEB_ENABLED = True
DEFAULT_GEMINI_GROUNDED_DAILY_DISCOVERY = True
DEFAULT_SEARCH_WEB_MAX_RESULTS = 5
DEFAULT_SEARCH_WEB_MAX_PER_QUERY = 3
DEFAULT_SEARCH_WEB_DEPTH = "basic"
DEFAULT_SEARCH_WEB_INCLUDE_ANSWER = True
DEFAULT_SEARCH_WEB_INCLUDE_DOMAINS: tuple[str, ...] = ()
DEFAULT_SEARCH_WEB_EXCLUDE_DOMAINS: tuple[str, ...] = ()
DEFAULT_YOUTUBE_SEARCH_ENABLED = True
DEFAULT_YOUTUBE_MAX_RESULTS = 4
DEFAULT_YOUTUBE_MAX_PER_QUERY = 2
DEFAULT_GOOGLE_IMAGE_ENABLED = True
DEFAULT_GOOGLE_IMAGE_MODEL = "gemini-3-pro-image-preview"
DEFAULT_GOOGLE_IMAGE_ASPECT_RATIO = "16:9"
DEFAULT_OPENAI_WEEKLY_MODEL = "gpt-5.4-pro"
DEFAULT_WEEKLY_MAJOR_EVENTS_RUN_WEEKDAY = 0
DEFAULT_WEEKLY_MAJOR_EVENTS_RUN_HOUR = 9
DEFAULT_WEEKLY_MAJOR_EVENTS_PER_LANE = 1
DEFAULT_GRADIENT_WIDTH = 1600
DEFAULT_GRADIENT_HEIGHT = 900
DEFAULT_GRADIENT_JPEG_QUALITY = 90
MAX_INLINE_VISUALS = 4
MAX_INLINE_CHARTS = 2
MAX_GENERATED_INLINE_IMAGES = 2
FINAL_REVIEW_MAX_HINTS = 16
FINAL_REVIEW_SUSPICIOUS_PATTERNS = (
    r"\bhtt\b",
    r"\(htt(?!p)",
    r"!\[[^\]]*\]\(\s*\)",
    r"\[[^\]]*\]\(\s*\)",
    r"\bAdvertisement\b",
    r"\bManage your account\b",
    r"\bFor premium support\b",
    r"\bSubscribe\b",
    r"\bSign in\b",
    r"\bSign up\b",
    r"\bContinue reading\b",
    r"\bRead more\b",
)
MDX_RENDER_MAX_HINTS = 16
MDX_VOID_ELEMENTS = (
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
)
MDX_VOID_TAGS_PATTERN = "|".join(MDX_VOID_ELEMENTS)
MDX_VOID_TAG_PATTERN = re.compile(
    rf"<(?P<tag>{MDX_VOID_TAGS_PATTERN})\b(?P<attrs>[^>]*)>",
    re.IGNORECASE,
)
MDX_UNCLOSED_VOID_TAG_PATTERN = re.compile(
    rf"<(?P<tag>{MDX_VOID_TAGS_PATTERN})\b(?![^>]*\/\s*>)[^>]*>",
    re.IGNORECASE,
)
MDX_STRAY_ANGLE_PATTERN = re.compile(r"<(?=\s|=|\d|-)")
CONTENT_JSON_SCHEMA = (
    "Required JSON keys: summary (2-3 sentences), key_points (array of 3-5 strings), "
    "body_markdown (string, MDX-friendly, ~1500-2200 words), "
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
MAX_IMAGE_ANALYSIS = 3
MAX_IMAGE_BYTES = 2_000_000
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
PIPELINE_DAILY_IMPACT = "daily-impact"
PIPELINE_WEEKLY_MAJOR_EVENTS = "weekly-major-events"
PIPELINE_CHOICES = (PIPELINE_DAILY_IMPACT, PIPELINE_WEEKLY_MAJOR_EVENTS)
DAILY_IMPACT_DISCOVERY_TOPICS = (
    "federal reserve policy inflation labor market unemployment treasury yields",
    "housing market mortgage rates home sales homebuilder inventory affordability",
    "trade tariffs sanctions foreign policy diplomacy supply chains shipping energy",
    "congress white house fiscal policy tax budget regulation antitrust",
    "banking credit spreads commercial real estate refinancing defaults lending standards",
    "oil natural gas electricity utilities commodity prices weather disaster infrastructure",
    "consumer confidence retail spending wages layoffs immigration population migration",
    "earnings guidance layoffs bankruptcies mergers regulation court ruling",
)
DAILY_IMPACT_CATEGORY_LABELS = {
    "stocks": "stocks",
    "real_estate": "real-estate",
}
DAILY_IMPACT_TAG_HINTS = {
    "stocks": ["market-impact", "stocks", "macro"],
    "real_estate": ["market-impact", "real-estate", "housing"],
}
MARKET_ANALYSIS_LANES = ("stocks", "real_estate")
DAILY_IMPACT_TEMPLATE_REQUIREMENTS = """
Template requirements (fixed order):
1) Open with a concise thesis that explains why the prior day's event matters for the target market.
2) Include one section that maps the transmission chain from event -> mechanism -> market effect.
3) Include one section focused on the highest-signal evidence or data points from the prior day.
4) Include one scenario section covering base case, upside, and downside with clear uncertainty.
5) End with a "What to watch next" section listing concrete indicators or triggers.

Rules:
- Use visual variety at section breaks: combine concise Markdown tables with chart-friendly numeric context and image-friendly explanatory moments. Markdown tables should be used selectively because the pipeline may inject charts/images.
- Do not rewrite the news chronologically.
- Distinguish verified facts from inference or uncertainty.
- Emphasize second-order effects, timing, and who is affected.
- Keep the analysis grounded in the prior day's evidence and explicitly note when evidence is thin.
""".strip()
WEEKLY_MAJOR_EVENTS_TEMPLATE_REQUIREMENTS = """
Template requirements (fixed order):
1) Open with a concise thesis that explains why the recent week's event matters for the target market.
2) Include one section that maps the transmission chain from event -> mechanism -> market effect.
3) Include one section focused on the highest-signal evidence or data points from the recent week.
4) Include one scenario section covering base case, upside, and downside with clear uncertainty.
5) End with a "What to watch next" section listing concrete indicators or triggers.

Rules:
- Use visual variety at section breaks: combine concise Markdown tables with chart-friendly numeric context and image-friendly explanatory moments. Markdown tables should be used selectively because the pipeline may inject charts/images.
- Do not rewrite the news chronologically.
- Distinguish verified facts from inference or uncertainty.
- Emphasize second-order effects, timing, and who is affected.
- Keep the analysis grounded in the recent week's evidence and explicitly note when evidence is thin.
""".strip()


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
    openai_api_key: str
    openai_weekly_model: str
    anthropic_api_key: str
    anthropic_model: str
    anthropic_model_content: str
    anthropic_model_meta: str
    anthropic_temperature: float
    anthropic_max_tokens: int
    anthropic_timeout_sec: int
    blog_domain: str
    content_language: str
    content_tone: str
    content_timezone: ZoneInfo
    use_multi_agent: bool
    google_api_key: str
    youtube_api_key: str
    tavily_api_key: str
    gemini_grounded_daily_discovery: bool
    fallback_category: str
    fallback_tags: list[str]
    post_draft: bool
    astro_root: Path
    content_dir: Path
    hero_base_dir: Path
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
    final_review_enabled: bool
    final_review_revisions: int
    mdx_render_guard_enabled: bool
    mdx_render_guard_revisions: int
    mdx_render_auto_fix: bool
    google_image_enabled: bool
    google_image_model: str
    google_image_aspect_ratio: str
    weekly_major_events_run_weekday: int
    weekly_major_events_run_hour: int
    weekly_major_events_per_lane: int


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


def _resolve_timezone(env: dict[str, str]) -> ZoneInfo:
    tz_name = str(env.get("CONTENT_TIMEZONE") or DEFAULT_CONTENT_TIMEZONE).strip()
    if not tz_name:
        tz_name = DEFAULT_CONTENT_TIMEZONE
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        logging.warning("Invalid CONTENT_TIMEZONE %r; falling back to UTC.", tz_name)
        return ZoneInfo("UTC")


def _resolve_state_path() -> Path:
    env = _resolve_env()
    raw = str(env.get("STATE_PATH") or "").strip()
    if not raw:
        return STATE_PATH
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = (ROOT_DIR / candidate).resolve()
    return candidate


def _build_config() -> AutomationConfig:
    env = _resolve_env()
    regions = _parse_list(env.get("TREND_REGIONS"), ["US"])
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
    openai_api_key = env.get("OPENAI_API_KEY", "").strip()
    if not gemini_api_key and google_api_key:
        gemini_api_key = google_api_key
    if not google_api_key and gemini_api_key:
        google_api_key = gemini_api_key
    anthropic_api_key = gemini_api_key
    anthropic_model = env.get("GEMINI_MODEL", DEFAULT_ANTHROPIC_MODEL)
    anthropic_model_content = env.get("GEMINI_MODEL_CONTENT", anthropic_model)
    anthropic_model_meta = env.get("GEMINI_MODEL_META", anthropic_model)
    anthropic_temperature = _parse_float(
        env.get("GEMINI_TEMPERATURE"),
        DEFAULT_ANTHROPIC_TEMPERATURE,
    )
    anthropic_max_tokens = _parse_int(
        env.get("GEMINI_MAX_TOKENS"),
        DEFAULT_ANTHROPIC_MAX_TOKENS,
    )
    anthropic_timeout_sec = _parse_int(
        env.get("GEMINI_TIMEOUT_SEC"),
        DEFAULT_ANTHROPIC_TIMEOUT_SEC,
    )
    if anthropic_timeout_sec <= 0:
        anthropic_timeout_sec = DEFAULT_ANTHROPIC_TIMEOUT_SEC
    if anthropic_timeout_sec > DEFAULT_ANTHROPIC_TIMEOUT_SEC:
        anthropic_timeout_sec = DEFAULT_ANTHROPIC_TIMEOUT_SEC

    blog_domain = env.get("BLOG_DOMAIN", DEFAULT_BLOG_DOMAIN)
    if not blog_domain.startswith("http"):
        blog_domain = f"https://{blog_domain}"
    content_language = env.get("CONTENT_LANGUAGE", DEFAULT_CONTENT_LANGUAGE).strip() or DEFAULT_CONTENT_LANGUAGE
    content_tone = env.get("CONTENT_TONE", DEFAULT_CONTENT_TONE).strip() or DEFAULT_CONTENT_TONE
    content_timezone = _resolve_timezone(env)
    use_multi_agent = _parse_bool(env.get("USE_MULTI_AGENT"), DEFAULT_USE_MULTI_AGENT)
    youtube_api_key = env.get("YOUTUBE_API_KEY", "").strip() or google_api_key
    tavily_api_key = env.get("TAVILY_API_KEY", "").strip()
    gemini_grounded_daily_discovery = _parse_bool(
        env.get("GEMINI_GROUNDED_DAILY_DISCOVERY"),
        DEFAULT_GEMINI_GROUNDED_DAILY_DISCOVERY,
    )
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
    final_review_enabled = _parse_bool(
        env.get("FINAL_REVIEW_ENABLED"),
        DEFAULT_FINAL_REVIEW_ENABLED,
    )
    final_review_revisions = _parse_int(
        env.get("FINAL_REVIEW_REVISIONS"),
        DEFAULT_FINAL_REVIEW_REVISIONS,
    )
    mdx_render_guard_enabled = _parse_bool(
        env.get("MDX_RENDER_GUARD_ENABLED"),
        DEFAULT_MDX_RENDER_GUARD_ENABLED,
    )
    mdx_render_guard_revisions = _parse_int(
        env.get("MDX_RENDER_GUARD_REVISIONS"),
        DEFAULT_MDX_RENDER_GUARD_REVISIONS,
    )
    mdx_render_auto_fix = _parse_bool(
        env.get("MDX_RENDER_AUTO_FIX"),
        DEFAULT_MDX_RENDER_AUTO_FIX,
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
    openai_weekly_model = env.get("OPENAI_WEEKLY_MODEL", DEFAULT_OPENAI_WEEKLY_MODEL).strip()
    weekly_major_events_run_weekday = _parse_int(
        env.get("WEEKLY_MAJOR_EVENTS_RUN_WEEKDAY"),
        DEFAULT_WEEKLY_MAJOR_EVENTS_RUN_WEEKDAY,
    )
    weekly_major_events_run_hour = _parse_int(
        env.get("WEEKLY_MAJOR_EVENTS_RUN_HOUR"),
        DEFAULT_WEEKLY_MAJOR_EVENTS_RUN_HOUR,
    )
    weekly_major_events_per_lane = max(
        1,
        _parse_int(
            env.get("WEEKLY_MAJOR_EVENTS_PER_LANE"),
            DEFAULT_WEEKLY_MAJOR_EVENTS_PER_LANE,
        ),
    )
    fallback_category = env.get("FALLBACK_CATEGORY", DEFAULT_FALLBACK_CATEGORY)
    fallback_tags = _parse_list(env.get("FALLBACK_TAGS"), DEFAULT_FALLBACK_TAGS)
    if not fallback_tags:
        fallback_tags = ["topic"]
    fallback_tags = fallback_tags[:3]
    post_draft = _parse_bool(env.get("POST_DRAFT"), False)

    astro_root = Path(env.get("ASTRO_ROOT", "../ai_blog_v1_astro")).resolve()
    content_dir = astro_root / "src" / "content" / "blog"
    hero_base_dir = astro_root / "public" / "images" / "posts"

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
        openai_api_key=openai_api_key,
        openai_weekly_model=openai_weekly_model or DEFAULT_OPENAI_WEEKLY_MODEL,
        anthropic_api_key=anthropic_api_key,
        anthropic_model=anthropic_model,
        anthropic_model_content=anthropic_model_content,
        anthropic_model_meta=anthropic_model_meta,
        anthropic_temperature=anthropic_temperature,
        anthropic_max_tokens=anthropic_max_tokens,
        anthropic_timeout_sec=anthropic_timeout_sec,
        blog_domain=blog_domain.rstrip("/"),
        content_language=content_language,
        content_tone=content_tone,
        content_timezone=content_timezone,
        use_multi_agent=use_multi_agent,
        google_api_key=google_api_key,
        youtube_api_key=youtube_api_key,
        tavily_api_key=tavily_api_key,
        gemini_grounded_daily_discovery=gemini_grounded_daily_discovery,
        fallback_category=fallback_category,
        fallback_tags=fallback_tags,
        post_draft=post_draft,
        astro_root=astro_root,
        content_dir=content_dir,
        hero_base_dir=hero_base_dir,
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
        final_review_enabled=final_review_enabled,
        final_review_revisions=final_review_revisions,
        mdx_render_guard_enabled=mdx_render_guard_enabled,
        mdx_render_guard_revisions=mdx_render_guard_revisions,
        mdx_render_auto_fix=mdx_render_auto_fix,
        google_image_enabled=google_image_enabled,
        google_image_model=google_image_model or DEFAULT_GOOGLE_IMAGE_MODEL,
        google_image_aspect_ratio=google_image_aspect_ratio or DEFAULT_GOOGLE_IMAGE_ASPECT_RATIO,
        weekly_major_events_run_weekday=weekly_major_events_run_weekday,
        weekly_major_events_run_hour=weekly_major_events_run_hour,
        weekly_major_events_per_lane=weekly_major_events_per_lane,
    )


def _keyword_matches(text: str, keyword: str) -> bool:
    key = keyword.strip().lower()
    if not key:
        return False
    if len(key) <= 3 and key.isascii() and key.isalnum():
        return re.search(rf"\b{re.escape(key)}\b", text) is not None
    return key in text


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


def _normalize_affected_lanes(value) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    raw_items = _ensure_list_of_strings(value)
    for item in raw_items:
        key = str(item).strip().lower().replace("-", "_").replace(" ", "_")
        if key in {"both", "all", "stocks_and_real_estate", "stock_and_real_estate"}:
            for lane in MARKET_ANALYSIS_LANES:
                if lane not in seen:
                    normalized.append(lane)
                    seen.add(lane)
            continue
        if key == "real_estate" or key == "realestate":
            key = "real_estate"
        elif key in {"stock", "stocks"}:
            key = "stocks"
        if key in MARKET_ANALYSIS_LANES and key not in seen:
            normalized.append(key)
            seen.add(key)
    return normalized


def _is_safe_public_url(url: str | None) -> bool:
    if not _is_valid_url(url):
        return False
    parsed = urlparse(str(url))
    if parsed.scheme not in {"http", "https"}:
        return False
    hostname = (parsed.hostname or "").strip().lower()
    if not hostname:
        return False
    if hostname in {"localhost", "127.0.0.1", "::1"}:
        return False
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        return True
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _filter_allowed_source_urls(values, *, allowed_urls: set[str]) -> list[str]:
    normalized_allowed = {_normalize_url_for_dedupe(url) for url in allowed_urls if _is_safe_public_url(url)}
    filtered: list[str] = []
    seen: set[str] = set()
    for value in _ensure_list_of_strings(values):
        url = str(value).strip()
        if not _is_safe_public_url(url):
            continue
        normalized = _normalize_url_for_dedupe(url)
        if normalized not in normalized_allowed or normalized in seen:
            continue
        filtered.append(url)
        seen.add(normalized)
    return filtered


def _domain_matches_rule(hostname: str, rule: str) -> bool:
    normalized_host = hostname.strip().lower().lstrip(".")
    normalized_rule = rule.strip().lower().lstrip(".")
    if not normalized_host or not normalized_rule:
        return False
    return normalized_host == normalized_rule or normalized_host.endswith(f".{normalized_rule}")


def _is_allowed_search_domain(url: str | None, config: AutomationConfig) -> bool:
    if not _is_safe_public_url(url):
        return False
    hostname = (urlparse(str(url)).hostname or "").strip().lower()
    if not hostname:
        return False
    if config.search_web_include_domains:
        if not any(_domain_matches_rule(hostname, rule) for rule in config.search_web_include_domains):
            return False
    if config.search_web_exclude_domains:
        if any(_domain_matches_rule(hostname, rule) for rule in config.search_web_exclude_domains):
            return False
    return True


def _uses_market_impact_template(pipeline: str | None) -> bool:
    return pipeline in {PIPELINE_DAILY_IMPACT, PIPELINE_WEEKLY_MAJOR_EVENTS}


def _market_impact_template_requirements(pipeline: str | None) -> str:
    if pipeline == PIPELINE_WEEKLY_MAJOR_EVENTS:
        return WEEKLY_MAJOR_EVENTS_TEMPLATE_REQUIREMENTS
    return DAILY_IMPACT_TEMPLATE_REQUIREMENTS


def _normalize_search_queries(value, *, limit: int = 8, max_length: int = 180) -> list[str]:
    items = _ensure_list_of_strings(value)
    normalized: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = re.sub(r"\s+", " ", str(item)).strip()
        if not text:
            continue
        if len(text) > max_length:
            text = _truncate_plain(text, max_length).rstrip(" ?!.,;:")
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(text)
        if len(normalized) >= limit:
            break
    return normalized


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
    source_urls = item.get("source_urls") or []
    if isinstance(source_urls, list):
        for value in source_urls:
            if _is_safe_public_url(value):
                urls.append(str(value))
    explore_link = item.get("explore_link")
    if _is_safe_public_url(explore_link):
        urls.append(explore_link)

    articles = item.get("news_articles") or []
    if isinstance(articles, list):
        for article in articles:
            if isinstance(article, dict):
                link = article.get("url")
                if _is_safe_public_url(link):
                    urls.append(link)

    metadata = item.get("metadata")
    if isinstance(metadata, dict):
        for value in metadata.values():
            if _is_safe_public_url(value):
                urls.append(value)

    return list(dict.fromkeys(urls))


def _extract_image_urls(item: dict) -> list[str]:
    urls: list[str] = []
    image = item.get("image")
    if isinstance(image, dict):
        image_url = image.get("url")
        if _is_safe_public_url(image_url):
            urls.append(image_url)

    articles = item.get("news_articles") or []
    if isinstance(articles, list):
        for article in articles:
            if isinstance(article, dict):
                image_url = article.get("image")
                if _is_safe_public_url(image_url):
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


def _strip_embedded_frontmatter_block(body: str) -> str:
    if not body:
        return body
    match = re.match(
        r"^\s*---\s*\n(?P<yaml>.*?)\n---\s*(?:\n+)?",
        body,
        flags=re.DOTALL,
    )
    if not match:
        return body
    yaml_block = match.group("yaml") or ""
    if not re.search(
        r"(?im)^\s*(title|description|primary_keyword|category|tags)\s*:",
        yaml_block,
    ):
        return body
    return body[match.end() :].lstrip("\n")


def _clean_body_text(body: str) -> str:
    body = _strip_embedded_frontmatter_block(body)
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


def _collect_review_hints(body: str) -> list[str]:
    if not body:
        return []
    patterns = [re.compile(pattern, re.IGNORECASE) for pattern in FINAL_REVIEW_SUSPICIOUS_PATTERNS]
    hints: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if any(pattern.search(stripped) for pattern in patterns):
            hints.append(_truncate_plain(stripped, 240))
            if len(hints) >= FINAL_REVIEW_MAX_HINTS:
                break
            continue
        if "](" in stripped:
            after = stripped.split("](", 1)[1]
            if ")" not in after:
                hints.append(_truncate_plain(stripped, 240))
                if len(hints) >= FINAL_REVIEW_MAX_HINTS:
                    break
    return hints


def _apply_to_non_fenced(text: str, transform) -> str:
    if not text:
        return text
    lines = text.splitlines()
    in_fence = False
    fence_marker = ""
    updated: list[str] = []
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            if not in_fence:
                in_fence = True
                fence_marker = marker
            elif marker == fence_marker:
                in_fence = False
                fence_marker = ""
            updated.append(line)
            continue
        if in_fence:
            updated.append(line)
            continue
        updated.append(transform(line))
    return "\n".join(updated)


def _fix_mdx_void_elements(body: str) -> str:
    if not body:
        return body

    def replacer(match: re.Match) -> str:
        raw = match.group(0)
        if raw.rstrip().endswith("/>"):
            return raw
        tag = match.group("tag")
        attrs = (match.group("attrs") or "").rstrip()
        if attrs:
            return f"<{tag}{attrs} />"
        return f"<{tag} />"

    def apply_line(line: str) -> str:
        return MDX_VOID_TAG_PATTERN.sub(replacer, line)

    return _apply_to_non_fenced(body, apply_line)


def _collect_mdx_render_hints(body: str) -> list[str]:
    if not body:
        return []
    hints: list[str] = []
    in_fence = False
    fence_marker = ""
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            if not in_fence:
                in_fence = True
                fence_marker = marker
            elif marker == fence_marker:
                in_fence = False
                fence_marker = ""
            continue
        if in_fence or not stripped:
            continue
        if MDX_UNCLOSED_VOID_TAG_PATTERN.search(stripped) or MDX_STRAY_ANGLE_PATTERN.search(stripped):
            hints.append(_truncate_plain(stripped, 240))
            if len(hints) >= MDX_RENDER_MAX_HINTS:
                break
    return hints


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
            source_name = _ensure_ascii_text(urlparse(url).netloc, "Source")
            sentence = f"{sentence} (Source: {source_name})"
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


def _load_state() -> dict:
    state_path = _resolve_state_path()
    return read_json(state_path, default={"topics": [], "slugs": []}) or {"topics": [], "slugs": []}


def _save_state(state: dict) -> None:
    state_path = _resolve_state_path()
    write_json(state_path, state)


class ClaudeClient:
    def __init__(self, api_key: str, model: str, timeout_sec: int) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout_sec = max(1, timeout_sec)
        self.base_url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"

    def _post(self, payload: dict) -> dict:
        request = Request(
            f"{self.base_url}?{urlencode({'key': self.api_key})}",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urlopen(request, timeout=self.timeout_sec) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _extract_text(data: dict) -> str:
        candidates = data.get("candidates", [])
        if not isinstance(candidates, list):
            raise RuntimeError("Gemini returned no candidates.")
        text_parts: list[str] = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            content = candidate.get("content")
            if not isinstance(content, dict):
                continue
            parts = content.get("parts", [])
            if not isinstance(parts, list):
                continue
            for part in parts:
                if not isinstance(part, dict):
                    continue
                text = part.get("text")
                if text:
                    text_parts.append(str(text))
        text = "\n".join(part for part in text_parts if part).strip()
        if not text:
            raise RuntimeError("Gemini returned no text.")
        return text

    def generate(self, prompt: str, *, temperature: float, max_tokens: int) -> str:
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY is not set.")
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": prompt}],
                }
            ],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        data = self._post(payload)
        return self._extract_text(data)

    def generate_with_google_search(
        self,
        prompt: str,
        *,
        temperature: float,
        max_tokens: int,
    ) -> tuple[str, dict]:
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY is not set.")
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": prompt}],
                }
            ],
            "tools": [{"google_search": {}}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        data = self._post(payload)
        return self._extract_text(data), data

    def generate_with_image(
        self,
        prompt: str,
        image_bytes: bytes,
        mime_type: str,
        *,
        temperature: float,
        max_tokens: int,
    ) -> str:
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY is not set.")
        if not image_bytes:
            raise RuntimeError("Image bytes are empty.")
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "inlineData": {
                                "mimeType": mime_type,
                                "data": base64.b64encode(image_bytes).decode("utf-8"),
                            }
                        },
                        {"text": prompt},
                    ],
                }
            ],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        data = self._post(payload)
        return self._extract_text(data)


class OpenAIResponsesClient:
    def __init__(self, api_key: str, model: str, timeout_sec: int) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout_sec = max(1, timeout_sec)

    def generate(self, *, instructions: str, input_text: str) -> str:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is not set.")
        if OpenAI is None:
            raise RuntimeError("openai package is not installed.")
        client = OpenAI(api_key=self.api_key, timeout=self.timeout_sec)
        response = client.responses.create(
            model=self.model,
            instructions=instructions,
            input=input_text,
        )
        output_text = getattr(response, "output_text", "")
        text = str(output_text or "").strip()
        if not text:
            raise RuntimeError("OpenAI returned no text.")
        return text


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


def _fetch_url_bytes(
    url: str,
    user_agent: str,
    timeout: int,
    delay_sec: float,
    max_retries: int,
    backoff_sec: float,
    *,
    max_bytes: int = MAX_IMAGE_BYTES,
) -> tuple[bytes | None, str | None]:
    sanitized_url = _sanitize_url(url)
    for attempt in range(max_retries + 1):
        _throttle_domain(sanitized_url, delay_sec)
        request = Request(sanitized_url, headers={"User-Agent": user_agent})
        try:
            with urlopen(request, timeout=timeout) as response:
                content_type = response.headers.get("Content-Type")
                data = response.read(max_bytes + 1)
                if len(data) > max_bytes:
                    logging.warning("Image too large to fetch: %s", sanitized_url)
                    return None, content_type
                return data, content_type
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
    return None, None


def _resolve_image_mime_type(url: str, content_type: str | None) -> str:
    if content_type:
        base_type = content_type.split(";", 1)[0].strip().lower()
        if base_type.startswith("image/"):
            return base_type
    guessed = mimetypes.guess_type(url)[0]
    if guessed and guessed.startswith("image/"):
        return guessed
    return "image/jpeg"


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


def _parse_datetime_value(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = _parse_pub_date(value)
        if not parsed:
            return None
        try:
            return datetime.fromisoformat(parsed.replace("Z", "+00:00"))
        except ValueError:
            return None


def _previous_day_window(
    content_timezone: ZoneInfo,
    *,
    publish_date: date | None = None,
) -> tuple[datetime, datetime]:
    base_date = publish_date or datetime.now(content_timezone).date()
    target_date = base_date - timedelta(days=1)
    start = datetime(target_date.year, target_date.month, target_date.day, tzinfo=content_timezone)
    end = start + timedelta(days=1) - timedelta(microseconds=1)
    return start, end


def _build_window_labels(start: datetime, end: datetime, *, publish_date: date) -> dict[str, str]:
    return {
        "publish_date": publish_date.strftime("%Y-%m-%d"),
        "publish_display_date": publish_date.strftime("%B %-d, %Y"),
        "target_date": start.strftime("%Y-%m-%d"),
        "display_date": start.strftime("%B %-d, %Y"),
        "window_summary": (
            f"{start.strftime('%Y-%m-%d %H:%M %Z')} to {end.strftime('%Y-%m-%d %H:%M %Z')}"
        ),
    }


def _previous_week_window(
    content_timezone: ZoneInfo,
    *,
    publish_date: date | None = None,
) -> tuple[datetime, datetime]:
    base_date = publish_date or datetime.now(content_timezone).date()
    end_date = base_date - timedelta(days=1)
    start_date = end_date - timedelta(days=6)
    start = datetime(start_date.year, start_date.month, start_date.day, tzinfo=content_timezone)
    end = datetime(end_date.year, end_date.month, end_date.day, tzinfo=content_timezone)
    end = end + timedelta(days=1) - timedelta(microseconds=1)
    return start, end


def _build_weekly_window_labels(start: datetime, end: datetime, *, publish_date: date) -> dict[str, str]:
    return {
        "publish_date": publish_date.strftime("%Y-%m-%d"),
        "publish_display_date": publish_date.strftime("%B %-d, %Y"),
        "window_start": start.strftime("%Y-%m-%d"),
        "window_end": end.strftime("%Y-%m-%d"),
        "week_key": f"{start.strftime('%Y-%m-%d')}_{end.strftime('%Y-%m-%d')}",
        "display_range": f"{start.strftime('%B %-d, %Y')} through {end.strftime('%B %-d, %Y')}",
        "window_summary": (
            f"{start.strftime('%Y-%m-%d %H:%M %Z')} to {end.strftime('%Y-%m-%d %H:%M %Z')}"
        ),
    }


def _is_within_window(value: str | None, start: datetime, end: datetime) -> bool:
    parsed = _parse_datetime_value(value)
    if parsed is None:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    localized = parsed.astimezone(start.tzinfo or timezone.utc)
    return start <= localized <= end


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
        if not _is_safe_public_url(link):
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
    search_depth: str | None = None,
    include_answer: bool | None = None,
) -> list[dict]:
    if not query or max_results <= 0:
        return []
    if not config.tavily_api_key:
        return []
    logging.info(
        "Tavily search query: %s (max_results=%s)",
        query,
        max_results,
    )
    payload: dict[str, object] = {
        "api_key": config.tavily_api_key,
        "query": query,
        "search_depth": config.search_web_depth,
        "max_results": min(max_results, 10),
        "include_answer": config.search_web_include_answer,
    }
    depth = (search_depth or config.search_web_depth or "").strip().lower()
    if depth not in {"basic", "advanced"}:
        depth = config.search_web_depth
    payload["search_depth"] = depth
    if include_answer is None:
        payload["include_answer"] = config.search_web_include_answer
    else:
        payload["include_answer"] = include_answer
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
        if not _is_safe_public_url(url):
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
    logging.info("Tavily search results: %s (query=%s)", len(results), query)
    return results


def _collect_candidates_for_queries(
    config: AutomationConfig,
    *,
    queries: list[str],
    region: str,
    language: str,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    web_limit: int | None = None,
    rss_limit: int | None = None,
) -> list[dict]:
    normalized_queries = [query.strip() for query in queries if query and query.strip()]
    if not normalized_queries:
        return []
    candidates: list[dict] = []
    web_budget = max(web_limit or config.search_web_max_results, 0)
    rss_budget = max(rss_limit or config.search_rss_max_results, 0)
    web_added = 0
    rss_added = 0
    for query in normalized_queries:
        if config.search_web_enabled and config.tavily_api_key and web_added < web_budget:
            remaining = min(config.search_web_max_per_query, web_budget - web_added)
            results = _search_web_tavily(
                query,
                max_results=max(remaining, 0),
                config=config,
                search_depth="advanced",
                include_answer=True,
            )
            candidates.extend(results)
            web_added += len(results)
        if config.search_rss_enabled and rss_added < rss_budget:
            remaining = min(config.search_rss_max_per_query, rss_budget - rss_added)
            results = _search_news_rss(
                query,
                region=region,
                language=language,
                max_results=max(remaining, 0),
                config=config,
            )
            candidates.extend(results)
            rss_added += len(results)
        if web_added >= web_budget and rss_added >= rss_budget:
            break
    deduped = _dedupe_candidates(candidates)
    if window_start and window_end:
        in_window = [
            candidate
            for candidate in deduped
            if _is_within_window(str(candidate.get("published_at") or ""), window_start, window_end)
        ]
        if len(in_window) >= min(3, len(deduped)):
            return in_window
        if in_window:
            merged = in_window[:]
            seen = {_normalize_url_for_dedupe(str(item.get("url") or "")) for item in in_window}
            for candidate in deduped:
                normalized = _normalize_url_for_dedupe(str(candidate.get("url") or ""))
                if normalized in seen:
                    continue
                merged.append(candidate)
            return merged
    return deduped


def _gather_raw_sources_for_queries(
    config: AutomationConfig,
    *,
    queries: list[str],
    region: str,
    language: str,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    max_sources: int | None = None,
    web_limit: int | None = None,
    rss_limit: int | None = None,
) -> list[dict]:
    candidates = _collect_candidates_for_queries(
        config,
        queries=queries,
        region=region,
        language=language,
        window_start=window_start,
        window_end=window_end,
        web_limit=web_limit,
        rss_limit=rss_limit,
    )
    if not candidates:
        return []
    return _fetch_sources_from_candidates(candidates, config, max_sources=max_sources)


def _extract_web_content_tavily(
    urls: list[str],
    *,
    config: AutomationConfig,
) -> list[dict]:
    if not urls or not config.tavily_api_key:
        return []
    logging.info(
        "Tavily extract request: %s urls (max_characters=%s)",
        len(urls),
        config.max_source_chars,
    )
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
        if not _is_safe_public_url(url):
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
    if not results:
        logging.warning("Tavily extract returned no results for %s urls", len(urls))
    else:
        logging.info("Tavily extract results: %s/%s", len(results), len(urls))
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
    if _is_safe_public_url(explore_link):
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
            if not _is_safe_public_url(url):
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
            if _is_safe_public_url(value):
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
        if not _is_safe_public_url(url):
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
        logging.info("Tavily API key missing; skip content extraction.")
        return []
    sources: list[dict] = []
    total_chars = 0
    hit_char_limit = False
    normalized_candidates: list[dict] = []
    candidate_by_url: dict[str, dict] = {}
    for candidate in candidates:
        url = candidate.get("url")
        if not _is_safe_public_url(url):
            continue
        normalized = _normalize_url_for_dedupe(str(url))
        candidate_by_url[normalized] = candidate
        normalized_candidates.append(candidate)
    limit = max_sources
    batch_size = 5 if limit is None else max(1, min(5, limit))
    total_candidates = len(normalized_candidates)
    if total_candidates == 0:
        logging.info("No valid candidates to extract.")
        return []
    logging.info(
        "Extracting content from %s candidates (limit=%s, batch_size=%s).",
        total_candidates,
        limit if limit is not None else "none",
        batch_size,
    )
    missing_extracts = 0
    empty_content = 0
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
        logging.info("Tavily extract batch: %s urls", len(batch_urls))
        extracted = _extract_web_content_tavily(batch_urls, config=config)
        logging.info(
            "Tavily extract batch results: %s/%s",
            len(extracted),
            len(batch_urls),
        )
        extracted_map = {
            _normalize_url_for_dedupe(item.get("url", "")): item for item in extracted
        }
        for url in batch_urls:
            if limit is not None and len(sources) >= limit:
                break
            normalized = _normalize_url_for_dedupe(url)
            candidate = candidate_by_url.get(normalized, {})
            item = extracted_map.get(normalized)
            fallback_text = str(candidate.get("snippet") or "").strip()
            if not item:
                missing_extracts += 1
                if not fallback_text:
                    continue
            content = str(item.get("content") or "").strip() if item else ""
            if not content:
                empty_content += 1
                content = fallback_text
            if not content:
                continue
            cleaned = _truncate(re.sub(r"\s+", " ", content), config.max_source_chars)
            if total_chars >= config.max_total_source_chars:
                cleaned = ""
                if not hit_char_limit:
                    logging.info(
                        "Reached max_total_source_chars (%s); skipping remaining content.",
                        config.max_total_source_chars,
                    )
                    hit_char_limit = True
            else:
                remaining = config.max_total_source_chars - total_chars
                if len(cleaned) > remaining:
                    cleaned = _truncate(cleaned, remaining)
                total_chars += len(cleaned)
            if not cleaned:
                continue
            sources.append(
                {
                    "url": url,
                    "title": (item.get("title") if item else None) or candidate.get("title") or "Untitled",
                    "publisher": candidate.get("publisher") or urlparse(url).netloc,
                    "published_at": candidate.get("published_at") or None,
                    "text": cleaned,
                    "origin": candidate.get("origin"),
                }
            )
    logging.info(
        "Extracted sources: %s (missing=%s, empty=%s, chars=%s/%s)",
        len(sources),
        missing_extracts,
        empty_content,
        total_chars,
        config.max_total_source_chars,
    )
    return sources


def _build_content_prompt(
    keyword: str,
    region: str,
    angle: str | None,
    traffic: str | None,
    sources: list[dict],
    image_urls: list[str],
    image_infos: list[dict] | None,
    references: list[str],
    language: str = "English",
    template_mode: str | None = None,
) -> str:
    sources_text = "\n".join(
        f"- {source['url']} | {source.get('title') or 'Untitled'}\n  {source['text']}"
        for source in sources
    )
    if image_infos:
        image_lines: list[str] = []
        for info in image_infos:
            url = str(info.get("url") or "").strip()
            if not _is_valid_url(url):
                continue
            description = str(info.get("description") or "No description available").strip()
            alt_text = str(info.get("alt_text") or "").strip()
            image_lines.append(f"- {url} | {description} | alt: {alt_text}".strip())
        images_text = "\n".join(image_lines) or "- None"
    else:
        images_text = "\n".join(f"- {url}" for url in image_urls) or "- None"
    refs_text = "\n".join(f"- {url}" for url in references) or "- None"
    has_images = bool(image_urls)
    image_requirement = (
        "Embed 1-3 images from the list at natural points between paragraphs. "
        "Avoid placing images in the first paragraph or final conclusion."
        if has_images
        else "No images are available. Do not add image markdown."
    )

    template_block = ""
    if _uses_market_impact_template(template_mode):
        template_block = f"\n\n{_market_impact_template_requirements(template_mode)}"
    angle_block = f"Angle: {angle}\n" if angle else ""

    return f"""
Role: US markets columnist and investigative writer.
Write in {language} only. ASCII characters only.

Primary keyword: {keyword}
Region: {region}
{angle_block}Traffic: {traffic or "unknown"}

Source notes (use these to build original analysis, not a list of links):
{sources_text}

Image URLs you MUST embed in the body (use Markdown images):
{images_text}

Reference URLs (frontmatter metadata only, never in body text):
{refs_text}

Output JSON only. {CONTENT_JSON_SCHEMA}

SEO requirements:
- Use the primary keyword in the first paragraph, one H2 heading, and the conclusion.
- Use 2-4 secondary keywords derived from the sources (natural phrasing, no stuffing).
- Keep paragraphs short (3-4 sentences).
- Include one FAQ section with 3-4 Q/A items.

Editorial requirements:
- Write a full topic column, not a summary or bullet digest.
- Provide depth: background, recent trigger, evidence/data, stakeholder impact, and forward-looking analysis.
- Write for US readers and interpret policy, regulation, housing, and market context in United States terms.
- Do NOT use the headings "Overview", "Key Points", or "Implications".
- Use 4-7 meaningful section headings tailored to the story.
- Include an opening paragraph with a clear angle, and a closing paragraph with a takeaway.
- Mention source names as plain text (e.g. "According to Nuveen") but do NOT include any hyperlinks, URLs, or Markdown link syntax in the body. All URLs belong in frontmatter only.
- {image_requirement}
- Do not tell readers to click the links for details; include the details in the column.
- Never include ellipses or truncated fragments. Rewrite into complete sentences.
- Target 2200-3200 words total.
- Do not include frontmatter.
{template_block}
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
- If Region is US, make the queries explicitly about the United States or the US market unless the angle itself requires narrower wording.
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
- If Region is US, make the replacement queries explicitly about the United States or the US market unless the angle itself requires narrower wording.
- Output JSON only with the keys below

Output JSON:
{{
  "queries": ["..."],
  "priority_sources": ["..."]
}}
""".strip()
    return _compose_prompt(system, user)


def _build_daily_event_map_prompt(*, window_label: str, raw_sources_json: str) -> str:
    system = """
You are a market-aware news analyst.
Review prior-day source material and extract only events that could plausibly move US stocks or US real estate.
You must think in causal chains, not headlines.
For each event, identify likely transmission mechanisms and propose follow-up searches for US stocks and US real estate separately.
""".strip()
    user = f"""
Time window: {window_label}

Raw sources:
{raw_sources_json}

Output JSON:
{{
  "events": [
    {{
      "event_id": "...",
      "title": "...",
      "summary": "...",
      "why_now": "...",
      "market_relevance": "...",
      "affected_lanes": ["stocks", "real_estate"],
      "evidence_urls": ["..."],
      "priority": "high|medium|low",
      "follow_up_queries": {{
        "stocks": ["..."],
        "real_estate": ["..."]
      }}
    }}
  ]
}}

Rules:
- Use only the provided sources.
- Keep only 4-8 events with meaningful market relevance.
- affected_lanes must show whether the event is likely to affect US stocks, US real estate, or both.
- Use both lanes when the event has a meaningful transmission path into both markets.
- Follow-up queries must search for second-order effects, not repeat the same headline.
- Stocks queries should focus on sectors, earnings, capital flows, regulation, rates, costs, or sentiment.
- Real-estate queries should focus on mortgage rates, affordability, housing demand, supply, refinancing, commercial real estate, or regional spillovers.
- If evidence is thin, lower priority and keep the summary cautious.
""".strip()
    return _compose_prompt(system, user)


def _build_daily_lane_selector_prompt(*, lane: str, window_label: str, events_json: str) -> str:
    lane_label = "stocks" if lane == "stocks" else "real estate"
    system = f"""
You are selecting the single best US {lane_label} analysis topic for a daily market briefing.
Choose the event with the clearest evidence, strongest transmission mechanism, and best potential for an original analysis article.
Prefer topics where the market consequence is more important than the headline itself.
""".strip()
    user = f"""
Time window: {window_label}
Target lane: {lane_label}

Candidate events JSON:
{events_json}

Output JSON:
{{
  "keyword": "...",
  "title": "...",
  "angle": "...",
  "why_now": "...",
  "focus_points": ["..."],
  "queries": ["..."],
  "source_urls": ["..."],
  "risk": "low|medium|high"
}}

Rules:
- Pick exactly one event.
- The angle must explain the mechanism and likely impact on US {lane_label}.
- queries must extend the original event into actionable follow-up research.
- focus_points should be 3-5 concise analytical questions or subtopics.
- Avoid generic titles like "latest updates" or "what happened".
    """.strip()
    return _compose_prompt(system, user)


def _build_weekly_major_events_prompt(
    *,
    window_label: str,
    topics_per_lane: int,
    raw_sources_json: str,
) -> tuple[str, str]:
    instructions = """
You are a US macro and markets editor.
Identify the most important recent events that could materially affect US stocks or US real estate.
Your job is topic discovery only. Do not write the article.
Prefer events with clear economic or market transmission mechanisms.
Frame every topic for a US audience and US market context.
Return strict JSON only.
""".strip()
    input_text = f"""
Time window: {window_label}

Tasks:
1) Identify up to {topics_per_lane} high-signal US stocks topic(s).
2) Identify up to {topics_per_lane} high-signal US real-estate topic(s).
3) For each topic, provide a concrete angle and follow-up research queries.

Raw sources (you must use only these):
{raw_sources_json}

Output JSON:
{{
  "topics": [
    {{
      "lane": "stocks|real_estate",
      "keyword": "...",
      "title": "...",
      "angle": "...",
      "why_now": "...",
      "focus_points": ["..."],
      "queries": ["..."],
      "source_urls": ["..."],
      "risk": "low|medium|high"
    }}
  ]
}}

Rules:
- Focus on recent events within or directly relevant to the time window.
- Use only the provided raw sources. Do not rely on unstated background knowledge.
- Each topic must be specific enough to research and turn into an evidence-first article.
- Prefer events with second-order impact on valuations, rates, credit, housing demand, supply, regulation, or sentiment.
- Avoid celebrity, rumor, and low-signal topics.
- Use lane=stocks or lane=real_estate only.
- Return no more than {topics_per_lane} topic(s) per lane.
- source_urls must come from the provided raw sources only.
""".strip()
    return instructions, input_text


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


def _build_outline_prompt(
    *,
    keyword: str,
    angle: str,
    evidence_summary: str,
    template_mode: str | None = None,
) -> str:
    system = """
You are the article architect.
Create a logical, reader-friendly structure that supports the chosen angle.
Balance context, evidence, impact, and forward-looking analysis.
Use section headings that are specific, concrete, and SEO-aware.
Anchor sections in evidence and reader intent (what they came to learn).
""".strip()
    template_block = ""
    if _uses_market_impact_template(template_mode):
        template_block = _market_impact_template_requirements(template_mode)
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
- Provide 5-8 sections.
- Avoid generic headings like "Overview" or "Conclusion".
- evidence_refs should point to source URLs or IDs.
- Ensure at least one section addresses "what changed/why now".
- Ensure at least one section addresses "impact / what it means for readers".
- Include the primary keyword (or close variation) in at least 2 headings.
- Include sections covering background/context, evidence or data, and outlook.
- FAQ should target high-intent reader questions, not trivia.
{template_block}
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


def _build_chart_plan_prompt(
    *,
    keyword: str,
    angle: str,
    summary: str,
    key_points: list[str],
    body_excerpt: str,
) -> str:
    key_points_text = "\n".join(f"- {point}" for point in key_points) or "- None"
    return f"""
SYSTEM:
You are a data-visual planner for financial/market explainers.
Return a compact JSON plan for up to 2 simple charts.

USER:
Topic: {keyword}
Angle: {angle}
Summary: {summary}
Key points:
{key_points_text}

Body excerpt:
{body_excerpt}

Rules:
- Use only numeric relationships explicitly stated in the input.
- Do not infer or invent values. If evidence is insufficient, return an empty chart list.
- chart_type must be one of: bar, line.
- labels and values must be equal length (2-6 items).
- values must be numbers only.
- Keep captions concise and factual.
- alt_text must describe the chart clearly.
- Output JSON only with this exact shape.

Output JSON:
{{
  "charts": [
    {{
      "title": "...",
      "chart_type": "bar|line",
      "labels": ["..."],
      "values": [1.0, 2.0],
      "unit": "...",
      "alt_text": "...",
      "caption": "..."
    }}
  ]
}}
""".strip()


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
Ground factual statements in the provided evidence and sources.
Mention source names as plain text only; do not include hyperlinks, URLs, or Markdown links in the body.
If evidence is thin, be cautious and state uncertainty explicitly.
""".strip()
    user = f"""
Section title: {section_heading}
Section goal: {section_goal}
Evidence/facts: {evidence_subset}
Relevant sources: {sources_subset}
Language: {language}

Writing rules:
- 4-7 paragraphs, 4-6 sentences each
- Mention source names as plain text only (for example, "According to Nuveen")
- Do not make unsupported claims
- Avoid hype or sensational wording
- Do not add a heading; the assembler will add it
- Prefer clear cause -> evidence -> implication flow
- If a key claim lacks evidence, mark it as uncertain rather than assert it
- Keep terminology consistent with sources (avoid re-labeling entities)
- Add specific context, data, or verification details where available

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
    template_mode: str | None = None,
) -> str:
    system = """
You are the editor in chief.
Assemble sections into a coherent article with smooth transitions.
Add an intro, conclusion, and FAQ without adding new facts.
Preserve source attributions as plain text names only and do not invent sources.
Do not include hyperlinks, raw URLs, or Markdown link syntax in the body.
Maintain a consistent voice and avoid redundancy across sections.
""".strip()
    template_block = ""
    if _uses_market_impact_template(template_mode):
        template_block = f"\n{_market_impact_template_requirements(template_mode)}"
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
- Mention source names as plain text only; do not include hyperlinks, URLs, or Markdown link syntax in the body. All URLs belong in frontmatter only.
- Target 5000-6000 words total
{template_block}

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
- Factual claims are supported by source attribution in plain text (no hyperlinks)
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


def _build_final_review_prompt(
    *,
    full_mdx: str,
    keyword: str,
    language: str,
    hints: list[str],
) -> str:
    system = """
You are a meticulous MDX editor and QA reviewer.
Check sentence structure, grammar, and scraping artifacts.
Never add new facts or sources. Keep source names as plain text only and remove hyperlinks/URLs from the body.
""".strip()
    hints_block = json.dumps(hints, ensure_ascii=True)
    user = f"""
Language: {language}
Primary keyword: {keyword}

Article (MDX):
{full_mdx}

Suspicious snippets (if any):
{hints_block}

Review focus:
- Sentences are grammatical and complete
- No UI/ads/navigation remnants or garbage text
- No hyperlinks or URLs in body text
- Markdown structure remains valid

Decision:
- status=pass if clean
- status=fix if minor removals or edits are enough
- status=regenerate if sentence structure is broadly broken or the article is incoherent

Output JSON only:
{{
  "status": "pass|fix|regenerate",
  "issues": [
    {{"type": "grammar|artifact|markdown|structure", "detail": "...", "fix_hint": "..."}}
  ],
  "cleaned_mdx": "..."
}}

Rules:
- If status is pass, cleaned_mdx must be an empty string.
- If status is fix or regenerate, cleaned_mdx must contain the full revised article.
- Use valid JSON and escape newlines as \\n.
""".strip()
    return _compose_prompt(system, user)


def _build_mdx_render_guard_prompt(*, full_mdx: str, hints: list[str]) -> str:
    system = """
You are an MDX rendering QA editor.
Fix MDX/JSX syntax issues that could break rendering.
Never add new facts or sources. Keep source names as plain text only and remove hyperlinks/URLs from the body.
""".strip()
    hints_block = json.dumps(hints, ensure_ascii=True)
    user = f"""
Article (MDX):
{full_mdx}

Suspicious snippets (if any):
{hints_block}

Review focus:
- Void HTML elements must be self-closing (e.g., <br />, <img />).
- Fix malformed tags or stray angle brackets in plain text.
- Preserve tables, headings, and source attributions as plain text names only.

Decision:
- status=pass if clean
- status=fix if edits are needed

Output JSON only:
{{
  "status": "pass|fix",
  "issues": [
    {{"type": "mdx|jsx|markdown", "detail": "...", "fix_hint": "..."}}
  ],
  "cleaned_mdx": "..."
}}

Rules:
- If status is pass, cleaned_mdx must be an empty string.
- If status is fix, cleaned_mdx must contain the full revised article.
- Use valid JSON and escape newlines as \\n.
""".strip()
    return _compose_prompt(system, user)


def _build_revision_prompt(*, full_mdx: str, issues_json: str, keyword: str) -> str:
    system = """
You are a senior editor revising an article to address quality issues.
You must fix the issues without adding new facts or sources.
Preserve existing source attributions as plain text names and only adjust wording or structure.
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
- Mention source names as plain text only; do not include hyperlinks, URLs, or Markdown link syntax in the body.

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


def _build_image_description_prompt() -> str:
    system = """
You are a visual analyst and accessibility writer.
Describe the image content precisely and extract useful keywords.
""".strip()
    user = """
Task:
- Describe the image in 1-2 sentences.
- Provide 3-6 short keywords or phrases.
- Provide a concrete alt text (6-12 words).

Rules:
- English only. ASCII only.
- Be specific about visible objects, setting, and actions.
- Avoid subjective adjectives like "beautiful" or "stunning".
- If the image is unclear, say "unclear image" and use generic keywords.

Output JSON:
{
  "description": "...",
  "keywords": ["..."],
  "alt_text": "..."
}
""".strip()
    return _compose_prompt(system, user)


def _describe_image_urls(
    config: AutomationConfig,
    writer: ClaudeClient,
    image_urls: list[str],
) -> list[dict]:
    if not image_urls or not config.anthropic_api_key:
        return []
    prompt = _build_image_description_prompt()
    results: list[dict] = []
    for url in list(dict.fromkeys(image_urls))[:MAX_IMAGE_ANALYSIS]:
        if not _is_valid_url(url):
            continue
        image_bytes, content_type = _fetch_url_bytes(
            url,
            config.user_agent,
            config.scrape_timeout,
            config.scrape_delay_sec,
            config.scrape_max_retries,
            config.scrape_backoff_sec,
        )
        if not image_bytes:
            continue
        mime_type = _resolve_image_mime_type(url, content_type)
        try:
            response = writer.generate_with_image(
                prompt,
                image_bytes,
                mime_type,
                temperature=min(config.anthropic_temperature, 0.4),
                max_tokens=min(config.anthropic_max_tokens, 600),
            )
            data = _extract_json_block(response)
        except Exception as exc:
            logging.warning("Image analysis failed for %s: %s", url, exc)
            data = None
        if not isinstance(data, dict):
            continue
        description = _ensure_ascii_text(str(data.get("description") or "").strip(), "")
        alt_text = _ensure_ascii_text(str(data.get("alt_text") or "").strip(), "")
        keywords = [
            _ensure_ascii_text(keyword, "")
            for keyword in _ensure_list_of_strings(data.get("keywords"))
        ]
        keywords = [keyword for keyword in keywords if keyword]
        if not description and not alt_text and not keywords:
            continue
        if not alt_text:
            alt_text = description or "Related image"
        results.append(
            {
                "url": url,
                "description": description,
                "alt_text": alt_text,
                "keywords": keywords,
            }
        )
    return results


def _extract_keywords_from_text(text: str, max_terms: int = 6) -> list[str]:
    tokens = re.findall(r"[a-zA-Z0-9]{3,}", text.lower())
    stopwords = {
        "the",
        "and",
        "with",
        "from",
        "this",
        "that",
        "image",
        "photo",
        "picture",
        "illustration",
        "graphic",
        "people",
        "person",
        "woman",
        "man",
        "men",
        "women",
        "crowd",
        "group",
        "scene",
        "background",
    }
    seen: set[str] = set()
    keywords: list[str] = []
    for token in tokens:
        if token in stopwords or token in seen:
            continue
        seen.add(token)
        keywords.append(token)
        if len(keywords) >= max_terms:
            break
    return keywords


def _is_text_block(block: str) -> bool:
    stripped = block.strip()
    if not stripped:
        return False
    if stripped.startswith(("#", "![", "-", ">", "```")):
        return False
    return True


def _score_block_for_keywords(block: str, keywords: list[str]) -> int:
    if not keywords:
        return 0
    text = block.lower()
    score = 0
    for keyword in keywords:
        normalized = keyword.lower().strip()
        if normalized and normalized in text:
            score += 1
    return score


def _insert_images_by_relevance(body: str, image_infos: list[dict]) -> str:
    if not image_infos:
        return body
    existing = re.findall(r"!\[.*?\]\((.*?)\)", body)
    existing_set = {url for url in existing if isinstance(url, str)}
    candidates: list[dict] = []
    for info in image_infos:
        if not isinstance(info, dict):
            continue
        url = info.get("url")
        if _is_valid_url(url) and url not in existing_set:
            candidates.append(info)
    if not candidates:
        return body
    parts = body.split("\n\n")
    candidate_indices = [index for index, part in enumerate(parts) if _is_text_block(part)]
    if len(candidate_indices) > 4:
        candidate_indices = candidate_indices[2:-2]
    if not candidate_indices:
        return body
    placements: list[tuple[int, dict]] = []
    for info in candidates[:3]:
        keywords = _ensure_list_of_strings(info.get("keywords"))
        if not keywords:
            keywords = _extract_keywords_from_text(str(info.get("description") or ""))
        best_index = None
        best_score = -1
        for index in candidate_indices:
            score = _score_block_for_keywords(parts[index], keywords)
            if score > best_score:
                best_score = score
                best_index = index
        if best_index is None:
            continue
        placements.append((best_index, info))
        candidate_indices.remove(best_index)
        if not candidate_indices:
            break
    if not placements:
        return body
    for index, info in sorted(placements, key=lambda item: item[0], reverse=True):
        alt_text = _ensure_ascii_text(
            str(info.get("alt_text") or info.get("description") or "Related image"),
            "Related image",
        )
        parts.insert(index + 1, f"![{alt_text}]({info.get('url')})")
    return "\n\n".join(parts)


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
    text_indices = [index for index, part in enumerate(parts) if _is_text_block(part)]
    if text_indices:
        insert_at = text_indices[min(len(text_indices) // 2, len(text_indices) - 1)]
    else:
        insert_at = 2 if len(parts) > 2 else len(parts)
    parts[insert_at:insert_at] = blocks
    return "\n\n".join(parts)


def _xml_escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _normalize_chart_specs(raw: object) -> list[dict]:
    if not isinstance(raw, list):
        return []
    normalized: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        chart_type = str(item.get("chart_type") or "").strip().lower()
        if chart_type not in {"bar", "line"}:
            continue
        labels = [
            _ensure_ascii_text(str(label).strip(), "")
            for label in (item.get("labels") or [])
            if str(label).strip()
        ]
        values_raw = item.get("values") or []
        values: list[float] = []
        for value in values_raw:
            try:
                values.append(float(value))
            except (TypeError, ValueError):
                values = []
                break
        if len(labels) < 2 or len(labels) > 6 or len(labels) != len(values):
            continue
        if max(values) == min(values):
            continue
        normalized.append(
            {
                "title": _ensure_ascii_text(str(item.get("title") or "Market signal").strip(), "Market signal"),
                "chart_type": chart_type,
                "labels": labels,
                "values": values,
                "unit": _ensure_ascii_text(str(item.get("unit") or "").strip(), ""),
                "alt_text": _ensure_ascii_text(
                    str(item.get("alt_text") or item.get("title") or "Chart").strip(),
                    "Chart",
                ),
                "caption": _ensure_ascii_text(str(item.get("caption") or "").strip(), ""),
            }
        )
        if len(normalized) >= MAX_INLINE_CHARTS:
            break
    return normalized


def _plan_inline_charts(
    config: AutomationConfig,
    writer: ClaudeClient,
    *,
    keyword: str,
    angle: str,
    summary: str,
    key_points: list[str],
    body: str,
) -> list[dict]:
    prompt = _build_chart_plan_prompt(
        keyword=keyword,
        angle=angle,
        summary=summary,
        key_points=key_points,
        body_excerpt=_truncate(_strip_markdown(body), 2200),
    )
    try:
        response = writer.generate(
            prompt,
            temperature=config.anthropic_temperature,
            max_tokens=config.anthropic_max_tokens,
        )
        data = _extract_json_block(response)
    except Exception as exc:
        logging.warning("Chart planning failed: %s", exc)
        return []
    if not isinstance(data, dict):
        return []
    return _normalize_chart_specs(data.get("charts"))


def _fallback_daily_impact_chart_spec(
    *,
    keyword: str,
    summary: str,
    key_points: list[str],
    body: str,
) -> dict:
    text = " ".join([summary, " ".join(key_points), _truncate(_strip_markdown(body), 2400)]).lower()
    factor_tokens = [
        ("Inflation", ("inflation", "cpi", "price", "prices")),
        ("Rates", ("yield", "yields", "rate", "rates", "fed")),
        ("Growth", ("growth", "gdp", "demand", "jobs", "employment")),
        ("Risk", ("risk", "volatility", "uncertainty", "downside", "stress")),
    ]
    labels: list[str] = []
    values: list[float] = []
    for idx, (label, tokens) in enumerate(factor_tokens, start=1):
        score = 0
        for token in tokens:
            score += text.count(token)
        score = max(score, 1)
        score += idx * 0.1
        labels.append(label)
        values.append(round(float(score), 2))
    return {
        "title": _ensure_ascii_text(f"{keyword} macro signal emphasis", "Macro signal emphasis"),
        "chart_type": "bar",
        "labels": labels,
        "values": values,
        "unit": "signal score",
        "alt_text": _ensure_ascii_text(
            f"Heuristic macro signal emphasis chart for {keyword}",
            "Heuristic macro signal emphasis chart",
        ),
        "caption": f"Key macro signal emphasis for {keyword}, based on article topic analysis.",
    }


def _render_chart_svg(spec: dict) -> str:
    width = 1000
    height = 560
    left = 90
    right = 50
    top = 90
    bottom = 130
    plot_w = width - left - right
    plot_h = height - top - bottom
    labels = spec.get("labels") or []
    values = spec.get("values") or []
    title = _xml_escape(spec.get("title") or "Market signal")
    unit = _xml_escape(spec.get("unit") or "")
    min_v = min(values)
    max_v = max(values)
    span = max(max_v - min_v, 1e-9)

    def y_of(value: float) -> float:
        return top + (max_v - value) / span * plot_h

    svg_lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="{_xml_escape(spec.get("alt_text") or spec.get("title") or "Chart")}">',
        '<rect width="100%" height="100%" fill="#ffffff" />',
        f'<text x="{left}" y="46" font-size="28" font-family="Arial, sans-serif" fill="#111827">{title}</text>',
        f'<line x1="{left}" y1="{top + plot_h}" x2="{left + plot_w}" y2="{top + plot_h}" stroke="#9ca3af" stroke-width="2"/>',
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_h}" stroke="#9ca3af" stroke-width="2"/>',
    ]

    ticks = 4
    for i in range(ticks + 1):
        val = min_v + (span * i / ticks)
        y = y_of(val)
        svg_lines.append(
            f'<line x1="{left}" y1="{y:.2f}" x2="{left + plot_w}" y2="{y:.2f}" stroke="#e5e7eb" stroke-width="1"/>'
        )
        label = f"{val:.2f}".rstrip("0").rstrip(".")
        if unit:
            label = f"{label} {unit}"
        svg_lines.append(
            f'<text x="{left - 12}" y="{y + 5:.2f}" text-anchor="end" font-size="13" font-family="Arial, sans-serif" fill="#6b7280">{_xml_escape(label)}</text>'
        )

    count = len(labels)
    if spec.get("chart_type") == "bar":
        slot = plot_w / count
        bar_w = slot * 0.6
        for idx, (label, value) in enumerate(zip(labels, values, strict=False)):
            x = left + idx * slot + (slot - bar_w) / 2
            y = y_of(value)
            h = top + plot_h - y
            svg_lines.append(
                f'<rect x="{x:.2f}" y="{y:.2f}" width="{bar_w:.2f}" height="{h:.2f}" fill="#2563eb" rx="4"/>'
            )
            svg_lines.append(
                f'<text x="{x + bar_w / 2:.2f}" y="{top + plot_h + 28}" text-anchor="middle" font-size="13" font-family="Arial, sans-serif" fill="#374151">{_xml_escape(label)}</text>'
            )
    else:
        step = plot_w / (count - 1)
        points: list[str] = []
        for idx, (label, value) in enumerate(zip(labels, values, strict=False)):
            x = left + idx * step
            y = y_of(value)
            points.append(f"{x:.2f},{y:.2f}")
            svg_lines.append(
                f'<circle cx="{x:.2f}" cy="{y:.2f}" r="5" fill="#2563eb"/>'
            )
            svg_lines.append(
                f'<text x="{x:.2f}" y="{top + plot_h + 28}" text-anchor="middle" font-size="13" font-family="Arial, sans-serif" fill="#374151">{_xml_escape(label)}</text>'
            )
        points_attr = " ".join(points)
        svg_lines.append(
            f'<polyline fill="none" stroke="#2563eb" stroke-width="3" points="{points_attr}"/>'
        )

    svg_lines.append("</svg>")
    return "\n".join(svg_lines)


_IMAGE_STYLE_DIRECTIVE_MARKERS = (
    "no text", "no logo", "no face", "clean white", "clean background",
    "white background", "corporate style", "corporate blue", "color palette",
    "high quality", "depth of field", "isometric", "minimalist ",
    "3d illustration", "clean corporate", "modern office setting", "gradient background",
)


def _extract_alt_from_image_prompt(prompt: str) -> str:
    clean = prompt.strip()
    lower = clean.lower()
    cutoff = len(clean)
    for marker in _IMAGE_STYLE_DIRECTIVE_MARKERS:
        idx = lower.find(marker)
        if 0 < idx < cutoff:
            cutoff = idx
    clean = re.sub(r"\s+illustration\s*$", "", clean[:cutoff].strip(" .,;:"), flags=re.IGNORECASE).strip(" .,;:")
    if len(clean) > 125:
        clean = clean[:122].rsplit(" ", 1)[0].rstrip(" .,;:") + "..."
    return clean or "Related illustration"


def _build_visual_markdown_block(*, alt_text: str, path: str, caption: str) -> str:
    image_line = f"![{alt_text}]({path})"
    safe_caption = _ensure_ascii_text(caption.strip(), "")
    if not safe_caption:
        return image_line
    return f"{image_line}\n\n*{safe_caption}*"


def _insert_visual_blocks(body: str, visual_blocks: list[str]) -> str:
    if not body or not visual_blocks:
        return body
    blocks = [part for part in body.split("\n\n") if part is not None]
    if not blocks:
        return body
    h2_indices = [i for i, part in enumerate(blocks) if part.strip().startswith("## ")]
    anchor_indices = h2_indices[1:] if len(h2_indices) > 1 else h2_indices
    if not anchor_indices:
        blocks.extend(visual_blocks)
        return "\n\n".join(blocks)
    placements: list[tuple[int, str]] = []
    total = len(visual_blocks)
    for idx, visual in enumerate(visual_blocks):
        anchor_pos = int(((idx + 1) * len(anchor_indices)) / (total + 1))
        if anchor_pos >= len(anchor_indices):
            anchor_pos = len(anchor_indices) - 1
        placements.append((anchor_indices[anchor_pos], visual))
    for anchor, visual in sorted(placements, key=lambda item: item[0], reverse=True):
        blocks.insert(anchor + 1, visual)
    return "\n\n".join(blocks)


def _parse_aspect_ratio(value: str | None) -> tuple[int, int]:
    if not value or ":" not in value:
        return DEFAULT_GRADIENT_WIDTH, DEFAULT_GRADIENT_HEIGHT
    pieces = value.split(":", 1)
    if len(pieces) != 2:
        return DEFAULT_GRADIENT_WIDTH, DEFAULT_GRADIENT_HEIGHT
    try:
        width_ratio = float(pieces[0])
        height_ratio = float(pieces[1])
    except ValueError:
        return DEFAULT_GRADIENT_WIDTH, DEFAULT_GRADIENT_HEIGHT
    if width_ratio <= 0 or height_ratio <= 0:
        return DEFAULT_GRADIENT_WIDTH, DEFAULT_GRADIENT_HEIGHT
    width = DEFAULT_GRADIENT_WIDTH
    height = max(1, int(round(width * height_ratio / width_ratio)))
    return width, height


def _hsv_to_rgb_int(hue: float, saturation: float, value: float) -> tuple[int, int, int]:
    red, green, blue = colorsys.hsv_to_rgb(hue, saturation, value)
    return int(red * 255), int(green * 255), int(blue * 255)


def _random_gradient_colors(rng: random.Random) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    base_hue = rng.random()
    shift = rng.uniform(0.2, 0.6)
    second_hue = (base_hue + shift) % 1.0
    saturation = rng.uniform(0.5, 0.85)
    value_a = rng.uniform(0.6, 0.95)
    value_b = rng.uniform(0.55, 0.9)
    return _hsv_to_rgb_int(base_hue, saturation, value_a), _hsv_to_rgb_int(
        second_hue, saturation, value_b
    )


def _build_gradient_pixels(
    width: int,
    height: int,
    start_rgb: tuple[int, int, int],
    end_rgb: tuple[int, int, int],
    angle: float,
) -> bytes:
    if width <= 0 or height <= 0:
        return b""
    dx = math.cos(angle)
    dy = math.sin(angle)
    pixels = bytearray(width * height * 3)
    row_len = width * 3
    for y in range(height):
        ny = 0.0 if height == 1 else (y / (height - 1)) * 2 - 1
        row_offset = y * row_len
        for x in range(width):
            nx = 0.0 if width == 1 else (x / (width - 1)) * 2 - 1
            t = (nx * dx + ny * dy) * 0.5 + 0.5
            if t < 0:
                t = 0.0
            elif t > 1:
                t = 1.0
            red = int(start_rgb[0] + (end_rgb[0] - start_rgb[0]) * t)
            green = int(start_rgb[1] + (end_rgb[1] - start_rgb[1]) * t)
            blue = int(start_rgb[2] + (end_rgb[2] - start_rgb[2]) * t)
            idx = row_offset + x * 3
            pixels[idx] = red
            pixels[idx + 1] = green
            pixels[idx + 2] = blue
    return bytes(pixels)


def _encode_png_bytes(width: int, height: int, pixels: bytes) -> bytes:
    if width <= 0 or height <= 0:
        return b""
    if len(pixels) != width * height * 3:
        return b""
    row_len = width * 3
    raw = bytearray()
    for y in range(height):
        start = y * row_len
        raw.append(0)
        raw.extend(pixels[start : start + row_len])
    compressed = zlib.compress(bytes(raw), level=6)

    def _chunk(tag: bytes, data: bytes) -> bytes:
        length = struct.pack(">I", len(data))
        crc = struct.pack(">I", binascii.crc32(tag + data) & 0xFFFFFFFF)
        return length + tag + data + crc

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", compressed)
        + _chunk(b"IEND", b"")
    )


def _write_gradient_jpeg(output_path: Path, width: int, height: int, pixels: bytes) -> bool:
    try:
        from PIL import Image  # type: ignore
    except Exception:
        return False
    try:
        image = Image.frombytes("RGB", (width, height), pixels)
        image.save(output_path, format="JPEG", quality=DEFAULT_GRADIENT_JPEG_QUALITY)
        return True
    except Exception as exc:
        logging.warning("Gradient JPEG generation failed: %s", exc)
        return False


def _convert_png_bytes_to_jpeg(png_bytes: bytes, output_path: Path) -> bool:
    tool = shutil.which("sips")
    if not tool:
        return False
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp.write(png_bytes)
            tmp_path = Path(tmp.name)
        result = subprocess.run(
            [tool, "-s", "format", "jpeg", str(tmp_path), "--out", str(output_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        return result.returncode == 0 and output_path.exists()
    except Exception as exc:
        logging.warning("Gradient JPEG conversion failed: %s", exc)
        return False
    finally:
        if tmp_path:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass


def _generate_hero_gradient(output_path: Path, config: AutomationConfig) -> bool:
    width, height = _parse_aspect_ratio(config.google_image_aspect_ratio)
    rng = random.Random(str(output_path))
    start_rgb, end_rgb = _random_gradient_colors(rng)
    angle = rng.uniform(0.0, math.pi * 2)
    pixels = _build_gradient_pixels(width, height, start_rgb, end_rgb, angle)
    if not pixels:
        return False
    if _write_gradient_jpeg(output_path, width, height, pixels):
        return True
    png_bytes = _encode_png_bytes(width, height, pixels)
    if not png_bytes:
        return False
    if _convert_png_bytes_to_jpeg(png_bytes, output_path):
        return True
    output_path.write_bytes(png_bytes)
    logging.warning("Gradient PNG saved with .jpg extension: %s", output_path)
    return True


def _generate_hero_image_google(prompt: str, output_path: Path, config: AutomationConfig) -> bool:
    if not config.google_image_enabled:
        return False
    api_key = config.google_api_key or config.gemini_api_key
    if not api_key:
        return False
    model = config.google_image_model or DEFAULT_GOOGLE_IMAGE_MODEL
    safe_prompt = _force_ascii(prompt).strip() or "Abstract tech illustration"
    payload = {
        "contents": [{"parts": [{"text": safe_prompt}]}],
        "generationConfig": {
            "responseModalities": ["TEXT", "IMAGE"],
            "imageConfig": {
                "aspectRatio": config.google_image_aspect_ratio
                or DEFAULT_GOOGLE_IMAGE_ASPECT_RATIO
            },
        },
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
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
    candidates = data.get("candidates")
    if not isinstance(candidates, list):
        return False
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content")
        if not isinstance(content, dict):
            continue
        parts = content.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict):
                continue
            inline = part.get("inlineData")
            if inline is None:
                inline = part.get("inline_data")
            if not isinstance(inline, dict):
                continue
            b64 = inline.get("data") or inline.get("bytesBase64Encoded")
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
        logging.info("Hero image generated via Gemini image model.")
        return
    if _generate_hero_gradient(output_path, config):
        logging.info("Hero image gradient generated locally.")
        return

    placeholder = config.astro_root / "src" / "assets" / "blog-placeholder-5.jpg"
    if placeholder.exists():
        output_path.write_bytes(placeholder.read_bytes())
        logging.info("Hero image fallback used.")
    else:
        logging.warning("Hero image placeholder missing: %s", placeholder)


def _materialize_inline_visuals(
    config: AutomationConfig,
    *,
    date_str: str,
    slug: str,
    chart_specs: list[dict],
    inline_image_prompts: list[str],
) -> list[str]:
    asset_key = f"{date_str}-{slug}"
    asset_dir = config.hero_base_dir / asset_key
    asset_dir.mkdir(parents=True, exist_ok=True)
    base_url = f"/images/posts/{asset_key}"
    visual_blocks: list[str] = []

    for idx, spec in enumerate(chart_specs[:MAX_INLINE_CHARTS], start=1):
        try:
            svg_path = asset_dir / f"chart-{idx}.svg"
            svg_path.write_text(_render_chart_svg(spec), encoding="utf-8")
            visual_blocks.append(
                _build_visual_markdown_block(
                    alt_text=_ensure_ascii_text(str(spec.get("alt_text") or "Chart").strip(), "Chart"),
                    path=f"{base_url}/chart-{idx}.svg",
                    caption=str(spec.get("caption") or "").strip(),
                )
            )
        except Exception as exc:
            logging.warning("Inline chart generation failed (%s): %s", idx, exc)

    for idx, prompt in enumerate(inline_image_prompts[:MAX_GENERATED_INLINE_IMAGES], start=1):
        safe_prompt = _ensure_ascii_text(prompt.strip(), "")
        if not safe_prompt:
            continue
        image_path = asset_dir / f"inline-{idx}.jpg"
        try:
            generated = _generate_hero_image_google(safe_prompt, image_path, config)
            if not generated:
                generated = _generate_hero_gradient(image_path, config)
            if not generated:
                continue
            visual_blocks.append(
                _build_visual_markdown_block(
                    alt_text=_ensure_ascii_text(_extract_alt_from_image_prompt(safe_prompt), "Related illustration"),
                    path=f"{base_url}/inline-{idx}.jpg",
                    caption="Generated supporting visual",
                )
            )
        except Exception as exc:
            logging.warning("Inline image generation failed (%s): %s", idx, exc)
    return visual_blocks[:MAX_INLINE_VISUALS]


def _build_frontmatter(
    *,
    title: str,
    description: str,
    date_str: str,
    slug: str,
    category: list[str],
    tags: list[str],
    reference_urls: list[str],
    draft: bool,
    hero_alt: str,
    domain: str,
) -> str:
    safe_title = title.replace('"', '\\"')
    safe_desc = description.replace('"', '\\"')
    safe_alt = hero_alt.replace('"', '\\"')
    category_list = ", ".join(f'"{item}"' for item in category)
    tag_list = ", ".join(f'"{tag}"' for tag in tags)
    references_yaml = (
        "references:\n"
        + "\n".join(f'  - "{url.replace(chr(34), r"\\\"")}"' for url in reference_urls)
        if reference_urls
        else "references: []"
    )
    canonical = f"{domain}/blog/{date_str}-{slug}"
    return (
        "---\n"
        f'title: "{safe_title}"\n'
        f'description: "{safe_desc}"\n'
        f"pubDate: {date_str}\n"
        f"updatedDate: {date_str}\n"
        f"category: [{category_list}]\n"
        f"tags: [{tag_list}]\n"
        f"{references_yaml}\n"
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
    chart_specs: list[dict] | None,
    inline_image_prompts: list[str] | None,
    slug_hint: str,
    date_str: str | None = None,
) -> Path:
    date_str = date_str or datetime.now(config.content_timezone).strftime("%Y-%m-%d")
    slug = _slugify(slug_hint) or f"topic-{int(time.time())}"
    post_path = config.content_dir / f"{date_str}-{slug}.mdx"
    if post_path.exists():
        slug = f"{slug}-{int(time.time())}"
        post_path = config.content_dir / f"{date_str}-{slug}.mdx"

    unique_refs = [url for url in dict.fromkeys(reference_urls) if _is_valid_url(url)]

    frontmatter = _build_frontmatter(
        title=title,
        description=description,
        date_str=date_str,
        slug=slug,
        category=category,
        tags=tags,
        reference_urls=unique_refs,
        draft=config.post_draft,
        hero_alt=hero_alt or title,
        domain=config.blog_domain,
    )

    content = body.strip()
    visual_blocks = _materialize_inline_visuals(
        config,
        date_str=date_str,
        slug=slug,
        chart_specs=chart_specs or [],
        inline_image_prompts=inline_image_prompts or [],
    )
    if visual_blocks:
        content = _insert_visual_blocks(content, visual_blocks)

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
    link_snippet = [
        _ensure_ascii_text(urlparse(str(link)).netloc, "Source") for link in links[:3] if _is_valid_url(str(link))
    ]
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
        f"{'Key reporting comes from ' + ', '.join(link_snippet) + '.' if link_snippet else ''}\n\n"
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


def _plan_research(
    config: AutomationConfig,
    writer: ClaudeClient,
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
            temperature=config.anthropic_temperature,
            max_tokens=config.anthropic_max_tokens,
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
        year = datetime.now(config.content_timezone).year
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


def _build_question_queries(
    config: AutomationConfig,
    keyword: str,
    angle: str | None = None,
) -> list[str]:
    base = keyword.strip()
    if not base:
        return []
    year = datetime.now(config.content_timezone).year
    angle_hint = ""
    if angle:
        concise_angle = _truncate_plain(re.sub(r"\s+", " ", angle.strip()), 80).rstrip(" ?!.,;:")
        if concise_angle:
            angle_hint = f" {concise_angle}"
    return [
        f"What changed about {base} in {year} and why now{angle_hint}?",
        f"Who is affected by {base} and what are the impacts in {year}?",
        f"What official statements, data, or reports support {base} in {year}?",
    ]


def _rescue_research_plan(
    config: AutomationConfig,
    writer: ClaudeClient,
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
            temperature=config.anthropic_temperature,
            max_tokens=config.anthropic_max_tokens,
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
    writer: ClaudeClient,
    topic: dict,
    research_plan: dict,
) -> list[dict]:
    keyword = str(topic.get("keyword") or "").strip() or "unknown"
    logging.info("Gathering sources for topic: %s", keyword)
    seed_urls = _extract_urls(topic)
    candidates = _candidate_sources_from_topic(topic)
    logging.info("Seed candidates from topic: %s", len(candidates))
    web_added_total = 0
    if config.search_web_enabled:
        if not config.tavily_api_key:
            logging.warning(
                "Web search enabled but TAVILY_API_KEY missing; skipping Tavily search."
            )
        else:
            queries = _ensure_list_of_strings(research_plan.get("queries"))[:8]
            logging.info("Web search queries: %s", len(queries))
            logging.info(
                "Web search settings: depth=%s include_answer=%s",
                config.search_web_depth,
                config.search_web_include_answer,
            )
            if config.search_web_include_domains:
                logging.info(
                    "Web search include domains: %s",
                    ", ".join(config.search_web_include_domains),
                )
            if config.search_web_exclude_domains:
                logging.info(
                    "Web search exclude domains: %s",
                    ", ".join(config.search_web_exclude_domains),
                )
            if seed_urls:
                logging.info("Focused URL search: %s urls", len(seed_urls))
                url_added = 0
                for url in seed_urls[:3]:
                    results = _search_web_tavily(
                        url,
                        max_results=1,
                        config=config,
                        search_depth="basic",
                        include_answer=False,
                    )
                    candidates.extend(results)
                    url_added += len(results)
                    web_added_total += len(results)
                logging.info("Focused URL search added: %s results", url_added)
            added = 0
            for query in queries:
                results = _search_web_tavily(
                    query,
                    max_results=config.search_web_max_per_query,
                    config=config,
                )
                candidates.extend(results)
                added += len(results)
                web_added_total += len(results)
                if added >= config.search_web_max_results:
                    break
            question_queries = _build_question_queries(
                config,
                keyword,
                angle=str(topic.get("angle") or "").strip(),
            )
            if question_queries:
                question_added = 0
                question_limit = max(3, config.search_web_max_results)
                for query in question_queries:
                    results = _search_web_tavily(
                        query,
                        max_results=config.search_web_max_per_query,
                        config=config,
                        search_depth="advanced",
                        include_answer=True,
                    )
                    candidates.extend(results)
                    question_added += len(results)
                    web_added_total += len(results)
                    if question_added >= question_limit:
                        break
                logging.info("Advanced question search added: %s results", question_added)
            logging.info("Web search added: %s results", web_added_total)
    else:
        logging.info("Web search disabled; skipping Tavily search.")
    rss_added_total = 0
    if config.search_rss_enabled:
        queries = _ensure_list_of_strings(research_plan.get("queries"))[:8]
        logging.info("RSS search queries: %s", len(queries))
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
            rss_added_total += len(results)
            if added >= config.search_rss_max_results:
                break
        logging.info("RSS search added: %s results", rss_added_total)
    else:
        logging.info("RSS search disabled; skipping RSS search.")
    pre_dedupe = len(candidates)
    candidates = _dedupe_candidates(candidates)
    logging.info("Candidates after dedupe: %s (from %s)", len(candidates), pre_dedupe)
    sources = _fetch_sources_from_candidates(
        candidates,
        config,
        max_sources=config.max_evidence_sources,
    )
    min_sources = min(3, config.max_evidence_sources)
    logging.info("Sources extracted: %s (min_required=%s)", len(sources), min_sources)
    if len(sources) >= min_sources:
        return sources
    logging.warning(
        "Insufficient sources (%s < %s); running rescue plan.",
        len(sources),
        min_sources,
    )
    rescue = _rescue_research_plan(
        config,
        writer,
        keyword=str(topic.get("keyword") or ""),
        angle=str(topic.get("angle") or ""),
        region=str(topic.get("region") or ""),
    )
    rescue_queries = _ensure_list_of_strings(rescue.get("queries"))[:6]
    if rescue_queries:
        logging.info("Rescue queries: %s", len(rescue_queries))
        rescue_web_added = 0
        rescue_rss_added = 0
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
                rescue_web_added += len(results)
                if added >= config.search_web_max_results:
                    break
            logging.info("Rescue web search added: %s results", rescue_web_added)
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
                rescue_rss_added += len(results)
                if added >= config.search_rss_max_results:
                    break
            logging.info("Rescue RSS search added: %s results", rescue_rss_added)
        candidates = _dedupe_candidates(candidates)
        logging.info("Candidates after rescue dedupe: %s", len(candidates))
        sources = _fetch_sources_from_candidates(
            candidates,
            config,
            max_sources=config.max_evidence_sources,
        )
        logging.info("Sources after rescue: %s", len(sources))
    else:
        logging.warning("Rescue plan returned no queries.")
    return sources


def _extract_structured_sources(
    config: AutomationConfig,
    writer: ClaudeClient,
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
            temperature=config.anthropic_temperature,
            max_tokens=config.anthropic_max_tokens,
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
    writer: ClaudeClient,
    sources: list[dict],
) -> dict:
    sources_json = json.dumps({"sources": sources}, ensure_ascii=True)
    prompt = _build_evidence_builder_prompt(sources_json=sources_json)
    try:
        response = writer.generate(
            prompt,
            temperature=config.anthropic_temperature,
            max_tokens=config.anthropic_max_tokens,
        )
        data = _extract_json_block(response)
    except Exception as exc:
        logging.warning("Evidence builder failed: %s", exc)
        data = None
    if isinstance(data, dict):
        return data
    return {"timeline": [], "claims": [], "open_questions": [], "conflicts": []}


def _discover_daily_market_events(
    config: AutomationConfig,
    writer: ClaudeClient,
    *,
    raw_sources: list[dict],
    window_label: str,
) -> list[dict]:
    if not raw_sources:
        return []
    prompt = _build_daily_event_map_prompt(
        window_label=window_label,
        raw_sources_json=json.dumps(raw_sources, ensure_ascii=True),
    )
    try:
        response = writer.generate(
            prompt,
            temperature=min(config.anthropic_temperature, 0.4),
            max_tokens=config.anthropic_max_tokens,
        )
        data = _extract_json_block(response)
    except Exception as exc:
        logging.warning("Daily event mapping failed: %s", exc)
        data = None
    events = data.get("events") if isinstance(data, dict) else None
    if not isinstance(events, list):
        return []
    normalized: list[dict] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        follow_up = event.get("follow_up_queries") if isinstance(event.get("follow_up_queries"), dict) else {}
        normalized.append(
            {
                "event_id": str(event.get("event_id") or "").strip() or _slugify(str(event.get("title") or "event")),
                "title": str(event.get("title") or "").strip(),
                "summary": str(event.get("summary") or "").strip(),
                "why_now": str(event.get("why_now") or "").strip(),
                "market_relevance": str(event.get("market_relevance") or "").strip(),
                "priority": str(event.get("priority") or "medium").strip() or "medium",
                "affected_lanes": _normalize_affected_lanes(event.get("affected_lanes")),
                "evidence_urls": _ensure_list_of_strings(event.get("evidence_urls")),
                "follow_up_queries": {
                    "stocks": _ensure_list_of_strings(follow_up.get("stocks")),
                    "real_estate": _ensure_list_of_strings(follow_up.get("real_estate")),
                },
            }
        )
    return [event for event in normalized if event.get("title")]


def _select_daily_lane_topic(
    config: AutomationConfig,
    writer: ClaudeClient,
    *,
    lane: str,
    events: list[dict],
    window_label: str,
) -> dict | None:
    if not events:
        return None
    prompt = _build_daily_lane_selector_prompt(
        lane=lane,
        window_label=window_label,
        events_json=json.dumps(events, ensure_ascii=True),
    )
    try:
        response = writer.generate(
            prompt,
            temperature=min(config.anthropic_temperature, 0.4),
            max_tokens=config.anthropic_max_tokens,
        )
        data = _extract_json_block(response)
    except Exception as exc:
        logging.warning("Daily lane selector failed (%s): %s", lane, exc)
        data = None
    if not isinstance(data, dict):
        return None
    allowed_urls = {
        url
        for event in events
        for url in _ensure_list_of_strings(event.get("evidence_urls"))
        if _is_safe_public_url(url)
    }
    evidence_urls = _filter_allowed_source_urls(data.get("source_urls"), allowed_urls=allowed_urls)
    event_lookup = {
        str(event.get("title") or "").strip().lower(): event
        for event in events
        if str(event.get("title") or "").strip()
    }
    matched_event = event_lookup.get(str(data.get("title") or "").strip().lower())
    queries = _normalize_search_queries(data.get("queries"))
    if matched_event:
        queries = _normalize_search_queries([
            *queries,
            *matched_event.get("follow_up_queries", {}).get(lane, []),
        ])
        fallback_urls = _filter_allowed_source_urls(
            matched_event.get("evidence_urls"),
            allowed_urls=allowed_urls,
        )
        if evidence_urls:
            evidence_urls = _filter_allowed_source_urls(
                [*evidence_urls, *fallback_urls],
                allowed_urls=allowed_urls,
            )
        else:
            evidence_urls = fallback_urls
    keyword = str(data.get("keyword") or data.get("title") or "").strip()
    if not keyword:
        return None
    return {
        "keyword": keyword,
        "title": str(data.get("title") or keyword).strip() or keyword,
        "angle": str(data.get("angle") or "").strip(),
        "why_now": str(data.get("why_now") or "").strip(),
        "focus_points": _ensure_list_of_strings(data.get("focus_points")),
        "queries": _normalize_search_queries(queries),
        "source_urls": evidence_urls,
        "risk": str(data.get("risk") or "medium").strip() or "medium",
        "analysis_lane": lane,
        "category_label": DAILY_IMPACT_CATEGORY_LABELS.get(lane, lane),
        "event_summary": str((matched_event or {}).get("summary") or "").strip(),
    }


def _build_outline(
    config: AutomationConfig,
    writer: ClaudeClient,
    *,
    keyword: str,
    angle: str,
    evidence_summary: str,
    template_mode: str | None = None,
) -> dict:
    prompt = _build_outline_prompt(
        keyword=keyword,
        angle=angle,
        evidence_summary=evidence_summary,
        template_mode=template_mode,
    )
    try:
        response = writer.generate(
            prompt,
            temperature=config.anthropic_temperature,
            max_tokens=config.anthropic_max_tokens,
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
    if template_mode == PIPELINE_DAILY_IMPACT:
        fallback_sections = [
            {
                "heading": f"Why the prior day matters for {keyword}",
                "goal": "State the thesis, the event, and why this matters now.",
                "evidence_refs": [],
            },
            {
                "heading": f"How the event flows into {keyword}",
                "goal": "Map the transmission mechanism from event to market effect.",
                "evidence_refs": [],
            },
            {
                "heading": f"The strongest evidence behind the {keyword} view",
                "goal": "Highlight the most important data points, claims, and caveats.",
                "evidence_refs": [],
            },
            {
                "heading": f"Scenarios and risk signals for {keyword}",
                "goal": "Lay out base, upside, downside, and uncertainty.",
                "evidence_refs": [],
            },
            {
                "heading": f"What to watch next for {keyword}",
                "goal": "List concrete signals, releases, or thresholds to monitor next.",
                "evidence_refs": [],
            },
        ]
    elif template_mode == PIPELINE_WEEKLY_MAJOR_EVENTS:
        fallback_sections = [
            {
                "heading": f"Why this week matters for {keyword}",
                "goal": "State the thesis, the event, and why this matters over the coming weeks.",
                "evidence_refs": [],
            },
            {
                "heading": f"How the event flows into {keyword}",
                "goal": "Map the transmission mechanism from event to market effect.",
                "evidence_refs": [],
            },
            {
                "heading": f"The strongest evidence behind the {keyword} view",
                "goal": "Highlight the most important data points, claims, and caveats.",
                "evidence_refs": [],
            },
            {
                "heading": f"Scenarios and risk signals for {keyword}",
                "goal": "Lay out base, upside, downside, and uncertainty.",
                "evidence_refs": [],
            },
            {
                "heading": f"What to watch next for {keyword}",
                "goal": "List concrete signals, releases, or thresholds to monitor next.",
                "evidence_refs": [],
            },
        ]
    return {"title_direction": "", "sections": fallback_sections, "faq": []}


def _allocate_resources(
    config: AutomationConfig,
    writer: ClaudeClient,
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
            temperature=config.anthropic_temperature,
            max_tokens=config.anthropic_max_tokens,
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
    writer: ClaudeClient,
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
                temperature=config.anthropic_temperature,
                max_tokens=config.anthropic_max_tokens,
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
    writer: ClaudeClient,
    *,
    section_mdx_list: list[str],
    faq_list: list[str],
    keyword: str,
    template_mode: str | None = None,
) -> str | None:
    prompt = _build_assembler_prompt(
        section_mdx_list=section_mdx_list,
        faq_list=faq_list,
        tone=config.content_tone,
        keyword=keyword,
        template_mode=template_mode,
    )
    try:
        response = writer.generate(
            prompt,
            temperature=config.anthropic_temperature,
            max_tokens=config.anthropic_max_tokens,
        )
        return response.strip()
    except Exception as exc:
        logging.warning("Assembler failed: %s", exc)
        return None


def _apply_quality_gate(
    config: AutomationConfig,
    writer: ClaudeClient,
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
                temperature=config.anthropic_temperature,
                max_tokens=config.anthropic_max_tokens,
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
                temperature=config.anthropic_temperature,
                max_tokens=config.anthropic_max_tokens,
            )
            content = revised.strip() or content
        except Exception as exc:
            logging.warning("Revision failed: %s", exc)
            return content
    return content


def _apply_final_review(
    config: AutomationConfig,
    writer: ClaudeClient,
    *,
    full_mdx: str,
    keyword: str,
) -> str:
    if not full_mdx or not config.final_review_enabled:
        return full_mdx
    if not config.anthropic_api_key:
        return full_mdx
    content = full_mdx
    hints = _collect_review_hints(content)
    attempts = max(config.final_review_revisions, 0) + 1
    for attempt in range(attempts):
        prompt = _build_final_review_prompt(
            full_mdx=content,
            keyword=keyword,
            language=config.content_language,
            hints=hints,
        )
        try:
            response = writer.generate(
                prompt,
                temperature=min(config.anthropic_temperature, 0.4),
                max_tokens=config.anthropic_max_tokens,
            )
            data = _extract_json_block(response)
        except Exception as exc:
            logging.warning("Final review failed: %s", exc)
            return content
        if not isinstance(data, dict):
            if attempt < attempts - 1:
                continue
            return content
        status = str(data.get("status") or "").strip().lower()
        issues = data.get("issues")
        issue_count = len(issues) if isinstance(issues, list) else 0
        logging.info("Final review status: %s (issues=%s)", status, issue_count)
        if status == "pass":
            return content
        cleaned = str(data.get("cleaned_mdx") or "").strip()
        if status in {"fix", "regenerate"} and cleaned:
            return cleaned
        if attempt >= attempts - 1:
            return content
    return content


def _apply_mdx_render_guard(
    config: AutomationConfig,
    writer: ClaudeClient,
    *,
    full_mdx: str,
) -> str:
    if not full_mdx or not config.mdx_render_guard_enabled:
        return full_mdx
    content = full_mdx
    if config.mdx_render_auto_fix:
        content = _fix_mdx_void_elements(content)
    hints = _collect_mdx_render_hints(content)
    if not hints:
        return content
    if not config.anthropic_api_key:
        return content
    attempts = max(config.mdx_render_guard_revisions, 0) + 1
    for attempt in range(attempts):
        prompt = _build_mdx_render_guard_prompt(full_mdx=content, hints=hints)
        try:
            response = writer.generate(
                prompt,
                temperature=min(config.anthropic_temperature, 0.4),
                max_tokens=config.anthropic_max_tokens,
            )
            data = _extract_json_block(response)
        except Exception as exc:
            logging.warning("MDX render guard failed: %s", exc)
            return content
        if not isinstance(data, dict):
            if attempt < attempts - 1:
                continue
            return content
        status = str(data.get("status") or "").strip().lower()
        issues = data.get("issues")
        issue_count = len(issues) if isinstance(issues, list) else 0
        logging.info("MDX render guard status: %s (issues=%s)", status, issue_count)
        if status == "pass":
            return content
        cleaned = str(data.get("cleaned_mdx") or "").strip()
        if status == "fix" and cleaned:
            content = cleaned
            if config.mdx_render_auto_fix:
                content = _fix_mdx_void_elements(content)
            hints = _collect_mdx_render_hints(content)
            if not hints:
                return content
        if attempt >= attempts - 1:
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
    writer: ClaudeClient,
    *,
    topic: dict,
    pipeline: str | None = None,
) -> dict | None:
    keyword = str(topic.get("keyword") or "").strip()
    if not keyword:
        return None
    template_mode = pipeline if _uses_market_impact_template(pipeline) else None
    default_angle = f"latest developments and impact for {keyword}"
    if template_mode == PIPELINE_DAILY_IMPACT:
        lane = str(topic.get("analysis_lane") or "").strip() or "market"
        default_angle = f"how prior-day developments could affect {lane.replace('_', ' ')} through second-order impacts"
    elif template_mode == PIPELINE_WEEKLY_MAJOR_EVENTS:
        lane = str(topic.get("analysis_lane") or "").strip() or "market"
        default_angle = f"how recent major developments could affect {lane.replace('_', ' ')} over the coming weeks"
    angle = str(topic.get("angle") or "").strip() or default_angle
    provided_plan = topic.get("research_plan") if isinstance(topic.get("research_plan"), dict) else {}
    planned = _plan_research(
        config,
        writer,
        keyword=keyword,
        angle=angle,
        region=str(topic.get("region") or ""),
    )
    research_plan = {
        "queries": _normalize_search_queries(
            [
                *_ensure_list_of_strings(provided_plan.get("queries")),
                *_ensure_list_of_strings(planned.get("queries")),
            ],
            limit=8,
        ),
        "priority_sources": list(
            dict.fromkeys(
                [
                    *_ensure_list_of_strings(provided_plan.get("priority_sources")),
                    *_ensure_list_of_strings(planned.get("priority_sources")),
                ]
            )
        )[:6],
        "must_verify": list(
            dict.fromkeys(
                [
                    *_ensure_list_of_strings(provided_plan.get("must_verify")),
                    *_ensure_list_of_strings(planned.get("must_verify")),
                ]
            )
        )[:8],
    }
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
        template_mode=template_mode,
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
        template_mode=template_mode,
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
    inline_image_prompts: list[str] = []
    if isinstance(resources, dict):
        hero = resources.get("hero_image")
        if isinstance(hero, dict):
            hero_hint = str(hero.get("style_prompt") or "").strip()
            hero_alt = str(hero.get("alt_text") or "").strip()
        for item in resources.get("inline_images") or []:
            if not isinstance(item, dict):
                continue
            prompt = _ensure_ascii_text(str(item.get("prompt_or_query") or "").strip(), "")
            if prompt:
                inline_image_prompts.append(prompt)
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
        "inline_image_prompts": inline_image_prompts[:MAX_GENERATED_INLINE_IMAGES],
    }


def _generate_post_for_topic(
    config: AutomationConfig,
    writer: ClaudeClient,
    meta_writer: ClaudeClient,
    topic: dict,
    pipeline: str | None = None,
) -> Path | None:
    keyword = topic.get("keyword")
    if not keyword:
        return None
    forced_category = str(topic.get("category_label") or "").strip()
    forced_tags = DAILY_IMPACT_TAG_HINTS.get(str(topic.get("analysis_lane") or "").strip(), [])

    image_urls = _extract_image_urls(topic)
    urls = _extract_urls(topic)
    alt_text = _ensure_ascii_text(f"{keyword} related image", "Related image")
    image_infos = _describe_image_urls(config, writer, image_urls)
    fallback_body = ""
    summary = ""
    key_points: list[str] = []
    image_prompt_hint = keyword
    body = ""
    hero_alt_hint = ""
    reference_urls = list(urls)

    template_mode = pipeline if _uses_market_impact_template(pipeline) else None
    inline_image_prompts: list[str] = []
    chart_specs: list[dict] = []
    if config.use_multi_agent:
        article = _generate_article_multi_agent(
            config,
            writer,
            topic=topic,
            pipeline=pipeline,
        )
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
            inline_image_prompts = _ensure_list_of_strings(article.get("inline_image_prompts"))

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
            angle=str(topic.get("angle") or "").strip() or None,
            traffic=topic.get("traffic"),
            sources=sources,
            image_urls=image_urls,
            image_infos=image_infos,
            references=urls,
            language=config.content_language,
            template_mode=template_mode,
        )

        try:
            response = writer.generate(
                content_prompt,
                temperature=config.anthropic_temperature,
                max_tokens=config.anthropic_max_tokens,
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

    if image_infos:
        body = _insert_images_by_relevance(body, image_infos)
    body = _ensure_images_in_body(body, image_urls, alt_text)
    body = _clean_body_text(body)
    body = _ensure_ascii_body(body, fallback_body or body)
    reviewed_body = _apply_final_review(
        config,
        writer,
        full_mdx=body,
        keyword=str(keyword),
    )
    if reviewed_body != body:
        body = _clean_body_text(reviewed_body)
        body = _ensure_ascii_body(body, body)
        summary = ""
    mdx_checked_body = _apply_mdx_render_guard(
        config,
        writer,
        full_mdx=body,
    )
    if mdx_checked_body != body:
        body = _clean_body_text(mdx_checked_body)
        body = _ensure_ascii_body(body, body)
        summary = ""

    if not summary:
        summary = _ensure_ascii_text(
            _first_sentences(_strip_markdown(body), count=2, max_len=200),
            "Trend summary of the topic.",
        )

    if _uses_market_impact_template(template_mode):
        chart_specs = _plan_inline_charts(
            config,
            writer,
            keyword=str(keyword),
            angle=str(topic.get("angle") or "").strip() or str(keyword),
            summary=summary,
            key_points=key_points,
            body=body,
        )
        if not chart_specs:
            chart_specs = [
                _fallback_daily_impact_chart_spec(
                    keyword=str(keyword),
                    summary=summary,
                    key_points=key_points,
                    body=body,
                )
            ]

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
            temperature=config.anthropic_temperature,
            max_tokens=config.anthropic_max_tokens,
        )
        meta_data = _extract_json_block(meta_response)
    except Exception as exc:
        logging.warning("Frontmatter LLM failed for %s: %s", keyword, exc)
        meta_data = None

    fallback_category = _ensure_ascii_text(config.fallback_category, "trend")
    fallback_tags = [
        _ensure_ascii_text(tag, "topic") for tag in config.fallback_tags if tag
    ] or ["topic"]
    if forced_tags:
        fallback_tags = list(dict.fromkeys([*forced_tags, *fallback_tags]))
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
        category_list = [forced_category] if forced_category else _normalize_category_list(
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
        default_title = f"{keyword} trend summary"
        if forced_category:
            default_title = f"{keyword}: what it means for {forced_category}"
        title = _ensure_ascii_text(default_title, "Trend summary")
        description = _ensure_ascii_text(
            summary or f"Key updates and context around {keyword}.",
            "Key updates and context around the topic.",
        )
        category_list = [forced_category] if forced_category else [fallback_category]
        tags_list = fallback_tags
        hero_alt = _ensure_ascii_text(f"{keyword} hero image", "Hero image")
        image_prompt = _ensure_ascii_text(
            image_prompt_hint or f"{keyword} concept illustration",
            "Concept illustration",
        )

    inline_image_prompts = [
        _ensure_ascii_text(prompt, "")
        for prompt in inline_image_prompts
        if _ensure_ascii_text(prompt, "")
    ]

    if _uses_market_impact_template(template_mode) and not inline_image_prompts:
        base_angle = _ensure_ascii_text(str(topic.get("angle") or "").strip(), "")
        inline_image_prompts = [
            _ensure_ascii_text(
                f"Editorial-style market illustration for {title}. Focus on {base_angle or keyword}. No text, no logos.",
                "Market illustration without text",
            )
        ]

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
        chart_specs=chart_specs,
        inline_image_prompts=inline_image_prompts,
        slug_hint=title,
        date_str=str(topic.get("publish_date") or "").strip() or None,
    )


def _save_trends_snapshot(payload: dict, *, content_timezone: ZoneInfo) -> None:
    timestamp = datetime.now(content_timezone).strftime("%Y-%m-%d-%H%M%S")
    path = ROOT_DIR / "data" / "trends" / f"{timestamp}-trends.json"
    write_json(path, payload)


def _build_daily_impact_discovery_queries(window_labels: dict[str, str]) -> list[str]:
    display_date = window_labels.get("display_date") or window_labels.get("target_date") or ""
    queries: list[str] = []
    for topic in DAILY_IMPACT_DISCOVERY_TOPICS:
        queries.append(f"{display_date} {topic} United States market impact")
        queries.append(f"{display_date} {topic} stocks housing real estate")
    return list(dict.fromkeys(query for query in queries if query))


def _build_gemini_grounded_daily_discovery_prompt(*, window_label: str, queries: list[str]) -> str:
    system = """
You are a US macro and market events editor using Google Search grounding.
Search the web for prior-day developments that could plausibly move US stocks or US real estate.
Do not write the article. Your job is to surface the best discovery coverage for the next stage.
Focus on recency, market transmission mechanisms, and source quality.
""".strip()
    query_block = "\n".join(f"- {query}" for query in queries if query.strip())
    user = f"""
Time window: {window_label}

Priority discovery queries:
{query_block or '- none'}

Instructions:
- Use Google Search grounding to identify the most relevant prior-day US market developments.
- Prioritize authoritative reporting, official statements, and high-signal market coverage.
- Cover both US stocks and US real estate when relevant.
- Keep the answer concise. The system will use your grounded citations/URLs as discovery candidates.
""".strip()
    return _compose_prompt(system, user)


def _extract_gemini_grounded_candidates(response_data: dict) -> list[dict]:
    candidates: list[dict] = []
    seen: set[str] = set()
    raw_candidates = response_data.get("candidates")
    if not isinstance(raw_candidates, list):
        return []
    for candidate in raw_candidates:
        if not isinstance(candidate, dict):
            continue
        metadata = candidate.get("groundingMetadata") or candidate.get("grounding_metadata")
        if not isinstance(metadata, dict):
            continue
        chunks = metadata.get("groundingChunks") or metadata.get("grounding_chunks") or []
        if not isinstance(chunks, list):
            continue
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            web = chunk.get("web") if isinstance(chunk.get("web"), dict) else {}
            url = str(web.get("uri") or web.get("url") or "").strip()
            if not _is_safe_public_url(url):
                continue
            normalized = _normalize_url_for_dedupe(url)
            if normalized in seen:
                continue
            seen.add(normalized)
            title = str(web.get("title") or chunk.get("title") or "").strip() or "Untitled"
            candidates.append(
                {
                    "url": url,
                    "title": title,
                    "publisher": urlparse(url).netloc,
                    "published_at": None,
                    "origin": "gemini_google_search",
                    "snippet": "",
                }
            )
    return candidates


def _validate_grounded_candidates_against_daily_window(
    grounded_candidates: list[dict],
    *,
    config: AutomationConfig,
    queries: list[str],
    region: str,
    language: str,
    window_start: datetime,
    window_end: datetime,
    web_limit: int,
    rss_limit: int,
) -> list[dict]:
    if not grounded_candidates:
        return []
    validated_candidates = _collect_candidates_for_queries(
        config,
        queries=queries,
        region=region,
        language=language,
        window_start=window_start,
        window_end=window_end,
        web_limit=web_limit,
        rss_limit=rss_limit,
    )
    if not validated_candidates:
        return []
    candidate_lookup = {
        _normalize_url_for_dedupe(str(candidate.get("url") or "")): candidate
        for candidate in validated_candidates
        if _is_safe_public_url(candidate.get("url"))
    }
    merged: list[dict] = []
    for grounded in grounded_candidates:
        if not _is_allowed_search_domain(grounded.get("url"), config):
            continue
        normalized = _normalize_url_for_dedupe(str(grounded.get("url") or ""))
        validated = candidate_lookup.get(normalized)
        if not validated:
            continue
        merged.append(
            {
                **validated,
                "origin": "gemini_google_search",
            }
        )
    return merged


def _gather_grounded_daily_discovery_sources(
    config: AutomationConfig,
    writer: ClaudeClient,
    *,
    queries: list[str],
    region: str,
    language: str,
    window_start: datetime,
    window_end: datetime,
    window_label: str,
    max_sources: int | None = None,
    min_sources: int = 1,
    web_limit: int | None = None,
    rss_limit: int | None = None,
) -> list[dict]:
    if not config.gemini_grounded_daily_discovery:
        return []
    if not config.gemini_api_key:
        logging.info("Gemini grounded daily discovery skipped because GEMINI_API_KEY is missing.")
        return []
    prompt = _build_gemini_grounded_daily_discovery_prompt(
        window_label=window_label,
        queries=queries,
    )
    try:
        _, response_data = writer.generate_with_google_search(
            prompt,
            temperature=min(config.anthropic_temperature, 0.3),
            max_tokens=config.anthropic_max_tokens,
        )
    except Exception as exc:
        logging.warning("Gemini grounded daily discovery failed: %s", exc)
        return []
    candidates = _extract_gemini_grounded_candidates(response_data)
    if not candidates:
        logging.warning("Gemini grounded daily discovery returned no usable citation URLs.")
        return []
    candidates = _validate_grounded_candidates_against_daily_window(
        candidates,
        config=config,
        queries=queries,
        region=region,
        language=language,
        window_start=window_start,
        window_end=window_end,
        web_limit=web_limit or max(config.search_web_max_results * 2, 8),
        rss_limit=rss_limit or max(config.search_rss_max_results * 2, 8),
    )
    if not candidates:
        logging.warning(
            "Gemini grounded daily discovery returned no candidates that passed daily window/domain validation."
        )
        return []
    sources = _fetch_sources_from_candidates(candidates, config, max_sources=max_sources)
    if len(sources) < max(1, min_sources):
        logging.warning("Gemini grounded daily discovery citations could not be hydrated into sources.")
        return []
    return sources


def _build_weekly_major_events_discovery_queries(window_labels: dict[str, str]) -> list[str]:
    date_range = window_labels.get("display_range") or window_labels.get("window_start") or ""
    queries: list[str] = []
    for topic in DAILY_IMPACT_DISCOVERY_TOPICS:
        queries.append(f"{date_range} {topic} United States stocks outlook")
        queries.append(f"{date_range} {topic} United States real estate outlook")
        queries.append(f"{date_range} {topic} macro impact on markets and housing")
    return list(dict.fromkeys(query for query in queries if query))


def _normalize_weekly_major_topics(
    raw_topics: list[dict],
    *,
    week_labels: dict[str, str],
    per_lane_limit: int,
    allowed_urls: set[str],
) -> list[dict]:
    normalized: list[dict] = []
    lane_counts = {lane: 0 for lane in MARKET_ANALYSIS_LANES}
    seen: set[tuple[str, str]] = set()
    for item in raw_topics:
        if not isinstance(item, dict):
            continue
        raw_lane = str(item.get("lane") or item.get("analysis_lane") or "").strip().lower()
        if raw_lane in {"stocks", "stock"}:
            lanes = ["stocks"]
        elif raw_lane in {"real_estate", "real-estate", "realestate"}:
            lanes = ["real_estate"]
        else:
            continue
        keyword = str(item.get("keyword") or item.get("title") or "").strip()
        if not keyword:
            continue
        title = str(item.get("title") or keyword).strip() or keyword
        angle = str(item.get("angle") or "").strip()
        why_now = str(item.get("why_now") or "").strip()
        focus_points = _ensure_list_of_strings(item.get("focus_points"))
        queries = _normalize_search_queries(item.get("queries"))
        source_urls = _filter_allowed_source_urls(item.get("source_urls"), allowed_urls=allowed_urls)
        risk = str(item.get("risk") or "medium").strip() or "medium"
        for lane in lanes:
            if lane_counts.get(lane, 0) >= per_lane_limit:
                continue
            topic_key = (lane, _normalize_keyword(keyword))
            if topic_key in seen:
                continue
            seen.add(topic_key)
            lane_counts[lane] = lane_counts.get(lane, 0) + 1
            normalized.append(
                {
                    "keyword": keyword,
                    "title": title,
                    "angle": angle,
                    "why_now": why_now,
                    "focus_points": focus_points,
                    "queries": queries,
                    "source_urls": source_urls,
                    "risk": risk,
                    "analysis_lane": lane,
                    "category_label": DAILY_IMPACT_CATEGORY_LABELS.get(lane, lane),
                    "region": "US",
                    "research_plan": {
                        "queries": queries,
                        "priority_sources": [],
                        "must_verify": focus_points,
                    },
                    "pipeline_window_label": week_labels["window_summary"],
                    "pipeline_date": week_labels["week_key"],
                    "publish_date": week_labels["publish_date"],
                }
            )
    return normalized


def _should_run_daily_impact_now(config: AutomationConfig) -> bool:
    env = _resolve_env()
    if not _parse_bool(env.get("ENFORCE_LOCAL_RUN_HOUR"), False):
        return True
    target_hour = _parse_int(env.get("DAILY_IMPACT_RUN_HOUR"), 8)
    now_local = datetime.now(config.content_timezone)
    return now_local.hour == target_hour


def _should_run_weekly_major_events_now(config: AutomationConfig) -> bool:
    env = _resolve_env()
    if not _parse_bool(env.get("ENFORCE_LOCAL_RUN_HOUR"), False):
        return True
    now_local = datetime.now(config.content_timezone)
    return (
        now_local.weekday() == config.weekly_major_events_run_weekday
        and now_local.hour == config.weekly_major_events_run_hour
    )


def run_daily_impact(
    config: AutomationConfig,
    *,
    publish_date: date | None = None,
    force: bool = False,
) -> None:
    if not force and not _should_run_daily_impact_now(config):
        logging.info("Daily impact pipeline skipped due to local-hour guard.")
        return
    config = replace(
        config,
        content_language="English",
        content_tone="analytical, evidence-driven, plainspoken",
        youtube_search_enabled=False,
    )
    resolved_publish_date = publish_date or datetime.now(config.content_timezone).date()
    window_start, window_end = _previous_day_window(
        config.content_timezone,
        publish_date=resolved_publish_date,
    )
    window_labels = _build_window_labels(
        window_start,
        window_end,
        publish_date=resolved_publish_date,
    )
    state = _load_state()
    completed_runs = set(_ensure_list_of_strings(state.get("daily_impact_runs")))
    if window_labels["target_date"] in completed_runs:
        logging.info("Daily impact already completed for %s", window_labels["target_date"])
        return
    writer = ClaudeClient(
        config.anthropic_api_key,
        config.anthropic_model_content,
        config.anthropic_timeout_sec,
    )
    discovery_queries = _build_daily_impact_discovery_queries(window_labels)
    discovery_max_sources = max(config.max_evidence_sources * 2, 8)
    discovery_sources = _gather_grounded_daily_discovery_sources(
        config,
        writer,
        queries=discovery_queries,
        region="US",
        language=config.content_language,
        window_start=window_start,
        window_end=window_end,
        window_label=window_labels["window_summary"],
        max_sources=discovery_max_sources,
        min_sources=min(3, discovery_max_sources),
        web_limit=max(config.search_web_max_results * 2, 8),
        rss_limit=max(config.search_rss_max_results * 2, 8),
    )
    if discovery_sources:
        logging.info("Daily impact using Gemini-grounded discovery sources: %s", len(discovery_sources))
    else:
        discovery_sources = _gather_raw_sources_for_queries(
            config,
            queries=discovery_queries,
            region="US",
            language=config.content_language,
            window_start=window_start,
            window_end=window_end,
            max_sources=discovery_max_sources,
            web_limit=max(config.search_web_max_results * 2, 8),
            rss_limit=max(config.search_rss_max_results * 2, 8),
        )
    if not discovery_sources:
        logging.warning("Daily impact pipeline found no discovery sources for %s", window_labels["display_date"])
        return
    events = _discover_daily_market_events(
        config,
        writer,
        raw_sources=discovery_sources,
        window_label=window_labels["window_summary"],
    )
    if not events:
        logging.warning("Daily impact pipeline could not derive market events.")
        return
    payload = {
        "pipeline": PIPELINE_DAILY_IMPACT,
        "window": window_labels,
        "events": events,
    }
    _save_trends_snapshot(payload, content_timezone=config.content_timezone)
    topics: list[dict] = []
    for lane in MARKET_ANALYSIS_LANES:
        lane_events = [event for event in events if lane in _normalize_affected_lanes(event.get("affected_lanes"))]
        if not lane_events:
            logging.info("Daily impact classification produced no %s topic candidates.", lane)
            continue
        selected = _select_daily_lane_topic(
            config,
            writer,
            lane=lane,
            events=lane_events,
            window_label=window_labels["window_summary"],
        )
        if not selected:
            logging.warning("Daily impact pipeline could not select a %s topic.", lane)
            continue
        follow_up_queries = _normalize_search_queries([
            *selected.get("queries", []),
            *[f"{selected['title']} {point}" for point in selected.get("focus_points", [])],
        ])
        selected["region"] = "US"
        selected["research_plan"] = {
            "queries": follow_up_queries,
            "priority_sources": [],
            "must_verify": selected.get("focus_points", []),
        }
        selected["pipeline_window_label"] = window_labels["window_summary"]
        selected["pipeline_date"] = window_labels["target_date"]
        selected["publish_date"] = window_labels["publish_date"]
        topics.append(selected)
    if not topics:
        logging.warning("Daily impact pipeline produced no publishable topics.")
        return
    _process_topics(
        config,
        topics=topics,
        pipeline=PIPELINE_DAILY_IMPACT,
    )
    state = _load_state()
    completed_runs = set(_ensure_list_of_strings(state.get("daily_impact_runs")))
    completed_runs.add(window_labels["target_date"])
    state["daily_impact_runs"] = sorted(completed_runs)
    _save_state(state)


def run_weekly_major_events(
    config: AutomationConfig,
    *,
    publish_date: date | None = None,
    force: bool = False,
) -> None:
    if not config.openai_api_key and not config.anthropic_api_key:
        logging.warning(
            "Weekly major-events pipeline skipped because neither OPENAI_API_KEY nor GEMINI_API_KEY is set."
        )
        return
    if not force and not _should_run_weekly_major_events_now(config):
        logging.info("Weekly major-events pipeline skipped due to local schedule guard.")
        return
    resolved_publish_date = publish_date or datetime.now(config.content_timezone).date()
    window_start, window_end = _previous_week_window(
        config.content_timezone,
        publish_date=resolved_publish_date,
    )
    week_labels = _build_weekly_window_labels(
        window_start,
        window_end,
        publish_date=resolved_publish_date,
    )
    state = _load_state()
    completed_runs = set(_ensure_list_of_strings(state.get("weekly_major_runs")))
    if week_labels["week_key"] in completed_runs:
        logging.info("Weekly major-events pipeline already completed for %s", week_labels["week_key"])
        return
    config = replace(
        config,
        content_language="English",
        content_tone="analytical, evidence-driven, plainspoken",
        youtube_search_enabled=False,
    )
    discovery_queries = _build_weekly_major_events_discovery_queries(week_labels)
    discovery_sources = _gather_raw_sources_for_queries(
        config,
        queries=discovery_queries,
        region="US",
        language=config.content_language,
        window_start=window_start,
        window_end=window_end,
        max_sources=max(config.max_evidence_sources * 3, 12),
        web_limit=max(config.search_web_max_results * 3, 12),
        rss_limit=max(config.search_rss_max_results * 3, 12),
    )
    if not discovery_sources:
        logging.warning("Weekly major-events pipeline found no discovery sources for %s", week_labels["display_range"])
        return
    prompt_instructions, prompt_input = _build_weekly_major_events_prompt(
        window_label=week_labels["display_range"],
        topics_per_lane=config.weekly_major_events_per_lane,
        raw_sources_json=json.dumps(discovery_sources, ensure_ascii=True),
    )
    if config.openai_api_key:
        weekly_writer = OpenAIResponsesClient(
            config.openai_api_key,
            config.openai_weekly_model,
            min(config.anthropic_timeout_sec, 120),
        )
        try:
            response = weekly_writer.generate(
                instructions=prompt_instructions,
                input_text=prompt_input,
            )
            data = _extract_json_block(response)
        except Exception as exc:
            logging.warning("Weekly major-events discovery failed: %s", exc)
            return
    else:
        logging.info("Weekly major-events discovery falling back to Gemini (no OPENAI_API_KEY).")
        gemini_writer = ClaudeClient(
            config.anthropic_api_key,
            config.anthropic_model_content,
            config.anthropic_timeout_sec,
        )
        try:
            response = gemini_writer.generate(
                _compose_prompt(prompt_instructions, prompt_input),
                temperature=config.anthropic_temperature,
                max_tokens=config.anthropic_max_tokens,
            )
            data = _extract_json_block(response)
        except Exception as exc:
            logging.warning("Weekly major-events Gemini discovery failed: %s", exc)
            return
    topics = _normalize_weekly_major_topics(
        data.get("topics") if isinstance(data, dict) and isinstance(data.get("topics"), list) else [],
        week_labels=week_labels,
        per_lane_limit=config.weekly_major_events_per_lane,
        allowed_urls={
            str(source.get("url") or "").strip()
            for source in discovery_sources
            if _is_safe_public_url(str(source.get("url") or "").strip())
        },
    )
    if not topics:
        logging.warning("Weekly major-events pipeline produced no publishable topics.")
        return
    _save_trends_snapshot(
        {
            "pipeline": PIPELINE_WEEKLY_MAJOR_EVENTS,
            "window": week_labels,
            "discovery_queries": discovery_queries,
            "discovery_sources": discovery_sources,
            "topics": topics,
        },
        content_timezone=config.content_timezone,
    )
    _process_topics(
        config,
        topics=topics,
        pipeline=PIPELINE_WEEKLY_MAJOR_EVENTS,
    )
    state = _load_state()
    completed_runs = set(_ensure_list_of_strings(state.get("weekly_major_runs")))
    completed_runs.add(week_labels["week_key"])
    state["weekly_major_runs"] = sorted(completed_runs)
    _save_state(state)


def _process_topics(
    config: AutomationConfig,
    *,
    topics: list[dict],
    pipeline: str | None = None,
) -> None:
    if not topics:
        logging.info("No topics found.")
        return

    state = _load_state()
    used = set(state.get("topics") or state.get("keywords") or [])
    slugs = set(state.get("slugs", []))

    writer = ClaudeClient(
        config.anthropic_api_key,
        config.anthropic_model_content,
        config.anthropic_timeout_sec,
    )
    meta_writer = ClaudeClient(
        config.anthropic_api_key,
        config.anthropic_model_meta,
        config.anthropic_timeout_sec,
    )
    for topic in topics:
        keyword = topic.get("keyword")
        if not keyword:
            continue
        normalized = _normalize_keyword(str(keyword))
        if pipeline in {PIPELINE_DAILY_IMPACT, PIPELINE_WEEKLY_MAJOR_EVENTS}:
            publish_key = str(
                topic.get("publish_date")
                or topic.get("pipeline_date")
                or topic.get("pipeline_key")
                or ""
            ).strip()
            topic_key = f"{publish_key}:{topic.get('region', '')}:{normalized}"
        else:
            topic_key = f"{topic.get('region', '')}:{normalized}"
        if topic_key in used:
            logging.info("Skip already processed topic: %s", topic_key)
            continue
        post_path = _generate_post_for_topic(
            config,
            writer,
            meta_writer,
            topic,
            pipeline=pipeline,
        )
        if post_path:
            used.add(topic_key)
            slugs.add(post_path.stem)
            state["topics"] = sorted(used)
            state["slugs"] = sorted(slugs)
            _save_state(state)
            logging.info("Post created: %s", post_path)

    state["topics"] = sorted(used)
    state["slugs"] = sorted(slugs)
    _save_state(state)


def run_pipeline(
    config: AutomationConfig,
    *,
    pipeline: str,
    publish_date: date | None = None,
    force: bool = False,
) -> None:
    if pipeline == PIPELINE_DAILY_IMPACT:
        run_daily_impact(config, publish_date=publish_date, force=force)
        return
    if pipeline == PIPELINE_WEEKLY_MAJOR_EVENTS:
        run_weekly_major_events(config, publish_date=publish_date, force=force)
        return
    raise ValueError(f"Unsupported pipeline: {pipeline}")


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )


def _parse_publish_date(raw: str, *, content_timezone: ZoneInfo) -> date:
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise SystemExit(f"Invalid --publish-date {raw!r}. Use YYYY-MM-DD.") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Automate trend-based blog posts.")
    parser.add_argument("--once", action="store_true", help="Run only once")
    parser.add_argument(
        "--pipeline",
        default=PIPELINE_DAILY_IMPACT,
        choices=PIPELINE_CHOICES,
        help="Pipeline mode",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level",
    )
    parser.add_argument(
        "--publish-date",
        default="",
        help="Publication date anchor for scheduled runs (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--backfill-days",
        type=int,
        default=0,
        help="Run daily-impact for the last N publication dates ending today",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    _configure_logging(args.log_level)
    config = _build_config()
    logging.info(
        "Automation start (pipeline=%s, interval=%s hours)",
        args.pipeline,
        config.interval_hours,
    )

    if args.once:
        publish_date = None
        if args.publish_date:
            publish_date = _parse_publish_date(args.publish_date, content_timezone=config.content_timezone)
        if args.pipeline == PIPELINE_DAILY_IMPACT and args.backfill_days > 0:
            total_days = max(1, args.backfill_days)
            today_local = datetime.now(config.content_timezone).date()
            for offset in range(total_days - 1, -1, -1):
                run_pipeline(
                    config,
                    pipeline=args.pipeline,
                    publish_date=today_local - timedelta(days=offset),
                    force=True,
                )
            return 0
        run_pipeline(
            config,
            pipeline=args.pipeline,
            publish_date=publish_date,
            force=bool(publish_date),
        )
        return 0

    while True:
        run_pipeline(config, pipeline=args.pipeline)
        sleep_seconds = max(config.interval_hours, 0.1) * 3600
        logging.info("Sleeping for %s seconds", sleep_seconds)
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
