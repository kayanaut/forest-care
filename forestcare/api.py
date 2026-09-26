"""HTTP API and static web UI.

Run:  uv run python -m forestcare serve   (or: uvicorn forestcare.api:create_app --factory)
Docs: http://127.0.0.1:8000/docs (OpenAPI, including the robot ingest contract)
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
import urllib.parse
from collections import Counter
from pathlib import Path
from typing import Iterator

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from . import export, review, stands
from .config import (CORRECTION_TAXA, NOTE_KINDS, REVIEW_DECISIONS, TARGET_TAXON, WEB_DIR, WMS_LAYERS, Settings)
from .db import connect, init_db
from .ingest import ingest_mission
from .models import MissionIn, PhotoImportMeta, ReviewIn, StandNoteIn
from .photos import PhotoError, PhotoSource, import_photos, verify_originals
from .reference import ReferenceData

_MEDIA = {".svg": "image/svg+xml", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
          ".tif": "image/tiff", ".tiff": "image/tiff"}
_SAFE_IMAGE_HEADERS = {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
                       "X-Content-Type-Options": "nosniff"}
MAX_UPLOAD_FILES = 500
_CONFIDENCE_BANDS = [(0.0, 0.5), (0.5, 0.7), (0.7, 0.85), (0.85, 1.01)]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    ref = ReferenceData(settings.reference_dir, settings.thresholds)
    t = settings.thresholds
    with connect(settings.db_path) as c:
        init_db(c)

    app = FastAPI(title="Forest Care Bonn", version="0.1.0",
                  description="Prototype: robot-assisted monitoring of Prunus serotina in Bonn with human verification.")

    def db() -> Iterator[sqlite3.Connection]:
        conn = connect(settings.db_path)
        try:
            yield conn
        finally:
            conn.close()

    # -- robot ingest ------------------------------------------------------------

    @app.post("/api/missions", tags=["robot"])
    def post_mission(mission: MissionIn, conn: sqlite3.Connection = Depends(db)) -> dict:
        """Ingest one survey mission with its candidate detections. Re-sending is safe (idempotent by uid)."""
        return ingest_mission(conn, ref, settings.image_dir, t, mission, actor=f"robot:{mission.robot_id}")

    @app.post("/api/photo-missions", tags=["photos"])
    def post_photo_mission(
        files: list[UploadFile] = File(..., description="Photos of one field visit (a folder upload)"),
        photographer: str = Form(...), area_name: str | None = Form(None), mission_id: str | None = Form(None),
        notes: str | None = Form(None), source_label: str | None = Form(None), simulated: bool = Form(False),
        conn: sqlite3.Connection = Depends(db),
    ) -> dict:
        """Import geotagged field photographs as one opportunistic mission. Originals are kept byte-for-byte."""
        try:
            meta = PhotoImportMeta(photographer=photographer, area_name=area_name or None, mission_id=mission_id or None,
                                   notes=notes or None, source_label=source_label or None, simulated=simulated)
        except ValidationError as exc:
            raise HTTPException(422, json.loads(exc.json(include_url=False, include_context=False)))
        if len(files) > MAX_UPLOAD_FILES:
            raise HTTPException(413, f"at most {MAX_UPLOAD_FILES} files per import; use the command line for larger folders")

        def reader(upload: UploadFile):
            def read() -> bytes:
                upload.file.seek(0)
                return upload.file.read()
            return read

        sources = [PhotoSource(f.filename or "unnamed", reader(f)) for f in files]
        try:
            return import_photos(conn, ref, settings.image_dir, t, sources, meta, actor=f"upload:{meta.photographer}")
        except PhotoError as exc:
            raise HTTPException(409, str(exc))

    @app.get("/api/observations/{obs_id}/original", tags=["photos"])
    def get_original(obs_id: int, conn: sqlite3.Connection = Depends(db)) -> Response:
        """The untouched original file, served only if its SHA-256 still matches the import record."""
        r = conn.execute("SELECT original_path, original_sha256, original_filename FROM observations WHERE id = ?",
                         (obs_id,)).fetchone()
        if r is None or not r["original_path"]:
            raise HTTPException(404, "no original file for this observation")
        base = settings.image_dir.resolve()
        path = (base / r["original_path"]).resolve()
        if not path.is_relative_to(base) or not path.is_file():
            raise HTTPException(404, "original file is missing")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != r["original_sha256"]:
            raise HTTPException(409, "integrity check failed: the stored original no longer matches its import hash")
        name = r["original_filename"].rsplit("/", 1)[-1]
        ascii_name = name.encode("ascii", "replace").decode().replace("?", "_").replace('"', "_")
        return Response(data, media_type=_MEDIA.get(path.suffix.lower(), "application/octet-stream"), headers={
            **_SAFE_IMAGE_HEADERS, "X-Content-SHA256": r["original_sha256"],
            "Content-Disposition": f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{urllib.parse.quote(name)}',
        })

    @app.get("/api/originals/verify", tags=["photos"])
    def get_verify_originals(conn: sqlite3.Connection = Depends(db)) -> dict:
        """Re-hash every stored original photo and report missing or changed files."""
        return verify_originals(conn, settings.image_dir)

    @app.get("/api/missions", tags=["robot"])
    def get_missions(conn: sqlite3.Connection = Depends(db)) -> list[dict]:
        counts = {r["mission_id"]: r["n"] for r in conn.execute("SELECT mission_id, COUNT(*) AS n FROM observations GROUP BY mission_id")}
        out = []
        for m in conn.execute("SELECT * FROM missions ORDER BY started_at"):
            d = dict(m)
            d["track"] = json.loads(d.pop("track_json"))
            d["provenance"] = json.loads(d.pop("provenance_json"))
            d["observations"] = counts.get(m["id"], 0)
            out.append(d)
        return out

    # -- observations & review -------------------------------------------------------

    @app.get("/api/observations", tags=["review"])
    def get_observations(year: int | None = None, status: str | None = None, stand_id: str | None = None,
                         conn: sqlite3.Connection = Depends(db)) -> list[dict]:
        sql = ("SELECT id, uid, mission_id, stand_id, observed_at, lat, lon, gnss_accuracy_m, predicted_taxon,"
               " target_probability, plant_count_est, phenology, review_status, source_kind, image_path"
               " FROM observations WHERE 1=1")
        args: list = []
        if year:
            sql += " AND substr(observed_at, 1, 4) = ?"
            args.append(str(year))
        if status:
            sql += " AND review_status = ?"
            args.append(status)
        if stand_id:
            sql += " AND stand_id = ?"
            args.append(stand_id)
        rows = [dict(r) for r in conn.execute(sql + " ORDER BY observed_at", args)]
        for r in rows:
            path = r.pop("image_path")
            r["image_url"] = f"/api/images/{path}" if path else None
        return rows

    @app.get("/api/observations/{obs_id}", tags=["review"])
    def get_observation(obs_id: int, conn: sqlite3.Connection = Depends(db)) -> dict:
        try:
            return review.observation_detail(conn, obs_id)
        except review.NotFound:
            raise HTTPException(404, "observation not found")

    @app.get("/api/review-queue", tags=["review"])
    def get_queue(conn: sqlite3.Connection = Depends(db)) -> list[dict]:
        return review.review_queue(conn)

    @app.post("/api/observations/{obs_id}/reviews", tags=["review"])
    def post_review(obs_id: int, body: ReviewIn, conn: sqlite3.Connection = Depends(db)) -> dict:
        """Record an expert decision. Reviews are append-only; the latest one counts."""
        try:
            return review.add_review(conn, obs_id, body, source_kind="human")
        except review.NotFound:
            raise HTTPException(404, "observation not found")

    # -- stands ------------------------------------------------------------------------

    @app.get("/api/stands", tags=["stands"])
    def get_stands(conn: sqlite3.Connection = Depends(db)) -> list[dict]:
        return stands.all_stands(conn, ref, t)

    @app.get("/api/stands/{stand_id}", tags=["stands"])
    def get_stand(stand_id: str, conn: sqlite3.Connection = Depends(db)) -> dict:
        s = stands.one_stand(conn, ref, t, stand_id)
        if s is None:
            raise HTTPException(404, "stand not found")
        s["notes"] = stands.stand_notes(conn, stand_id)
        s["observations"] = get_observations(stand_id=stand_id, conn=conn)
        return s

    @app.post("/api/stands/{stand_id}/notes", tags=["stands"])
    def post_note(stand_id: str, body: StandNoteIn, conn: sqlite3.Connection = Depends(db)) -> dict:
        """Human-entered stand log (notes, monitoring decisions, management carried out)."""
        try:
            return stands.add_note(conn, stand_id, body, source_kind="human")
        except KeyError:
            raise HTTPException(404, "stand not found")

    # -- exports -------------------------------------------------------------------------

    @app.get("/api/export/stands.geojson", tags=["export"])
    def export_geojson(include_unverified: bool = False, conn: sqlite3.Connection = Depends(db)) -> JSONResponse:
        data = export.stands_geojson(stands.all_stands(conn, ref, t), include_unverified)
        return JSONResponse(data, headers={"Content-Disposition": 'attachment; filename="forestcare_stands.geojson"'})

    @app.get("/api/export/lanuk-draft.csv", tags=["export"])
    def export_lanuk(conn: sqlite3.Connection = Depends(db)) -> Response:
        body = export.lanuk_draft_csv(conn, stands.all_stands(conn, ref, t))
        return Response("﻿" + body, media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="lanuk_neobiota_entwurf.csv"'})

    # -- reference data, provenance, metadata ------------------------------------------------

    @app.get("/api/reference/{layer}", tags=["reference"])
    def get_reference(layer: str) -> dict:
        try:
            return ref.layer_geojson(layer)
        except KeyError:
            raise HTTPException(404, "unknown layer")

    @app.get("/api/species", tags=["reference"])
    def get_species() -> dict:
        return ref.species

    @app.get("/api/meta", tags=["reference"])
    def get_meta() -> dict:
        return {
            "target_taxon": TARGET_TAXON,
            "statuses": {k: {"label": v[0], "description": v[1]} for k, v in stands.STATUS_INFO.items()},
            "review_decisions": REVIEW_DECISIONS,
            "correction_taxa": CORRECTION_TAXA,
            "note_kinds": NOTE_KINDS,
            "thresholds": dataclasses.asdict(t),
            "wms_layers": WMS_LAYERS,
        }

    @app.get("/api/sources", tags=["reference"])
    def get_sources(conn: sqlite3.Connection = Depends(db)) -> dict:
        def by_kind(sql: str) -> dict:
            return {r[0]: r[1] for r in conn.execute(sql)}
        return {
            "reference": ref.manifest,
            "lanuk_neobiota": {k: v for k, v in ref.lanuk_neobiota.items() if k != "bonn_records"},
            "species_profile_sources": ref.species["sources"],
            "live_map_services": WMS_LAYERS,
            "runtime": {
                "missions": by_kind("SELECT source_kind, COUNT(*) FROM missions GROUP BY source_kind"),
                "observations": by_kind("SELECT source_kind, COUNT(*) FROM observations GROUP BY source_kind"),
                "reviews": by_kind("SELECT source_kind, COUNT(*) FROM reviews GROUP BY source_kind"),
                "stand_notes": by_kind("SELECT source_kind, COUNT(*) FROM stand_notes GROUP BY source_kind"),
            },
        }

    @app.get("/api/summary", tags=["reference"])
    def get_summary(conn: sqlite3.Connection = Depends(db)) -> dict:
        all_s = stands.all_stands(conn, ref, t)
        calibration = []
        for lo, hi in _CONFIDENCE_BANDS:
            c = Counter(r[0] for r in conn.execute(
                "SELECT review_status FROM observations WHERE target_probability >= ? AND target_probability < ?", (lo, hi)))
            calibration.append({"band": f"{lo:.2f}–{min(hi, 1):.2f}", **{k: c[k] for k in REVIEW_DECISIONS}, "pending": c["pending"]})
        c = Counter(r[0] for r in conn.execute("SELECT review_status FROM observations WHERE target_probability IS NULL"))
        if c:
            calibration.append({"band": "no model (field photos)", **{k: c[k] for k in REVIEW_DECISIONS}, "pending": c["pending"]})
        return {
            "stands_by_status": dict(Counter(s["status"] for s in all_s)),
            "needs_inspection": sum(s["needs_inspection"] for s in all_s),
            "observations_by_status": {r[0]: r[1] for r in conn.execute(
                "SELECT review_status, COUNT(*) FROM observations GROUP BY review_status")},
            "years": [r[0] for r in conn.execute("SELECT DISTINCT year FROM missions ORDER BY year")],
            "review_outcome_by_model_probability": calibration,
            "simulated": bool(conn.execute("SELECT 1 FROM missions WHERE source_kind = 'simulated' LIMIT 1").fetchone()),
        }

    @app.get("/api/events", tags=["reference"])
    def get_events(limit: int = Query(100, le=1000), conn: sqlite3.Connection = Depends(db)) -> list[dict]:
        return [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]

    @app.get("/api/images/{path:path}", tags=["review"], include_in_schema=False)
    def get_image(path: str) -> FileResponse:
        base = settings.image_dir.resolve()
        target = (base / path).resolve()
        if not target.is_relative_to(base) or not target.is_file() or target.suffix.lower() not in _MEDIA:
            raise HTTPException(404, "image not found")
        # Images come from robots and uploads; never let an SVG run script if opened directly.
        return FileResponse(target, media_type=_MEDIA[target.suffix.lower()], headers=_SAFE_IMAGE_HEADERS)

    # -- web UI ----------------------------------------------------------------------------

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(Path(WEB_DIR) / "index.html")

    return app

