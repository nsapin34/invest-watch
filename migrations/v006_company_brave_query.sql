-- invest-watch - v006: per-company Brave Search query override
--
-- Brave receives a single primary query per company per run. By default it is
-- the company name, which is too vague for short or ambiguous names (CATL,
-- KSTR, Wasion...). `brave_query` (nullable) replaces that primary Brave query
-- when set. Sourced from `[[companies]] brave_query = "..."`.

ALTER TABLE company ADD COLUMN brave_query TEXT;
