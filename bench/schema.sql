-- World Bank open data, shaped as a small star schema.
--
-- The design choices here exist to make the benchmark questions interesting
-- rather than to mirror the API:
--
--   * economies, not countries. The API returns 295 rows, of which 78 are
--     aggregates ("World", "Euro area", "Africa Eastern and Southern"). They sit
--     in the same table with is_country = 0, because that is exactly the trap a
--     text-to-SQL model should have to notice: "how many countries..." is wrong
--     if it counts the World.
--
--   * observations hold measurements only. A missing year is an absent row, not
--     a row with a NULL value. That makes "which countries have no data for
--     2020" a real LEFT JOIN / NOT EXISTS question instead of a scan for NULLs.

CREATE TABLE IF NOT EXISTS regions (
    region_id  TEXT PRIMARY KEY,   -- e.g. 'ECS'
    name       TEXT NOT NULL       -- 'Europe & Central Asia'
);

CREATE TABLE IF NOT EXISTS income_levels (
    income_id  TEXT PRIMARY KEY,   -- 'HIC', 'LIC', 'LMC', 'UMC'
    name       TEXT NOT NULL       -- 'High income'
);

CREATE TABLE IF NOT EXISTS economies (
    iso3          TEXT PRIMARY KEY,  -- 'GRC'
    iso2          TEXT,              -- 'GR'
    name          TEXT NOT NULL,     -- 'Greece'
    region_id     TEXT REFERENCES regions(region_id),        -- NULL for aggregates
    income_id     TEXT REFERENCES income_levels(income_id),  -- NULL for aggregates
    capital_city  TEXT,              -- NULL where the API sends an empty string
    latitude      REAL,
    longitude     REAL,
    is_country    INTEGER NOT NULL   -- 0 for aggregates
);
CREATE INDEX IF NOT EXISTS idx_econ_region ON economies(region_id);
CREATE INDEX IF NOT EXISTS idx_econ_income ON economies(income_id);

CREATE TABLE IF NOT EXISTS indicators (
    indicator_id  TEXT PRIMARY KEY,  -- 'NY.GDP.PCAP.CD'
    name          TEXT NOT NULL,     -- 'GDP per capita (current US$)'
    topic         TEXT               -- coarse grouping, set by the loader
);

CREATE TABLE IF NOT EXISTS observations (
    iso3          TEXT NOT NULL REFERENCES economies(iso3),
    indicator_id  TEXT NOT NULL REFERENCES indicators(indicator_id),
    year          INTEGER NOT NULL,
    value         REAL NOT NULL,
    PRIMARY KEY (iso3, indicator_id, year)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_obs_ind_year ON observations(indicator_id, year);

-- Provenance, so a stale database is visible rather than silently assumed fresh.
CREATE TABLE IF NOT EXISTS load_log (
    id            INTEGER PRIMARY KEY,
    loaded_at     REAL NOT NULL,
    indicator_id  TEXT,
    rows_returned INTEGER,
    rows_stored   INTEGER,
    note          TEXT
);
