"""Mission ingest: validate, place in Bonn, capture context, link to a stand.

Two entry points share the storage helpers below:
  - `ingest_mission`  robot missions (JSON contract, `models.MissionIn`)
  - `photos.import_photos`  a folder of geotagged field photographs
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import sqlite3
from datetime import timedelta
from pathlib import Path

from .config import TARGET_TAXON, Thresholds
from .db import log_event, now_iso, to_utc_iso
from .geo import haversine_m
from .models import ImageIn, MissionIn, ObservationIn
from .reference import ReferenceData

_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/svg+xml": "svg"}


def qc_flags(obs: ObservationIn, ctx: dict, mission: MissionIn, t: Thresholds) -> list[str]:
    flags = []
    if obs.gnss_accuracy_m > t.poor_gnss_m:
        flags.append("poor_gnss")
    lo, hi = t.ambiguous_band
    if lo <= obs.target_probability <= hi:
        flags.append("ambiguous_prediction")
    if obs.predicted_taxon != TARGET_TAXON:
        flags.append("predicted_other_taxon")
    if ctx["landuse"] is None:
        flags.append("outside_mapped_woodland")
    if obs.image is None:
        flags.append("no_image")
    slack = timedelta(minutes=5)
    if not (mission.started_at - slack <= obs.observed_at <= mission.ended_at + slack):
        flags.append("timestamp_outside_mission")
    return flags


def link_stand(conn: sqlite3.Connection, lat: float, lon: float, accuracy_m: float | None, t: Thresholds) -> str:
    """Attach to the stand of the best-matching earlier observation, else open a new stand.

    The allowed distance grows with the GNSS uncertainty of both positions, so a
    sloppy fix under canopy is not split off as a separate stand, while precise
    fixes in open parkland keep neighbouring stands apart. Positions without a
    reported accuracy use `assumed_accuracy_m`.
    """
    search_m = 200.0  # bounding-box prefilter only; the real test is below
    d_lat, d_lon = search_m / 111_200, search_m / 70_400
    rows = conn.execute(
        "SELECT stand_id, lat, lon, gnss_accuracy_m FROM observations"
        " WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
        (lat - d_lat, lat + d_lat, lon - d_lon, lon + d_lon),
    ).fetchall()
    own = accuracy_m or t.assumed_accuracy_m
    best_id, best_score = None, 1.0
    for r in rows:
        allowed = t.stand_spread_m + t.link_sigma_factor * math.hypot(own, r["gnss_accuracy_m"] or t.assumed_accuracy_m)
        score = haversine_m(lat, lon, r["lat"], r["lon"]) / allowed
        if score <= best_score:
            best_id, best_score = r["stand_id"], score
    if best_id is not None:
        return best_id
    n = conn.execute("SELECT COUNT(*) FROM stands").fetchone()[0] + 1
    stand_id = f"PS-{n:04d}"
    conn.execute("INSERT INTO stands (id, created_at) VALUES (?, ?)", (stand_id, now_iso()))
    return stand_id


def save_image(image: ImageIn, image_dir: Path, mission_id: str, uid: str) -> tuple[str, str]:
    try:
        raw = base64.b64decode(image.data_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("image is not valid base64") from exc
    rel = Path(mission_id) / f"{uid}.{_EXT[image.media_type]}"
    target = image_dir / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    return rel.as_posix(), hashlib.sha256(raw).hexdigest()


def uid_exists(conn: sqlite3.Connection, uid: str) -> bool:
    return conn.execute("SELECT 1 FROM observations WHERE uid = ?", (uid,)).fetchone() is not None


def insert_mission(conn: sqlite3.Connection, *, mission_id: str, robot_id: str, area_name: str | None,
                   started_at: str, ended_at: str, year: int, protocol: str, track: list,
                   detection_range_m: float | None, notes: str | None, source_kind: str, provenance: dict) -> None:
    """Insert once; a re-sent mission keeps its first record."""
    if conn.execute("SELECT 1 FROM missions WHERE id = ?", (mission_id,)).fetchone() is not None:
        return
    conn.execute(
        "INSERT INTO missions (id, robot_id, area_name, started_at, ended_at, year, protocol, track_json,"
        " detection_range_m, notes, source_kind, provenance_json, ingested_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (mission_id, robot_id, area_name, started_at, ended_at, year, protocol, json.dumps(track),
         detection_range_m, notes, source_kind, json.dumps(provenance), now_iso()),
    )


_OBS_COLUMNS = ("uid", "mission_id", "stand_id", "observed_at", "lat", "lon", "gnss_accuracy_m", "predicted_taxon",
                "confidence", "target_probability", "alternatives_json", "plant_count_est", "height_class",
                "phenology", "image_path", "image_sha256", "original_path", "original_sha256", "original_filename",
                "original_bytes", "metadata_json", "qc_flags_json", "context_json", "source_kind", "provenance_json")


def insert_observation(conn: sqlite3.Connection, t: Thresholds, *, ctx: dict, **fields) -> str:
    """Link to a stand and store. `fields` uses the column names above (except stand_id/context_json)."""
    stand_id = link_stand(conn, fields["lat"], fields["lon"], fields.get("gnss_accuracy_m"), t)
    row = {**{c: None for c in _OBS_COLUMNS}, "alternatives_json": "[]", **fields,
           "stand_id": stand_id, "context_json": json.dumps(ctx, ensure_ascii=False)}
    conn.execute(f"INSERT INTO observations ({', '.join(_OBS_COLUMNS)}) VALUES ({', '.join('?' * len(_OBS_COLUMNS))})",
                 [row[c] for c in _OBS_COLUMNS])
    return stand_id


def ingest_mission(conn: sqlite3.Connection, ref: ReferenceData, image_dir: Path, t: Thresholds,
                   mission: MissionIn, actor: str) -> dict:
    now = now_iso()
    payload_sha = hashlib.sha256(mission.model_dump_json().encode()).hexdigest()
    insert_mission(
        conn, mission_id=mission.mission_id, robot_id=mission.robot_id, area_name=mission.area_name,
        started_at=to_utc_iso(mission.started_at), ended_at=to_utc_iso(mission.ended_at),
        year=mission.started_at.year, protocol="transect", track=mission.track,
        detection_range_m=mission.detection_range_m, notes=mission.notes, source_kind=mission.source_kind,
        provenance={"robot_id": mission.robot_id, "sensors": mission.sensors, "model": mission.model.model_dump(),
                    "simulator": mission.simulator, "payload_sha256": payload_sha, "ingested_by": actor,
                    "ingested_at": now},
    )

    accepted, duplicates, rejected = 0, 0, []
    for obs in mission.observations:
        if uid_exists(conn, obs.uid):
            duplicates += 1
            continue
        ctx = ref.context(obs.lat, obs.lon)
        if not ctx["in_bonn"]:
            rejected.append({"uid": obs.uid, "reason": "outside the Bonn city boundary"})
            continue
        image_path = image_sha = None
        if obs.image is not None:
            try:
                image_path, image_sha = save_image(obs.image, image_dir, mission.mission_id, obs.uid)
            except ValueError as exc:
                rejected.append({"uid": obs.uid, "reason": str(exc)})
                continue
        insert_observation(
            conn, t, ctx=ctx, uid=obs.uid, mission_id=mission.mission_id, observed_at=to_utc_iso(obs.observed_at),
            lat=obs.lat, lon=obs.lon, gnss_accuracy_m=obs.gnss_accuracy_m, predicted_taxon=obs.predicted_taxon,
            confidence=obs.confidence, target_probability=obs.target_probability,
            alternatives_json=json.dumps([a.model_dump() for a in obs.alternatives]),
            plant_count_est=obs.plant_count_est, height_class=obs.height_class, phenology=obs.phenology,
            image_path=image_path, image_sha256=image_sha, qc_flags_json=json.dumps(qc_flags(obs, ctx, mission, t)),
            source_kind=mission.source_kind,
            provenance_json=json.dumps({
                "source_kind": mission.source_kind, "robot_id": mission.robot_id, "mission_id": mission.mission_id,
                "model": mission.model.model_dump(), "simulator": mission.simulator,
                "mission_payload_sha256": payload_sha, "image_sha256": image_sha, "ingested_at": now,
            }),
        )
        accepted += 1

    result = {"mission_id": mission.mission_id, "accepted": accepted, "duplicates": duplicates, "rejected": rejected}
    log_event(conn, actor, "ingest", mission.mission_id, json.dumps(result))
    conn.commit()
    return result
