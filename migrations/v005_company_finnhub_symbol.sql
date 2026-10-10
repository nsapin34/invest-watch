-- ─────────────────────────────────────────────────────────────────────────────
-- invest-watch — v005: per-company Finnhub symbol
--
-- The Finnhub `company-news` endpoint takes an exchange-qualified *symbol*,
-- never free text: `symbol=AIQUY` works, `symbol=Air Liquide` returns an empty
-- list, `symbol=SpaceX` returns HTTP 403 ("You don't have access to this
-- resource").  The free tier only serves North-American / OTC symbols, while
-- `ticker` holds the *listing* ticker (Euronext `AI`, HKEX `3750`) used for
-- display, searches and dedup.
--
-- `finnhub_symbol` therefore holds the curated API symbol (nullable: NULL means
-- "not covered by the subscription" → the engine skips the company cleanly, no
-- request, no error).  Sourced from `[[companies]] finnhub_symbol = "..."`.
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE company ADD COLUMN finnhub_symbol TEXT;
