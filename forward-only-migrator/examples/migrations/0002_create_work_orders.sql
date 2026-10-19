CREATE TABLE work_orders (
    id       INTEGER PRIMARY KEY,
    asset_id INTEGER NOT NULL REFERENCES assets (id),
    title    TEXT    NOT NULL,
    status   TEXT    NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done'))
);
