-- invest-watch - v007: per-company HKEXnews internal stock id
--
-- The HKEXnews title-search endpoint keys announcements by an internal
-- `stockId` (not the listing code): 03750 -> 1000259940, 03393 -> 10254...
-- Mapping: https://www.hkexnews.hk/ncms/script/eds/activestock_sehk_c.json
-- Nullable: NULL means the company is not queried on HKEXnews.

ALTER TABLE company ADD COLUMN hkex_stock_id TEXT;
