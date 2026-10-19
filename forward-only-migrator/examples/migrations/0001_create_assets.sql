-- Fictional plant data. Brands and names are invented.
CREATE TABLE assets (
    id      INTEGER PRIMARY KEY,
    tag     TEXT NOT NULL UNIQUE CHECK (tag GLOB 'AST-[0-9][0-9][0-9][0-9]'),
    brand   TEXT NOT NULL,
    model   TEXT NOT NULL
);
CREATE TABLE legacy_notes (
    id       INTEGER PRIMARY KEY,
    asset_id INTEGER REFERENCES assets (id),
    note     TEXT
);
INSERT INTO assets (tag, brand, model) VALUES
    ('AST-0001', 'Vortex',   'VX-200 compressor'),
    ('AST-0002', 'Helix',    'HX-50 pump'),
    ('AST-0003', 'Ironwood', 'IW-9 conveyor drive');
