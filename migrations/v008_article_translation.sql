-- invest-watch - v008: French translation of non-French articles
--
-- Filled at collection time by the Microsoft Translator integration
-- ([translation] section). The original title/content stay the reference for
-- dedup and hashes; the French version feeds the report, the LLM and search.
-- NULL means "not translated" (French source, language not targeted, budget
-- reached or translator unavailable).

ALTER TABLE article ADD COLUMN title_fr TEXT;
ALTER TABLE article ADD COLUMN content_fr TEXT;
