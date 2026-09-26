"""SQLite storage. One file, no server, easy to inspect and back up.

Design rules:
- Robot observations are candidates, never facts. Only rows in `reviews`
  written by people turn them into verified records.
- `reviews`, `stand_notes` and `events` are append-only (an audit trail).
- Stand status is computed on read from observations, reviews, mission
  coverage and notes, so there is no stored status that can drift.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS missions (
    id                TEXT PRIMARY KEY,
    robot_id          TEXT NOT NULL,
    area_name         TEXT,
    started_at        TEXT NOT NULL,
    ended_at          TEXT NOT NULL,
    year              INTEGER NOT NULL,
    track_json        TEXT NOT NULL,          -- MultiLineString coordinates actually driven
    detection_range_m REAL NOT NULL,
    notes             TEXT,
    source_kind       TEXT NOT NULL CHECK (source_kind IN ('simulated', 'robot')),
    provenance_json   TEXT NOT NULL,
    ingested_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stands (
    id          TEXT PRIMARY KEY,             -- PS-0001 ...
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observations (
    id                 INTEGER PRIMARY KEY,
    uid                TEXT NOT NULL UNIQUE,  -- robot-side id, makes re-sends idempotent
    mission_id         TEXT NOT NULL REFERENCES missions(id),
    stand_id           TEXT NOT NULL REFERENCES stands(id),
    observed_at        TEXT NOT NULL,
    lat                REAL NOT NULL,
    lon                REAL NOT NULL,
    gnss_accuracy_m    REAL NOT NULL,
    predicted_taxon    TEXT NOT NULL,
    confidence         REAL NOT NULL,
    target_probability REAL NOT NULL,
    alternatives_json  TEXT NOT NULL,
    plant_count_est    INTEGER NOT NULL,
    height_class       TEXT,
    phenology          TEXT,
    image_path         TEXT,
    image_sha256       TEXT,
    qc_flags_json      TEXT NOT NULL,
    context_json       TEXT NOT NULL,         -- reference-data context captured at ingest
    source_kind        TEXT NOT NULL CHECK (source_kind IN ('simulated', 'robot')),
    provenance_json    TEXT NOT NULL,
    review_status      TEXT NOT NULL DEFAULT 'pending'  -- latest review decision, denormalised for queries
);
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
    source_kind     TEXT NOT NULL CHECK (source_kind IN ('human', 'simulated'))
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


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def to_utc_iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()


def log_event(conn: sqlite3.Connection, actor: str, kind: str, ref: str | None, detail: str | None = None) -> None:
    conn.execute("INSERT INTO events (at, actor, kind, ref, detail_json) VALUES (?, ?, ?, ?, ?)",
                 (now_iso(), actor, kind, ref, detail))
