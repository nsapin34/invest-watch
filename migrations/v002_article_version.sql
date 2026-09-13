-- ─────────────────────────────────────────────────────────────────────────────
-- invest-watch — v002: article_version table (content change history, §9.1)
-- Enables the 'updated' status tracking (dedup layer 2) added in Step 3.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS article_version (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id    INTEGER NOT NULL REFERENCES article (id) ON DELETE CASCADE,
    content_hash  TEXT,
    title         TEXT,
    excerpt       TEXT,
    changed_at    TEXT    NOT NULL,
    change_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_article_version_article ON article_version (article_id);