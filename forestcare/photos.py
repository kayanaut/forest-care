"""Import a folder of geotagged field photographs as one mission.

Field photos differ from robot detections in three ways, and the import keeps
those differences visible instead of hiding them:

  - There is no model prediction. The expert labels each photo in the review UI.
  - Position accuracy and time zone are often missing from EXIF. Missing values
    stay NULL, get a quality flag, and each derived value records its source.
  - Photos are taken where someone chose to look (opportunistic, presence-only).
    Such missions never count as survey coverage, so they cannot create
    "not re-detected" absences.

Original files are stored byte-for-byte next to a display preview and a
MANIFEST.json. Their SHA-256 is kept in the database, and `verify_originals`
checks that nothing has changed since import.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

from PIL import ExifTags, Image, ImageOps, UnidentifiedImageError
from PIL.TiffImagePlugin import IFDRational

from .config import PHOTO_PREVIEW_MAX_PX, Thresholds
from .db import log_event, now_iso
from .ingest import insert_mission, insert_observation, uid_exists
from .models import PhotoImportMeta
from .reference import ReferenceData

SUPPORTED_SUFFIXES = {".jpg", ".jpeg", ".tif", ".tiff", ".png"}
MAX_PHOTO_BYTES = 60 * 1024 * 1024
BONN_TZ = ZoneInfo("Europe/Berlin")
SYNTHETIC_MARKER = "SYNTHETIC TEST PHOTO"  # written by simulator/photos.py
IMPORTER = {"name": "forestcare-photo-import", "version": "0.2.0"}


class PhotoError(ValueError):
    """The file cannot become an observation; the message is shown to the user."""


@dataclass
class PhotoSource:
    """A file to import. `read` is called twice (metadata pass, storage pass) so large folders
    never sit in memory at once; the two reads must return identical bytes."""
    name: str
    read: Callable[[], bytes]
    mtime: float | None = None


@dataclass
class PhotoInfo:
    lat: float
    lon: float
    observed_at: datetime
    time_source: str
    accuracy_m: float | None
    accuracy_source: str | None
    heading_deg: float | None
    altitude_m: float | None
    camera: str
    size_px: tuple[int, int]
    description: str
    exif: dict
    flags: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------- EXIF

def _jsonable(v):
    if isinstance(v, IFDRational):
        return None if v.denominator == 0 else float(v)
    if isinstance(v, bytes):
        if len(v) <= 64:
            text = v.decode("ascii", "replace").strip("\x00 ")
            return text if text.isprintable() else v.hex()
        return f"<{len(v)} bytes, kept in the original file>"
    if isinstance(v, (tuple, list)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, str):
        return v.strip("\x00 ")
    return v


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return None if f != f else f  # NaN from 0/0 rationals


def _dms(values, ref: str | None) -> float | None:
    parts = [_num(x) for x in values] if isinstance(values, (tuple, list)) else [_num(values)]
    if not parts or any(p is None for p in parts):
        return None
    deg = parts[0] + (parts[1] / 60 if len(parts) > 1 else 0) + (parts[2] / 3600 if len(parts) > 2 else 0)
    return -deg if (ref or "").strip().upper() in ("S", "W") else deg


def exif_dump(img: Image.Image) -> tuple[dict, dict, dict]:
    """(image IFD, Exif IFD, GPS IFD) with readable tag names. MakerNote is summarised, not copied."""
    exif = img.getexif()
    base = {ExifTags.TAGS.get(k, str(k)): v for k, v in exif.items() if k not in (0x8769, 0x8825)}
    sub = {ExifTags.TAGS.get(k, str(k)): v for k, v in exif.get_ifd(ExifTags.IFD.Exif).items()}
    gps = {ExifTags.GPSTAGS.get(k, str(k)): v for k, v in exif.get_ifd(ExifTags.IFD.GPSInfo).items()}
    if "MakerNote" in sub:
        sub["MakerNote"] = f"<{len(sub['MakerNote'])} bytes, kept in the original file>"
    return base, sub, gps


def _capture_time(sub: dict, base: dict, gps: dict) -> tuple[datetime, str, list[str]]:
    local = (sub.get("DateTimeOriginal") or base.get("DateTime") or "").strip("\x00 ")
    offset = (sub.get("OffsetTimeOriginal") or sub.get("OffsetTime") or "").strip("\x00 ")
    fmt = "%Y:%m:%d %H:%M:%S"
    if local and offset:
        try:
            return datetime.strptime(f"{local} {offset}", f"{fmt} %z"), \
                "EXIF DateTimeOriginal + OffsetTimeOriginal", []
        except ValueError:
            pass
    if gps.get("GPSDateStamp") and gps.get("GPSTimeStamp"):
        h, m, s = (_num(x) or 0 for x in gps["GPSTimeStamp"])
        try:
            d = datetime.strptime(str(gps["GPSDateStamp"]).strip("\x00 "), "%Y:%m:%d")
            return d.replace(hour=int(h), minute=int(m), second=int(s), tzinfo=timezone.utc), "EXIF GPS time stamp (UTC)", []
        except ValueError:
            pass
    if local:
        try:
            naive = datetime.strptime(local, fmt)
            return naive.replace(tzinfo=BONN_TZ), "EXIF DateTimeOriginal, time zone assumed Europe/Berlin", ["timezone_assumed"]
        except ValueError:
            pass
    raise PhotoError("no readable capture time in the EXIF metadata")


def read_photo(raw: bytes) -> PhotoInfo:
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise PhotoError(f"not a readable image ({exc.__class__.__name__})") from exc
    base, sub, gps = exif_dump(img)
    lat = _dms(gps.get("GPSLatitude"), gps.get("GPSLatitudeRef")) if "GPSLatitude" in gps else None
    lon = _dms(gps.get("GPSLongitude"), gps.get("GPSLongitudeRef")) if "GPSLongitude" in gps else None
    if lat is None or lon is None:
        raise PhotoError("no GPS position in the EXIF metadata")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (abs(lat) < 1e-9 and abs(lon) < 1e-9):
        raise PhotoError(f"invalid GPS position ({lat:.6f}, {lon:.6f})")
    observed_at, time_source, flags = _capture_time(sub, base, gps)

    accuracy = _num(gps.get("GPSHPositioningError"))
    accuracy_source = "EXIF GPSHPositioningError" if accuracy is not None else None
    if accuracy is None:
        flags.append("accuracy_unknown")
    altitude = _num(gps.get("GPSAltitude"))
    if altitude is not None and gps.get("GPSAltitudeRef") in (1, b"\x01"):
        altitude = -altitude
    camera = " ".join(x for x in (str(base.get("Make", "")).strip("\x00 "), str(base.get("Model", "")).strip("\x00 ")) if x)
    return PhotoInfo(
        lat=lat, lon=lon, observed_at=observed_at, time_source=time_source,
        accuracy_m=accuracy, accuracy_source=accuracy_source,
        heading_deg=_num(gps.get("GPSImgDirection")), altitude_m=altitude,
        camera=camera or "unknown camera", size_px=img.size,
        description=str(base.get("ImageDescription", "")).strip("\x00 "),
        exif={"image": _jsonable(base), "exif": _jsonable(sub), "gps": _jsonable(gps)}, flags=flags,
    )


def make_preview(raw: bytes) -> bytes:
    """Upright, downscaled JPEG for the browser. Carries no metadata; the original keeps all of it."""
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    img.thumbnail((PHOTO_PREVIEW_MAX_PX, PHOTO_PREVIEW_MAX_PX))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue()


# ---------------------------------------------------------------------------- import

def _safe_name(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).name).strip("._") or "photo"
    return stem[-80:]


def folder_sources(folder: Path, recursive: bool = True) -> list[PhotoSource]:
    files = sorted(p for p in (folder.rglob("*") if recursive else folder.iterdir()) if p.is_file())
    return [PhotoSource(p.relative_to(folder).as_posix(), p.read_bytes, p.stat().st_mtime) for p in files]


def import_photos(conn: sqlite3.Connection, ref: ReferenceData, image_dir: Path, t: Thresholds,
                  sources: Iterable[PhotoSource], meta: PhotoImportMeta, actor: str) -> dict:
    report: dict = {"mission_id": None, "accepted": [], "duplicates": [], "rejected": [], "skipped": []}
    candidates = []

    # Pass 1: read metadata only; nothing is stored yet.
    for src in sources:
        suffix = Path(src.name).suffix.lower()
        if Path(src.name).name.startswith(".") or suffix not in SUPPORTED_SUFFIXES:
            reason = ("HEIC/HEIF is not supported yet; export as JPEG" if suffix in (".heic", ".heif")
                      else "not a supported photo file (.jpg, .jpeg, .tif, .tiff, .png)")
            report["skipped"].append({"file": src.name, "reason": reason})
            continue
        raw = src.read()
        if len(raw) > MAX_PHOTO_BYTES:
            report["rejected"].append({"file": src.name, "reason": f"larger than {MAX_PHOTO_BYTES // 2**20} MB"})
            continue
        sha = hashlib.sha256(raw).hexdigest()
        uid = f"PHOTO-{sha[:16]}"
        if uid_exists(conn, uid) or any(c["uid"] == uid for c in candidates):
            report["duplicates"].append({"file": src.name, "uid": uid})
            continue
        try:
            info = read_photo(raw)
        except PhotoError as exc:
            report["rejected"].append({"file": src.name, "reason": str(exc)})
            continue
        ctx = ref.context(info.lat, info.lon)
        if not ctx["in_bonn"]:
            report["rejected"].append({"file": src.name, "reason": "outside the Bonn city boundary"})
            continue
        candidates.append({"src": src, "sha": sha, "uid": uid, "info": info, "ctx": ctx, "bytes": len(raw)})

    if not candidates:
        log_event(conn, actor, "photo_import", None, json.dumps(report))
        conn.commit()
        return report

    candidates.sort(key=lambda c: c["info"].observed_at)
    first, last = candidates[0]["info"].observed_at, candidates[-1]["info"].observed_at
    mission_id = meta.mission_id or (
        f"PHOTO-{first.astimezone(BONN_TZ):%Y%m%d}-"
        + hashlib.sha256("".join(sorted(c["sha"] for c in candidates)).encode()).hexdigest()[:6])
    existing = conn.execute("SELECT protocol FROM missions WHERE id = ?", (mission_id,)).fetchone()
    if existing is not None and existing["protocol"] != "opportunistic":
        raise PhotoError(f"mission id {mission_id} is already used by a survey mission")
    synthetic = meta.simulated or any(SYNTHETIC_MARKER in c["info"].description for c in candidates)
    source_kind = "simulated" if synthetic else "field_photos"
    cameras = Counter(c["info"].camera for c in candidates)
    now = now_iso()
    track = [[round(c["info"].lon, 7), round(c["info"].lat, 7)] for c in candidates]
    if existing is not None:
        # Adding photos to an existing photo mission: widen its time range, extend its capture track.
        m = conn.execute("SELECT started_at, ended_at, track_json FROM missions WHERE id = ?", (mission_id,)).fetchone()
        old_track = json.loads(m["track_json"])
        conn.execute("UPDATE missions SET started_at = MIN(started_at, ?), ended_at = MAX(ended_at, ?), track_json = ?"
                     " WHERE id = ?", (first.astimezone(timezone.utc).isoformat(), last.astimezone(timezone.utc).isoformat(),
                                        json.dumps(old_track + [track]), mission_id))
    insert_mission(
        conn, mission_id=mission_id, robot_id=f"camera:{cameras.most_common(1)[0][0]}", area_name=meta.area_name,
        started_at=first.astimezone(timezone.utc).isoformat(), ended_at=last.astimezone(timezone.utc).isoformat(),
        year=first.astimezone(BONN_TZ).year, protocol="opportunistic",
        track=[track],
        detection_range_m=None, notes=meta.notes, source_kind=source_kind,
        provenance={"importer": IMPORTER, "photographer": meta.photographer, "source_label": meta.source_label,
                    "cameras": dict(cameras), "imported_by": actor, "imported_at": now,
                    "synthetic_marker_found": synthetic and not meta.simulated, "marked_simulated": meta.simulated},
    )

    # Pass 2: store originals (verified), previews, observations.
    mission_dir = image_dir / mission_id
    (mission_dir / "originals").mkdir(parents=True, exist_ok=True)
    (mission_dir / "previews").mkdir(parents=True, exist_ok=True)
    manifest_path = mission_dir / "originals" / "MANIFEST.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {"mission_id": mission_id, "files": []}
    for c in candidates:
        raw = c["src"].read()
        if hashlib.sha256(raw).hexdigest() != c["sha"]:
            report["rejected"].append({"file": c["src"].name, "reason": "file changed during import"})
            continue
        info: PhotoInfo = c["info"]
        original_rel = f"{mission_id}/originals/{c['sha'][:16]}_{_safe_name(c['src'].name)}"
        original = image_dir / original_rel
        original.write_bytes(raw)
        if hashlib.sha256(original.read_bytes()).hexdigest() != c["sha"]:
            raise OSError(f"stored copy of {c['src'].name} does not match the source")
        preview = make_preview(raw)
        preview_rel = f"{mission_id}/previews/{c['uid']}.jpg"
        (image_dir / preview_rel).write_bytes(preview)

        flags = list(info.flags) + ["no_model_prediction"]
        if info.accuracy_m is not None and info.accuracy_m > t.poor_gnss_m:
            flags.append("poor_gnss")
        if c["ctx"]["landuse"] is None:
            flags.append("outside_mapped_woodland")
        observed_utc = info.observed_at.astimezone(timezone.utc).replace(microsecond=0).isoformat()
        metadata = {
            "derived": {
                "lat": info.lat, "lon": info.lon, "position_source": "EXIF GPSLatitude/GPSLongitude",
                "accuracy_m": info.accuracy_m, "accuracy_source": info.accuracy_source,
                "accuracy_assumed_for_linking_m": None if info.accuracy_m is not None else t.assumed_accuracy_m,
                "observed_at": info.observed_at.isoformat(), "time_source": info.time_source,
                "heading_deg": info.heading_deg, "altitude_m": info.altitude_m,
                "camera": info.camera, "size_px": list(info.size_px),
            },
            "file": {"original_filename": c["src"].name, "bytes": c["bytes"], "sha256": c["sha"],
                     "file_mtime": datetime.fromtimestamp(c["src"].mtime, timezone.utc).isoformat() if c["src"].mtime else None,
                     "source_label": meta.source_label},
            "exif": info.exif,
        }
        stand_id = insert_observation(
            conn, t, ctx=c["ctx"], uid=c["uid"], mission_id=mission_id, observed_at=observed_utc,
            lat=info.lat, lon=info.lon, gnss_accuracy_m=info.accuracy_m,
            image_path=preview_rel, image_sha256=hashlib.sha256(preview).hexdigest(),
            original_path=original_rel, original_sha256=c["sha"], original_filename=c["src"].name,
            original_bytes=c["bytes"], metadata_json=json.dumps(metadata, ensure_ascii=False),
            qc_flags_json=json.dumps(flags), source_kind=source_kind,
            provenance_json=json.dumps({
                "source_kind": source_kind, "robot_id": f"camera:{info.camera}", "mission_id": mission_id,
                "photographer": meta.photographer, "importer": IMPORTER, "original_sha256": c["sha"],
                "image_sha256": hashlib.sha256(preview).hexdigest(), "ingested_at": now,
                "time_source": info.time_source, "accuracy_source": info.accuracy_source,
            }),
        )
        manifest["files"].append({"original_filename": c["src"].name, "stored_as": Path(original_rel).name,
                                  "sha256": c["sha"], "bytes": c["bytes"], "uid": c["uid"], "imported_at": now})
        report["accepted"].append({"file": c["src"].name, "uid": c["uid"], "stand_id": stand_id,
                                   "flags": flags, "time_source": info.time_source})

    manifest["updated_at"] = now
    manifest_path.write_text(json.dumps(manifest, indent=1, ensure_ascii=False))
    report["mission_id"] = mission_id
    report["source_kind"] = source_kind
    log_event(conn, actor, "photo_import", mission_id, json.dumps(
        {k: len(v) if isinstance(v, list) else v for k, v in report.items()}))
    conn.commit()
    return report


def verify_originals(conn: sqlite3.Connection, image_dir: Path) -> dict:
    """Re-hash every stored original and compare with the SHA-256 recorded at import."""
    ok, problems = 0, []
    for r in conn.execute("SELECT uid, original_path, original_sha256 FROM observations WHERE original_path IS NOT NULL"):
        path = image_dir / r["original_path"]
        if not path.is_file():
            problems.append({"uid": r["uid"], "path": r["original_path"], "problem": "missing"})
        elif hashlib.sha256(path.read_bytes()).hexdigest() != r["original_sha256"]:
            problems.append({"uid": r["uid"], "path": r["original_path"], "problem": "content changed (hash mismatch)"})
        else:
            ok += 1
    return {"checked": ok + len(problems), "ok": ok, "problems": problems}
