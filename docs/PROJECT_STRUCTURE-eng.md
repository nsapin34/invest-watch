# Project Structure — invest-watch

> Derived from functional and technical specifications v1.3 and architecture document.
> This file is the reference for initialising the repository before development begins.

---

## Repository layout

```
invest-watch/                            ← git root
│
├── invest_watch/                        ← main Python package  (import invest_watch)
│   ├── __init__.py
│   ├── cli.py                     ← entry point: argparse, all user commands
│   ├── orchestrator.py            ← daily workflow (15 steps), run.lock, --resume
│   │
│   ├── config_manager.py          ← loads conf/config.toml, resolves ${VAR}
│   ├── logger.py                  ← stdlib logging, rotating file + per-run file
│   ├── error_manager.py           ← processing_error, backoff, circuit-breaker
│   │
│   ├── db_manager.py              ← SQLite WAL connection, versioned migrations, FTS5
│   ├── company_repository.py      ← CRUD companies, aliases, search vectors
│   ├── market_repository.py       ← CRUD markets / sectors / industries, hierarchy
│   ├── source_repository.py       ← CRUD sources, ping validation, metrics
│   │
│   ├── search_engine.py           ← query generation, RSS → Google News RSS → DDG
│   ├── fetcher.py                 ← polite HTTP, robots.txt, final URL capture
│   ├── cleaner.py                 ← trafilatura → main text, excerpt, normalisation
│   ├── date_detector.py           ← JSON-LD / meta / RSS / dateparser, date_certaine
│   ├── lang_detector.py           ← lingua (300 chars), langdetect fallback
│   │
│   ├── classifier.py              ← rules + optional LLM enrichment, scores
│   ├── dedup.py                   ← 4 layers (URL / SHA-256 / Jaccard / MinHash) + event clustering
│   ├── short_link.py              ← secure random code, DB uniqueness, Markdown render
│   │
│   ├── llm_client.py              ← watsonx.ai port: IAM auth, prompts, retries, circuit-breaker
│   ├── summarizer.py              ← article JSON summaries, company/market/global digests
│   │
│   ├── insight_engine.py          ← V1+: F22–F27 (catalyst, momentum, fact-check, thesis, macro, sentiment)
│   └── web_ui.py                  ← V2: FastAPI/Flask server bound to 127.0.0.1
│
├── conf/
│   ├── config.toml                ← runtime config (git-ignored — see config.example.toml)
│   └── config.example.toml        ← template with fictitious values, committed to git
│
├── data/                          ← runtime data (git-ignored)
│   ├── invest.db                  ← SQLite database (WAL mode)
│   ├── run.lock                   ← anti-double-run lock (created/deleted each run)
│   ├── stop.flag                  ← clean-shutdown flag (written by `invest stop`)
│   └── last_run.status            ← exit status of the last run (for the scheduler)
│
├── reports/                       ← generated daily reports (git-ignored)
│   ├── YYYY-MM-DD.md
│   └── YYYY-MM-DD.json
│
├── logs/                          ← application logs (git-ignored)
│   ├── invest.log                 ← rotating global log
│   └── run-YYYY-MM-DD.log         ← one log file per execution
│
├── backups/                       ← daily SQLite backups via VACUUM INTO (git-ignored)
│   └── invest-YYYY-MM-DD.db
│
├── migrations/                    ← versioned SQL migration scripts
│   ├── v001_initial_schema.sql
│   ├── v002_article_version.sql
│   └── ...
│
├── tests/
│   ├── __init__.py
│   ├── conftest.py                ← shared fixtures (in-memory DB, LLM stub, httpx-mock)
│   │
│   ├── unit/
│   │   ├── test_config_manager.py
│   │   ├── test_dedup.py
│   │   ├── test_date_detector.py
│   │   ├── test_short_link.py
│   │   ├── test_cleaner.py
│   │   └── ...
│   │
│   ├── integration/
│   │   ├── test_workflow.py       ← full run with mocked sources
│   │   ├── test_orchestrator.py   ← resume, lock, partial failure, exit codes
│   │   └── test_migration.py      ← v1 → v2, data preserved, idempotent
│   │
│   ├── stubs/
│   │   └── llm_stub.py            ← LLMStub: zero network, configurable raise_after
│   │
│   └── fixtures/
│       ├── rss/
│       │   ├── hkex_sample.xml
│       │   ├── nbs_sample.xml
│       │   ├── pboC_sample.xml
│       │   └── google_news_sample.xml
│       └── html/
│           ├── article_full.html
│           ├── article_paywall.html
│           ├── article_dynamic.html
│           ├── article_404.html
│           └── article_chinese.html
│
├── docs/                          ← project documentation
│   ├── SPECIFICATIONS.md          ← functional & technical specs v1.3 (FR)
│   ├── SPECIFICATIONS-eng.md      ← functional & technical specs v1.3 (EN)
│   ├── architecture.md            ← architecture document (FR)
│   ├── architecture-eng.md        ← architecture document (EN)
│   ├── PROJECT_STRUCTURE-eng.md   ← this file
│   ├── specifications/            ← spec source parts (FR)
│   ├── specifications-eng/        ← spec source parts (EN)
│   ├── architecture/              ← architecture source parts (FR)
│   └── architecture-eng/          ← architecture source parts (EN)
│
├── utils/
│   └── merge_specs.py             ← merges part files into a single Markdown document
│
├── .env                           ← secrets: WATSONX_API_KEY, WATSONX_URL, etc. (git-ignored, chmod 700)
├── .gitignore
├── pyproject.toml                 ← package metadata, dependencies, [project.scripts] entry point
├── requirements.txt               ← pinned dependencies for reproducible installs
└── README.md
```

---

## Key files — notes

### `.env` (git-ignored, permissions 700)

```dotenv
WATSONX_API_KEY=your-ibm-cloud-api-key
WATSONX_URL=https://eu-de.ml.cloud.ibm.com
WATSONX_PROJECT_ID=your-project-uuid
WATSONX_MODEL_ID=ibm/granite-3-8b-instruct
```

Never commit this file. `conf/config.toml` references these values as `${WATSONX_API_KEY}` etc.

### `.gitignore` — minimum entries

```
# Runtime data
data/
logs/
reports/
backups/

# Secrets
.env
conf/config.toml

# Python
__pycache__/
*.py[cod]
.venv/
*.egg-info/
dist/
.pytest_cache/
.coverage
htmlcov/
```

### `pyproject.toml` — entry point

```toml
[project.scripts]
invest-watch = "invest_watch.cli:main"
```

This makes `invest-watch run`, `invest-watch report`, etc. available after `pip install -e .`.

---

## Git-ignored vs committed

| Path | Committed | Reason |
|---|---|---|
| `conf/config.example.toml` | ✅ | Safe template, no secrets |
| `conf/config.toml` | ❌ | May contain non-secret settings that vary per machine |
| `.env` | ❌ | Contains API keys |
| `data/` | ❌ | Runtime database and lock files |
| `logs/` | ❌ | Generated at runtime |
| `reports/` | ❌ | Generated at runtime |
| `backups/` | ❌ | Generated at runtime |
| `migrations/` | ✅ | SQL scripts are source code |
| `tests/fixtures/` | ✅ | Static HTML/RSS samples, no secrets |
| `invest_watch/` | ✅ | Source code |
| `docs/` | ✅ | Documentation |
| `utils/` | ✅ | Dev tooling |

---

## Module → file mapping (§5.2 specs / §4 architecture)

| Spec module | File | Step introduced |
|---|---|---|
| `config_manager` | `invest_watch/config_manager.py` | Step 1 |
| `db_manager` | `invest_watch/db_manager.py` | Step 1 |
| `company_repository` | `invest_watch/company_repository.py` | Step 1 |
| `market_repository` | `invest_watch/market_repository.py` | Step 1 |
| `source_repository` | `invest_watch/source_repository.py` | Step 1 |
| `logger` | `invest_watch/logger.py` | Step 1 |
| `cli` | `invest_watch/cli.py` | Step 1 |
| `search_engine` | `invest_watch/search_engine.py` | Step 2 |
| `fetcher` | `invest_watch/fetcher.py` | Step 2 |
| `cleaner` | `invest_watch/cleaner.py` | Step 2 |
| `date_detector` | `invest_watch/date_detector.py` | Step 2 |
| `lang_detector` | `invest_watch/lang_detector.py` | Step 2 |
| `classifier` | `invest_watch/classifier.py` | Step 3 |
| `dedup` | `invest_watch/dedup.py` | Step 3 |
| `short_link` | `invest_watch/short_link.py` | Step 3 |
| `llm_client` | `invest_watch/llm_client.py` | Step 4 |
| `error_manager` | `invest_watch/error_manager.py` | Step 4 |
| `summarizer` | `invest_watch/summarizer.py` | Step 5 |
| `orchestrator` | `invest_watch/orchestrator.py` | Step 6 |
| `insight_engine` | `invest_watch/insight_engine.py` | Step 8 (V1+) |
| `web_ui` | `invest_watch/web_ui.py` | Step 9 (V2) |

*Steps refer to the implementation plan in architecture §8.*

---

*Derived from SPECIFICATIONS-eng.md v1.3 and architecture-eng.md — invest-watch.*
