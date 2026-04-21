# ai_blog_v1_AI — Python AI Pipeline

**Role:** Content generation engine. Trend collection → topic selection → research → writing → image generation → MDX output into sibling Astro repo.

## STRUCTURE

```
ai_blog_v1_AI/
├── scripts/
│   ├── auto_blog.py        # CORE: full pipeline monolith (6000+ lines)
│   └── collect_trends.py   # CLI: Google Trends RSS/CSV → JSON snapshots
├── src/
│   ├── collectors/
│   │   └── trendspyg_collector.py  # trendspyg library wrapper
│   ├── config/
│   │   └── settings.py             # all pipeline defaults (override via env)
│   └── store/
│       └── local_store.py          # read_json / write_json helpers
├── data/
│   ├── trends/             # collected trend snapshots (gitignored)
│   └── state/              # published.json — de-dupe state (gitignored)
├── docs/
│   ├── BLOG_AUTOMATION_FLOW_V2.md  # LEIA multi-agent prompts + content rules
│   ├── DEV_PLAN.md                 # roadmap, open architectural questions
│   └── web_search.md               # Tavily searchWeb/extractWebContent rules
├── .github/workflows/auto_blog.yml
├── env.template            # canonical env var reference with defaults
└── requirements.txt        # only: trendspyg, openai
```

## WHERE TO LOOK

| Task | Location |
|------|----------|
| Pipeline entry | `scripts/auto_blog.py` → `run_pipeline()` → `run_daily_impact()` / `run_weekly_major_events()` |
| Edit a pipeline stage | Search `auto_blog.py` by stage name: `outline_architect`, `section_writer`, `assembler`, `quality_gate`, `final_review`, `mdx_render_guard` |
| Add/change defaults | `src/config/settings.py` (env vars override at runtime) |
| All env var names | `env.template` |
| CI schedule/steps | `.github/workflows/auto_blog.yml` |
| Content writing rules | `docs/BLOG_AUTOMATION_FLOW_V2.md` |
| Trend collection | `src/collectors/trendspyg_collector.py` |

## EXTERNAL SERVICES

| Service | Env Var | Used For |
|---------|---------|----------|
| Gemini (text) | `GEMINI_API_KEY` | Primary post generation (`gemini-*` model) |
| OpenAI | `OPENAI_API_KEY` | Weekly topic discovery (`gpt-*` model) |
| Gemini Image | `GOOGLE_API_KEY` | Hero image generation; falls back to local gradient |
| Tavily | `TAVILY_API_KEY` | Web search + content extraction |
| YouTube Data API | `YOUTUBE_API_KEY` | Related video sourcing |
| trendspyg | — | Google Trends RSS/CSV (no key needed) |

**Note:** Gemini and Tavily are called via `urllib` (stdlib HTTP), not their SDKs. Only `trendspyg` and `openai` are in `requirements.txt`.

## KEY ENV VARS

| Var | Default | Notes |
|-----|---------|-------|
| `ASTRO_ROOT` | `../ai_blog_v1_astro` | Path to Astro repo root |
| `STATE_PATH` | `../ai_blog_v1_astro/.ai_state/published.json` | De-dupe state file |
| `POST_DRAFT` | `false` | Set `true` to prevent live publish |
| `CONTENT_TIMEZONE` | `America/New_York` | Affects pubDate |
| `USE_MULTI_AGENT` | `true` | Enables LEIA multi-agent pipeline |
| `FINAL_REVIEW_REVISIONS` | `2` | LLM revision loops after writing |
| `MDX_RENDER_AUTO_FIX` | `true` | Auto-fixes void HTML tags before publishing |
| `ENFORCE_LOCAL_RUN_HOUR` | `0`/`1` | Set to `1` by GHA on scheduled runs; prevents off-hour execution |

## CONVENTIONS

- Python 3.11 in CI; locally 3.12–3.14 may be in `__pycache__` — pin to 3.11 for CI parity
- `sys.path.append(str(SRC_DIR))` in both scripts — no package install required (`# noqa: E402` suppresses linter)
- All configuration via env vars only; no CLI args beyond `--once --pipeline <mode>`
- MDX files written to: `$ASTRO_ROOT/src/content/blog/YYYY-MM-DD-slug.mdx`
- Hero images written to: `$ASTRO_ROOT/public/images/posts/<slug>/hero.jpg` (1200×630)
- Bot commit message: `"Auto: publish blog updates"` (GHA step)

## ANTI-PATTERNS (THIS REPO)

- **Never commit `.env`** — gitignored, but `git add .` from here will skip it; rotate keys periodically
- **`ClaudeClient` class wraps Gemini**, not Anthropic — naming mismatch; do not rename without updating all call sites
- **Model names in `env.template` are fabricated** — `gemini-3.1-pro-preview` and `gpt-5.4-pro` do not exist; check actual API docs before changing
- **Duplicate env keys**: `SCRAPE_DELAY_SEC` (5.0→1.0) and `SCRAPE_BACKOFF_SEC` (10.0→5.0) defined twice in `.env` — second wins; consolidate before editing
- **`path/to/venv/`** inside repo root — accidental artifact; add to `.gitignore` or delete; never import from it
- **No test suite** — zero test files; quality enforced in-pipeline via `quality_gate`, `final_review`, `mdx_render_guard`
- **Pipeline stages swallow failures silently** — `except Exception: return None` pattern throughout; WARNING logs do not raise or set exit code 1
- **`quality:gate` npm script** (Astro side) is never called in this CI workflow — run it manually after pipeline outputs

## RUNNING LOCALLY

```bash
cp env.template .env          # fill in: GEMINI_API_KEY, OPENAI_API_KEY, TAVILY_API_KEY, YOUTUBE_API_KEY, GOOGLE_API_KEY
pip install -r requirements.txt

# Run daily pipeline
python scripts/auto_blog.py --once --pipeline daily-impact

# Run weekly pipeline
python scripts/auto_blog.py --once --pipeline weekly-major-events

# Collect trends only
python scripts/collect_trends.py
```
