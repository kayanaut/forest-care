"""Field-photo missions: EXIF reading, import, preservation of originals, labelling, presence-only logic."""

import hashlib
import io
import json
import sqlite3

import pytest
from PIL import ExifTags, Image
from PIL.TiffImagePlugin import IFDRational

from forestcare.db import connect, init_db
from forestcare.models import PhotoImportMeta
from forestcare.photos import PhotoError, PhotoSource, folder_sources, import_photos, read_photo, verify_originals
from simulator.photos import SamplePhoto, jpeg_bytes

from .conftest import KOTTENFORST, mission, observation, offset


def photo(name, lat, lon, when="2026:09:20 10:30:00", **kw) -> SamplePhoto:
    kw.setdefault("synthetic_marker", False)
    return SamplePhoto(name, kw.pop("taxon", "Prunus serotina"), lat, lon, when, **kw)


def write(folder, *photos):
    folder.mkdir(parents=True, exist_ok=True)
    for i, p in enumerate(photos):
        (folder / p.filename).write_bytes(jpeg_bytes(p, seed=i))
    return folder


def run_import(conn, ref, settings, folder, **meta):
    meta.setdefault("photographer", "Test Botanist")
    return import_photos(conn, ref, settings.image_dir, settings.thresholds, folder_sources(folder),
                         PhotoImportMeta(**meta), actor="test")


# ---------------------------------------------------------------------------- EXIF

def test_read_photo_with_full_exif():
    lat, lon = KOTTENFORST
    info = read_photo(jpeg_bytes(photo("a.jpg", lat, lon, accuracy_m=4.7, heading_deg=210.0)))
    assert (info.lat, info.lon) == pytest.approx(KOTTENFORST, abs=1e-6)
    assert info.observed_at.isoformat() == "2026-09-20T10:30:00+02:00"
    assert info.time_source == "EXIF DateTimeOriginal + OffsetTimeOriginal"
    assert info.accuracy_m == pytest.approx(4.7) and info.accuracy_source == "EXIF GPSHPositioningError"
    assert info.heading_deg == pytest.approx(210.0) and info.altitude_m == pytest.approx(92.4)
    assert info.camera == "Apple iPhone 13" and info.flags == []
    assert info.exif["gps"]["GPSHPositioningError"] == pytest.approx(4.7)


def test_missing_offset_and_accuracy_are_flagged_not_invented():
    lat, lon = KOTTENFORST
    info = read_photo(jpeg_bytes(photo("b.jpg", lat, lon, offset=None, accuracy_m=None)))
    assert info.observed_at.isoformat() == "2026-09-20T10:30:00+02:00"  # Bonn summer time
    assert "time zone assumed Europe/Berlin" in info.time_source
    assert info.accuracy_m is None
    assert set(info.flags) == {"timezone_assumed", "accuracy_unknown"}


def test_gps_timestamp_is_preferred_over_an_unzoned_local_time():
    img = Image.new("RGB", (32, 24))
    exif = Image.Exif()
    exif.get_ifd(ExifTags.IFD.Exif)[ExifTags.Base.DateTimeOriginal] = "2026:01:15 12:00:00"
    gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
    gps[ExifTags.GPS.GPSLatitudeRef], gps[ExifTags.GPS.GPSLongitudeRef] = "N", "E"
    gps[ExifTags.GPS.GPSLatitude] = (IFDRational(50), IFDRational(39), IFDRational(4478, 100))
    gps[ExifTags.GPS.GPSLongitude] = (IFDRational(7), IFDRational(3), IFDRational(3222, 100))
    gps[ExifTags.GPS.GPSDateStamp] = "2026:01:15"
    gps[ExifTags.GPS.GPSTimeStamp] = (IFDRational(11), IFDRational(0), IFDRational(7))
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    info = read_photo(buf.getvalue())
    assert info.observed_at.isoformat() == "2026-01-15T11:00:07+00:00"
    assert info.time_source == "EXIF GPS time stamp (UTC)" and "timezone_assumed" not in info.flags


@pytest.mark.parametrize("sample,message", [
    (photo("n.jpg", None, None), "no GPS position"),
    (photo("z.jpg", 0.0, 0.0), "invalid GPS position"),
])
def test_unusable_positions(sample, message):
    with pytest.raises(PhotoError, match=message):
        read_photo(jpeg_bytes(sample))


def test_not_an_image():
    with pytest.raises(PhotoError, match="not a readable image"):
        read_photo(b"this is not a jpeg")


# ---------------------------------------------------------------------------- import

def test_import_folder_report_and_preservation(conn, ref, settings, tmp_path):
    lat, lon = KOTTENFORST
    folder = write(tmp_path / "walk",
                   photo("IMG_1.JPG", lat, lon),
                   photo("IMG_2.JPG", *offset(lat, lon, 3, 2), offset=None, accuracy_m=None),
                   photo("IMG_nogps.JPG", None, None),
                   photo("IMG_alfter.JPG", 50.690, 7.000))
    (folder / "IMG_1 copy.JPG").write_bytes((folder / "IMG_1.JPG").read_bytes())
    (folder / "notes.txt").write_text("x")
    (folder / "IMG_3.HEIC").write_bytes(b"heic")
    r = run_import(conn, ref, settings, folder, area_name="Kottenforst walk", source_label="walk")

    assert len(r["accepted"]) == 2 and len(r["duplicates"]) == 1
    assert {x["reason"] for x in r["rejected"]} == {"no GPS position in the EXIF metadata", "outside the Bonn city boundary"}
    assert {x["file"] for x in r["skipped"]} == {"notes.txt", "IMG_3.HEIC"}
    assert any("HEIC" in x["reason"] for x in r["skipped"])
    assert r["source_kind"] == "field_photos"

    m = conn.execute("SELECT * FROM missions WHERE id = ?", (r["mission_id"],)).fetchone()
    assert m["protocol"] == "opportunistic" and m["detection_range_m"] is None
    assert m["robot_id"] == "camera:Apple iPhone 13"
    assert json.loads(m["provenance_json"])["photographer"] == "Test Botanist"

    for row in conn.execute("SELECT * FROM observations"):
        source = folder / (row["original_filename"])
        stored = settings.image_dir / row["original_path"]
        assert stored.read_bytes() == source.read_bytes()                     # byte-for-byte
        assert row["original_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
        assert row["predicted_taxon"] is None and row["target_probability"] is None
        assert "no_model_prediction" in json.loads(row["qc_flags_json"])
        meta = json.loads(row["metadata_json"])
        assert meta["file"]["original_filename"] == row["original_filename"]
        assert "GPSLatitude" in meta["exif"]["gps"]
        preview = Image.open(settings.image_dir / row["image_path"])
        assert preview.format == "JPEG" and max(preview.size) <= 1600
        assert not preview.getexif().get_ifd(ExifTags.IFD.GPSInfo)          # previews carry no location metadata
    unknown = conn.execute("SELECT * FROM observations WHERE original_filename = 'IMG_2.JPG'").fetchone()
    assert unknown["gnss_accuracy_m"] is None
    assert json.loads(unknown["metadata_json"])["derived"]["accuracy_assumed_for_linking_m"] == 10.0

    manifest = json.loads((settings.image_dir / r["mission_id"] / "originals" / "MANIFEST.json").read_text())
    # Of two identical files the first in folder order is kept ("IMG_1 copy.JPG" sorts before "IMG_1.JPG").
    assert sorted(f["original_filename"] for f in manifest["files"]) == ["IMG_1 copy.JPG", "IMG_2.JPG"]
    assert r["duplicates"][0]["file"] == "IMG_1.JPG"
    assert verify_originals(conn, settings.image_dir) == {"checked": 2, "ok": 2, "problems": []}


def test_reimport_is_idempotent(conn, ref, settings, tmp_path):
    lat, lon = KOTTENFORST
    folder = write(tmp_path / "walk", photo("IMG_1.JPG", lat, lon), photo("IMG_2.JPG", *offset(lat, lon, 40, 0)))
    first = run_import(conn, ref, settings, folder)
    second = run_import(conn, ref, settings, folder)
    assert len(first["accepted"]) == 2
    assert second["accepted"] == [] and len(second["duplicates"]) == 2 and second["mission_id"] is None
    assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM missions").fetchone()[0] == 1


def test_adding_photos_to_an_existing_photo_mission(conn, ref, settings, tmp_path):
    lat, lon = KOTTENFORST
    run_import(conn, ref, settings, write(tmp_path / "a", photo("IMG_1.JPG", lat, lon, "2026:09:20 10:00:00")), mission_id="WALK-1")
    run_import(conn, ref, settings, write(tmp_path / "b", photo("IMG_2.JPG", *offset(lat, lon, 50, 0), "2026:09:21 15:00:00")),
               mission_id="WALK-1")
    m = conn.execute("SELECT * FROM missions").fetchone()
    assert (m["started_at"], m["ended_at"]) == ("2026-09-20T08:00:00+00:00", "2026-09-21T13:00:00+00:00")
    assert len(json.loads(m["track_json"])) == 2
    assert conn.execute("SELECT COUNT(*) FROM observations WHERE mission_id = 'WALK-1'").fetchone()[0] == 2


def test_synthetic_marker_forces_simulated(conn, ref, settings, tmp_path):
    lat, lon = KOTTENFORST
    folder = write(tmp_path / "walk", photo("IMG_1.JPG", lat, lon, synthetic_marker=True))
    r = run_import(conn, ref, settings, folder)
    assert r["source_kind"] == "simulated"
    assert conn.execute("SELECT source_kind FROM observations").fetchone()[0] == "simulated"


def test_photo_mission_cannot_reuse_a_survey_mission_id(client, conn, ref, settings, tmp_path):
    lat, lon = KOTTENFORST
    client.post("/api/missions", json=mission("M25", "2025-06-04", []))
    folder = write(tmp_path / "walk", photo("IMG_1.JPG", lat, lon))
    with pytest.raises(PhotoError, match="already used by a survey mission"):
        run_import(conn, ref, settings, folder, mission_id="M25")


def test_file_changed_between_passes_is_not_stored(conn, ref, settings):
    lat, lon = KOTTENFORST
    versions = iter([jpeg_bytes(photo("a.jpg", lat, lon)), jpeg_bytes(photo("a.jpg", lat, lon, heading_deg=10.0))])
    r = import_photos(conn, ref, settings.image_dir, settings.thresholds, [PhotoSource("a.jpg", lambda: next(versions))],
                      PhotoImportMeta(photographer="Test Botanist"), actor="test")
    assert r["rejected"] == [{"file": "a.jpg", "reason": "file changed during import"}]
    assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0


# ---------------------------------------------------------------------------- API

def _upload(client, *photos, **form):
    files = [("files", (p.filename, jpeg_bytes(p, seed=i), "image/jpeg")) for i, p in enumerate(photos)]
    return client.post("/api/photo-missions", files=files, data={"photographer": "Test Botanist", **form})


def test_upload_endpoint_and_original_download(client, settings):
    lat, lon = KOTTENFORST
    p = photo("Kottenforst/IMG_Ä1.JPG", lat, lon)
    r = _upload(client, p, area_name="Upload test")
    assert r.status_code == 200, r.text
    assert len(r.json()["accepted"]) == 1
    obs = client.get("/api/observations").json()[0]
    detail = client.get(f"/api/observations/{obs['id']}").json()
    assert detail["original_url"] and detail["metadata"]["derived"]["time_source"].startswith("EXIF")

    dl = client.get(detail["original_url"])
    assert dl.status_code == 200
    assert dl.content == jpeg_bytes(p, seed=0)
    assert dl.headers["x-content-sha256"] == detail["original_sha256"]
    assert "filename*=UTF-8''IMG_%C3%841.JPG" in dl.headers["content-disposition"]

    # Tampering with the stored original is detected, and the file is no longer served.
    stored = settings.image_dir / detail["original_path"]
    stored.write_bytes(stored.read_bytes() + b"x")
    assert client.get(detail["original_url"]).status_code == 409
    report = client.get("/api/originals/verify").json()
    assert report["problems"][0]["problem"].startswith("content changed")


def test_upload_validation(client):
    lat, lon = KOTTENFORST
    assert _upload(client, photo("a.jpg", lat, lon), photographer="X").status_code == 422
    assert client.get("/api/observations/1/original").status_code == 404


# ---------------------------------------------------------------------------- labelling & stand logic

def _review(client, oid, decision, **labels):
    r = client.post(f"/api/observations/{oid}/reviews", json={"decision": decision, "reviewer": "Test Expert", **labels})
    assert r.status_code == 200, r.text


def _robot_stand_2025(client, plants=6):
    lat, lon = KOTTENFORST
    client.post("/api/missions", json=mission("M25", "2025-06-04", [
        observation("r1", lat, lon, "2025-06-04T09:30:00+02:00", count=plants)]))
    _review(client, client.get("/api/observations").json()[0]["id"], "confirmed")


def _photo_2026(client, dx=1.0):
    lat, lon = KOTTENFORST
    assert _upload(client, photo("IMG_2026.JPG", *offset(lat, lon, dx, 0))).status_code == 200
    return next(o for o in client.get("/api/observations").json() if o["uid"].startswith("PHOTO-"))


def test_labels_only_for_confirmed(client):
    lat, lon = KOTTENFORST
    _upload(client, photo("a.jpg", lat, lon))
    oid = client.get("/api/observations").json()[0]["id"]
    r = client.post(f"/api/observations/{oid}/reviews", json={"decision": "rejected", "reviewer": "X Y", "plant_count": 3})
    assert r.status_code == 422


def test_uncounted_photo_confirms_presence_but_not_a_trend(client):
    _robot_stand_2025(client, plants=6)
    p = _photo_2026(client)
    _review(client, p["id"], "confirmed")
    s = client.get("/api/stands").json()[0]
    assert s["status"] == "confirmed_present"
    assert s["years"][-1]["presence_only"] is True
    assert s["latest_plants_confirmed"] == 6  # still the last counted year


def test_counted_photo_label_feeds_the_trend(client):
    _robot_stand_2025(client, plants=6)
    p = _photo_2026(client)
    _review(client, p["id"], "confirmed", plant_count=12, phenology="fruiting")
    s = client.get("/api/stands").json()[0]
    assert s["status"] == "expanding"
    assert s["latest_plants_confirmed"] == 12
    d = client.get(f"/api/observations/{p['id']}").json()
    assert d["reviews"][-1]["plant_count"] == 12 and d["reviews"][-1]["phenology"] == "fruiting"


def test_expert_label_overrides_robot_estimate(client):
    _robot_stand_2025(client, plants=6)
    oid = client.get("/api/observations").json()[0]["id"]
    _review(client, oid, "confirmed", plant_count=2)
    assert client.get("/api/stands").json()[0]["years"][0]["plants_confirmed"] == 2


def test_rejected_photo_is_not_evidence_of_absence(client):
    _robot_stand_2025(client, plants=6)
    p = _photo_2026(client)
    _review(client, p["id"], "rejected", corrected_taxon="Prunus padus")
    assert client.get("/api/stands").json()[0]["status"] == "not_surveyed"


def test_photo_walk_never_counts_as_coverage(client):
    _robot_stand_2025(client, plants=6)
    _photo_2026(client, dx=45)  # photographed a different shrub 45 m away: new stand
    stands = {s["id"]: s for s in client.get("/api/stands").json()}
    assert len(stands) == 2
    assert stands["PS-0001"]["status"] == "not_surveyed"


def test_lanuk_export_for_uncounted_photo_stand(client):
    lat, lon = KOTTENFORST
    _upload(client, photo("a.jpg", lat, lon))
    _review(client, client.get("/api/observations").json()[0]["id"], "confirmed")
    import csv
    rows = list(csv.DictReader(io.StringIO(client.get("/api/export/lanuk-draft.csv").text.lstrip("﻿")), delimiter=";"))
    assert rows[0]["individuen"] == "keine Angabe" and rows[0]["anz_abs"] == ""
    assert rows[0]["bemerkung"].startswith("Feldfotos;")
    assert rows[0]["data_status"] == "ENTWURF – vor Meldung fachlich prüfen"


# ---------------------------------------------------------------------------- migration

V1_SCHEMA = """
CREATE TABLE missions (id TEXT PRIMARY KEY, robot_id TEXT NOT NULL, area_name TEXT, started_at TEXT NOT NULL,
  ended_at TEXT NOT NULL, year INTEGER NOT NULL, track_json TEXT NOT NULL, detection_range_m REAL NOT NULL, notes TEXT,
  source_kind TEXT NOT NULL CHECK (source_kind IN ('simulated', 'robot')), provenance_json TEXT NOT NULL, ingested_at TEXT NOT NULL);
CREATE TABLE stands (id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
CREATE TABLE observations (id INTEGER PRIMARY KEY, uid TEXT NOT NULL UNIQUE, mission_id TEXT NOT NULL REFERENCES missions(id),
  stand_id TEXT NOT NULL REFERENCES stands(id), observed_at TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL,
  gnss_accuracy_m REAL NOT NULL, predicted_taxon TEXT NOT NULL, confidence REAL NOT NULL, target_probability REAL NOT NULL,
  alternatives_json TEXT NOT NULL, plant_count_est INTEGER NOT NULL, height_class TEXT, phenology TEXT, image_path TEXT,
  image_sha256 TEXT, qc_flags_json TEXT NOT NULL, context_json TEXT NOT NULL,
  source_kind TEXT NOT NULL CHECK (source_kind IN ('simulated', 'robot')), provenance_json TEXT NOT NULL,
  review_status TEXT NOT NULL DEFAULT 'pending');
CREATE TABLE reviews (id INTEGER PRIMARY KEY, observation_id INTEGER NOT NULL REFERENCES observations(id),
  decision TEXT NOT NULL, corrected_taxon TEXT, note TEXT, reviewer TEXT NOT NULL, reviewer_role TEXT,
  reviewed_at TEXT NOT NULL, source_kind TEXT NOT NULL);
CREATE TABLE stand_notes (id INTEGER PRIMARY KEY, stand_id TEXT NOT NULL REFERENCES stands(id), kind TEXT NOT NULL,
  text TEXT NOT NULL, action_date TEXT, recorded_by TEXT NOT NULL, recorded_at TEXT NOT NULL, source_kind TEXT NOT NULL);
CREATE TABLE events (id INTEGER PRIMARY KEY, at TEXT NOT NULL, actor TEXT NOT NULL, kind TEXT NOT NULL, ref TEXT, detail_json TEXT);
INSERT INTO missions VALUES ('M1', 'R', 'A', '2025-06-04T07:00:00+00:00', '2025-06-04T08:00:00+00:00', 2025, '[]', 10, NULL,
  'robot', '{}', '2025-06-05T00:00:00+00:00');
INSERT INTO stands VALUES ('PS-0001', '2025-06-05T00:00:00+00:00');
INSERT INTO observations VALUES (1, 'u1', 'M1', 'PS-0001', '2025-06-04T07:30:00+00:00', 50.66, 7.05, 3, 'Prunus serotina',
  0.9, 0.9, '[]', 2, 'shrub', 'flowering', NULL, NULL, '[]', '{}', 'robot', '{}', 'confirmed');
INSERT INTO reviews VALUES (1, 1, 'confirmed', NULL, NULL, 'Expert', NULL, '2025-06-06T00:00:00+00:00', 'human');
"""


def test_migration_from_v1_keeps_data(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(V1_SCHEMA)
    old.close()
    conn = connect(path)
    init_db(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    o = conn.execute("SELECT * FROM observations").fetchone()
    assert (o["uid"], o["plant_count_est"], o["review_status"], o["original_path"]) == ("u1", 2, "confirmed", None)
    assert conn.execute("SELECT protocol FROM missions").fetchone()[0] == "transect"
    assert conn.execute("SELECT plant_count FROM reviews").fetchone()[0] is None
    # the relaxed constraints now accept a field photo without model output
    conn.execute("INSERT INTO missions (id, robot_id, started_at, ended_at, year, protocol, track_json, source_kind,"
                 " provenance_json, ingested_at) VALUES ('P1', 'camera:x', 'a', 'b', 2026, 'opportunistic', '[]',"
                 " 'field_photos', '{}', 'c')")
    conn.execute("INSERT INTO observations (uid, mission_id, stand_id, observed_at, lat, lon, qc_flags_json, context_json,"
                 " source_kind, provenance_json) VALUES ('p1', 'P1', 'PS-0001', 'x', 50.7, 7.1, '[]', '{}', 'field_photos', '{}')")
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    init_db(conn)  # idempotent
