-- ─────────────────────────────────────────────────────────────────────────────
-- invest-watch — v004: allow 'degraded' as a daily_run status
--
-- The orchestrator now distinguishes a legitimately empty configuration
-- (no active sources configured → 'completed', ENF-01) from a run whose
-- configured sources produced no article (robots blocks, consent
-- interstitials, missing API keys → 'degraded').
--
-- SQLite cannot ALTER a CHECK constraint, so the table is rebuilt and its
-- rows copied. The `checkpoint` column added in v003 is preserved.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE daily_run_new (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date     TEXT    NOT NULL UNIQUE,
    period_start TEXT    NOT NULL,
    period_end   TEXT    NOT NULL,
    status       TEXT    NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'partial', 'failed', 'cancelled', 'degraded')),
    started_at   TEXT,
    finished_at  TEXT,
    stats_json   TEXT,
    error_count  INTEGER NOT NULL DEFAULT 0,
    llm_status   TEXT    CHECK (llm_status IN ('ok', 'degraded', 'off')),
    trigger      TEXT    CHECK (trigger IN ('scheduler', 'manual', 'retry')),
    created_at   TEXT    NOT NULL,
    checkpoint   TEXT
);

INSERT INTO daily_run_new
    (id, run_date, period_start, period_end, status, started_at, finished_at,
     stats_json, error_count, llm_status, trigger, created_at, checkpoint)
SELECT
    id, run_date, period_start, period_end, status, started_at, finished_at,
    stats_json, error_count, llm_status, trigger, created_at, checkpoint
FROM daily_run;

DROP TABLE daily_run;
ALTER TABLE daily_run_new RENAME TO daily_run;
