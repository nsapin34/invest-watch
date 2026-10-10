-- invest-watch - v010: thematic company links
--
-- company.topic_keywords / company.peers: JSON lists from config.toml
-- ([[companies]] topic_keywords / peers). The classifier links an article
-- to a company even without naming it:
--   direct : company name / alias / ticker found (previous behaviour)
--   peer   : a competitor/partner is named AND a topic keyword is present
--   sector : enough topic keywords (scoring.topic_link_min)

ALTER TABLE company ADD COLUMN topic_keywords TEXT;
ALTER TABLE company ADD COLUMN peers TEXT;

ALTER TABLE article_company ADD COLUMN link_type TEXT NOT NULL DEFAULT 'direct'
    CHECK (link_type IN ('direct', 'sector', 'peer'));

CREATE INDEX IF NOT EXISTS idx_article_company_link ON article_company (company_id, link_type);
