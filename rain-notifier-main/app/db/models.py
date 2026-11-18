"""SQLite schema (spec 7). Plain sqlite3, no ORM."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS zones (
    name            TEXT PRIMARY KEY,
    met_districts   TEXT,
    points          TEXT,
    centroid        TEXT
);

CREATE TABLE IF NOT EXISTS dropoff_points (
    zone            TEXT NOT NULL,
    address         TEXT NOT NULL,
    lat             REAL,
    lon             REAL,
    PRIMARY KEY (zone, address)
);

CREATE TABLE IF NOT EXISTS observations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    zone            TEXT NOT NULL,
    point           TEXT NOT NULL,
    source          TEXT NOT NULL,
    ts              TEXT NOT NULL,          -- UTC ISO-8601
    precip_mm_h     REAL,
    probability_pct REAL,
    kind            TEXT NOT NULL DEFAULT 'nowcast',  -- nowcast | warning | forecast | rain_window | zone_warning
    warning_type    TEXT,
    text            TEXT
);
CREATE INDEX IF NOT EXISTS idx_obs_zone_ts ON observations (zone, ts);

CREATE TABLE IF NOT EXISTS state_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    zone            TEXT NOT NULL,
    from_state      TEXT NOT NULL,
    to_state        TEXT NOT NULL,
    ts              TEXT NOT NULL,          -- UTC ISO-8601
    reason          TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_zone_ts ON state_events (zone, ts);

CREATE TABLE IF NOT EXISTS alerts_sent (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    zone            TEXT NOT NULL,
    state           TEXT NOT NULL,
    ts              TEXT NOT NULL,
    text            TEXT,
    message_id      TEXT,
    merged          INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS acks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id        INTEGER,
    zone            TEXT NOT NULL,
    user            TEXT,
    ts              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS incentive_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    zone            TEXT NOT NULL,
    ts              TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'manual'
);

CREATE TABLE IF NOT EXISTS source_health (
    name            TEXT PRIMARY KEY,
    ok              INTEGER NOT NULL DEFAULT 0,
    last_ok         TEXT,
    last_error      TEXT,
    updated         TEXT
);

CREATE TABLE IF NOT EXISTS snoozes (
    zone            TEXT PRIMARY KEY,
    until_utc       TEXT NOT NULL
);
"""
