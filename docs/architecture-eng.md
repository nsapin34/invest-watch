# Architecture — invest-watch

> Based on functional and technical specifications v1.3 (2026-09-08).
> Decisions DA-01 to DA-07 decided and integrated — see §7 and §A.

---

## 1. Requirements Summary

### Functional Requirements

| Ref | Requirement |
|---|---|
| EF-01 | Automatic daily collection (07:00) of articles published the previous day (D-1) for portfolio companies |
| EF-02 | CLI CRUD management of companies (max 50), markets/sectors/industries, sources and aliases |
| EF-03 | Source cascade: Editorial RSS → Google News RSS → DuckDuckGo |
| EF-04 | Date filtering (D-1 ± 6 h) in the user timezone (default `Europe/Paris`) |
| EF-05 | Page text extraction and cleaning (`trafilatura`, `httpx`, `robots.txt` compliance) |
| EF-06 | Language detection (`lingua`) and date detection (`dateparser` + JSON-LD / meta) |
| EF-07 | Article classification by company, market and category (rules + optional LLM enrichment) |
| EF-08 | 4-layer deduplication (normalised URL, SHA-256, title Jaccard, content MinHash) + event clustering |
| EF-09 | Article summaries (structured JSON) and daily digests (company / market / global) via IBM watsonx.ai LLM |
| EF-10 | Full degraded mode if LLM unavailable (heuristic excerpts, uninterrupted workflow) |
| EF-11 | Local short links (6-8 character code) traced back to the original URL |
| EF-12 | Daily reports `reports/YYYY-MM-DD.md` and `reports/YYYY-MM-DD.json` |
| EF-13 | Automatic purge of text content at 90 days; daily SQLite backup (`VACUUM INTO`) |
| EF-14 | Complete CLI: `invest-watch run/report/company/market/status/errors/export/search/source/db` |
| EF-15 | Advanced V1+ features: catalyst calendar (F22), momentum detection (F23), cross-source figure check (F24), investment thesis (F25), macro drivers (F26), 30-90 d sentiment (F27) |
| EF-16 | Local web interface (V2) on `127.0.0.1` for browsing and editing |

### Non-Functional Requirements

| Ref | Requirement |
|---|---|
| ENF-01 | **Resilience**: any unavailable optional service (LLM, source, engine) triggers degraded mode — the application never stops |
| ENF-02 | **Idempotence**: each workflow step is repeatable without duplicates (upserts under uniqueness constraints) |
| ENF-03 | **Max durations**: 90 min per run overall; 30 s/page; 10 s/RSS feed |
| ENF-04 | **Network compliance**: `robots.txt` respected, identified User-Agent `invest-watch/1.0`, rate ≤ 0.5 req/s/host, 40 engine queries max/run |
| ENF-05 | **Secret security**: API keys never in plain text in config, logs or reports; `.env` outside git (permissions 700) |
| ENF-06 | **Traceability**: every fact linked to a `#id` → article → original URL |
| ENF-07 | **Portability**: Windows (main V1 target), Linux/macOS documented |
| ENF-08 | **Testability**: all tests offline without an API key; `pytest-cov` coverage ≥ 80% |
| ENF-09 | **Extensibility**: modular architecture allowing V2 additions (`web_ui`, `insight_engine`) without core refactoring |
| ENF-10 | **Controlled volume**: ≤ 50 companies; 40 queries/run; 150 LLM calls/run; responses > 2 MB rejected |

---

## 2. Ambiguities and Open Questions

All ambiguities below have been resolved. Decisions are integrated in §7 (DA-08 to DA-11).

| # | Ambiguity | Decision ✔ |
|---|---|---|
| A1 ✔ | **D8 — watsonx.ai model** | Model selectable via `.env` (`WATSONX_MODEL_ID`); no hardcoded default — see DA-01 |
| A2 ✔ | **D9 — watsonx.ai account tier** | Quota treated as unlimited; no blocking cost cap — see DA-02 |
| A3 ✔ | **D19 — V2 interface form** | Local FastAPI web interface bound to `127.0.0.1` — see DA-03 |
| A4 ✔ | **Free Brave Search** | Brave Search included in V1; free tier confirmed at 1,000 req/month — see DA-08 |
| A5 ✔ | **RSS feeds for Chinese sources (NBS, PBoC, CSRC)** | `type = 'website'` mode with page scraping via `fetcher` (`httpx` + `trafilatura`) — see DA-04 |
| A6 ✔ | **F22 — Catalyst calendar population** | Automatic extraction via regex + LLM is sufficient; no manual entry interface needed — see DA-09 |
| A7 ✔ | **Recovery of a partially billed LLM run** | No `--resume` recovery planned; in-memory cache per run only; re-processing on restart is acceptable — see DA-10 |
| A8 ✔ | **FTS5 sync and purge** | Separate background task is acceptable; purge does not need to be synchronous with the main transaction — see DA-11 |

---

## 3. Proposed Architecture

The architecture is **modular, local, and serverless**, centred on a single SQLite file. The OS scheduler triggers the CLI which orchestrates a pipeline of independent modules, each individually failable.

```
Planificateur OS (07h00)
        │
        ▼
  ┌──── CLI (invest) ─────────────────────────────────────────────────┐
  │  argparse : run, report, company, market, status, errors, export, │
  │             search, source, db, catalysts, momentum, thesis, macro │
  └───────────────────────────┬───────────────────────────────────────┘
                              │
                              ▼
  ┌───────────────────── ORCHESTRATEUR ───────────────────────────────┐
  │  Verrou run.lock · daily_run lifecycle · Workflow §10 (15 étapes) │
  │  Reprise --resume · Arrêt propre stop.flag · Stats + rapport      │
  └──┬──────┬──────┬────────┬────────┬────────┬───────┬──────────────┘
     │      │      │        │        │        │       │
  SEARCH  FETCH  CLEAN  CLASSIFY   DEDUP  SUMMARIZE SHORT_LINK
  (RSS/   (http+  (trafi-  (rules+  (4     (LLM or   (short
  GNews/  robots) latura)  LLM opt.) layers) degraded) codes)
   DDG)
     │      │      │        │        │        │       │
     └──────┴──────┴────────┴────────┴────────┴───────┘
                             │
               ┌─────────────┴────────────┐
               │        DB_MANAGER        │  data/invest.db
               │  SQLite WAL · FTS5       │  versioned migrations
               │  repositories · purge    │  backup VACUUM INTO
               └──────────────────────────┘
                             │
         ┌───────────────────┼───────────────────┐
         │                   │                   │
   INSIGHT_ENGINE (V1+)   WEB_UI (V2)       CONF · LOGGER · ERROR_MGR
   F22–F27                127.0.0.1
```

**Architectural principles:**

1. **No module writes directly to the database**: everything goes through `db_manager` (repositories); every insertion is tied to a `daily_run`.
2. **All network calls go through `fetcher` / `search_engine`**: global rate limiting managed by token, unique User-Agent, timeouts.
3. **`llm_client` is a port**: watsonx.ai implementation, test stub, and `off` (degraded) mode with zero dependencies.
4. **Each workflow step is idempotent**: upsert under uniqueness constraints — resuming never creates duplicates.
5. **Partial failure tolerated**: any exception in a module is caught, recorded in `processing_error`, and the run continues with the remaining sources.

---

## 4. Components, Responsibilities and Interfaces

| Module | Responsibility | Inputs | Outputs | Key dependencies |
|---|---|---|---|---|
| `config_manager` | Load `conf/config.toml`, resolve `${VAR}` from env / `.env`; produce an immutable config | TOML + environment variables | `AppConfig` object (read-only) | `tomllib`, `python-dotenv` |
| `cli` | User entry point; dispatch commands to orchestrator or repositories | `sys.argv` arguments | Orchestrator / repository calls, terminal display | `argparse`, all repositories |
| `orchestrator` | Chains the 15 workflow steps; manages `daily_run`, lock, resume, clean shutdown | Config + active company/market lists | Updated `daily_run`, report written to disk | All modules |
| `company_repository` | CRUD companies, aliases, associated markets; search vectors (name, ticker, ISIN, site) | CLI commands / orchestrator | `Company` objects, paginated lists | `db_manager` |
| `market_repository` | CRUD markets/sectors/industries; `parent_id` hierarchy; GICS taxonomy | CLI commands / orchestrator | `Market` objects, hierarchical trees | `db_manager` |
| `source_repository` | CRUD sources; HTTP/RSS ping validation; `last_success_at` / `error_count` metrics | CLI commands / orchestrator | `Source` objects | `db_manager`, `httpx` |
| `search_engine` | Generates queries (3/company + 2/market, cap 40/run); RSS → Google News RSS → DDG cascade; rate limiting with jitter | `Company` + `Market` lists + source config | Raw candidates `(title, url, date_str, source_id)` | `feedparser`, `httpx`, `duckduckgo-search` |
| `fetcher` | Polite HTTP retrieval; `robots.txt` compliance (per-host cache); capture of final URL after redirect; size bounded to 2 MB | Candidate URLs | `(html_content, url_final, status_code)` | `httpx`, `urllib.robotparser` |
| `cleaner` | HTML → main text (`trafilatura`); boilerplate removal; 400-char extract; whitespace normalisation | Raw HTML | `(text, excerpt, word_count)` | `trafilatura`, `beautifulsoup4` |
| `date_detector` | Publication date from JSON-LD, meta tags, RSS feed, then `dateparser`; `date_certaine` flag | HTML + RSS metadata + config timezone | `(published_at UTC, date_certaine bool)` | `dateparser`, `trafilatura` |
| `lang_detector` | Language detection on first 300 characters; `langdetect` fallback | Text (300 chars) | BCP-47 language code | `lingua-language-detector`, `langdetect` |
| `classifier` | Associates companies/markets/categories by rules (aliases, keywords); computes `relevance_score` and `importance_level`; optional LLM enrichment when confidence is low | Text + metadata + config lists | `article_company/market/category` junctions + scores | `db_manager`, `llm_client` (opt.) |
| `dedup` | Layer 1: URL normalisation (remove `utm_*`, fragment); Layer 2: SHA-256 of normalised text; Layer 3: title Jaccard ≥ 0.85; Layer 4: MinHash ≥ 0.9; Layer 5: event clustering (same company, ±24 h, similarity ≥ threshold) | Current article + last 7 days in memory | `(status, event_id, dedup_reason)` | `datasketch`, `db_manager` |
| `llm_client` | watsonx.ai call port: IAM auth, prompt construction, JSON parsing, schema validation, timeouts, retries (3×), circuit-breaker (3 consecutive failures → degraded), call counter | Truncated text + context (companies/markets) | Validated structured JSON or `LLMDegradedError` | `ibm-watsonx-ai` SDK |
| `summarizer` | Article summaries (structured JSON); company / market / global digests; degraded mode (lead excerpt + heuristic facts); per-`content_hash` cache | Relevant articles + existing summaries | `summary` records (type `article/company/market/global`) | `llm_client`, `db_manager` |
| `short_link` | Generates a secure random short code (6-8 chars, `secrets.token_urlsafe`) with DB uniqueness; `[code](url)` Markdown rendering | Original URL + `article_id` | `short_code` TEXT | `db_manager`, `secrets` stdlib |
| `db_manager` | SQLite WAL connection; versioned migrations (`app_meta.db_version`); repositories; FTS5 `article_fts`; 90-day `content` purge; `VACUUM INTO` backup | SQL / Python objects | Cursors / results / transactions | `sqlite3` stdlib |
| `logger` | UTC-timestamped logs; size rotation; dedicated `logs/run-YYYY-MM-DD.log` per execution; configurable levels | Application events | `logs/invest.log`, `logs/run-DATE.log` | `logging` stdlib |
| `error_manager` | Records `processing_error`; exponential backoff (2 s → 8 s → 30 s); per-source isolation; LLM circuit-breaker; statuses `new/retrying/failed/resolved` | Exceptions + context (step, source_id, article_id) | `processing_error` in DB; retry/skip decision | `db_manager` |
| `insight_engine` **(V1+)** | Catalyst calendar (F22); momentum detection (F23); figure fact-check (F24); investment thesis (F25); macro drivers (F26); 30-90 d sentiment (F27) | Existing articles/summaries/mentions + config | `catalyst_event`, `mention_stats`, `macro_indicator`, `sentiment_history` tables; report sections | `db_manager`, `llm_client` (opt.) |
| `web_ui` **(V2)** | Local `127.0.0.1` web server; browsing news/summaries/markets; editing companies/markets; triggering runs; all writes via repositories | Local HTTP requests | HTML / JSON pages; orchestrator trigger | `FastAPI` or `Flask`, all repositories, orchestrator |

---

## 5. Data Model and Main Flows

### Entity-Relationship Schema (main tables)

```
┌──────────────┐        ┌──────────────────┐        ┌──────────────┐
│   company    │───┐    │  company_market   │────────│   market     │
│  id, name,   │   └───▶│  company_id FK    │        │  id, name,   │
│  ticker, isin│        │  market_id FK     │        │  type, parent│
│  country, ... │        │  weight           │        └──────────────┘
└──────┬───────┘        └──────────────────┘
       │
       │  ┌───────────────────────────────────────────────────────────┐
       │  │                       article                             │
       │  │  id, run_id FK, source_id FK, url_original, url_normalized│
       │  │  title, language, published_at (UTC), date_certaine       │
       │  │  content (purge 90d), excerpt, content_hash, title_hash   │
       │  │  short_code, status, event_id FK (self), relevance_score  │
       │  │  importance_level, requires_human_check, update_count     │
       │  └────────┬──────────────────────────────────┬──────────────┘
       │           │                                  │
       │  ┌────────▼────────┐              ┌──────────▼──────────┐
       │  │ article_company │              │  article_market      │
       │  │ article_id FK   │              │  article_id FK       │
       │  │ company_id FK   │              │  market_id FK        │
       │  │ relevance, ...  │              │  relevance, ...      │
       │  └─────────────────┘              └─────────────────────┘
       │
       │  ┌───────────────┐   ┌────────────────┐   ┌────────────────┐
       └─▶│ catalyst_event│   │  mention_stats  │   │sentiment_history│
          │ F22           │   │  F23            │   │  F27           │
          └───────────────┘   └────────────────┘   └────────────────┘

┌──────────────┐   ┌──────────────┐   ┌──────────────┐
│  daily_run   │   │   summary    │   │processing_err│
│  run_date,   │──▶│  type, ref_id│   │  step, source│
│  status, stats│  │  content (MD)│   │  retry_count │
│  llm_status  │  │  json_payload│   │  status      │
└──────────────┘  │  model_id,   │   └──────────────┘
                  │  llm_used    │
                  └──────────────┘

┌──────────────┐   ┌──────────────┐   ┌──────────────┐
│  short_link  │   │article_version│  │macro_indicator│
│  short_code  │   │  article_id  │  │  F26          │
│  url_original│   │  content_hash│  │  indicator_key│
│  article_id  │   │  changed_at  │  │  value, period│
└──────────────┘   └──────────────┘   └──────────────┘
```

### Main Flow — Daily Workflow

```
Scheduler → CLI → Orchestrator
    │
    ├─ 1. Compute D-1 period (configured timezone → UTC) → period_start / period_end
    ├─ 2. Load config (companies, markets, active sources)
    ├─ 3. Generate queries (3/company + 2/market, cap 40)
    │
    ├─ 4. Query sources ──▶ [NEVER BLOCKING]
    │       Editorial RSS → Google News RSS → DuckDuckGo
    │       → raw candidates (title, url, date, source_id)
    │
    ├─ 5. Filter by date (D-1 ± 6 h, date_certaine)
    ├─ 6. Extract content (fetcher + trafilatura)
    ├─ 7. Clean text (normalisation, SHA-256 hash)
    ├─ 8. Deduplicate (4 layers vs DB 7 d + intra-run)
    ├─ 9. Classify (language, categories, companies/markets, scores)
    ├─ 10. Assess importance (rules: category + authority + magnitude)
    │
    ├─ 11. Summarise ──▶ [CIRCUIT-BREAKER]
    │        LLM watsonx.ai (relevance filter ≥ 0.5, 150-call budget, hash cache)
    │        → degraded mode if unavailable
    │
    ├─ 12. Save (idempotent SQLite upserts)
    ├─ 13. Digests (company → market → global)
    ├─ 14. Generate report (reports/YYYY-MM-DD.md + .json)
    └─ 15. Wrap-up (stats_json, completed/partial status, 90d purge, backup)
```

### Deduplication Flow

```
Incoming article
    │
    ├─ Layer 1: normalised URL (remove utm_*, fragment, lowercase)
    │              → exact duplicate? → status = 'duplicate', FK to original
    │
    ├─ Layer 2: SHA-256(normalised text)
    │              → identical hash? → status = 'duplicate'
    │
    ├─ Layer 3: Jaccard(title tokens) ≥ 0.85
    │              → same wire story? → status = 'duplicate'
    │
    ├─ Layer 4: MinHash(content, 128 permutations) ≥ 0.9
    │              → near-copy? → status = 'redundant'
    │              unless rank 1-2 source or diff > 10% → 'complementary'
    │
    └─ Layer 5: Event clustering
                   same company + ±24 h window + similarity ≥ threshold
                   → first = head (event_id = self.id)
                   → subsequent ones attached (event_id = head.id)
                   new if no cluster in last 14 days
```

---

## 6. Identified Risks

### Security

| Risk | Severity | Mitigation |
|---|---|---|
| Exposure of the watsonx.ai API key | **High** | `.env` outside git (permissions 700); never in logs, reports, `config.toml`; documented rotation; OS keychain (`keyring`) as alternative |
| Prompt injection via web content into LLM prompts | **Medium** | Sanitised templates; text truncated to 8,000 chars; factual prompts (`do not invent anything`); JSON output validated by strict schema |
| Portfolio data exposure via V2 web UI | **Medium** | Server bound to `127.0.0.1` only; no public API; all writes via repositories; action logging |
| Unauthorised read or theft of `invest.db` | **Medium** | File permissions restricted to OS user; SQLCipher option (V3); 90-day purge limits historical exposure |
| Data exfiltration via texts sent to the LLM | **Low** | Minimal data sent (title, date, source, truncated text, candidates); no keys, paths, internal identifiers, or out-of-budget content |

### Performance

| Risk | Severity | Mitigation |
|---|---|---|
| Exceeding the max duration (90 min) with 50 companies | **Medium** | Internal quotas (40 queries, 30 s/page, 10 s/feed); global timeout; run marked `partial` if exceeded |
| SQLite database size explosion | **Medium** | 90-day `content` purge; FTS5 synchronised to purge; daily `VACUUM INTO`; `max_response_bytes = 2 MB` |
| IP blocking (DuckDuckGo, aggregators) | **Medium** | Polite rate + random jitter; low volumes; `use_search_engines = false` disables scraping |
| Unexpected LLM cost (tokens / calls) | **Medium** | `relevance_score ≥ 0.5` filter; per-`content_hash` cache (no re-calls); `max_calls_per_run = 150` bound; circuit-breaker; metrics in report |
| Slowdown on Chinese text (lingua, dateparser) | **Low** | `lingua` detection on 300 chars only; faster `langdetect` fallback; `dateparser` configured with current timezone |

### Maintenance

| Risk | Severity | Mitigation |
|---|---|---|
| Free sources disappear or change (Google News RSS, DDG, Brave) | **High** | Configurable source cascade; `invest-watch sources validate` detects dead feeds; sources editable in DB without redeployment |
| SQLite schema drift during evolution | **Medium** | Versioned SQL migrations (`app_meta.db_version`); migration tests (`v1 → v2`); automatic backup before schema upgrade |
| watsonx.ai API / SDK evolution | **Medium** | `llm_client` is a port (interface) — the watsonx.ai implementation is swappable without touching the rest of the code |
| Undetected LLM hallucinations in reports | **High** | Factual prompts (`do not invent anything`); strict JSON schema with bounds and enumerations; mandatory `#id` references; "unverified" section; human review of critical items |
| Paywalls and dynamic content inaccessible | **High** | Metadata only retained (title, OpenGraph excerpt); no headless browser in V1; test fixtures covering these cases |
| Corrupted recovery after crash | **Low** | `run.lock` + upsert idempotence + daily backup; `--resume` only replays `new` / `retrying` errors |

---

## 7. Architecture Decisions — Decided ✔

All decisions below have been validated and are integrated in sections §4, §8 and Annex §A.

| # | Decision | Chosen option | Implementation consequences |
|---|---|---|---|
| DA-01 ✔ | **watsonx.ai SDK + model + region** | Integrate `ibm-watsonx-ai` via `ModelInference`; model and region URL selectable in `.env` (`WATSONX_MODEL_ID`, `WATSONX_URL`) | See §A — full SDK integration with code example, region list and `.env` pattern |
| DA-02 ✔ | **LLM budget** | **Unlimited** — no blocking `max_calls_per_run` cap; call metrics remain traced in `daily_run.stats_json` | Remove the cost bound in `[llm]`; keep `max_calls_per_run` as informational only (non-blocking) |
| DA-03 ✔ | **V2 graphical interface** | **Local FastAPI web** bound to `127.0.0.1` | `fastapi` + `uvicorn` dependency; tests via `httpx.AsyncClient(app=app)`; `invest-watch gui` launches `uvicorn` |
| DA-04 ✔ | **Chinese sources without RSS** | `type = 'website'` in `source` table; standard `fetcher` (`httpx` + `trafilatura`) for NBS, PBoC, CSRC | Add `type = 'website'` to `source.type` `CHECK` enum; `search_engine` can call `fetcher` directly for these sources |
| DA-05 ✔ | **LLM cache** | **In-memory cache per run only** — `{content_hash: summary_json}` dict in RAM, not persisted | No intra-run re-calls; no `--resume` recovery planned (see DA-10); articles not yet summarised are reprocessed on the next run |
| DA-06 ✔ | **FTS5 purge** | **Separate background task** (asynchronous) — not synchronous with the main transaction | `purge_fts5()` called at step 15 in a separate thread; main workflow is not blocked if it is slow |
| DA-07 ✔ | **Institutional source testing** | Static fixtures `tests/fixtures/` + `httpx-mock`; CI entirely offline | Create directories `tests/fixtures/rss/`, `tests/fixtures/html/` with NBS, PBoC, HKEXnews, SEC EDGAR examples |
| DA-08 ✔ | **Brave Search** | Included in V1; free tier at 1,000 req/month; API key in `.env` (`BRAVE_API_KEY`) | Add `brave` as a `search` source type in the cascade (after Google News RSS, before DuckDuckGo); `search_engine` calls Brave Search API with `BRAVE_API_KEY` |
| DA-09 ✔ | **Catalyst calendar F22 population** | Automatic extraction via regex + LLM only; no manual entry interface | `insight_engine` handles extraction; no dedicated CLI entry form needed for Step 8 |
| DA-10 ✔ | **LLM run recovery (`--resume`)** | No `--resume` planned; in-memory cache per run only; re-processing on next run is acceptable | Remove `--resume` from scope; simplifies `orchestrator` and `llm_client`; DA-05 updated accordingly |
| DA-11 ✔ | **FTS5 purge mode** | Separate background task (asynchronous) | `purge_fts5()` runs in a thread at step 15; does not block the main transaction; DA-06 updated accordingly |

---

## 8. Implementation Plan by Steps

Each step is **independent, testable and deliverable** without blocking the next. The order follows ascending technical dependencies.

---

### Step 1 — Foundation: configuration, database, basic CLI

**Scope**: `config_manager`, `db_manager` (initial schema + versioned migrations), `logger`, `cli` (`argparse` skeleton), `company_repository`, `market_repository`, `source_repository`.

**Tests**:
- Valid / invalid / missing-secrets TOML loading
- SQLite schema creation in memory (`sqlite3:///:memory:`)
- CRUD companies, markets, sources (uniqueness, constraints, `parent_id` hierarchy)
- Idempotent `v1 → v2` migration, `app_meta.db_version` updated, data preserved

**Deliverable**: `invest-watch company add/list/edit/remove`, `invest-watch market …`, `invest-watch source …`, `invest-watch db migrate` operational.

---

### Step 2 — Collection: search, extraction and cleaning

**Scope**: `search_engine` (RSS + Google News RSS + DuckDuckGo), `fetcher` (httpx + robots.txt), `cleaner` (trafilatura), `date_detector`, `lang_detector`.

**Tests**:
- Varied HTML fixtures: multilingual, partial paywall, dynamic content, 404, 429, response > 2 MB
- Date filtering: explicit, relative, multiple timezones, ±6 h tolerance, correct `date_certaine`
- `robots.txt` compliance: per-host cache, forbidden paths rejected
- Rate limiting: jitter + rate respected on simulated calls

**Deliverable**: `invest-watch run` produces a list of raw candidates in the database (no processing or report).

---

### Step 3 — Processing: classification, deduplication, short links

**Scope**: `classifier` (company/market/category association rules, scores), `dedup` (4 layers + event clustering), `short_link`.

**Tests**:
- Deduplication truth table: 10 articles (4 URL/hash duplicates, 3 near-identical titles, 1 near-copy MinHash, 2 new) → exact expected statuses
- Event clustering: same company ±24 h → correct `event_id`
- Unique short codes (6-8 chars, `secrets.token_urlsafe`), conformant `[code](url)` rendering

**Deliverable**: articles in database with `new/duplicate/redundant/updated` statuses, `article_company/market/category` junctions, short codes.

---

### Step 4 — LLM: watsonx.ai client and degraded mode

**Scope**: `llm_client` (interface + watsonx.ai implementation + test stub + `off` mode), `error_manager` (circuit-breaker, exponential backoff, per-source isolation).

**Tests**:
- LLM stub: all tests offline, without API key
- Connection/total timeout, 5xx, exhausted quota → 3 retries → circuit-breaker activated (`llm_status = degraded`)
- Invalid JSON → 1 corrective attempt → degraded mode + error logged
- Per-`content_hash` cache: second call on same hash → no LLM call
- `llm.enabled = false` mode: full run without any LLM network call

**Deliverable**: swappable `llm_client` (watsonx.ai / stub / off); degraded mode producing excerpts without ever crashing.

---

### Step 5 — Summaries and daily report

**Scope**: `summarizer` (article JSON summaries + company/market/global digests + degraded mode), `.md` + `.json` report generator.

**Tests**:
- Prompt golden tests: JSON parsing, schema validation (enumerations, 0..1 bounds, max length)
- Out-of-schema response → 1 corrective attempt → controlled degraded mode
- Digests citing `#id`s mapped to existing articles in database
- Report conformant with §13.2 structure (company, market, critical, error sections)
- `model_id` / `prompt_version` present in each summary footer

**Deliverable**: `invest-watch run` produces complete `reports/YYYY-MM-DD.md` + `.json` with traced summaries and citations.

---

### Step 6 — Complete orchestrator and resilience

**Scope**: `orchestrator` (15-step workflow, `run.lock` lock, anti-double-execution, `--resume` recovery, clean shutdown via `stop.flag`, `daily_run` lifecycle management), 90-day purge, `VACUUM INTO` backup.

**Tests**:
- Full run with all mocked sources: `daily_run` in expected state, report generated, no duplicates
- Process killed mid-run → `--resume`: recovery without duplicates or data loss, lock released, consistent status
- Single source failure: `partial` run, other sources processed, `processing_error` populated
- Anti-double-execution: concurrent second `invest-watch run` → immediate exit (code 0)
- Exit codes: `completed = 0`, `partial = 1`, `failed = 2`

**Deliverable**: end-to-end workflow invocable by Windows Task Scheduler (`schtasks`), criteria AC3, AC4, AC13, AC16 validated.

---

### Step 7 — Complete CLI and consultation

**Scope**: all remaining CLI commands (`invest-watch report/status/errors/export/search`), FTS5 search (`invest-watch search "word"`), structured report display.

**Tests**:
- All commands tested on a pre-populated database (no real run)
- `invest-watch search`: FTS5 returns matching articles, purge synchronises FTS5
- `invest-watch status`: `daily_run` state, LLM availability, lock
- `invest-watch export --format md/json`: file conformant with §13.2 format
- No command exposes secrets in its output

**Deliverable**: complete CLI §13.1; acceptance criteria AC1–AC16 fully validated.

---

### Step 8 — Advanced V1+ features (F22–F27)

**Scope**: `insight_engine` — F22 catalyst calendar, F23 momentum detection, F24 figure fact-check, F25 investment thesis, F26 macro drivers, F27 30-90 d sentiment.

**Tests**:
- **F22**: date extraction from IR press release fixtures; manual CLI entry; `TOMORROW` section in report
- **F23**: `count_24h` vs `baseline_30d` calculation; `×2.5 / ×5 / ×10` thresholds; `⚡ MOMENTUM` box in report
- **F24**: LLM stub returning `figures/discrepancies` JSON; `⚠ DISCREPANCIES` block if non-empty
- **F25**: heuristic tagging (no LLM); LLM opt-in; `thesis_signal` in `article_company`
- **F26**: NBS / PBoC / CAAM RSS fixture ingestion; `macro_indicator` upsert; `MACRO DRIVERS` section
- **F27**: `sentiment_history` calculation from `summary.json_payload`; CLI `invest-watch sentiment <company> --days 90`

**Deliverable**: `invest-watch catalysts / momentum / macro / thesis / sentiment` operational; dedicated sections in the report.

---

### Step 9 — Local web interface (V2)

**Scope**: `web_ui` (FastAPI or Flask, bound to `127.0.0.1`) — news/summaries/markets browsing by date, company/market editing, run triggering, `article.flagged` flagging.

**Tests**:
- All routes tested via `httpx.AsyncClient` or `TestClient` (local HTTP client)
- No route accessible outside `127.0.0.1` (binding verification)
- All writes go through repositories (same constraints as the CLI)
- Run trigger: `daily_run` created, interface remains responsive, status refreshed
- No data sent externally (no outbound network request from the UI)

**Deliverable**: `invest-watch gui` starts the server and opens the browser; company/market editing via web interface conformant with specification §17.

---

*Document generated from SPECIFICATIONS.md v1.3 — invest-watch.*

---

## Annex A — IBM watsonx.ai SDK Integration

> Decision DA-01 decided. This annex is the implementation reference for the `llm_client` module.

### A.1 Installation

```bash
pip install ibm-watsonx-ai
```

The package is available on PyPI and maintained by IBM. It provides the `ModelInference` interface for calls to foundation models, as well as `APIClient` and `Credentials` for IAM authentication.

Recommended version in `requirements.txt`:

```
ibm-watsonx-ai>=1.1.22
```

### A.2 Environment Variables (`.env`)

All watsonx.ai configuration variables are declared in the `.env` file (outside git, permissions 700). The model and region are thus **changeable without touching the code**.

```dotenv
# ── watsonx.ai ──────────────────────────────────────────────────────────────
# IBM Cloud IAM API key
WATSONX_API_KEY=your-ibm-cloud-api-key

# Regional endpoint URL — choose from:
#   Dallas (US)    : https://us-south.ml.cloud.ibm.com
#   Frankfurt (EU) : https://eu-de.ml.cloud.ibm.com   <- GDPR recommended
#   London (EU)    : https://eu-gb.ml.cloud.ibm.com
#   Tokyo          : https://jp-tok.ml.cloud.ibm.com
#   Sydney         : https://au-syd.ml.cloud.ibm.com
WATSONX_URL=https://eu-de.ml.cloud.ibm.com

# watsonx.ai project identifier (or space_id for a deployment)
WATSONX_PROJECT_ID=your-project-uuid

# Model to use — examples available by region:
#   ibm/granite-3-8b-instruct
#   ibm/granite-3-3-8b-instruct
#   meta-llama/llama-3-3-70b-instruct
# Check your region's catalogue:
#   curl -H "Authorization: Bearer $TOKEN" \
#        "$WATSONX_URL/ml/v1/foundation_model_specs?version=2024-09-16"
WATSONX_MODEL_ID=ibm/granite-3-8b-instruct
```

The `config_manager` resolves these variables via `${WATSONX_API_KEY}`, `${WATSONX_URL}`, `${WATSONX_PROJECT_ID}`, `${WATSONX_MODEL_ID}` — never hardcoded in `config.toml`.

### A.3 `llm_client` Module Implementation

```python
# invest_watch/llm_client.py
"""
LLM interface — swappable port.
Implementation: IBM watsonx.ai via ibm-watsonx-ai SDK.
Replaceable by a test stub or 'off' mode without modifying
the rest of the code.
"""
from __future__ import annotations

import json
import time
import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


class LLMDegradedError(Exception):
    """Raised when the circuit-breaker is open or off mode is active."""


@dataclass
class LLMClientConfig:
    enabled: bool = True
    model_id: str = "ibm/granite-3-8b-instruct"   # overridden by .env
    url: str = "https://eu-de.ml.cloud.ibm.com"    # overridden by .env
    project_id: str = ""                            # overridden by .env
    api_key: str = ""                               # overridden by .env
    max_new_tokens: int = 700
    temperature: float = 0.1
    top_p: float = 0.9
    timeout_seconds: int = 120
    max_retries: int = 2
    circuit_breaker_threshold: int = 3


class WatsonxLLMClient:
    """Concrete implementation via ibm-watsonx-ai SDK (ModelInference)."""

    def __init__(self, config: LLMClientConfig) -> None:
        self._cfg = config
        self._consecutive_failures = 0
        self._degraded = False
        self._model = None  # initialised on demand (lazy)

        # Per-run memory cache: {content_hash: str} -> JSON str result
        # Reset on each instantiation (= each run)
        self._cache: dict[str, str] = {}

    # ── lazy initialisation ────────────────────────────────────────────────

    def _get_model(self):
        """Creates ModelInference if needed (lazy, avoids import at startup)."""
        if self._model is not None:
            return self._model

        from ibm_watsonx_ai import Credentials
        from ibm_watsonx_ai.foundation_models import ModelInference
        from ibm_watsonx_ai.metanames import GenTextParamsMetaNames as GenParams

        credentials = Credentials(
            url=self._cfg.url,
            api_key=self._cfg.api_key,
        )
        params = {
            GenParams.MAX_NEW_TOKENS: self._cfg.max_new_tokens,
            GenParams.TEMPERATURE: self._cfg.temperature,
            GenParams.TOP_P: self._cfg.top_p,
        }
        self._model = ModelInference(
            model_id=self._cfg.model_id,
            credentials=credentials,
            params=params,
            project_id=self._cfg.project_id,
        )
        log.info("LLM initialised: model=%s url=%s", self._cfg.model_id, self._cfg.url)
        return self._model

    # ── main call ──────────────────────────────────────────────────────────

    def generate(self, prompt: str, content_hash: str | None = None) -> str:
        """
        Sends the prompt and returns the generated text (raw str).
        Handles in-run memory cache, retries and circuit-breaker.
        Raises LLMDegradedError if circuit is open or off mode is active.
        """
        if not self._cfg.enabled:
            raise LLMDegradedError("LLM disabled (llm.enabled = false)")

        if self._degraded:
            raise LLMDegradedError("Circuit-breaker open — degraded mode active")

        # Intra-run memory cache
        if content_hash and content_hash in self._cache:
            log.debug("LLM cache hit for hash %s", content_hash)
            return self._cache[content_hash]

        last_exc: Exception | None = None
        for attempt in range(self._cfg.max_retries + 1):
            try:
                model = self._get_model()
                response = model.generate(prompt=prompt)
                result: str = response["results"][0]["generated_text"]
                self._consecutive_failures = 0
                if content_hash:
                    self._cache[content_hash] = result
                return result

            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                self._consecutive_failures += 1
                log.warning(
                    "LLM error (attempt %d/%d): %s",
                    attempt + 1, self._cfg.max_retries + 1, exc,
                )

                # No retry on 4xx
                status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
                if status and 400 <= int(status) < 500:
                    break

                # Circuit-breaker
                if self._consecutive_failures >= self._cfg.circuit_breaker_threshold:
                    self._degraded = True
                    log.error(
                        "LLM circuit-breaker open after %d consecutive failures",
                        self._consecutive_failures,
                    )
                    raise LLMDegradedError("Circuit-breaker open") from exc

                # Exponential backoff: 2 s, 8 s, 30 s
                if attempt < self._cfg.max_retries:
                    backoff = (2 ** (attempt + 1)) * 2
                    log.info("Backoff %d s before next attempt", backoff)
                    time.sleep(backoff)

        raise LLMDegradedError(f"LLM unavailable after {self._cfg.max_retries + 1} attempts") from last_exc

    # ── utilities ──────────────────────────────────────────────────────────

    @property
    def is_degraded(self) -> bool:
        return self._degraded

    def reset_circuit_breaker(self) -> None:
        """Reset by the orchestrator on the next run."""
        self._degraded = False
        self._consecutive_failures = 0


def build_llm_client(config: LLMClientConfig) -> WatsonxLLMClient:
    """Factory — replace with a stub in tests."""
    return WatsonxLLMClient(config)
```

### A.4 Config loading in `config_manager`

```python
# In config_manager.py — LLM section
import os

def _load_llm_config(raw: dict) -> LLMClientConfig:
    llm_section = raw.get("llm", {})
    return LLMClientConfig(
        enabled=llm_section.get("enabled", True),
        # Model and region: priority .env > config.toml
        model_id=os.environ.get("WATSONX_MODEL_ID")
                 or llm_section.get("model", "ibm/granite-3-8b-instruct"),
        url=os.environ.get("WATSONX_URL")
            or llm_section.get("endpoint", "https://eu-de.ml.cloud.ibm.com"),
        project_id=os.environ.get("WATSONX_PROJECT_ID")
                   or llm_section.get("project_id", ""),
        api_key=os.environ.get("WATSONX_API_KEY", ""),
        max_new_tokens=llm_section.get("max_new_tokens", 700),
        temperature=llm_section.get("temperature", 0.1),
        top_p=llm_section.get("top_p", 0.9),
        timeout_seconds=llm_section.get("timeout_seconds", 120),
        max_retries=llm_section.get("max_retries", 2),
        circuit_breaker_threshold=llm_section.get("circuit_breaker_threshold", 3),
    )
```

**Resolution priority**: `WATSONX_MODEL_ID` variable in `.env` or environment > value in `config.toml` > hardcoded default. Thus, changing the model or region requires **no code modification** — only a `.env` update.

### A.5 Test Stub

```python
# tests/stubs/llm_stub.py
from invest.llm_client import LLMDegradedError

class LLMStub:
    """Replaces WatsonxLLMClient in all tests. Zero network calls."""

    def __init__(self, response: str = '{"faits": [], "importance": "faible"}',
                 raise_after: int = -1):
        self._response = response
        self._raise_after = raise_after
        self._call_count = 0
        self.is_degraded = False

    def generate(self, prompt: str, content_hash: str | None = None) -> str:
        self._call_count += 1
        if self._raise_after >= 0 and self._call_count > self._raise_after:
            raise LLMDegradedError("Stub: simulated circuit open")
        return self._response

    def reset_circuit_breaker(self) -> None:
        self.is_degraded = False
```

### A.6 Available Regions (reference)

| Region code | Endpoint URL | Note |
|---|---|---|
| `us-south` | `https://us-south.ml.cloud.ibm.com` | Dallas — IBM Cloud default region |
| `eu-de` | `https://eu-de.ml.cloud.ibm.com` | Frankfurt — **GDPR recommended** |
| `eu-gb` | `https://eu-gb.ml.cloud.ibm.com` | London |
| `jp-tok` | `https://jp-tok.ml.cloud.ibm.com` | Tokyo |
| `au-syd` | `https://au-syd.ml.cloud.ibm.com` | Sydney (SDK ≥ 1.1.23) |

> To list available models in your region:
> ```bash
> TOKEN=$(curl -s -X POST "https://iam.cloud.ibm.com/identity/token" \
>   -H "Content-Type: application/x-www-form-urlencoded" \
>   -d "grant_type=urn:ibm:params:oauth:grant-type:apikey&apikey=$WATSONX_API_KEY" \
>   | python -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
>
> curl -s -H "Authorization: Bearer $TOKEN" \
>   "https://eu-de.ml.cloud.ibm.com/ml/v1/foundation_model_specs?version=2024-09-16" \
>   | python -m json.tool | grep '"model_id"'
> ```

### A.7 `[llm]` Section in `config.toml` (non-secret values)

```toml
[llm]
enabled = true
# model and endpoint are overridden by WATSONX_MODEL_ID and WATSONX_URL in .env
# The values below serve as fallback if env variables are absent.
model     = "ibm/granite-3-8b-instruct"
endpoint  = "https://eu-de.ml.cloud.ibm.com"
# project_id is overridden by WATSONX_PROJECT_ID in .env
project_id = ""

max_new_tokens            = 700
temperature               = 0.1
top_p                     = 0.9
timeout_seconds           = 120
max_retries               = 2
circuit_breaker_threshold = 3
# max_calls_per_run: informational only (unlimited budget — DA-02)
max_calls_per_run         = 0
```

> **Note DA-02**: `max_calls_per_run = 0` means "no limit". The value is retained as informational in `daily_run.stats_json` for usage tracking, but is never blocking.
