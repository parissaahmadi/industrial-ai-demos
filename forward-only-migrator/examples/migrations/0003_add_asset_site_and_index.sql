ALTER TABLE assets ADD COLUMN site TEXT;
CREATE INDEX idx_work_orders_asset ON work_orders (asset_id, status);
