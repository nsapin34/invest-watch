-- ─────────────────────────────────────────────────────────────────────────────
-- invest-watch — v003: orchestrator checkpoint column (Step 6.2)
--
-- Adds `checkpoint` to `daily_run` so the orchestrator can persist which of
-- the 15 workflow steps completed last.  On --resume the orchestrator skips
-- every step whose id <= checkpoint, preventing duplicate processing.
--
-- Nullable: NULL means the run never reached step 1 (or was created before
-- this migration); a value like 'fetch' means all steps up to and including
-- 'fetch' are done.
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE daily_run ADD COLUMN checkpoint TEXT;
