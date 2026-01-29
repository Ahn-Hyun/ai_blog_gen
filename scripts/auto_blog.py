from __future__ import annotations

import argparse
import base64
import binascii
import colorsys
import http.client
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
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
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
DEFAULT_MAX_SOURCE_CHARS = 10000
DEFAULT_MAX_TOTAL_SOURCE_CHARS = 40000
DEFAULT_SCRAPE_TIMEOUT = 12
DEFAULT_SCRAPE_DELAY_SEC = 1.0
DEFAULT_SCRAPE_MAX_RETRIES = 2
DEFAULT_SCRAPE_BACKOFF_SEC = 5.0
DEFAULT_GEMINI_MODEL = "gemini-3-pro-preview"
DEFAULT_GEMINI_MODEL_CONTENT = DEFAULT_GEMINI_MODEL
DEFAULT_GEMINI_MODEL_META = DEFAULT_GEMINI_MODEL
DEFAULT_GEMINI_TEMPERATURE = 0.6
DEFAULT_GEMINI_MAX_TOKENS = 60000
DEFAULT_GEMINI_TIMEOUT_SEC = 900
DEFAULT_BLOG_DOMAIN = "blog.ship-write.com"
DEFAULT_CONTENT_LANGUAGE = "English"
DEFAULT_CONTENT_TONE = "neutral, informative"
DEFAULT_CONTENT_TIMEZONE = "America/New_York"
DEFAULT_USE_MULTI_AGENT = True
DEFAULT_SEARCH_RSS_ENABLED = True
DEFAULT_SEARCH_RSS_MAX_RESULTS = 4
DEFAULT_SEARCH_RSS_MAX_PER_QUERY = 3
DEFAULT_MAX_EVIDENCE_SOURCES = 4
DEFAULT_QUALITY_GATE_REVISIONS = 0
DEFAULT_FINAL_REVIEW_ENABLED = True
DEFAULT_FINAL_REVIEW_REVISIONS = 2
DEFAULT_SEARCH_WEB_ENABLED = True
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
DEFAULT_GRADIENT_WIDTH = 1600
DEFAULT_GRADIENT_HEIGHT = 900
DEFAULT_GRADIENT_JPEG_QUALITY = 90
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
PIPELINE_RECENT = "recent"
PIPELINE_HIGH_INTENT = "high-intent"
PIPELINE_CHOICES = (PIPELINE_RECENT, PIPELINE_HIGH_INTENT)
TRENDSPYG_CATEGORIES = {
    "all",
    "autos",
    "beauty",
    "business",
    "climate",
    "entertainment",
    "food",
    "games",
    "health",
    "hobbies",
    "jobs",
    "law",
    "other",
    "pets",
    "politics",
    "science",
    "shopping",
    "sports",
    "technology",
    "travel",
}
HIGH_INTENT_CATEGORY_ALIASES = {
    "b2b": "business",
    "b2b_saas": "business",
    "business_industrial": "business",
    "business_and_industrial": "business",
    "saas": "business",
    "enterprise": "business",
    "finance": "business",
    "finance_insurance": "business",
    "insurance": "business",
    "banking": "business",
    "career": "jobs",
    "education": "jobs",
    "career_education": "jobs",
    "jobs_education": "jobs",
    "jobs_and_education": "jobs",
    "jobs": "jobs",
    "tech": "technology",
    "technology": "technology",
    "computers_electronics": "technology",
    "internet_telecom": "technology",
    "software": "technology",
    "developer_tools": "technology",
    "productivity_software": "technology",
}
DEFAULT_HIGH_INTENT_CATEGORY_HINTS = (
    "b2b_saas",
    "finance_insurance",
    "career_education",
    "tech",
)
HIGH_INTENT_RSS_CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "business": (
        "saas",
        "b2b",
        "enterprise",
        "subscription",
        "pricing",
        "invoice",
        "billing",
        "payroll",
        "crm",
        "erp",
        "procurement",
        "vendor",
        "fintech",
        "bank",
        "banking",
        "loan",
        "mortgage",
        "insurance",
        "insurer",
        "policy",
        "premium",
        "credit",
        "credit card",
        "debit",
        "broker",
        "investment",
        "investor",
        "stock",
        "stocks",
        "bond",
        "bonds",
        "treasury",
        "yield",
        "rate",
        "interest",
        "forex",
        "currency",
        "dollar",
        "index",
        "cpi",
        "inflation",
        "earnings",
        "guidance",
        "ipo",
        "sec",
    ),
    "jobs": (
        "job",
        "jobs",
        "hiring",
        "hire",
        "recruit",
        "recruiting",
        "recruiter",
        "resume",
        "cv",
        "interview",
        "salary",
        "compensation",
        "layoff",
        "layoffs",
        "unemployment",
        "career",
        "internship",
        "degree",
        "university",
        "college",
        "school",
        "tuition",
        "scholarship",
        "course",
        "bootcamp",
        "certification",
        "training",
        "exam",
        "admission",
        "student",
    ),
    "technology": (
        "software",
        "app",
        "application",
        "api",
        "cloud",
        "aws",
        "azure",
        "gcp",
        "google cloud",
        "microsoft",
        "github",
        "gitlab",
        "docker",
        "kubernetes",
        "devops",
        "cybersecurity",
        "security",
        "sso",
        "identity",
        "database",
        "analytics",
        "ai",
        "machine learning",
        "ml",
        "llm",
        "model",
        "sdk",
        "framework",
        "programming",
        "developer",
        "code",
        "ide",
        "automation",
        "platform",
        "tool",
        "tools",
        "integration",
        "data",
    ),
}
HIGH_INTENT_TEMPLATE_REQUIREMENTS = """
Template requirements (fixed order):
1) Add a "TL;DR" section after the intro (3-5 bullet points max).
2) Add a "Comparison table" section using a Markdown table.
   Columns: Option, Best for, Pros, Cons, Pricing/Cost (use "unknown" if not supported).
3) Add a "Pros and cons" section with two bullet lists.
4) Add a "FAQ" section (2-4 Q/A).

Rules:
- Use only facts supported by the provided sources and citations.
- Reuse existing citations inside the table or bullets when possible.
- If evidence is thin, label details as "unknown" or "varies" and avoid numbers.
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
    gemini_model: str
    gemini_model_content: str
    gemini_model_meta: str
    gemini_temperature: float
    gemini_max_tokens: int
    gemini_timeout_sec: int
    blog_domain: str
    content_language: str
    content_tone: str
    content_timezone: ZoneInfo
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
    if not gemini_api_key and google_api_key:
        gemini_api_key = google_api_key
    if not google_api_key and gemini_api_key:
        google_api_key = gemini_api_key
    gemini_model = env.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
    gemini_model_content = env.get("GEMINI_MODEL_CONTENT", gemini_model)
    gemini_model_meta = env.get("GEMINI_MODEL_META", gemini_model)
    gemini_temperature = _parse_float(env.get("GEMINI_TEMPERATURE"), DEFAULT_GEMINI_TEMPERATURE)
    gemini_max_tokens = _parse_int(env.get("GEMINI_MAX_TOKENS"), DEFAULT_GEMINI_MAX_TOKENS)
    gemini_timeout_sec = _parse_int(env.get("GEMINI_TIMEOUT_SEC"), DEFAULT_GEMINI_TIMEOUT_SEC)
    if gemini_timeout_sec <= 0:
        gemini_timeout_sec = DEFAULT_GEMINI_TIMEOUT_SEC
    if gemini_timeout_sec > DEFAULT_GEMINI_TIMEOUT_SEC:
        gemini_timeout_sec = DEFAULT_GEMINI_TIMEOUT_SEC

    blog_domain = env.get("BLOG_DOMAIN", DEFAULT_BLOG_DOMAIN)
    if not blog_domain.startswith("http"):
        blog_domain = f"https://{blog_domain}"
    content_language = env.get("CONTENT_LANGUAGE", DEFAULT_CONTENT_LANGUAGE).strip() or DEFAULT_CONTENT_LANGUAGE
    content_tone = env.get("CONTENT_TONE", DEFAULT_CONTENT_TONE).strip() or DEFAULT_CONTENT_TONE
    content_timezone = _resolve_timezone(env)
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
    final_review_enabled = _parse_bool(
        env.get("FINAL_REVIEW_ENABLED"),
        DEFAULT_FINAL_REVIEW_ENABLED,
    )
    final_review_revisions = _parse_int(
        env.get("FINAL_REVIEW_REVISIONS"),
        DEFAULT_FINAL_REVIEW_REVISIONS,
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
        gemini_model=gemini_model,
        gemini_model_content=gemini_model_content,
        gemini_model_meta=gemini_model_meta,
        gemini_temperature=gemini_temperature,
        gemini_max_tokens=gemini_max_tokens,
        gemini_timeout_sec=gemini_timeout_sec,
        blog_domain=blog_domain.rstrip("/"),
        content_language=content_language,
        content_tone=content_tone,
        content_timezone=content_timezone,
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
        google_image_enabled=google_image_enabled,
        google_image_model=google_image_model or DEFAULT_GOOGLE_IMAGE_MODEL,
        google_image_aspect_ratio=google_image_aspect_ratio or DEFAULT_GOOGLE_IMAGE_ASPECT_RATIO,
    )


def _build_high_intent_settings(config: AutomationConfig) -> dict[str, object]:
    env = _resolve_env()
    regions = _parse_list(env.get("HIGH_INTENT_REGIONS"), ["US"])
    trend_source = env.get("HIGH_INTENT_TREND_SOURCE", "rss").strip().lower() or "rss"
    trend_method = env.get("HIGH_INTENT_TREND_METHOD", DEFAULT_TREND_METHOD).strip() or DEFAULT_TREND_METHOD
    trend_window_hours = _parse_int(env.get("HIGH_INTENT_TREND_WINDOW_HOURS"), 24)
    csv_sort_by = env.get("HIGH_INTENT_CSV_SORT_BY", DEFAULT_CSV_SORT_BY)
    trend_limit = _parse_int(env.get("HIGH_INTENT_TREND_LIMIT"), DEFAULT_LIMIT)
    trend_sleep_sec = _parse_float(env.get("HIGH_INTENT_TREND_SLEEP_SEC"), 1.0)
    max_topic_rank = _parse_int(env.get("HIGH_INTENT_MAX_TOPIC_RANK"), 5)
    csv_active_only = _parse_bool(env.get("HIGH_INTENT_CSV_ACTIVE_ONLY"), False)
    csv_download_dir = env.get("HIGH_INTENT_CSV_DOWNLOAD_DIR", "").strip()
    csv_max_retries = _parse_int(env.get("HIGH_INTENT_CSV_MAX_RETRIES"), 2)
    csv_retry_delay_sec = _parse_float(env.get("HIGH_INTENT_CSV_RETRY_DELAY_SEC"), 2.0)
    allow_rss_fallback = _parse_bool(env.get("HIGH_INTENT_ALLOW_RSS_FALLBACK"), True)
    rss_min_matches = _parse_int(env.get("HIGH_INTENT_RSS_MIN_MATCHES"), 1)
    raw_categories = _parse_list(
        env.get("HIGH_INTENT_TREND_CATEGORIES"),
        DEFAULT_HIGH_INTENT_CATEGORY_HINTS,
    )
    return {
        "regions": regions,
        "trend_source": trend_source,
        "trend_method": trend_method,
        "trend_window_hours": trend_window_hours,
        "csv_sort_by": csv_sort_by,
        "trend_limit": trend_limit,
        "trend_sleep_sec": trend_sleep_sec,
        "max_topic_rank": max_topic_rank,
        "raw_categories": raw_categories,
        "csv_active_only": csv_active_only,
        "csv_download_dir": csv_download_dir,
        "csv_max_retries": csv_max_retries,
        "csv_retry_delay_sec": csv_retry_delay_sec,
        "allow_rss_fallback": allow_rss_fallback,
        "rss_min_matches": rss_min_matches,
    }


def _normalize_high_intent_categories(raw_categories: Iterable[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in raw_categories:
        key = str(raw).strip().lower()
        if not key:
            continue
        key = key.replace("/", "_").replace("-", "_").replace(" ", "_")
        mapped = HIGH_INTENT_CATEGORY_ALIASES.get(key, key)
        if mapped in TRENDSPYG_CATEGORIES and mapped != "all" and mapped not in seen:
            normalized.append(mapped)
            seen.add(mapped)
    return normalized


def _build_high_intent_filter_text(topic: dict) -> str:
    chunks: list[str] = []
    keyword = str(topic.get("keyword") or "").strip()
    if keyword:
        chunks.append(keyword)
    articles = topic.get("news_articles") or []
    if isinstance(articles, list):
        for item in articles:
            if not isinstance(item, dict):
                continue
            headline = str(item.get("headline") or "").strip()
            source = str(item.get("source") or "").strip()
            if headline:
                chunks.append(headline)
            if source:
                chunks.append(source)
    return " ".join(chunks).lower()


def _filter_high_intent_rss_items(
    items: list[dict],
    categories: list[str],
    *,
    min_matches: int,
) -> list[dict]:
    if not items:
        return []
    min_matches = max(1, int(min_matches))
    allowed = [c for c in categories if c in HIGH_INTENT_RSS_CATEGORY_KEYWORDS]
    if not allowed:
        return []
    filtered: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        text = _build_high_intent_filter_text(item)
        if not text:
            continue
        matched: list[str] = []
        for category in allowed:
            keywords = HIGH_INTENT_RSS_CATEGORY_KEYWORDS.get(category, ())
            hits = sum(1 for kw in keywords if kw and kw in text)
            if hits >= min_matches:
                matched.append(category)
        if matched:
            metadata = item.get("metadata") or {}
            metadata = dict(metadata) if isinstance(metadata, dict) else {}
            metadata["high_intent_categories"] = matched
            metadata["high_intent_filter"] = "rss_keywords"
            item["metadata"] = metadata
            filtered.append(item)
    return filtered


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
    state_path = _resolve_state_path()
    return read_json(state_path, default={"topics": [], "slugs": []}) or {"topics": [], "slugs": []}


def _save_state(state: dict) -> None:
    state_path = _resolve_state_path()
    write_json(state_path, state)


class GeminiClient:
    def __init__(self, api_key: str, model: str, timeout_sec: int) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout_sec = max(1, timeout_sec)
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
        with urlopen(request, timeout=self.timeout_sec) as response:
            data = json.loads(response.read().decode("utf-8"))
        candidates = data.get("candidates", [])
        if not candidates:
            raise RuntimeError("Gemini returned no candidates.")
        parts = candidates[0].get("content", {}).get("parts", [])
        text_parts = [part.get("text", "") for part in parts if isinstance(part, dict)]
        return "\n".join(text_parts).strip()

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
        url = f"{self.base_url}/models/{self.model}:generateContent?key={self.api_key}"
        inline_data = {
            "mimeType": mime_type,
            "data": base64.b64encode(image_bytes).decode("utf-8"),
        }
        payload = {
            "contents": [{"parts": [{"text": prompt}, {"inlineData": inline_data}]}],
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
        with urlopen(request, timeout=self.timeout_sec) as response:
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
    logging.info("Tavily search results: %s (query=%s)", len(results), query)
    return results


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
        logging.info("Tavily API key missing; skip content extraction.")
        return []
    sources: list[dict] = []
    total_chars = 0
    hit_char_limit = False
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
            item = extracted_map.get(normalized)
            if not item:
                missing_extracts += 1
                continue
            content = str(item.get("content") or "").strip()
            if not content:
                empty_content += 1
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
    if template_mode == PIPELINE_HIGH_INTENT:
        template_block = f"\n\n{HIGH_INTENT_TEMPLATE_REQUIREMENTS}"

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
- Keep paragraphs short (3-4 sentences).
- Include one FAQ section with 3-4 Q/A items.

Editorial requirements:
- Write a full topic column, not a summary or bullet digest.
- Provide depth: background, recent trigger, evidence/data, stakeholder impact, and forward-looking analysis.
- Include at least 3 inline citations with Markdown links inside paragraphs.
- Do NOT use the headings "Overview", "Key Points", or "Implications".
- Use 4-7 meaningful section headings tailored to the story.
- Include an opening paragraph with a clear angle, and a closing paragraph with a takeaway.
- Integrate at least 2 source links inline in the body (e.g., "According to [Source](url)...").
- {image_requirement}
- Avoid listing raw URLs; all URLs must be Markdown links.
- Do not tell readers to click the links for details; include the details in the column.
- Never include ellipses or truncated fragments. Rewrite into complete sentences.
- Target 5000-6000 words total.
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
    if template_mode == PIPELINE_HIGH_INTENT:
        template_block = """
Template requirements:
- Include at least one section focused on comparisons or alternatives.
- Include at least one section focused on pricing/cost or decision criteria.
- Ensure headings fit high-intent readers looking for choices or fixes.
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
- 4-7 paragraphs, 4-6 sentences each
- Include at least 2 inline citations with Markdown links
- Do not make claims without citations
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
Preserve all citations and do not invent sources.
Maintain a consistent voice and avoid redundancy across sections.
""".strip()
    template_block = ""
    if template_mode == PIPELINE_HIGH_INTENT:
        template_block = f"\n{HIGH_INTENT_TEMPLATE_REQUIREMENTS}"
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
Never add new facts or sources. Preserve citations and Markdown links.
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
- No broken URLs or partial links (e.g., "htt")
- No empty Markdown links/images
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
    writer: GeminiClient,
    image_urls: list[str],
) -> list[dict]:
    if not image_urls or not config.gemini_api_key:
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
                temperature=min(config.gemini_temperature, 0.4),
                max_tokens=min(config.gemini_max_tokens, 600),
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
    date_str = datetime.now(config.content_timezone).strftime("%Y-%m-%d")
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
    angle_hint = f" {angle.strip()}" if angle else ""
    return [
        f"What changed about {base} in {year} and why now{angle_hint}?",
        f"Who is affected by {base} and what are the impacts in {year}?",
        f"What official statements, data, or reports support {base} in {year}?",
    ]


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
    if template_mode == PIPELINE_HIGH_INTENT:
        fallback_sections.insert(
            2,
            {
                "heading": f"{keyword} alternatives and comparisons",
                "goal": "Compare options and highlight key differences for buyers.",
                "evidence_refs": [],
            },
        )
        fallback_sections.insert(
            3,
            {
                "heading": f"{keyword} pricing and decision criteria",
                "goal": "Summarize cost signals and how to evaluate choices.",
                "evidence_refs": [],
            },
        )
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


def _apply_final_review(
    config: AutomationConfig,
    writer: GeminiClient,
    *,
    full_mdx: str,
    keyword: str,
) -> str:
    if not full_mdx or not config.final_review_enabled:
        return full_mdx
    if not config.gemini_api_key:
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
                temperature=min(config.gemini_temperature, 0.4),
                max_tokens=config.gemini_max_tokens,
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
    pipeline: str | None = None,
) -> dict | None:
    keyword = str(topic.get("keyword") or "").strip()
    if not keyword:
        return None
    template_mode = PIPELINE_HIGH_INTENT if pipeline == PIPELINE_HIGH_INTENT else None
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
    pipeline: str | None = None,
) -> Path | None:
    keyword = topic.get("keyword")
    if not keyword:
        return None

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

    template_mode = PIPELINE_HIGH_INTENT if pipeline == PIPELINE_HIGH_INTENT else None
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
            image_infos=image_infos,
            references=urls,
            language=config.content_language,
            template_mode=template_mode,
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

    if image_infos:
        body = _insert_images_by_relevance(body, image_infos)
    body = _ensure_images_in_body(body, image_urls, alt_text)
    body = _linkify_urls(body)
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


def _save_trends_snapshot(payload: dict, *, content_timezone: ZoneInfo) -> None:
    timestamp = datetime.now(content_timezone).strftime("%Y-%m-%d-%H%M%S")
    path = ROOT_DIR / "data" / "trends" / f"{timestamp}-trends.json"
    write_json(path, payload)


def _collect_trends_payload(
    *,
    regions: list[str],
    trend_source: str,
    trend_method: str,
    trend_limit: int,
    trend_sleep_sec: float,
    trend_window_hours: int,
    csv_sort_by: str,
    categories: list[str] | None,
    csv_active_only: bool = False,
    csv_download_dir: str | None = None,
    csv_max_retries: int = 1,
    csv_retry_delay_sec: float = 1.0,
    include_images: bool,
    include_articles: bool,
    max_articles_per_trend: int,
    cache: bool,
    allow_rss_fallback: bool = True,
) -> dict:
    source_mode = trend_source.strip().lower()
    try:
        payload = collect_trending_searches(
            regions,
            limit=trend_limit,
            sleep_sec=trend_sleep_sec,
            method=trend_method,
            source=source_mode,
            window_hours=trend_window_hours,
            csv_sort_by=csv_sort_by,
            categories=categories,
            csv_active_only=csv_active_only,
            csv_download_dir=csv_download_dir,
            csv_max_retries=csv_max_retries,
            csv_retry_delay_sec=csv_retry_delay_sec,
            include_images=include_images,
            include_articles=include_articles,
            max_articles_per_trend=max_articles_per_trend,
            cache=cache,
        )
    except RuntimeError as exc:
        if source_mode == "csv" and allow_rss_fallback:
            logging.warning("CSV source failed; falling back to RSS. Error: %s", exc)
            payload = collect_trending_searches(
                regions,
                limit=trend_limit,
                sleep_sec=trend_sleep_sec,
                method=trend_method,
                source="rss",
                window_hours=trend_window_hours,
                csv_sort_by=csv_sort_by,
                categories=None,
                csv_active_only=False,
                csv_download_dir=None,
                csv_max_retries=1,
                csv_retry_delay_sec=0.0,
                include_images=include_images,
                include_articles=include_articles,
                max_articles_per_trend=max_articles_per_trend,
                cache=cache,
            )
        else:
            raise
    return payload


def _process_topics(
    config: AutomationConfig,
    *,
    topics: list[dict],
    ranker_enabled: bool = True,
    pipeline: str | None = None,
) -> None:
    if not topics:
        logging.info("No topics found.")
        return

    state = _load_state()
    used = set(state.get("topics") or state.get("keywords") or [])
    slugs = set(state.get("slugs", []))

    writer = GeminiClient(
        config.gemini_api_key,
        config.gemini_model_content,
        config.gemini_timeout_sec,
    )
    meta_writer = GeminiClient(
        config.gemini_api_key,
        config.gemini_model_meta,
        config.gemini_timeout_sec,
    )
    if ranker_enabled and config.use_multi_agent:
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
            logging.info("Post created: %s", post_path)

    state["topics"] = sorted(used)
    state["slugs"] = sorted(slugs)
    _save_state(state)


def run_once(config: AutomationConfig) -> None:
    payload = _collect_trends_payload(
        regions=config.regions,
        trend_limit=config.trend_limit,
        trend_sleep_sec=config.trend_sleep_sec,
        trend_method=config.trend_method,
        trend_source=config.trend_source,
        trend_window_hours=config.trend_window_hours,
        csv_sort_by=config.csv_sort_by,
        categories=None,
        csv_active_only=False,
        csv_download_dir=None,
        csv_max_retries=1,
        csv_retry_delay_sec=0.0,
        include_images=config.rss_include_images,
        include_articles=config.rss_include_articles,
        max_articles_per_trend=config.rss_max_articles_per_trend,
        cache=config.rss_cache,
    )
    payload["pipeline"] = PIPELINE_RECENT
    _save_trends_snapshot(payload, content_timezone=config.content_timezone)

    topics = _select_topics_by_region(payload, config.regions, config.max_topic_rank)
    _process_topics(config, topics=topics, pipeline=PIPELINE_RECENT)


def run_high_intent(config: AutomationConfig) -> None:
    settings = _build_high_intent_settings(config)
    regions = settings["regions"]
    if not isinstance(regions, list) or not regions:
        logging.warning("HIGH_INTENT_REGIONS is empty; skipping pipeline.")
        return
    raw_categories = settings["raw_categories"]
    categories = _normalize_high_intent_categories(raw_categories)
    if not categories:
        logging.warning("HIGH_INTENT_TREND_CATEGORIES resolved to empty; skipping pipeline.")
        return
    logging.info("High-intent category filters: %s", categories)
    csv_download_dir = settings["csv_download_dir"]
    if not csv_download_dir:
        csv_download_dir = str((ROOT_DIR / "data" / "downloads").resolve())
    allow_rss_fallback = bool(settings["allow_rss_fallback"])
    rss_min_matches = int(settings["rss_min_matches"])
    trend_source = str(settings["trend_source"]).strip().lower()
    if trend_source == "rss":
        payload = _collect_trends_payload(
            regions=regions,
            trend_limit=int(settings["trend_limit"]),
            trend_sleep_sec=float(settings["trend_sleep_sec"]),
            trend_method=str(settings["trend_method"]),
            trend_source="rss",
            trend_window_hours=int(settings["trend_window_hours"]),
            csv_sort_by=str(settings["csv_sort_by"]),
            categories=None,
            csv_active_only=False,
            csv_download_dir=None,
            csv_max_retries=1,
            csv_retry_delay_sec=0.0,
            include_images=config.rss_include_images,
            include_articles=config.rss_include_articles,
            max_articles_per_trend=config.rss_max_articles_per_trend,
            cache=config.rss_cache,
            allow_rss_fallback=False,
        )
        payload["pipeline"] = PIPELINE_HIGH_INTENT
        payload["rss_filter_categories"] = categories
        payload["rss_filter_min_matches"] = rss_min_matches
        items = payload.get("items", [])
        if isinstance(items, list):
            filtered = _filter_high_intent_rss_items(
                items,
                categories,
                min_matches=rss_min_matches,
            )
            payload["items"] = filtered
        _save_trends_snapshot(payload, content_timezone=config.content_timezone)
        max_topic_rank = int(settings["max_topic_rank"])
        topics = _select_topics_by_region(payload, regions, max_topic_rank)
        _process_topics(
            config,
            topics=topics,
            ranker_enabled=False,
            pipeline=PIPELINE_HIGH_INTENT,
        )
        return
    if trend_source != "csv":
        logging.warning("Unknown trend source; forcing csv for high-intent.")
        trend_source = "csv"
    try:
        payload = _collect_trends_payload(
            regions=regions,
            trend_limit=int(settings["trend_limit"]),
            trend_sleep_sec=float(settings["trend_sleep_sec"]),
            trend_method=str(settings["trend_method"]),
            trend_source=trend_source,
            trend_window_hours=int(settings["trend_window_hours"]),
            csv_sort_by=str(settings["csv_sort_by"]),
            categories=categories,
            csv_active_only=bool(settings["csv_active_only"]),
            csv_download_dir=csv_download_dir,
            csv_max_retries=int(settings["csv_max_retries"]),
            csv_retry_delay_sec=float(settings["csv_retry_delay_sec"]),
            include_images=config.rss_include_images,
            include_articles=config.rss_include_articles,
            max_articles_per_trend=config.rss_max_articles_per_trend,
            cache=config.rss_cache,
            allow_rss_fallback=False,
        )
    except RuntimeError as exc:
        logging.warning("High-intent CSV collection failed. Error: %s", exc)
        if not allow_rss_fallback:
            return
        payload = _collect_trends_payload(
            regions=regions,
            trend_limit=int(settings["trend_limit"]),
            trend_sleep_sec=float(settings["trend_sleep_sec"]),
            trend_method=str(settings["trend_method"]),
            trend_source="rss",
            trend_window_hours=int(settings["trend_window_hours"]),
            csv_sort_by=str(settings["csv_sort_by"]),
            categories=None,
            csv_active_only=False,
            csv_download_dir=None,
            csv_max_retries=1,
            csv_retry_delay_sec=0.0,
            include_images=config.rss_include_images,
            include_articles=config.rss_include_articles,
            max_articles_per_trend=config.rss_max_articles_per_trend,
            cache=config.rss_cache,
            allow_rss_fallback=False,
        )
        payload["pipeline"] = PIPELINE_HIGH_INTENT
        payload["rss_filter_categories"] = categories
        payload["rss_filter_min_matches"] = rss_min_matches
        items = payload.get("items", [])
        if isinstance(items, list):
            filtered = _filter_high_intent_rss_items(
                items,
                categories,
                min_matches=rss_min_matches,
            )
            payload["items"] = filtered
        _save_trends_snapshot(payload, content_timezone=config.content_timezone)
        max_topic_rank = int(settings["max_topic_rank"])
        topics = _select_topics_by_region(payload, regions, max_topic_rank)
        _process_topics(
            config,
            topics=topics,
            ranker_enabled=False,
            pipeline=PIPELINE_HIGH_INTENT,
        )
        return
    payload["pipeline"] = PIPELINE_HIGH_INTENT
    _save_trends_snapshot(payload, content_timezone=config.content_timezone)

    max_topic_rank = int(settings["max_topic_rank"])
    topics = _select_topics_by_region(payload, regions, max_topic_rank)
    _process_topics(
        config,
        topics=topics,
        ranker_enabled=False,
        pipeline=PIPELINE_HIGH_INTENT,
    )


def run_pipeline(config: AutomationConfig, *, pipeline: str) -> None:
    if pipeline == PIPELINE_HIGH_INTENT:
        run_high_intent(config)
        return
    run_once(config)


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Automate trend-based blog posts.")
    parser.add_argument("--once", action="store_true", help="Run only once")
    parser.add_argument(
        "--pipeline",
        default=PIPELINE_RECENT,
        choices=PIPELINE_CHOICES,
        help="Pipeline mode (recent or high-intent)",
    )
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
    logging.info(
        "Automation start (pipeline=%s, interval=%s hours)",
        args.pipeline,
        config.interval_hours,
    )

    if args.once:
        run_pipeline(config, pipeline=args.pipeline)
        return 0

    while True:
        run_pipeline(config, pipeline=args.pipeline)
        sleep_seconds = max(config.interval_hours, 0.1) * 3600
        logging.info("Sleeping for %s seconds", sleep_seconds)
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
