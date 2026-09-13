# Functional and Technical Specifications — "invest-watch" Application

Automated daily monitoring of information published the previous day regarding companies held by a user (global portfolio, strong China component), from free Internet sources, local storage, and optional analysis via an LLM on IBM watsonx.ai.

> **Version 1.3 — 2026-09-08 — Options §4.6, §4.7 and §17 decided; V2 GUI and investment-oriented ideas (A1, A2, A3, B1, B2, B5, China pack) integrated into the V2 roadmap (§17). Decision D19 (interface form) to be confirmed. All other default options are validated as-is.**
> **Guiding principle:** any unavailable optional service (LLM, source, engine, network) triggers degraded mode; the application never stops due to the absence of an optional service.

---

## 1. Executive Summary

**The problem.** A personal investor holds shares of companies spread across all continents, with a significant share in China. Every morning, they need to know what happened *the previous day* regarding these companies and their markets: financial results, regulation, technology, competition, geopolitics. Without a tool, this requires hours of reading fragmented, multilingual, redundant, and uneven-quality press.

**The solution (MVP).** A local Python application, without a GUI, launched every morning by the operating system's task scheduler. It queries free sources (RSS feeds as a priority, free search engines as fallback), filters content published the previous day, extracts and cleans text from pages, detects language and date, classifies and scores information by company / market / category, detects duplicates and syndicated copies, summarises (watsonx.ai if available, otherwise via heuristics), stores everything in a SQLite file, and produces a daily Markdown + JSON report organised by company and by market. Every piece of information remains traceable to its source via a local short link.

**Guiding principles:**

- **Free and compliant:** free sources, respect for `robots.txt`, terms of service, and rate limits.
- **Traceability:** no fact without a reference to a retained article and an original URL.
- **The LLM invents nothing:** summaries based solely on collected content, validated JSON output, mandatory references.
- **Tolerable partial failure:** a failed source (or the LLM) does not interrupt processing of other sources.
- **Simplicity:** a CLI, a single database file, zero servers, zero GUI.

## 2. Assumptions and Open Questions

Every undecided question receives a **reasonable default value**, to be confirmed before development. Choices marked "to decide" must be arbitrated in §20.

| # | Question | Proposed default value | Rationale |
|---|---|---|---|
| Q1 | Company list format | TOML file + SQLite table, editable via CLI `invest-watch company add/list/edit/remove` | Single config; CLI avoids manual editing errors |
| Q2 | Company identification | Name + ticker (opt.) + ISIN (opt.) + country + official website + main market + aliases (opt.); minimum: name + country | Aliases compensate for variations ("BYD", "BYD Company Limited", "比亚迪"); ISIN serves as deduplication pivot |
| Q3 | Definition of markets and sectors | 3-level taxonomy: **market** (region/exchange: "China A", "Nasdaq"), **sector** (GICS level), **industry** (GICS level); hierarchy via `parent_id`, editable | Recognised and stable standard; customisable regions |
| Q4 | Source languages | French, English, Chinese (Simplified); customisable per company | Covers global press + China |
| Q5 | "Day" timezone | `Europe/Paris` (configurable) | "Previous day" is defined in this timezone; Asian compromise handled in §6.3 and §10 |
| Q6 | Launch time | 07:00 local | After Asian markets close, before European open |
| Q7 | Max collection duration | 90 min overall; 30 s/page; 10 s/feed | Partial failure is tolerated (§10) |
| Q8 | Max tracked companies | 50 | Caps network volume and LLM cost |
| Q9 | Database type | SQLite — single file `data/invest.db` | Simple, reliable, zero services, built-in FTS5, free, no quotas |
| Q10 | Free search provider | Cascade: Editorial RSS → Google News RSS → DuckDuckGo; optional Brave/Tavily | See §4 and §6 |
| Q11 | Free service limits and quotas | DDG ~1 req/3 s; Brave ~2,000 req/month (to verify at implementation); Tavily ~1,000 credits/month; **application quota: 40 requests/day** | Avoid IP blocking and overages |
| Q12 | watsonx.ai model | Granite instruct (`ibm/granite-3-8b-instruct` — to confirm in catalogue) | Interchangeable via the `LLMClient` interface |
| Q13 | watsonx.ai authentication | IAM API key + regional endpoint + `project_id`; official SDK `ibm-watsonx-ai` | Key via environment variable / restricted `.env` file (§8.7) — user choice |
| Q14 | Summary format | Article: 3-6 sentences + structured JSON; digests: Markdown by sections with `#id` references | JSON for machine, Markdown for human |
| Q15 | Data retention | Text content: 90 days (auto-purge); metadata: unlimited; daily backup | Copyright compliance + controlled volume |
| Q16 | Daily report / notification | No in V1 (file report + CLI query) | V2: email / Telegram |
| Q17 | Data sent to LLM | Title, date, source, truncated text ≤ 8,000 characters, candidate company/market list | Minimum necessary (§8.2) |
| Q18 | Errors | Logs + `processing_error` table + report section + non-zero exit code | Verifiable by scheduler |
| Q19 | Report language | French | Articles cited in original language |
| Q20 | Datetimes | UTC storage; thresholds converted to configured timezone | Avoids offset errors |

> **Priority user decisions: D1 (timezone), D6/D7 (search engines), D8/D9 (watsonx.ai), D15 (taxonomy), D16 (sending to LLM).**

## 3. Functional Specifications

### 3.1 Problem Addressed, Users, Daily Operation

- **Problem**: an individual tracks a global portfolio (strong China component) and wastes time searching, reading, deduplicating, and manually classifying information published the previous day about their companies and markets.
- **Users**: an individual non-professional investor, French-speaking, working on Windows (target environment), with zero GUI in V1.
- **Expected daily operation**: every morning at 07:00, the scheduler runs `invest-watch run`; the application collects, processes, stores, and produces `reports/YYYY-MM-DD.md` and `reports/YYYY-MM-DD.json`; the user views results via `invest-watch report`, `invest-watch company X`, `invest-watch market Y`.

### 3.2 Initial Scope Limits

1. Strictly personal, single-user, single-workstation use.
2. No real-time information: stock quotes and prices are neither collected nor computed.
3. Collection limited to the previous day (D-1); no long-term historical catch-up.
4. Static exports (files); no REST API or web server.
5. Coverage: reasonable sample of configured sources, not exhaustive web indexing.

### 3.3 Features Explicitly Excluded from V1

- Graphical user interface and mobile application.
- Notifications (push, email, messaging apps).
- Portfolio management, valuation, performance calculation.
- Price or threshold alerts.
- Multi-user, cloud sync, web interface.
- Real-time collection / streaming.
- Automated article translation.
- Link shortening via an external service (V1 = local short links, §4.5).
- Retrieval of content behind paywalls.

### 3.4 Feature Catalogue (V1)

| # | Feature | Description |
|---|---|---|
| F1 | Tracked company management | CLI CRUD, identification fields, activation, languages, aliases |
| F2 | Market / sector / industry management | CLI CRUD, hierarchy, GICS default set + regions |
| F3 | Company ↔ market association | `company_market` table; n-to-n relationship, optional weight |
| F4 | Internet search | Cascade RSS → search engines; rate limiting; internal quotas |
| F5 | Publication date filtering | D-1 in configured timezone, ±6 h tolerance, `date_certaine` flag |
| F6 | Page content extraction | Polite HTTP (identified User-Agent, robots.txt); HTML → text |
| F7 | Article metadata | title, date, source, author, original URL + final URL after redirection |
| F8 | Language detection | `lingua` on first 300 characters |
| F9 | Classification | companies / markets / categories (rules + optional LLM enrichment) |
| F10 | Relevance / importance scoring | scores 0..1 + level (critical / high / medium / low) |
| F11 | Duplicate detection | 4 layers + event clustering (§7) |
| F12 | Individual article summary | LLM (structured JSON) or degraded mode (excerpt) |
| F13 | Daily digest by company | Markdown digest citing article `#id`s |
| F14 | Daily digest by market | same |
| F15 | Global summary | synthesis of critical points + points to check |
| F16 | Data storage | transactional SQLite inserts / upserts |
| F17 | Short link generation | unique local 6-8 character code; rendering `[code](url)` |
| F18 | Logging | timestamped logs, rotation, levels |
| F19 | Retry after error | `processing_error` + automatic / manual retry |
| F20 | Automatic execution every morning | scheduler-invokable CLI entry, anti-double-run lock |
| F21 | Consultation / export | CLI + Markdown / JSON reports |
| F22 | Catalyst calendar | earnings dates, monthly sales releases, trade shows, regulatory deadlines, agreement expirations; "watch tomorrow" option |
| F23 | Mention spike detection (momentum) | 24 h company article volume vs 30-day baseline → "something is happening" alert (pure heuristic, zero LLM) |
| F24 | Cross-source figure verification | for each event cluster, the LLM extracts key figures from each source and flags discrepancies (+1 LLM call per cluster) |
| F25 | Investment thesis per company | local "my thesis" field (bull/bear case, exit thresholds) + automatic "supports / contradicts / neutral" tagging on each relevant article |
| F26 | Macro/sector driver dashboard | commodity prices (lithium, copper, rare earths, steel), China EV sales (CAAM), China PMI, PBoC decisions, central bank rates and decisions (other regions) |
| F27 | 30-90 day sentiment & momentum | LLM tone curve + volume per company / market over rolling window (added to V2 GUI) |

## 4. Technology Choices

Principle: mature, free libraries with minimal external dependencies. Each choice evaluates role, advantages, limitations, costs / quotas, lock-in risk, and alternative.

### 4.1 Local Database

| Option | Role | Advantages | Limitations | Risk | Alternative |
|---|---|---|---|---|---|
| **SQLite** (`sqlite3` stdlib) ✔ | Persistence in a single file | Zero service, ACID, reliable, portable, built-in FTS5, free, no quotas | Single writer at a time (sufficient here); hot backup via `VACUUM INTO` | None | DuckDB (analytics oriented), PostgreSQL (oversized locally) |

> **Recommendation: SQLite**, `data/invest.db` file, WAL mode enabled, versioned SQL migrations managed by the `db_manager` module.

### 4.2 Free Internet Search Sources and Libraries

| Option | Role | Advantages | Limitations / quotas / costs | Lock-in risk | Alternative |
|---|---|---|---|---|---|
| **RSS feeds** (`feedparser`) ✔ | Primary discovery | Legal (associated with publisher terms), metadata (title, date, link) provided, zero cost | Variable coverage; some feeds cease to exist | Medium (decaying feeds) | Search level 2 |
| **Google News RSS** | Keyphrase search without API key | Free, multilingual (`hl=fr`, `hl=en`, `hl=zh-CN`), approximate "after/before" filter | Undocumented as API; availability not guaranteed; rate limit required | High | DuckDuckGo, Brave |
| **DuckDuckGo** (`duckduckgo-search`) | Web fallback without API key | Free, simple | HTML page scraping: fragile, IP blocking risk, vague terms of service | High | Brave / Tavily |
| **Brave Search API** (free tier) | Stable fallback | ~2,000 free requests/month (to re-verify at implementation), real API | Monthly quota; free tier longevity not guaranteed | Medium | Tavily, Serper (paid) |
| **Tavily Search** (free tier) | LLM-oriented search | ~1,000 credits/month, date bounding parameter | Low quota | Medium | Brave |
| **Free institutional APIs** (SEC EDGAR/FTS, HKEXnews, central banks, INSEE…) | Regulatory and macro sources | Reliable, legal, structured; EDGAR requires ~1 req/s politeness | Limited scope | Low | — |
| **Free Chinese institutional feeds** | China macro and regulatory data | Reliable, official, free, structured; cover national statistics, monetary policy, stock markets, auto sales, and benchmark financial press | Some feeds in Chinese only; rate limits to respect | Low | — |

> **Recommendation: cascading strategy of Editorial RSS → Google News RSS → DuckDuckGo**, with internal quotas (see §6.6).

> **Chinese institutional feeds** (full details in §21.B2): NBS — National Bureau of Statistics (Jiang Jun press conferences + CPI/PMI indicators); PBoC — People's Bank of China (monetary policy releases); CSRC — China Securities Regulatory Commission; SHSE/SZSE — Shanghai and Shenzhen Stock Exchanges (listed company disclosures); HKEXnews — Hong Kong Stock Exchange regulatory filing repository; CAAM — China Association of Automobile Manufacturers (monthly EV sales); financial press: Caixin Global (RSS), South China Morning Post (RSS).

### 4.3 HTTP and Content Extraction

| Option | Role | Advantages | Limitations | Recommendation |
|---|---|---|---|---|
| `httpx` | HTTP client | Modern sync/async API, timeouts, redirects, optional HTTP/2 | — | Used everywhere, `follow_redirects=True` to capture the final URL |
| `trafilatura` | HTML → main text + date | Excellent rendering, reads `datePublished` / `article:published_time`, multilingual | Medium-weight dependency; response size needs bounding | Primary extraction |
| `beautifulsoup4` + `lxml` | Complementary parsing (feeds, meta, JSON-LD) | Flexible | More verbose | Ad hoc |
| `dateparser` | Multilingual / relative dates | "2 hours ago", Chinese formats, etc. | Timezone ambiguities | Always configured with current timezone |
| `lingua-language-detector` | Language detection | Accurate, no downloads | Slower | On first 300 characters; fallback `langdetect` |
| `feedparser` | RSS / Atom parsing | De facto standard | Some non-compliant feeds | — |

### 4.4 LLM Summaries

| Option | Role | Advantages | Limitations / costs | Recommendation |
|---|---|---|---|---|
| `ibm-watsonx-ai` SDK | Calls to hosted model | Official IBM, IAM management (token), streaming, generation parameters | No guaranteed public free tier (demo credits to check on account); parameters to align with current documentation | Concrete implementation behind the `LLMClient` interface |
| **Degraded mode without LLM** | Extractive summaries | Free, offline, always functional | Less refined summaries (excerpt + heuristic facts) | Always toggleable (`llm.enabled = false`) |

### 4.5 Short Link Creation

| Option | Advantages | Limitations | Recommendation |
|---|---|---|---|
| **Local short code** (`short_link` table, 6-8 secure random characters) ✔ | Free, offline, URL never sent to third parties, full traceability | Resolvable only via local database (sufficient for local reports) | V1 |
| is.gd / TinyURL (free keyless APIs) | Real public web links | External dependency, outages, quotas, URL submitted to third party | V2 option, disabled by default |

### 4.6 Scheduling and Background Task

| Option | Advantages | Limitations | Recommendation |
|---|---|---|---|
| **Windows Task Scheduler** ✔ **selected choice** | Native, starts on boot or login, network condition, history | Legacy CLI syntax (`schtasks`) | V1 on Windows — see §12.1 |
| cron (Linux / macOS) | Standard | Differs by OS | Provide crontabs in documentation |
| `schedule` library (Python) | Easy to write | Requires permanent process (fragile, potential leaks) | Not recommended |
| Docker container | Isolation | Overhead, scheduling needs bridging | Not for a local V1 |


### 4.7 Secret Storage

| Option | Advantages | Limitations | Recommendation |
|---|---|---|---|
| **Environment variables / restricted `.env` file** ✔ **selected choice** | Simple, universal, usable by task scheduler without GUI session, cross-OS compatible | Plaintext file on disk — mitigated by 700 permissions, excluded from git, never in logs/db/reports | **V1** — see §11 and §8.7 |
| OS keyring via `keyring` (Windows Credential Manager, macOS Keychain, Linux Secret Service) | Keys never in plaintext on disk, OS-permissioned | Some providers require a GUI session (issues for runs launched without user session) | Alternative / optional evolution |
| Fernet encryption of full config | Encrypted at rest | Encryption key management (chicken-and-egg problem) | V2 evolution |

### 4.8 Report Export

| Format | Usage | Decision |
|---|---|---|
| Markdown | human-readable daily report | V1 |
| JSON | structured machine export (all fields) | V1 |
| **Local web interface** | viewing news, summaries, and markets + editing tracked companies/markets in browser (V2, §17) | **V2 — user decision** |
| HTML | standalone report page export | V2 |
| PDF | printable archiving | V3 — non-essential |

## 5. Proposed Architecture

### 5.1 Overview

```text
Planificateur OS (07h00) ───▶  invest-watch (CLI)
         │                    argparse : run, report, company, market,
         │                    status, errors, export, sources, migrate
         ▼
   ┌───────────────────────────────────────────────────────────┐
   │                      ORCHESTRATEUR                         │
   │   enchaîne les étapes du workflow (§10), tolère les        │
   │   échecs, met à jour daily_run, assemble le rapport        │
   └──┬──────┬──────┬────────┬────────┬────────┬───────┬────────┘
      │      │      │        │        │        │       │
 SEARCH   FETCHER CLEANER CLASSIFIER  DEDUP SUMMARIZER SHORT_LINK
 (rss/    (http+  (trafila- (règles+  (minhash) (LLM ou   (codes
 search/   robots) tura)     LLM opt.)            dégradé)  courts)
  api)
      │      │      │        │        │        │       │
      └──────┴──────┴────────┴────────┴────────┴───────┴────────┘
                                 │
                       ┌─────────┴──────────┐
                       │      DB_MANAGER    │  SQLite data/invest.db
                       │  schéma, migrations│  WAL + FTS5
                       │  repositories      │
                       └────────────────────┘
   CONF (TOML) · LOGGER · ERROR_MANAGER (retries/circuit-breaker) · METRICS
```

### 5.2 Modules and Responsibilities

| Module | Responsibility | Main interactions |
|---|---|---|
| `config_manager` | Load / validate `conf/config.toml`, resolve `${VAR}` and secrets (sources: environment / restricted `.env` file; OS keychain as alternative), provide an immutable config | All modules at initialisation |
| `company_repository` | CRUD companies, aliases, search vectors (name, ticker, ISIN, site) | orchestrator, classifier |
| `market_repository` | CRUD markets / sectors / industries, hierarchy by parent | orchestrator, classifier |
| `search_engine` | Query generation, RSS → Google News RSS → search engines cascade, rate limiting, metadata-annotated results (title, url, date, source) | fetcher, sources |
| `fetcher` | Polite HTTP retrieval of pages: application User-Agent, robots.txt (per-host cache), timeouts, capture of final URL, size bounds | cleaner, short_link |
| `cleaner` | trafilatura → main text; boilerplate removal; extract (400 characters); normalisation | date_detector, lang_detector |
| `date_detector` | Publication date: JSON-LD, meta tags, RSS, then `dateparser`; result flagged "certain / uncertain" | classifier |
| `classifier` | Language, categories, associated companies / markets (rules + optional LLM enrichment), initial scores | db_manager, llm_client |
| `dedup` | URL normalisation, SHA-256 hash, title similarity (Jaccard), MinHash (`datasketch`), event clustering | db_manager |
| `llm_client` | watsonx.ai call interface: auth, prompts, JSON parsing, timeouts, retries, call counter, circuit-breaker | summarizer, classifier |
| `summarizer` | Article summaries (structured JSON), company / market / global digests; degraded mode | llm_client, db_manager |
| `short_link` | Short code generation (secure random, unique in DB), Markdown rendering | db_manager |
| `db_manager` | SQLite connection, WAL, versioned migrations, repositories, FTS5 | all |
| `orchestrator` | Runs the daily workflow, updates `daily_run`, assembles the report, handles resume / lock | all |
| `logger` | stdlib logging, rotating file + per-run file, configurable levels | all |
| `error_manager` | Records `processing_error`, backoff, per-source isolation, LLM circuit-breaker | orchestrator, modules |
| `cli` | User commands (§13) | orchestrator, repositories |
| `web_ui` **(V2)** | Local interactive web interface: server bound to `127.0.0.1`, browsing of today's and previous days' news / summaries / markets, editing of tracked companies and markets, triggering a run; acts exclusively through repositories and the orchestrator | cli, orchestrator, repositories |
| `insight_engine` **(V2)** | Investment-oriented insights (A1–B5, China pack, §17): catalyst calendar, mention spikes, figure fact-checking, investment thesis, macro/sector drivers, 30–90 d sentiment; runs optionally after the V1 workflow | orchestrator, classifier, db_manager |

### 5.3 Interaction Rules

1. **No module writes directly to the database**: everything goes through `db_manager` (repositories); every insertion is tied to a `daily_run`.
2. **All network calls go through `fetcher` / `search_engine`**: global rate limiting (tokens per second), unique User-Agent, timeouts.
3. **`LLMClient` is a port**: watsonx.ai implementation, test stub, and "off" (degraded) mode with zero dependencies.
4. **Each workflow step is idempotent** (upsert under uniqueness constraints) — resuming never creates duplicates.

## 6. Internet Search Strategy

Objective: cover monitoring of tracked companies and their markets using free, legal, and configurable sources.

### 6.1 Query Generation

- **Per company** (from aliases and templates):
  - `{name}` · `{short name}` · `{ticker}` (e.g. `BYDDY`) · `{name} + {industry keyword}`;
  - Chinese-language variant for Chinese companies (official name, e.g. `比亚迪`).
- **Per market / sector**: `{label} news`, `{market name} regulation`, `{sector} {country}`, etc.
- **Volumes**: by default **3 queries / company + 2 queries / active market**, global cap **40 queries per run** (section `max_queries_per_run`).
- **Query safety**: templates never incorporate unsanitised free text; reserved engine characters are escaped.

### 6.2 Restricting to the Previous Day's Information

1. **RSS feeds**: filter on `published` / `published_parsed` converted to UTC, compared against the interval [D-1 00:00, D-1 23:59:59 **in the configured timezone**, converted to UTC].
2. **Search engines**: retain results whose extracted date falls within the interval; Google News RSS "after/before" filters are **hints**, never ground truth.
3. **Unknown date**: article kept with `date_certaine = false`, reduced score, flagged in the report.
4. **Tolerance**: ±6 hours around the interval (configurable) to avoid missing late publications; **no historical catch-up collection in V1**.

### 6.3 Time Zones

- Everything is stored in UTC; the interval is calculated in the user's timezone (Q5/D1).
- For Asian companies, a Chinese morning publication falls on the previous day in Europe: an accepted trade-off, compensated by the ±6 h tolerance and by targeting local stock exchange feeds.
- `dateparser` is always configured with the current timezone; no naive parsing.

### 6.4 Source Prioritisation

| Rank | Type | Configurable V1 examples |
|---|---|---|
| 1 | **Official company** | Press releases and investor relations pages (page + RSS feed if available) |
| 2 | **Regulatory** | SEC EDGAR/FTS, HKEXnews, SHSE/SZSE, CSRC, local authorities; **Chinese institutional feeds**: NBS (CPI/PMI), PBoC, CAAM (EV sales), Caixin RSS, SCMP RSS |
| 3 | **Reference editorial feeds** | Google News RSS by language; RSS feeds from verified financial media |
| 4 | **Major global media** (public access) | DuckDuckGo / Brave queries targeting trusted domains |
| 5 | **Specialist** | Sector-specific sites (batteries, semiconductors, commodities, China…) |

The official source of an event (rank 1-2) is **retained even if redundant**: it is the reference for verification.


### 6.5 Quality Filtering

- Allowed domains / blacklists (generated content, click farms, aggregators without added value) in `[sources]` and the `source` table.
- Minimum constraints: non-empty title, extracted text ≥ 200 characters, plausible date, recognised language.
- Boilerplate detection: useful text / markup ratio; advertising-content-driven pages → excluded.
- Articles with no link to tracked companies / markets → status `unrelated`, not displayed by default.

### 6.6 Compliance, robots.txt, and Rate Limits

- **Identified User-Agent**: `invest-watch/1.0 (+contact address)` — never a disguised browser User-Agent.
- **robots.txt**: respected for web pages (`urllib.robotparser`, per-host cache); RSS feeds are not subject to robots.txt but to publishers' terms of use.
- **Rate**: minimum 1 request / host / 2 s by default (configurable), with random jitter; daily internal quota (40 engine queries).
- **HTTP response codes**: 429 → wait `Retry-After`; 5xx → exponential backoff (2 attempts); 410/403 → source marked as errored (`processing_error`, `source.error_count`).
- **Documented grey area**: HTML scraping of search engines (DuckDuckGo) is the last resort and can be disabled via `use_search_engines = false`.

### 6.7 Inaccessible Pages, Paywalls, Dynamic Content

- Connect/read timeout; responses > 2 MB rejected or truncated.
- **Paywalls**: only metadata is kept (title, date, source, OpenGraph extract / meta description) — no bypassing, ever.
- **Dynamic content**: V1 **without headless browser**; if text is not server-side rendered, only the metadata summary is kept.

### 6.8 Original vs Republished Information

- The **primary source** of an event is the one with the earliest date **or** rank 1-2 (official).
- Others are marked `redundant` / `duplicate` (§7), unless their content differs by more than 10% (threshold `minhash_threshold`) → `complementary` and kept as additional sources.

### 6.9 Source Configuration Mechanism

- `source` table editable via CLI (`invest-watch source add/list/disable/validate`) and/or `[[sources]]` sections in the TOML.
- Fields: URL, type (`rss` / `search` / `website` / `api`), language, priority rank, specific rate limit, active.
- `invest-watch sources validate` tests each source (HTTP code, RSS parse) and reports dead feeds — integrated into metrics (§17).

## 7. Deduplication and Non-Redundancy Strategy

### 7.1 Successive Layers (in execution order)

| # | Layer | Rule | Assigned status |
|---|---|---|---|
| 1 | **Normalised URL** | Normalisation: lowercase scheme/host, removal of tracking parameters (`utm_*`, `fbclid`, `gclid`, `mc_*`), fragment removal, final URL after redirect | `duplicate` (exact match, reference to the initial article) |
| 2 | **Content hash** | SHA-256 of normalised text (lowercase, whitespace unified) | `duplicate` if hash is identical |
| 3 | **Title similarity** | Jaccard on normalised tokens (stopwords removed, stem stripped) ≥ 0.85 | `duplicate` (same wire story) |
| 4 | **Content similarity** | MinHash (`datasketch`, 128 permutations), cosine ≥ 0.9 (configurable) | `redundant` (near-copy) |
| 5 | **Event clustering** | Clustering: same company + ±24 h window + similarity ≥ threshold | the first becomes the "event head" (`event_id`), subsequent ones are attached to it |

### 7.2 Specific Retention Rules

- **Multiple sources retained** for the same event if: the source is official / institutional (rank 1-2), **or** the article brings new facts (difference > 10%), **or** the stories report distinct details.
- **Possible statuses**: `new` (first observation), `updated` (new facts on an already-tracked event), `redundant` (same story, no added value), `duplicate` (identical URL/hash), `unrelated`.
- **History**: `article_version` table (traces of hash/title/extract changes); `article.update_count` incremented.

### 7.3 "New Event vs Update" Decision

An event is **new** if: no article from the **last 14 days** belongs to the same cluster **and** the key facts (entities, figures, dates) differ materially.
It is **updated** if a cluster exists **and** the content differs by ≥ threshold (3 distinct additional facts observed).
Otherwise → `redundant`.

### 7.4 Day-to-Day Anti-Redundancy

- At each run, articles from the **last 7 days** are loaded into memory (hash, titles, MinHash) for comparison.
- Thresholds in `[dedup]`. Exception: official articles (rank 1-2) from a new run are always retained even if redundant, as their update date may be significant.
- The daily report **does not re-display** `duplicate` / `redundant` items from previous days; it displays `updated` items with the note "updated since DD/MM".

## 8. IBM watsonx.ai LLM Integration

### 8.1 Types of Operations Delegated to the LLM

1. **Article summary** (structured JSON: facts, impacts, uncertainties, category, verification needed, scores).
2. **Daily digest per company** and **per market** (from article summaries).
3. **Global summary** (from digests).
4. (Optional, can be disabled) **Reclassification** of an article when rule confidence is low — bounded and validated revision of the category and scores.

Excluded: free-form content generation, buy/sell decisions, translation (outside V2).

### 8.2 Data Sent and Not Sent

**Sent** (only if the article passes the relevance filter):
- Title, publication date, source name, detected language;
- Extracted text truncated to **8,000 characters** (configurable) — otherwise, 1,500-character extract;
- List of candidate companies / markets already classified by rules.

**Never sent**: API keys, local paths, configuration, internal identifiers (`daily_run`, errors), out-of-budget content, content unrelated to tracking, personal data.

### 8.3 Prompts

**Common principles**: factual system role, **strict JSON** output, prohibition on adding information absent from the text ("if the information is not in the text, respond `null` / []"), response prefix `{`.

**Prompt — article summary (template to be adapted to the implementation)**:

```text
Tu es un analyste factuel. Tu résumes UNIQUEMENT le contenu fourni.
CONTEXTE :
- Compagnies suivies : {noms}
- Marchés suivis : {noms}
TEXTE À ANALYSER (langue : {langue}) :
--- DÉBUT ---
{titre}
{date}
{texte}
--- FIN ---
Produis UNIQUEMENT du JSON valide selon ce schéma :
{
  "langue_resume": "fr",
  "titre_resume": "...",
  "categorie": "financial_results|corporate_actions|mergers_acquisitions|regulator|product|operations|macro|geopolitical|competitive|sustainability|other",
  "compagnies_concernees": ["..."],
  "marches_couverts": ["..."],
  "faits": ["..."],
  "consequences_potentielles": [
    {"cible": "...", "impact": "positive|negative|mixed|unknown",
     "horizon": "court|moyen|long|inconnu", "confidence": "haute|moyenne|basse"}
  ],
  "incertitudes_ou_conflits": ["..."],
  "verification_humaine_requise": true,
  "importance": "critique|haute|moyenne|faible",
  "pertinence": 0.0,
  "note_contradictions": ["..."]
}
Regles : ne rien inventer ; si un champ est inconnu, utiliser null ou [] ;
les faits doivent etre verification dans le texte fourni.
```

**Prompt — daily summary per company**: input = list of articles (title, date, source, structured summary and its `#id`); output = Markdown digest with **each fact referenced** by the article's `#id` (`[a12]`), fixed structure: `## Company`, `### Reported facts`, `### Potential consequences`, `### Points to verify`. Same requirements for market and global digests (the global adds a "Critical information" section). Identical schemas at the field level but in Markdown, with a mandatory `[#id]` citation after each assertion.

### 8.4 Expected Response Format and Validation

- Output parsed as JSON; **strict JSON schema validation**: enumerations, score bounds (0..1), maximum length.
- Invalid JSON → 1 single corrective attempt ("the JSON was invalid: fix it"); failing that, degraded fallback + error logged.
- LLM scores are **clamped then re-scaled** by the classifier (relevance 0..1, importance on 4 levels) — never used as-is.

### 8.5 Errors, Timeouts, Retries

- Timeouts: connection 10 s, total **120 s per call** (configurable).
- Retries: 3 attempts with exponential backoff (2 s, 8 s, 30 s) for 5xx / timeout / network; **no retry** on 4xx or exceeded quotas.
- **Circuit-breaker**: 3 consecutive failures → `daily_run.llm_status = degraded` for the rest of the run; the workflow switches to degraded mode; the counter resets at the next run.
- Strict cache: no second call for the same `content_hash`.


### 8.6 Cost and Call Count Reduction

- LLM call **only** for relevant articles (`relevance_score ≥ relevance_min`) and **one call per article**.
- Systematic truncation: text ≤ 8,000 characters (beyond that: degraded mode for that article, no chunked summarisation in V1).
- **Per-hash cache**: content already summarised is never summarised again.
- **Per-run call cap**: `max_calls_per_run = 150`; the counter is exposed in `daily_run.stats_json` and the report.
- Digests reuse article summaries: no recomputation.

### 8.7 Authentication and Secure Handling

- **IAM API key via environment variable or restricted `.env` file** (`invest_watch/.env`, permissions 700, excluded from git) — resolved by the config-manager as `${WATSONX_API_KEY}`; the OS keychain (`keyring`, item `invest-watch/watsonx-api-key`) remains a documented alternative.
- Regional endpoint, `project_id` / `space_id`, model, generation parameters: in `[watsonx]` of the TOML (non-secret).
- No secrets in logs, reports, or exports.

### 8.8 Traceability, Inventory, and Degraded Mode

- Each `summary` carries: `model_id`, `model_version`, `prompt_version`, `llm_used`, `validation_status`, `article_ids` (references `#id`).
- Summaries cite their sources; each article retains its original URL + short code.
- `invest-watch status` displays LLM availability; each report begins with the line "LLM analysis: watsonx.ai (model) — available / degraded / disabled".
- **Full degraded mode**: without SDK, without a key, without LLM network — summaries are produced by heuristics (lead extract + first facts), marked `llm_used = 0`. The workflow never crashes due to the absence of an LLM.

## 9. Data Model

Base `data/invest.db` (SQLite, WAL). Conventions: tables in lowercase, `INTEGER PRIMARY KEY AUTOINCREMENT` keys, dates stored in **UTC** (TEXT ISO-8601), explicit indexes, `CHECK` constraints for enumerations. "Req." = required field.

### 9.1 Tables

**`company`**

| Field | Type | Req. | Constraint / Index |
|---|---|---|---|
| id | INTEGER PK | yes | auto-increment |
| name | TEXT | yes | UNIQUE (case-insensitive comparison) |
| legal_name | TEXT | | |
| ticker | TEXT | | index |
| isin | TEXT | | UNIQUE if present, index |
| country_code | TEXT | | index (ISO 3166-1 alpha-2) |
| currency | TEXT | | (ISO 4217) |
| website | TEXT | | |
| primary_market_id | INTEGER | | FK → market.id |
| summary | TEXT | | |
| active | INTEGER | yes | default 1 |
| created_at / updated_at | TEXT | yes | UTC ISO-8601 |

**`market`**

| Field | Type | Req. | Constraint / Index |
|---|---|---|---|
| id | INTEGER PK | yes | |
| name | TEXT | yes | UNIQUE |
| code | TEXT | | UNIQUE (e.g. "CN.A", "GICS.2510") |
| type | TEXT | yes | CHECK in ('market','sector','industry') |
| parent_id | INTEGER | | FK → market.id (hierarchy) |
| active | INTEGER | yes | |
| created_at / updated_at | TEXT | yes | |

**`company_market`** (n-n relation)

| Field | Type | Req. | Constraint |
|---|---|---|---|
| company_id | INTEGER | yes | FK → company.id, ON DELETE CASCADE |
| market_id | INTEGER | yes | FK → market.id, ON DELETE CASCADE |
| weight | REAL | | relative importance, default 1.0 |

Composite PK (company_id, market_id).

**`source`**

| Field | Type | Req. | Constraint / Index |
|---|---|---|---|
| id | INTEGER PK | yes | |
| name | TEXT | yes | UNIQUE |
| type | TEXT | yes | CHECK in ('rss','search','website','api') |
| url | TEXT | yes | |
| lang | TEXT | | default '*', index |
| priority | INTEGER | yes | default 5, index |
| rate_limit_per_sec | REAL | | default 0.5 |
| active | INTEGER | yes | default 1 |
| last_success_at / last_error_at | TEXT | | |
| error_count | INTEGER | yes | default 0 |
| created_at / updated_at | TEXT | yes | |

**`search_alias`**

| Field | Type | Req. | Constraint |
|---|---|---|---|
| id | INTEGER PK | yes | |
| entity_type | TEXT | yes | CHECK in ('company','market') |
| company_id | INTEGER | | FK → company.id (if company) |
| market_id | INTEGER | | FK → market.id (if market) |
| term | TEXT | yes | |
| lang | TEXT | | default '*' |
| priority | INTEGER | | default 5 |
| active | INTEGER | yes | |
| created_at | TEXT | yes | |

Exclusivity constraint: `CHECK ((entity_type='company' AND company_id IS NOT NULL AND market_id IS NULL) OR (entity_type='market' AND market_id IS NOT NULL AND company_id IS NULL))`.


**`article`**

| Field | Type | Req. | Constraint / Index |
|---|---|---|---|
| id | INTEGER PK | yes | auto-increment |
| run_id | INTEGER | yes | FK → daily_run.id, index |
| source_id | INTEGER | | FK → source.id, index |
| guid | TEXT | | UNIQUE if present (feed identifier) |
| url_original | TEXT | yes | UNIQUE |
| url_normalized | TEXT | yes | UNIQUE, index |
| title | TEXT | yes | |
| author | TEXT | | |
| language | TEXT | | index |
| published_at | TEXT (UTC) | | index |
| date_certaine | INTEGER | yes | 0 = estimated |
| collected_at | TEXT (UTC) | yes | |
| content | TEXT | | stored text, 90-day purge |
| excerpt | TEXT | | first 400 characters, retained |
| content_hash | TEXT | | UNIQUE, index (SHA-256) |
| title_hash | TEXT | | index |
| short_code | TEXT | yes | UNIQUE |
| status | TEXT | yes | CHECK in ('new','updated','redundant','duplicate','unrelated') |
| event_id | INTEGER | | FK → article.id (cluster head), index |
| dedup_reason | TEXT | | |
| relevance_score | REAL | | default 0 |
| importance_level | TEXT | | CHECK in ('critique','haute','moyenne','faible') |
| requires_human_check | INTEGER | | default 0 |
| word_count | INTEGER | | |
| first_seen_run_id | INTEGER | | FK → daily_run.id |
| update_count | INTEGER | | default 0 |
| flagged | INTEGER | | default 0 (user attention) |
| created_at / updated_at | TEXT | yes | |

**`article_company`** — `article_id` FK yes, `company_id` FK yes, `mention_count` INTEGER, `relevance_in_article` REAL; PK (article_id, company_id).
**`article_market`** — identical to `article_company` with `market_id`.
**`article_category`** — `article_id` FK yes, `category_id` FK yes, `confidence` REAL; PK (article_id, category_id).

**`category`** — `id` PK, `name` TEXT UNIQUE yes, `description`.

**`summary`**

| Field | Type | Req. | Constraint / Index |
|---|---|---|---|
| id | INTEGER PK | yes | |
| run_id | INTEGER | yes | FK → daily_run.id, index |
| type | TEXT | yes | CHECK in ('article','company','market','global') |
| ref_id | INTEGER | | target article / company / market id |
| content | TEXT | yes | Markdown (digest) or JSON (article) |
| json_payload | TEXT | | structured article JSON |
| model_id / model_version | TEXT | | traceability |
| prompt_version | TEXT | | |
| llm_used | INTEGER | yes | 0 or 1 |
| content_hash | TEXT | | cache (concerned article) |
| validation_status | TEXT | yes | CHECK in ('ok','degraded','invalid') |
| article_ids | TEXT | | JSON list of referenced `#id`s |
| token_usage | INTEGER | | optional |
| created_at | TEXT | yes | |

Index (run_id, type, ref_id); `UNIQUE (run_id, type, ref_id)` constraint.

**`daily_run`**

| Field | Type | Req. | Constraint / Index |
|---|---|---|---|
| id | INTEGER PK | yes | |
| run_date | TEXT | yes | UNIQUE (YYYY-MM-DD) |
| period_start / period_end | TEXT (UTC) | yes | |
| status | TEXT | yes | CHECK in ('pending','running','completed','partial','failed','cancelled') |
| started_at / finished_at | TEXT | | |
| stats_json | TEXT | | result counts, duplicates, LLM calls, durations |
| error_count | INTEGER | | default 0 |
| llm_status | TEXT | | CHECK in ('ok','degraded','off') |
| trigger | TEXT | | CHECK in ('scheduler','manual','retry') |
| created_at | TEXT | yes | |

**`processing_error`**

| Field | Type | Req. | Constraint |
|---|---|---|---|
| id | INTEGER PK | yes | |
| run_id | INTEGER | | FK → daily_run.id |
| step | TEXT | yes | |
| source_id | INTEGER | | FK → source.id |
| article_id | INTEGER | | FK → article.id |
| error_type | TEXT | yes | |
| message | TEXT | | |
| traceback | TEXT | | |
| retry_count | INTEGER | | default 0 |
| status | TEXT | yes | CHECK in ('new','retrying','failed','resolved') |
| created_at / updated_at | TEXT | yes | |

**`short_link`** — `short_code` TEXT PK, `url_original` TEXT UNIQUE yes, `article_id` FK, `provider` TEXT default 'local', `external_url` TEXT, `created_at` yes.

**`article_version`** — `id` PK, `article_id` FK yes index, `content_hash`, `title`, `excerpt`, `changed_at` yes, `change_reason`.

**`app_meta`** — `key` TEXT PK, `value` TEXT (schema version, last run, LLM catalogue cache…).

### 9.2 Required Stored Data

Original URL ✔ · short link / identifier ✔ (`short_code`) · title ✔ · text content ✔ · excerpt ✔ · source ✔ · publication date ✔ · collection date ✔ · companies ✔ (junction) · markets ✔ (junction) · categories ✔ (junction) · relevance score ✔ · importance score ✔ · hash fingerprint ✔ · generated summary ✔ · model version ✔ · summary article references ✔ (`summary.article_ids`).

### 9.3 Full-Text Search and Retention

- Virtual **FTS5** table `article_fts` (title, excerpt, content) synchronised on inserts — CLI search `invest-watch search "word"`.
- **Daily purge**: `content` of articles older than 90 days set to `NULL` (+ corresponding FTS); metadata, excerpts, summaries and links are retained.

## 10. Daily Workflow

**Common preamble**: `data/run.lock` lock (created in exclusive mode, timestamped) — if present and recent (< 2 h), the second process exits immediately (code 0); otherwise forced recovery is possible via `--force`. Insertion of `daily_run (run_date, status = 'running')`.

| Step | Actions | Output | On failure |
|---|---|---|---|
| 1. Period | D-1 interval in configured timezone, converted to UTC | `period_start`, `period_end` | stop the run |
| 2. Load config | active companies + markets, aliases, active sources | in-memory lists | stop the run |
| 3. Generate queries | 3 / company + 2 / market, per language, capped | deduplicated query queue | continue with existing |
| 4. Query sources | RSS cascade → Google News RSS → engines, with rate limiting | raw candidates (title, url, date, source) | **never blocking**: `processing_error`, `partial` status |
| 5. Filter by date | interval ± tolerance; `date_certaine` assignment | retained candidates | idem |
| 6. Extract content | fetcher + trafilatura | content, excerpt, final URL | failed page → excluded or `fetch_error` |
| 7. Clean text | boilerplate, normalisation | clean text + hash | — |
| 8. Deduplicate | §7 layers vs database (7 days) + intra-run | statuses, `event_id` | — |
| 9. Classify | language, categories, companies / markets, heuristic scores; optional LLM enrichment | junctions + scores | heuristics only |
| 10. Assess importance | rules (category + source authority + magnitude) | `importance_level` | — |
| 11. Summarise | LLM (filter, budget, circuit-breaker) or degraded | `summary` articles | degraded + error |
| 12. Save | idempotent upserts | updated database | transaction does not block the rest |
| 13. Digests | company → market → global (LLM or degraded) | `summary` + report | degraded |
| 14. Report | `reports/YYYY-MM-DD.md` + `.json` | files | partial report logged |
| 15. Wrap-up | `stats_json`, status (`completed` / `partial`), 90-day purge, daily backup | database + backup | — |

**Partial failure**: each failure is isolated by source / page / step; the run switches to `partial` on the first recoverable error; `invest-watch errors` lists everything; `invest-watch run --resume <run_id>` only reprocesses what is missing (idempotence of upserts); `failed` errors are moved back to `retrying` on the next run (max 3 attempts).

**Timezones and China**: the D-1 threshold is strict in the user timezone; for Chinese companies, the ±6 h tolerance mitigates loss of early local morning publications (decision Q5/D1).

## 11. Configuration

Format: **TOML** (`conf/config.toml`), loaded by `tomllib` (Python ≥ 3.11), versionable and commented. Typical structure:

```toml
[app]
timezone = "Europe/Paris"
run_time = "07:00"
max_duration_minutes = 90
language = "fr"

[storage]
db_path = "data/invest.db"
reports_dir = "reports"
backups_dir = "backups"
logs_dir = "logs"
retention_days = 90
backup_enabled = true

[collect]
period_tolerance_hours = 6
min_content_chars = 200
max_response_bytes = 2000000
rate_limit_requests_per_sec = 0.5
max_queries_per_run = 40
queries_per_company = 3
queries_per_market = 2
fetch_user_agent = "invest-watch/1.0 (+contact@example.com)"
respect_robots = true
use_search_engines = true

[dedup]
title_similarity = 0.85
minhash_threshold = 0.9
event_window_hours = 24
seen_days = 7
new_event_lookback_days = 14

[scoring]
relevance_min = 0.5
importance_default = "moyenne"
importance_boost_categories = ["regulator", "financial_results"]
importance_boost_source_priority = [1, 2]

[llm]
enabled = true
model = "ibm/granite-3-8b-instruct"        # verify against your region's catalogue
endpoint = "https://us-south.ml.cloud.ibm.com"
project_id = "..."                          # or space_id
max_input_chars = 8000
max_new_tokens = 700
temperature = 0.1
top_p = 0.9
timeout_seconds = 120
max_retries = 2
circuit_breaker_threshold = 3
max_calls_per_run = 150

[logging]
level = "INFO"
max_bytes = 5000000
backup_count = 5

# Sources: optional (also managed in the database via `invest-watch source add`)
[[sources]]
name = "HKEX News RSS"
type = "rss"
url = "https://www1.hkexnews.hk/rss/..."
lang = "en"
priority = 2

[[sources]]
name = "google-news-rss"
type = "search"
url = "https://news.google.com/rss/search?q={query}&hl={lang}"
lang = "*"
priority = 3

# Companies: optional (also managed via `invest-watch company add`)
[[companies]]
name = "BYD"
ticker = "BYDDY"
isin = "CNE100000296"
country = "CN"
website = "https://www.byd.com"
alias = ["比亚迪"]
langue = ["zh", "en"]
[[companies.markets]]
name = "Véhicules électriques"
type = "industry"
```

**Secrets**: never in plain text in this file — the config-manager resolves `${WATSONX_API_KEY}` from the environment or from the restricted `.env` file (`invest_watch/.env`, permissions 700, excluded from git); the OS keychain (`keyring`, item `invest-watch/watsonx-api-key`) is the documented alternative. `conf/config.example.toml` is provided with fictitious values; `conf/config.toml` and `.env` are git-ignored.

## 12. Background Execution

### 12.1 Recommendation

**On Windows (target environment): Task Scheduler.** Task created at the end of installation (or manually):

```text
schtasks /Create /TN "invest-watch-daily" /TR "py -3 -m invest_watch run" /SC DAILY /ST 07:00 /F
```

- Starts even when the user is not logged in (dedicated account); network condition enabled.
- **Documented alternatives**: Linux/macOS cron (`0 7 * * * cd /… && python -m invest_watch run >> logs/cron.log 2>&1`).
- **No persistent process in V1** (no NSSM service, no `schedule`): the run lives and dies with the invoked process — simple and leak-free.

### 12.2 Isolation, Recovery, Verification, Shutdown

- **Anti-double-execution**: `data/run.lock` lock (`O_CREAT | O_EXCL`); second process → immediate exit (code 0); lock removed at end of run; `--force` to override after timestamp verification.
- **Recovery from interrupted run**: `invest-watch run --date <YYYY-MM-DD> --resume <run_id>` re-reads `processing_error` (status `new` / `retrying`); idempotent upserts prevent duplicates; expensive steps (LLM) are only replayed for unsummarised articles.
- **Knowing whether execution succeeded**: exit code ≠ 0 on global failure, `data/last_run.status` file, `daily_run.status` table, written report.
- **Logs**: `logs/invest.log` (rotation) + `logs/run-YYYY-MM-DD.log` per execution.
- **Time and timezone**: `run_time` + `timezone` in the TOML; the application computes the period (D-1) itself — it does not depend on the scheduler's timezone.
- **Clean shutdown**: `invest-watch stop` writes `data/stop.flag`; the workflow checks the flag between steps and exits as `cancelled`; Ctrl-C is handled (rollback of the current transaction, lock cleanup).

## 13. Interface and Output Format

### 13.1 CLI Commands (V1)

| Command | Role |
|---|---|
| `invest-watch run` | daily collection (manual or scheduler) |
| `invest-watch run --date <YYYYMMDD> [--resume <run_id>]` | relaunch / resume a day |
| `invest-watch report [--date <YYYYMMDD>]` | display the latest report (or one for a specific date) |
| `invest-watch company <name>` | company information |
| `invest-watch market <name>` | market / sector information |
| `invest-watch status` | database state, last run, LLM availability, lock |
| `invest-watch errors [--limit N]` | recent errors |
| `invest-watch export --format md/json --date <YYYYMMDD>` | human-readable export |
| `invest-watch search "words"` | full-text search (FTS5) |
| `invest-watch company add/list/edit/remove` · `invest-watch market …` · `invest-watch source …` | entity management |
| `invest-watch db migrate` | apply migrations |
| `invest-watch gui` (V2) | starts the local web interface server (`127.0.0.1`) and opens the default browser; stopped with Ctrl-C |

### 13.2 Daily Markdown Report Structure

```text
# invest-watch — Report of 2026-09-08

**Period covered**: 2026-09-07 00:00 → 23:59:59 (Europe/Paris)
**LLM analysis**: watsonx.ai (ibm/granite-3-8b-instruct, prompt v1.0) — available ✔
**Execution**: run #142 · completed · 34 min · 2 minor errors

## Global summary of 2026-09-08
(3-5 sentences + "Critical points" + "Points to verify")

## 🚨 Critical information
| Company | Title | Impact (horizon) | Source | Check |
| BYD | (title) | negative (short), conf. high | [a12](url) | ✔ |

## By company
### BYD (CN)
**Reported facts**: … [a12] [a15]
**Potential consequences**: …
**Points to verify**: …

## By market
### Electric vehicles (sector)
**Facts**: … [a12]
### China — regulation

## Key articles of the day
| # | Date | Company | Market | Cat. | Score | Title | Source |
| a12 | 07-09 | BYD | EV | regulator | 0.87 | … | [a12](url) |

## Unverified information / uncertainties
- (extracts from `summary.json_payload.incertitudes_ou_conflits` and `note_contradictions`)

## Errors & inaccessible sources
- feed "Reuters": 429 after 2 attempts; page X: paywall (metadata only).

> Auto-generated report — every fact is verifiable in its original source.
```

### 13.3 Display Rules

- **Short links**: local code displayed as `[a12]`, Markdown link `[a12](url_originale)`, key `"a12": "url"` in JSON.
- **Display priority**: importance (critical → high) → date → score.
- **Explicit distinction between three registers**: ✅ reported fact (with source) / 🔮 potential consequence (horizon + confidence) / ❓ hypothesis or unverified element (dedicated list).
- Contradictory information: `note_contradictions` field included in the "unverified" section.
- Each summary displays `model_id` / `prompt_version` in the footer.

## 14. Security, Compliance and Privacy

| Topic | Proposed measure |
|---|---|
| **API keys** | Environment variable / restricted `.env` file (git-excluded, permissions 700) — V1 choice; OS keychain (`keyring`) as alternative; never in `config.toml`, logs or reports; documented rotation |
| **Local data protection** | SQLite database stored locally; **no inbound network component in V1** (in V2, web interface bound to `127.0.0.1` only); file permissions restricted to the user; WAL; V2 option: SQLCipher encryption or OS-encrypted folder |
| **Local web interface (V2)** | Server bound to `127.0.0.1` only — no network exposure; no remote access or network authentication (local access only); every write goes through the same validations as the CLI (repositories); reads and edits are logged; no data sent externally |
| **Data sent to LLM** | Minimum necessary (§8.2): title, date, source, truncated text, candidates; strict truncation; **acknowledged and documented risk**: content is transmitted to IBM Cloud (configurable region, EU preference) |
| **Retention rules** | Text content 90 days (auto-purge); metadata, excerpts, summaries retained; backups 30 days |
| **Website terms of use** | robots.txt compliance, identified User-Agent, polite rate limiting; RSS and official APIs by default; search engine scraping = configurable fallback documented as a grey area; no prohibited techniques (no captcha bypass or paywall circumvention) |
| **Rights over collected content** | Personal private use, short excerpts, automatic deletion; no redistribution |
| **Source traceability** | Every fact cites `#id` → article → original URL → short code; no orphaned information |
| **Misinformation risk** | No blind trust: `date_certaine` status, `requires_human_check`, "unverified" section, contradiction detection, displayed confidence score |
| **LLM hallucination** | Factual prompts ("do not invent anything"), JSON output validated by schema, mandatory references; missing field → `null`; degraded mode produces literal excerpts only |

## 15. Test Plan

Framework: `pytest` + `pytest-cov` (≥ 80%), in-memory SQLite per test, `responses` / `httpx-mock` for networking, `LLMClient` stub everywhere; HTML / RSS / JSON fixture pages in `tests/fixtures/`. All tests run **offline and without an API key**.

| Category | Tested content | Success criteria |
|---|---|---|
| **Unit** | config_manager, URL normalisation, hash, date parsing, scores, short links, purge | expected paths OK, edge cases handled (unknown date, out-of-bounds score), no unhandled exceptions |
| **Web interface (V2)** | browsing (news by company, summaries, markets for today and previous days), editing tracked companies and markets, triggering a run, last run status | routes tested via a local HTTP client; no route accessible outside `127.0.0.1`; writes conform to constraints (repositories); run status page is consistent |
| **Integration (workflow)** | full run with mocked sources | database in expected state (daily_run, junctions, counters), report generated, no duplicate articles |
| **Simulated web data** | varied HTML pages (local, multilingual, partial paywall, dynamic, 404, 429) | correct extraction, accurate metadata, failures classified, run not blocked |
| **Deduplication** | URL variants, identical hashes, near-identical titles, wire story reprints, event 2 days apart | truth table: each case gets the expected status (`duplicate` / `redundant` / `updated` / `new` / `complementary`) |
| **Date filtering** | explicit dates, relative dates, timezones, ±6 h tolerance | correct selection; `date_certaine` accurate |
| **Summary generation** | golden tests for prompts, JSON parsing, schema validation | valid and conformant JSON; out-of-schema response → 1 retry then degraded |
| **LLM unavailability** | timeout, 5xx, quota, invalid JSON | circuit-breaker triggered, degraded mode, `partial` run, recovery on next run |
| **Source unavailability** | dead feed, 403, malformed feed | source isolated (`processing_error`), other sources processed |
| **Recovery after interruption** | process killed mid-run, `--resume` | recovery without duplicates or data loss, lock released, consistent status |
| **Scheduling** | CLI invocation as launched by the scheduler | expected exit code, `last_run.status` written |
| **Database migration** | schema upgrade v1 → v2 | data preserved, `app_meta.db_version` up to date, migration idempotent |
| **Non-regression** | full suite run on every integration | zero regressions, coverage reported |
| **Invalid LLM responses** | malformed JSON, out-of-enumeration content, exhausted tokens | controlled rejection, bounded retries, degraded mode without crash |

## 16. MVP — Minimal Viable Scope

**Included**: TOML configuration; complete CLI; SQLite + migrations; companies and aliases (CLI); default hierarchical markets; full daily workflow (§10); RSS → Google News RSS → DuckDuckGo cascade (`use_search_engines = false` toggle); D-1 filter; trafilatura; language + date detection; rule-based classification; heuristic scores; 4-layer deduplication + event clustering; watsonx.ai LLM summaries with full degraded mode; local short links; Markdown + JSON reports; logs + errors + resumption; 90-day purge; daily backup; Windows scheduled task; test plan (§15).

**Excluded from MVP** (deferred): graphical interface (§17), LLM enrichment of low-confidence classification, advanced FTS5 search, HTML export, notifications, external shortener, chunked summarisation, Docker, multi-user.

## 17. Future Evolutions

**V2 (other improvements) — validated roadmap**: notifications (email / Telegram); standalone HTML export; LLM reclassification at low confidence; per-company trend curves; "degraded sources" dashboard; weekly report; configurable Brave/Tavily API keys; enriched FTS5 full-text search. To be scheduled after MVP validation.

**V2 — Graphical Interface (user decision, form to confirm — D19)**: the user has requested a graphical interface to **browse each company's news, summaries, and markets for the current day and previous days**, as well as to **change tracked companies** and **verify / adjust tracked markets**. Recommendation: **interactive local web interface** (server bound to `127.0.0.1`, browsing via the browser, company/market editing through the same repositories as the CLI), with a desktop application alternative (PySide6/Qt). See §20 D19.

### Functional Specification — V2 Graphical Interface

**Form and launch**

- **Interactive local web interface**: server bound to `127.0.0.1` (no network exposure), navigation via the browser.
- Lightweight web framework (recommendation: FastAPI — alternative: Flask), UI in HTML/CSS/JS, no heavy frontend dependency.
- Command: `invest-watch gui` starts the server and opens the default browser; clean shutdown via Ctrl-C (or `invest-watch stop`).
- Role: complement to the CLI — the CLI remains available and takes priority; the interface is a convenience for browsing and editing.

**Browsing**

- **News by company**: list of articles for the current day and previous days (date picker), with title, date, source, language, status (new / updated / redundant), relevance score, importance level, category, and **clickable short link to the original URL**.
- **Summaries**: individual summary for each article (facts, potential consequences, uncertainties) and daily digests by company, by market, and global.
- **Markets**: tracked markets / sectors / industries, with their associated articles for the selected date.
- **System status**: last run (status, period covered, duration, errors), active sources, LLM availability.

**Editing tracked companies**

- Add, edit (name, ticker, ISIN, country, currency, official website, aliases, languages, primary market, active/inactive) and delete companies.
- Same validations as the CLI (name uniqueness, ISIN, country/market consistency).

**Verifying and adjusting tracked markets**

- Associate / dissociate markets from a company (`company_market` relation).
- Add a market / sector / industry to the taxonomy (fields: name, code, type, parent), adjust the hierarchy and activation.
- These changes immediately feed into the next run's searches (no restart required).

**Other actions**

- "Run a collection" button: triggers an `invest-watch run` via the orchestrator as a background task; the interface remains responsive and the run status is refreshed.
- User flagging "to read" / "to review" on an article (`article.flagged`).

**Interface security**

- Server bound to `127.0.0.1` only — no access from the network; no network authentication (local access).
- All writes go through the repositories (CLI constraints and validations) — never direct database access from the HTML side.
- Browsing and edits are logged; no data sent externally.
- Not exposed: source editing (remains in CLI), multi-user, public API.

**Tests (see §15)**: local routing, editing compliant with constraints, consistent run status, no exposure outside `127.0.0.1`.

**V3 (future)**: LLM-based translation; sentiment analysis and named entity recognition (NER); threshold alerts; multi-user; Docker for CI testing; SQLCipher encryption; integration with market data (prices) separate from the information feed.

## 18. Acceptance Criteria

| # | Criterion | Verification |
|---|---|---|
| AC1 | Management of multiple companies | `invest-watch company add/list` with ≥ 3 companies without regression |
| AC2 | One company associated with multiple markets | `company_market` with ≥ 2 rows for one company; consistent per-market display |
| AC3 | Automatic morning collection | scheduler invokes `invest-watch run` at 07:00; `daily_run` created; anti-duplicate lock verified |
| AC4 | Default D-1 coverage | `period_start` / `period_end` correct in the configured timezone (test with D, D-1, D-2) |
| AC5 | Results in a database file | `data/invest.db` non-empty after a run; repository queries OK |
| AC6 | Original text and URLs preserved | `article.url_original` identical to the source input; `article.content` = text extracted from the fixture |
| AC7 | Sources presented as very short links | unique `short_code` of 6-8 characters; `[code](url)` rendering present in the report |
| AC8 | Duplicates detected | set of 10 articles (4 URL/hash duplicates, 3 identical titles) → exact expected statuses |
| AC9 | No unnecessary repetition on following days | re-run D-1 then D: the D report shows only `new` / `updated` |
| AC10 | Reports organised by company and by market | sections present and consistent with the data |
| AC11 | Summaries citing their sources | each `summary` contains `#id` values mapped to existing articles |
| AC12 | Configurable free sources | `invest-watch source add/list/disable`; feed changes without code |
| AC13 | One source error isolated | 1 invalid feed + 1 OK feed → `partial` run, good source processed |
| AC14 | Degraded mode without LLM | `llm.enabled = false` (and missing key): full run, degraded summaries, mode mentioned in report |
| AC15 | Secrets not in plain text in config | `config.toml` never contains the key; required via environment / restricted `.env` (OS keychain as alternative) on first run |
| AC16 | Executions and errors traced | `daily_run` and `processing_error` populated; `stats_json` consistent; correct exit codes |

## 19. Risks and Mitigation Measures

| Risk | Probability / Impact | Mitigation |
|---|---|---|
| A free search API changes its terms or disappears | High / Medium | Source cascade, configurable sources, internal quotas, documented network fallback |
| IP blocking (DuckDuckGo, aggregators) | Medium / Medium | Polite rate limiting + jitter, low volumes, `use_search_engines` toggle |
| Paywalls / evolving page HTML | High / Low | Metadata only, tolerant trafilatura, tests on varied fixtures |
| Schema drift / SQLite migration | Low / High | Versioned migrations + migration tests + backup before upgrade |
| Unexpected LLM cost | Medium / Medium | Strict bounds, relevance filter, truncation, cache, degraded mode, metrics in report |
| LLM hallucination / bias | Medium / High | Factual prompts, JSON schema, mandatory references, "unverified" section, human review of critical points |
| Non-compliance with a site's terms of use | Medium / Medium | Official RSS/APIs by default, robots.txt, identified User-Agent, no prohibited techniques |
| Exposure of personal portfolio data | Low / High | Local data, optional OS encryption, purge, LLM limited to the minimum required |
| Corrupted crash recovery | Low / Medium | Lock + idempotence + upserts + daily `VACUUM INTO` backup |
| Failures of institutional sources (SEC, HKEXnews…) | Medium / Low | Prioritisation, isolation, `last_success_at` / `error_count` source metric |
| Explosive volumes (50 companies) | Medium / Medium | Quotas, efficient deduplication, purge, bounded FTS5 |

## 20. Final List of Decisions to Make Before Development

| # | Decision | Proposed recommendation | Default value if not decided |
|---|---|---|---|
| D1 | "Day" timezone | `Europe/Paris` | `Europe/Paris` |
| D2 | Launch time | 07:00 | 07:00 |
| D3 | Maximum number of companies | 50 | 50 |
| D4 | Primary source languages | fr + en + zh | fr + en + zh |
| D5 | Database type | SQLite | SQLite |
| D6 | Primary free search engine | RSS + Google News RSS; DuckDuckGo as fallback | same |
| D7 | Supplementary paid API (Brave / Tavily) | no purchase in V1 | none |
| D8 | watsonx.ai model and region | Granite instruct; EU region preferred | to confirm in catalogue at implementation |
| D9 | watsonx.ai account (tier / budget) | to verify (free credits or subscription) | no account: degraded mode |
| D10 | Monthly LLM budget | ≤ €5 (reduced by bounds and cache) | strictly applied bound |
| D11 | Content retention | 90 days | 90 days |
| D12 | Report language and structure | French, §13 structure | same |
| D13 | PDF export | no in V1 | no |
| D14 | Notifications | no in V1 | no |
| D15 | Markets / sectors taxonomy | GICS + custom regions | GICS + regions |
| D16 | Sending content to LLM accepted | yes, limited, explicit (opt-in in TOML) | mandatory opt-in |
| D17 | Backup retention | 30 days | 30 days |
| D18 | Project / Python module name | `invest_watch` (package `invest_watch/`) | `invest_watch` |
| D19 | **V2 graphical interface form** | **Interactive local web interface** (server `127.0.0.1`, FastAPI or Flask, HTML/CSS/JS); alternative: PySide6/Qt desktop application | interactive local web interface |

### Decided Decisions (2026-09-08)

- **§4.6 / §12**: scheduling by **Windows Task Scheduler** retained for V1 (Windows target); cron kept in documentation for Linux/macOS.
- **§4.7 / §8.7 / §11 / §14 / AC15**: secrets via **environment variables / restricted `.env` file** (outside git, 700 permissions); OS keychain (`keyring`) as optional alternative.
- **§17**: the **V2 roadmap (recommended)** is retained for the post-MVP period; a **graphical interface** must be added to V2 (browsing news / summaries / markets for the current day and previous days, management of tracked companies, verification and adjustment of tracked markets) — **recommended form: interactive local web (D19, to confirm)**.
- All other default options in the document are **validated as-is** by the user.

### Final Remarks

- This document is **ready to serve as a basis for implementation**: data model (§9), workflow (§10), prompts (§8.3), and test plan (§15) are written at the "interface specification" level.
- **Points to recheck at implementation date**: the watsonx.ai model catalogue and `ibm-watsonx-ai` SDK parameters (D8), the current state of free tiers for search engines (D6/D7), and the **final form of the V2 graphical interface (D19)**.
- Any decision not decided above uses the default value indicated — and each value is reproducible in `config.toml`.

---

## 21. Detailed Specifications for Advanced Features (A1–A3, B1–B2, B5)

> These features constitute the first batch of post-MVP evolutions. They remain 100% local and integrate into the existing architecture (same SQLite database, same CLI, same workflow).

---

### 21.A1 — Catalyst Calendar (F22)

**Objective**: anticipate market movements by displaying, per company, the upcoming dated events likely to move the share price.

**Sources**: RSS feeds from official Investor Relations pages + public institutional agendas (all free, no LLM required).

**Tracked event types**

| Category | Concrete examples |
|---|---|
| Financial results | Quarterly / half-year / annual publication |
| Monthly sales | BYD, SAIC, Geely and other Chinese manufacturers (monthly figures published ~5th of the month) |
| Trade shows and conferences | CES (January), IAA (September), Auto Shanghai / Beijing (annual alternation), Battery Show |
| Regulatory deadlines | SEC/HKEX/CSRC filing deadlines, CO₂ emission quota updates |
| Agreement expirations | End of partnerships, licences, supplier agreements flagged in previous press releases |

**"Watch tomorrow" option**: each evening, the `TOMORROW` section of the report lists events whose date falls within the next 24 h for active companies.

**Implementation**

- New table `catalyst_event`: `id`, `company_id` FK, `event_type` TEXT, `event_date` TEXT (UTC), `title`, `source_url`, `source_id` FK, `confirmed` INTEGER (0 = estimated, 1 = official date), `notes`, `created_at`, `updated_at`.
- Population: automatic extraction during article processing (regex + optional LLM on IR press releases) + manual CLI entry (`invest-watch catalyst add/list/delete`).
- CLI display: `invest-watch catalysts [--company X] [--days 7]`; dedicated section in the daily Markdown report.
- **Estimated complexity**: Medium — official IR RSS feeds (free), no mandatory LLM.

---

### 21.A2 — Mention Spike / Momentum Detection (F23)

**Objective**: detect that "something is happening" with a company even before reading the articles, using a purely statistical signal.

**Algorithm (zero LLM, purely heuristic)**

1. For each active company, count the number of articles collected in the D-1 window (24 h) → `count_24h`.
2. Calculate the 30-day moving average → `baseline_30d` (stored in `app_meta` or a dedicated table).
3. If `count_24h ≥ baseline_30d × alert_threshold` (default: `2.5`, configurable in `[momentum]` in TOML) → `MOMENTUM` alert in the report.
4. The spike level is graduated: `×2` → "unusual activity", `×5` → "strong spike", `×10` → "exceptional spike".

**Data model**: table `mention_stats` — `company_id` FK, `stat_date` TEXT, `count_24h` INTEGER, `baseline_30d` REAL, `ratio` REAL, `alert_triggered` INTEGER; UNIQUE (company_id, stat_date).

**Display**: `⚡ MOMENTUM` box at the top of the company section in the report if `alert_triggered = 1`; `invest-watch momentum [--company X]` shows the history.

**Estimated complexity**: Low — simple arithmetic calculations, no external dependency.

---

### 21.A3 — Cross-Source Figure Verification (F24)

**Objective**: detect factual discrepancies between different sources covering the same event (e.g. "Reuters: $1.2B vs Bloomberg: $1.5B"), directly delivering analytical value for an investor.

**Trigger**: on each event cluster (`event_id` identical, ≥ 2 articles) including at least one article at `importance_level` ≥ `high`.

**Dedicated prompt (1 LLM call per cluster)**

```
Rôle : analyste financier rigoureux.
Tâche : extraire tous les chiffres clés (montants, %, capacités, volumes, dates) de chacun des {n} articles ci-dessous et signaler tout écart ≥ 5 % ou toute contradiction entre sources.
Format de réponse JSON :
{
  "figures": [{"label": "...", "values": [{"source": "...", "value": "...", "unit": "..."}]}],
  "discrepancies": [{"label": "...", "detail": "..."}],
  "confidence": "high|medium|low"
}
```

- Controlled by existing LLM bounds (§8.6): only executes if `count(cluster) ≤ max_articles_per_cluster` (default 6) and `importance ≥ high`.
- Result stored in `summary` (type `cross_check`, ref_id = cluster's event_id).
- Display: `⚠ DISCREPANCIES DETECTED` block in the report if `discrepancies` is non-empty.

**Estimated complexity**: Medium — +1 LLM call per affected cluster, controlled by existing bounds.

---

### 21.B1 — Investment Thesis per Company (F25)

**Objective**: link each piece of news directly to the investor's position by automatically tagging each article as "supports / contradicts / neutral with respect to my thesis".

**Thesis structure (local fields, never sent to LLM unless explicit opt-in)**

| Field | Type | Description |
|---|---|---|
| `thesis_bull` | TEXT | Personal bullish scenario (reasons to buy / hold) |
| `thesis_bear` | TEXT | Bearish scenario / identified risks |
| `exit_threshold_up` | TEXT | Upside exit threshold or condition |
| `exit_threshold_down` | TEXT | Downside exit threshold or condition |
| `last_reviewed_at` | TEXT | Date of last human review |

These fields are added to the `company` table (versioned migration).

**Automatic article tagging**

- **Heuristic rule (no LLM)**: keyword comparison extracted from `thesis_bull` / `thesis_bear` against the article title + excerpt → quick tagging `thesis_signal` IN ('supports','contradicts','neutral').
- **LLM enrichment (opt-in, `thesis.use_llm = true`)**: 1 LLM call per article at `importance_level ≥ high` with a short prompt summarising the thesis + article excerpt → JSON response `{"signal": "supports|contradicts|neutral", "reason": "..."}`.
- The thesis is **never sent to the LLM** if `thesis.use_llm = false` (default).

**Data model**: column `thesis_signal` TEXT + `thesis_reason` TEXT added to `article_company`; CHECK IN ('supports','contradicts','neutral',NULL).

**CLI**: `invest-watch thesis set <company> [--bull "..." --bear "..." --exit-up "..." --exit-down "..."]`; `invest-watch thesis show <company>`; `invest-watch thesis review` lists recent "contradicts" articles.

**Display**: in the report, each article for a company with a thesis is labelled `[✓ supports]` / `[✗ contradicts]` / `[~ neutral]`; a "Points that contradict my thesis" section at the end of the company section if ≥ 1 `contradicts` article.

**Estimated complexity**: Medium (1 LLM call per relevant article in activated mode, or simple rule without LLM) — 100% local.

---

### 21.B2 — Macro/Sectoral Drivers Dashboard (F26)

**Objective**: monitor the macroeconomic and sectoral indicators that condition investment decisions, in particular for the EV / China sector.

#### Tracked Indicators

**Commodities** (spot price or public index)

| Indicator | Recommended free source |
|---|---|
| Lithium price (carbonate, hydroxide) | Shanghai Metals Market (SMM) RSS / Fastmarkets public releases |
| Copper price (LME) | LME public releases / Reuters commodities RSS |
| Rare earth prices (neodymium, praseodymium…) | SMM RSS / USGS releases |
| Steel price | Mysteel releases / World Steel Association |

**China — macro and EV sector**

| Indicator | Free institutional source |
|---|---|
| Monthly EV sales in China (total + BEV/PHEV) | **CAAM** — China Association of Automobile Manufacturers, published ~5th of the month |
| Manufacturing and non-manufacturing PMI | **NBS** — National Bureau of Statistics (monthly publication) |
| CPI (inflation) | **NBS** — monthly publication |
| Interest rate / RRR / LPR decisions | **PBoC** — People's Bank of China, official releases |

**Other regions — macro**

| Indicator | Source |
|---|---|
| Policy rates (Fed, ECB, BoE, BoJ) | Official central bank releases (RSS) |
| Global / euro area / US PMI | S&P Global PMI releases (RSS) |
| Key bond yields (10-year US, DE, CN) | Public data from finance ministries / central banks |

#### Free Chinese Institutional Feeds (detail)

| Abbreviation | Full name | Content | Typical URL |
|---|---|---|---|
| **NBS** | National Bureau of Statistics of China *(Guojia Tongjiju)* | CPI, PPI, GDP, official PMI, population; Jiang Jun press conferences | `stats.gov.cn` — RSS feeds + press release pages |
| **PBoC** | People's Bank of China *(Zhongguo Renmin Yinhang)* | LPR rates, RRR, open market operations, monetary policy releases | `pbc.gov.cn` — official releases |
| **CSRC** | China Securities Regulatory Commission *(Zhongguo Zhengjianhui)* | Sanctions, new rules, IPO approvals, circulars | `csrc.gov.cn` |
| **SHSE** | Shanghai Stock Exchange *(Shanghai Zhengquan Jiaoyisuo)* | Listed company disclosures (A-shares), notices, suspensions | `sse.com.cn` |
| **SZSE** | Shenzhen Stock Exchange *(Shenzhen Zhengquan Jiaoyisuo)* | Listed company disclosures (A-shares and ChiNext/STAR) | `szse.cn` |
| **HKEXnews** | Hong Kong Stock Exchange regulatory filing repository | Disclosures of Hong Kong-listed companies (H-shares, Red Chips); feed by ticker | `hkexnews.hk` — REST API + RSS feed per company |
| **CAAM** | China Association of Automobile Manufacturers *(Zhongguo Qiche Gongye Xiehui)* | Monthly EV sales (BEV + PHEV), production, exports, by manufacturer | `caam.org.cn` — monthly releases |
| **Caixin Global** | Caixin Media — English edition | Caixin PMI (independent of NBS), macro analysis, Chinese financial sector | `caixin.com` — public RSS feed |
| **SCMP** | South China Morning Post | Political, economic and regulatory news on China / Hong Kong | `scmp.com` — public RSS feed |

> **Integration**: these sources are declared in the `source` table with `type = 'rss'` or `type = 'api'`, `lang = 'zh'` or `'en'`, priority rank 2. They feed the macro dashboard (F26) and the standard collection (F4) according to their nature.

#### Implementation

- New table `macro_indicator`: `id`, `indicator_key` TEXT UNIQUE, `label`, `value` TEXT, `unit`, `period` TEXT (e.g. "2025-05"), `source_id` FK, `published_at` TEXT (UTC), `collected_at`, `created_at`.
- The daily workflow queries the configured macro sources and inserts / updates indicators (`upsert` on `indicator_key + period`).
- CLI: `invest-watch macro [--indicator lithium] [--since 2025-01]`; summary table in the daily report (`MACRO DRIVERS` section).
- **Estimated complexity**: Medium — free institutional feeds, simple structure, no LLM required.

---

### 21.B5 — Sentiment & Momentum 30–90 d (F27)

**Objective**: provide a temporal view of editorial tone and coverage volume per company / market, over a rolling 30–90 day window, to detect underlying trends invisible on a daily basis.

**Two components**

| Component | Data source | Method |
|---|---|---|
| **Mention volume** | `mention_stats.count_24h` (calculated by F23) | 30–90 d historical curve, 7-day moving average |
| **Average tone (sentiment)** | Sentiment score extracted from existing LLM summaries (`summary.json_payload.sentiment`) | Weighted average by `relevance_score` over the window, normalised −1..+1 |

**Enrichment of the existing summary prompt (§8.3)**: add the field `"sentiment": "positive|neutral|negative"` + `"sentiment_score": float −1..1` to the JSON output of F12 (individual summary). Marginal cost: 0 additional tokens if the field is included in the existing JSON format constraint.

**Data model**: table `sentiment_history` — `company_id` FK (or `market_id` FK), `stat_date` TEXT, `sentiment_avg` REAL, `sentiment_count` INTEGER, `volume_7d_avg` REAL; UNIQUE (company_id/market_id, stat_date).

**Display**: grafted onto the V2 graphical interface (§17) — interactive sentiment + volume curve over 30/60/90 d; in V1 CLI: `invest-watch sentiment <company> [--days 90]` returns an ASCII table with weekly values.

**Estimated complexity**: Medium — grafts onto existing data and summaries; the graphical curve is V2, calculation and storage can be activated from V1 onwards.

---

*End of document — invest-watch, functional and technical specifications v1.3.*
