-- invest-watch - v009: weekly per-company summaries (`invest-watch weekly`)
--
-- One row per (ISO week, company). Independent of daily_run: the weekly
-- command reads the articles of the week and spends ONE LLM call per company.
-- A row with llm_used = 1 is skipped on re-run (unless --force), so a run
-- interrupted by a watsonx quota error only re-pays the missing companies.

CREATE TABLE IF NOT EXISTS weekly_summary (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    week              TEXT    NOT NULL,             -- ISO week, e.g. 2026-W41
    company_id        INTEGER NOT NULL REFERENCES company (id) ON DELETE CASCADE,
    period_start      TEXT    NOT NULL,             -- YYYY-MM-DD (Monday)
    period_end        TEXT    NOT NULL,             -- YYYY-MM-DD (Sunday)
    content           TEXT    NOT NULL,
    llm_used          INTEGER NOT NULL CHECK (llm_used IN (0, 1)),
    validation_status TEXT    NOT NULL CHECK (validation_status IN ('ok', 'degraded')),
    model_id          TEXT,
    prompt_version    TEXT,
    article_ids       TEXT,                         -- JSON list of article ids used
    article_count     INTEGER NOT NULL DEFAULT 0,
    input_chars       INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT    NOT NULL,
    UNIQUE (week, company_id)
);

CREATE INDEX IF NOT EXISTS idx_weekly_summary_week ON weekly_summary (week);
