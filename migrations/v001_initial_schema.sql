-- ─────────────────────────────────────────────────────────────────────────────
-- invest-watch — v001: initial schema (specifications §9.1, V1 core tables)
-- Conventions: lowercase tables, INTEGER PK AUTOINCREMENT, dates UTC ISO-8601
-- (TEXT), explicit indexes, CHECK constraints for enumerations.
-- ─────────────────────────────────────────────────────────────────────────────

-- Markets / sectors / industries — 3-level taxonomy (parent_id hierarchy)
CREATE TABLE IF NOT EXISTS market (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL UNIQUE,
    code       TEXT    UNIQUE,
    type       TEXT    NOT NULL CHECK (type IN ('market', 'sector', 'industry')),
    parent_id  INTEGER REFERENCES market (id) ON DELETE SET NULL,
    active     INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT    NOT NULL,
    updated_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_market_parent ON market (parent_id);
CREATE INDEX IF NOT EXISTS idx_market_type ON market (type);

-- Companies (max 50, enforced at repository level per ENF-10)
CREATE TABLE IF NOT EXISTS company (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT    COLLATE NOCASE NOT NULL UNIQUE,
    legal_name        TEXT,
    ticker            TEXT,
    isin              TEXT    UNIQUE,
    country_code      TEXT,
    currency          TEXT,
    website           TEXT,
    primary_market_id INTEGER REFERENCES market (id) ON DELETE SET NULL,
    summary           TEXT,
    active            INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_company_ticker ON company (ticker);
CREATE INDEX IF NOT EXISTS idx_company_isin ON company (isin);
CREATE INDEX IF NOT EXISTS idx_company_country ON company (country_code);
CREATE INDEX IF NOT EXISTS idx_company_primary_market ON company (primary_market_id);

-- n-n relation company <-> market
CREATE TABLE IF NOT EXISTS company_market (
    company_id INTEGER NOT NULL REFERENCES company (id) ON DELETE CASCADE,
    market_id  INTEGER NOT NULL REFERENCES market (id) ON DELETE CASCADE,
    weight     REAL    NOT NULL DEFAULT 1.0,
    PRIMARY KEY (company_id, market_id)
);

CREATE INDEX IF NOT EXISTS idx_company_market_market ON company_market (market_id);

-- Sources (cascade priority: rss -> search -> website -> api)
CREATE TABLE IF NOT EXISTS source (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    name               TEXT    NOT NULL UNIQUE,
    type               TEXT    NOT NULL CHECK (type IN ('rss', 'search', 'website', 'api')),
    url                TEXT    NOT NULL,
    lang               TEXT    NOT NULL DEFAULT '*',
    priority           INTEGER NOT NULL DEFAULT 5,
    rate_limit_per_sec REAL    NOT NULL DEFAULT 0.5,
    active             INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    last_success_at    TEXT,
    last_error_at      TEXT,
    error_count        INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT    NOT NULL,
    updated_at         TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_source_lang ON source (lang);
CREATE INDEX IF NOT EXISTS idx_source_priority ON source (priority);
CREATE INDEX IF NOT EXISTS idx_source_type ON source (type);
CREATE INDEX IF NOT EXISTS idx_source_active ON source (active);

-- Search aliases (company or market terms used for query generation)
CREATE TABLE IF NOT EXISTS search_alias (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT    NOT NULL CHECK (entity_type IN ('company', 'market')),
    company_id  INTEGER REFERENCES company (id) ON DELETE CASCADE,
    market_id   INTEGER REFERENCES market (id) ON DELETE CASCADE,
    term        TEXT    NOT NULL,
    lang        TEXT    NOT NULL DEFAULT '*',
    priority    INTEGER NOT NULL DEFAULT 5,
    active      INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at  TEXT    NOT NULL,
    CHECK (
        (entity_type = 'company' AND company_id IS NOT NULL AND market_id IS NULL)
        OR
        (entity_type = 'market' AND market_id IS NOT NULL AND company_id IS NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_alias_company_term
    ON search_alias (company_id, term) WHERE entity_type = 'company';
CREATE UNIQUE INDEX IF NOT EXISTS uq_alias_market_term
    ON search_alias (market_id, term) WHERE entity_type = 'market';
CREATE INDEX IF NOT EXISTS idx_alias_term ON search_alias (term);
CREATE INDEX IF NOT EXISTS idx_alias_entity ON search_alias (entity_type, company_id, market_id);

-- Daily runs (one row per execution; lifecycle managed by the orchestrator)
CREATE TABLE IF NOT EXISTS daily_run (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date     TEXT    NOT NULL UNIQUE,
    period_start TEXT    NOT NULL,
    period_end   TEXT    NOT NULL,
    status       TEXT    NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'partial', 'failed', 'cancelled')),
    started_at   TEXT,
    finished_at  TEXT,
    stats_json   TEXT,
    error_count  INTEGER NOT NULL DEFAULT 0,
    llm_status   TEXT    CHECK (llm_status IN ('ok', 'degraded', 'off')),
    trigger      TEXT    CHECK (trigger IN ('scheduler', 'manual', 'retry')),
    created_at   TEXT    NOT NULL
);

-- Articles (dedup columns per §7; `content` purged after 90 days per §9.3)
CREATE TABLE IF NOT EXISTS article (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id               INTEGER NOT NULL REFERENCES daily_run (id) ON DELETE CASCADE,
    source_id            INTEGER REFERENCES source (id) ON DELETE SET NULL,
    guid                 TEXT    UNIQUE,
    url_original         TEXT    NOT NULL UNIQUE,
    url_normalized       TEXT    NOT NULL UNIQUE,
    title                TEXT    NOT NULL,
    author               TEXT,
    language             TEXT,
    published_at         TEXT,
    date_certaine        INTEGER NOT NULL DEFAULT 0 CHECK (date_certaine IN (0, 1)),
    collected_at         TEXT    NOT NULL,
    content              TEXT,
    excerpt              TEXT,
    content_hash         TEXT    UNIQUE,
    title_hash           TEXT,
    short_code           TEXT    NOT NULL UNIQUE,
    status               TEXT    NOT NULL CHECK (status IN ('new', 'updated', 'redundant', 'duplicate', 'unrelated')),
    event_id             INTEGER REFERENCES article (id) ON DELETE SET NULL,
    dedup_reason         TEXT,
    relevance_score      REAL    NOT NULL DEFAULT 0,
    importance_level     TEXT    CHECK (importance_level IN ('critique', 'haute', 'moyenne', 'faible')),
    requires_human_check INTEGER NOT NULL DEFAULT 0 CHECK (requires_human_check IN (0, 1)),
    word_count           INTEGER,
    first_seen_run_id    INTEGER REFERENCES daily_run (id) ON DELETE SET NULL,
    update_count         INTEGER NOT NULL DEFAULT 0,
    flagged              INTEGER NOT NULL DEFAULT 0 CHECK (flagged IN (0, 1)),
    created_at           TEXT    NOT NULL,
    updated_at           TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_article_run ON article (run_id);
CREATE INDEX IF NOT EXISTS idx_article_source ON article (source_id);
CREATE INDEX IF NOT EXISTS idx_article_language ON article (language);
CREATE INDEX IF NOT EXISTS idx_article_published ON article (published_at);
CREATE INDEX IF NOT EXISTS idx_article_content_hash ON article (content_hash);
CREATE INDEX IF NOT EXISTS idx_article_title_hash ON article (title_hash);
CREATE INDEX IF NOT EXISTS idx_article_status ON article (status);
CREATE INDEX IF NOT EXISTS idx_article_event ON article (event_id);

-- Classification categories
CREATE TABLE IF NOT EXISTS category (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    description TEXT
);

CREATE TABLE IF NOT EXISTS article_company (
    article_id           INTEGER NOT NULL REFERENCES article (id) ON DELETE CASCADE,
    company_id           INTEGER NOT NULL REFERENCES company (id) ON DELETE CASCADE,
    mention_count        INTEGER NOT NULL DEFAULT 1,
    relevance_in_article REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (article_id, company_id)
);

CREATE INDEX IF NOT EXISTS idx_article_company_company ON article_company (company_id);

CREATE TABLE IF NOT EXISTS article_market (
    article_id           INTEGER NOT NULL REFERENCES article (id) ON DELETE CASCADE,
    market_id            INTEGER NOT NULL REFERENCES market (id) ON DELETE CASCADE,
    mention_count        INTEGER NOT NULL DEFAULT 1,
    relevance_in_article REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (article_id, market_id)
);

CREATE INDEX IF NOT EXISTS idx_article_market_market ON article_market (market_id);

CREATE TABLE IF NOT EXISTS article_category (
    article_id  INTEGER NOT NULL REFERENCES article (id) ON DELETE CASCADE,
    category_id INTEGER NOT NULL REFERENCES category (id) ON DELETE CASCADE,
    confidence  REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (article_id, category_id)
);

CREATE INDEX IF NOT EXISTS idx_article_category_category ON article_category (category_id);

-- LLM summaries and digests (per-run unique per type/ref; degraded mode allowed)
CREATE TABLE IF NOT EXISTS summary (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            INTEGER NOT NULL REFERENCES daily_run (id) ON DELETE CASCADE,
    type              TEXT    NOT NULL CHECK (type IN ('article', 'company', 'market', 'global')),
    ref_id            INTEGER,
    content           TEXT    NOT NULL,
    json_payload      TEXT,
    model_id          TEXT,
    model_version     TEXT,
    prompt_version    TEXT,
    llm_used          INTEGER NOT NULL CHECK (llm_used IN (0, 1)),
    content_hash      TEXT,
    validation_status TEXT    NOT NULL CHECK (validation_status IN ('ok', 'degraded', 'invalid')),
    article_ids       TEXT,
    token_usage       INTEGER,
    created_at        TEXT    NOT NULL,
    UNIQUE (run_id, type, ref_id)
);

CREATE INDEX IF NOT EXISTS idx_summary_run_type_ref ON summary (run_id, type, ref_id);
-- Same uniqueness but NULL-friendly (global digests have ref_id NULL):
CREATE UNIQUE INDEX IF NOT EXISTS uq_summary_run_type_ref_coalesce
    ON summary (run_id, type, COALESCE(ref_id, 0));

-- Processing errors (fed by error_manager — Step 4)
CREATE TABLE IF NOT EXISTS processing_error (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER REFERENCES daily_run (id) ON DELETE SET NULL,
    step        TEXT    NOT NULL,
    source_id   INTEGER REFERENCES source (id) ON DELETE SET NULL,
    article_id  INTEGER REFERENCES article (id) ON DELETE SET NULL,
    error_type  TEXT    NOT NULL,
    message     TEXT,
    traceback   TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    status      TEXT    NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'retrying', 'failed', 'resolved')),
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_processing_error_run ON processing_error (run_id);
CREATE INDEX IF NOT EXISTS idx_processing_error_status ON processing_error (status);
CREATE INDEX IF NOT EXISTS idx_processing_error_source ON processing_error (source_id);

-- Local short links (traceability: every fact -> article -> original URL)
CREATE TABLE IF NOT EXISTS short_link (
    short_code   TEXT PRIMARY KEY,
    url_original TEXT    NOT NULL UNIQUE,
    article_id   INTEGER REFERENCES article (id) ON DELETE CASCADE,
    provider     TEXT    NOT NULL DEFAULT 'local',
    external_url TEXT,
    created_at   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_short_link_article ON short_link (article_id);

-- Key/value metadata: schema version, last run, LLM catalogue cache ...
CREATE TABLE IF NOT EXISTS app_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Full-text search index (CLI `invest-watch search`, synced by later steps)
CREATE VIRTUAL TABLE IF NOT EXISTS article_fts USING fts5 (
    article_id UNINDEXED,
    title,
    excerpt,
    content
);