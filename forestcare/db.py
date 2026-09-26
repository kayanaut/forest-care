"""SQLite storage. One file, no server, easy to inspect and back up.

Design rules:
- Robot detections and field photos are candidates, never facts. Only rows in
  `reviews` written by people turn them into verified records.
- `reviews`, `stand_notes` and `events` are append-only (an audit trail).
- Stand status is computed on read from observations, reviews, mission
  coverage and notes, so there is no stored status that can drift.
- Original photo files are kept byte-for-byte; the database stores their
  SHA-256 so their integrity can be checked at any time.

Schema versions (PRAGMA user_version):
  1  robot missions only (prototype 0.1)
  2  field-photo missions: nullable model output / accuracy / count, original
     file columns, mission protocol, label columns on reviews
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 2

_MISSIONS = """
CREATE TABLE {name} (
    id                TEXT PRIMARY KEY,
    robot_id          TEXT NOT NULL,          -- robot or camera that captured the data
    area_name         TEXT,
    started_at        TEXT NOT NULL,
    ended_at          TEXT NOT NULL,
    year              INTEGER NOT NULL,
    -- 'transect': systematic survey, the driven track defines where absence can be inferred.
    -- 'opportunistic': field photos, presence-only; never used to infer absence.
    protocol          TEXT NOT NULL DEFAULT 'transect' CHECK (protocol IN ('transect', 'opportunistic')),
    track_json        TEXT NOT NULL,          -- MultiLineString coordinates (for photos: capture sequence, display only)
    detection_range_m REAL,
    notes             TEXT,
    source_kind       TEXT NOT NULL CHECK (source_kind IN ('simulated', 'robot', 'field_photos')),
    provenance_json   TEXT NOT NULL,
    ingested_at       TEXT NOT NULL
)"""

_OBSERVATIONS = """
CREATE TABLE {name} (
    id                 INTEGER PRIMARY KEY,
    uid                TEXT NOT NULL UNIQUE,  -- robot-side id / PHOTO-<sha256 prefix>; makes re-sends idempotent
    mission_id         TEXT NOT NULL REFERENCES missions(id),
    stand_id           TEXT NOT NULL REFERENCES stands(id),
    observed_at        TEXT NOT NULL,
    lat                REAL NOT NULL,
    lon                REAL NOT NULL,
    gnss_accuracy_m    REAL,                  -- NULL = not reported by the device
    predicted_taxon    TEXT,                  -- NULL = no model prediction (e.g. field photo)
    confidence         REAL,
    target_probability REAL,
    alternatives_json  TEXT NOT NULL DEFAULT '[]',
    plant_count_est    INTEGER,               -- NULL = not estimated
    height_class       TEXT,
    phenology          TEXT,
    image_path         TEXT,                  -- image shown in the UI (for photos: a preview)
    image_sha256       TEXT,
    original_path      TEXT,                  -- untouched original file, relative to the image dir
    original_sha256    TEXT,
    original_filename  TEXT,
    original_bytes     INTEGER,
    metadata_json      TEXT,                  -- full EXIF dump + how each derived value was obtained
    qc_flags_json      TEXT NOT NULL,
    context_json       TEXT NOT NULL,         -- reference-data context captured at ingest
    source_kind        TEXT NOT NULL CHECK (source_kind IN ('simulated', 'robot', 'field_photos')),
    provenance_json    TEXT NOT NULL,
    review_status      TEXT NOT NULL DEFAULT 'pending'  -- latest review decision, denormalised for queries
)"""

SCHEMA = f"""
{_MISSIONS.format(name="IF NOT EXISTS missions")};

CREATE TABLE IF NOT EXISTS stands (
    id          TEXT PRIMARY KEY,             -- PS-0001 ...
    created_at  TEXT NOT NULL
);

{_OBSERVATIONS.format(name="IF NOT EXISTS observations")};
CREATE INDEX IF NOT EXISTS idx_obs_stand ON observations(stand_id);
CREATE INDEX IF NOT EXISTS idx_obs_mission ON observations(mission_id);
CREATE INDEX IF NOT EXISTS idx_obs_status ON observations(review_status);

CREATE TABLE IF NOT EXISTS reviews (
    id              INTEGER PRIMARY KEY,
    observation_id  INTEGER NOT NULL REFERENCES observations(id),
    decision        TEXT NOT NULL CHECK (decision IN ('confirmed', 'rejected', 'uncertain', 'field_visit')),
    corrected_taxon TEXT,
    note            TEXT,
    reviewer        TEXT NOT NULL,
    reviewer_role   TEXT,
    reviewed_at     TEXT NOT NULL,
    source_kind     TEXT NOT NULL CHECK (source_kind IN ('human', 'simulated')),
    -- optional expert labels; they override the device's estimates for this observation
    plant_count     INTEGER CHECK (plant_count IS NULL OR plant_count >= 1),
    height_class    TEXT,
    phenology       TEXT
);
CREATE INDEX IF NOT EXISTS idx_reviews_obs ON reviews(observation_id);

CREATE TABLE IF NOT EXISTS stand_notes (
    id          INTEGER PRIMARY KEY,
    stand_id    TEXT NOT NULL REFERENCES stands(id),
    kind        TEXT NOT NULL CHECK (kind IN ('note', 'monitoring_decision', 'management_action')),
    text        TEXT NOT NULL,
    action_date TEXT,
    recorded_by TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    source_kind TEXT NOT NULL CHECK (source_kind IN ('human', 'simulated'))
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    at          TEXT NOT NULL,
    actor       TEXT NOT NULL,
    kind        TEXT NOT NULL,
    ref         TEXT,
    detail_json TEXT
);
"""

_V1_MISSION_COLS = ("id, robot_id, area_name, started_at, ended_at, year, track_json, detection_range_m, notes,"
                    " source_kind, provenance_json, ingested_at")
_V1_OBS_COLS = ("id, uid, mission_id, stand_id, observed_at, lat, lon, gnss_accuracy_m, predicted_taxon, confidence,"
                " target_probability, alternatives_json, plant_count_est, height_class, phenology, image_path,"
                " image_sha256, qc_flags_json, context_json, source_kind, provenance_json, review_status")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """Rebuild missions/observations with the relaxed constraints; keep every row.

    Follows SQLite's documented procedure for changing constraints (new table,
    copy, drop, rename) with foreign keys switched off for the duration.
    """
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        with conn:
            conn.execute("BEGIN")  # DDL is not auto-wrapped by sqlite3; keep the whole rebuild atomic
            conn.execute(_MISSIONS.format(name="missions_v2"))
            conn.execute(f"INSERT INTO missions_v2 ({_V1_MISSION_COLS}, protocol)"
                         f" SELECT {_V1_MISSION_COLS}, 'transect' FROM missions")
            conn.execute("DROP TABLE missions")
            conn.execute("ALTER TABLE missions_v2 RENAME TO missions")
            conn.execute(_OBSERVATIONS.format(name="observations_v2"))
            conn.execute(f"INSERT INTO observations_v2 ({_V1_OBS_COLS}) SELECT {_V1_OBS_COLS} FROM observations")
            conn.execute("DROP TABLE observations")
            conn.execute("ALTER TABLE observations_v2 RENAME TO observations")
            for col, typ in (("plant_count", "INTEGER CHECK (plant_count IS NULL OR plant_count >= 1)"),
                             ("height_class", "TEXT"), ("phenology", "TEXT")):
                conn.execute(f"ALTER TABLE reviews ADD COLUMN {col} {typ}")
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            problems = conn.execute("PRAGMA foreign_key_check").fetchall()
            if problems:
                raise RuntimeError(f"migration left dangling references: {problems[:5]}")
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def init_db(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    has_tables = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'missions'").fetchone() is not None
    if has_tables and version < 2:
        _migrate_v1_to_v2(conn)
    conn.executescript(SCHEMA)  # creates what is missing (indexes after a rebuild, or a fresh database)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def to_utc_iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()


def log_event(conn: sqlite3.Connection, actor: str, kind: str, ref: str | None, detail: str | None = None) -> None:
    conn.execute("INSERT INTO events (at, actor, kind, ref, detail_json) VALUES (?, ?, ?, ?, ?)",
                 (now_iso(), actor, kind, ref, detail))
