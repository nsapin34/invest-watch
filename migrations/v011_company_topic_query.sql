-- invest-watch - v011: per-company Brave topic query
--
-- One extra Brave query per company (entity_type 'topic') aimed at the
-- company's sector rather than its name. It shares max_brave_queries_per_run
-- with the company and market queries (served after companies, before
-- markets) and counts toward max_queries_per_run. Collected articles carry no
-- forced company link: the classifier decides (direct / sector / peer / none).
-- Nullable: NULL = no topic query.

ALTER TABLE company ADD COLUMN topic_query TEXT;
