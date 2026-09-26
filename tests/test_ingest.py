import json

from .conftest import KOTTENFORST, mission, observation, offset


def test_ingest_stores_observations_with_context_and_provenance(client, settings):
    lat, lon = KOTTENFORST
    r = client.post("/api/missions", json=mission("M1", "2025-06-04", [observation("M1-1", lat, lon, "2025-06-04T09:30:00+02:00")]))
    assert r.status_code == 200
    assert r.json() == {"mission_id": "M1", "accepted": 1, "duplicates": 0, "rejected": []}

    o = client.get("/api/observations").json()[0]
    assert o["review_status"] == "pending"
    assert o["source_kind"] == "simulated"
    detail = client.get(f"/api/observations/{o['id']}").json()
    assert detail["context"]["nsg"][0]["id"] == "BN-003"
    assert detail["provenance"]["simulator"]["name"] == "pytest"
    assert len(detail["provenance"]["mission_payload_sha256"]) == 64
    assert (settings.image_dir / "M1" / "M1-1.svg").exists()


def test_resending_a_mission_is_idempotent(client):
    lat, lon = KOTTENFORST
    payload = mission("M1", "2025-06-04", [observation("M1-1", lat, lon, "2025-06-04T09:30:00+02:00")])
    client.post("/api/missions", json=payload)
    r = client.post("/api/missions", json=payload).json()
    assert r["accepted"] == 0 and r["duplicates"] == 1
    assert len(client.get("/api/observations").json()) == 1


def test_observations_outside_bonn_are_rejected_not_stored(client):
    lat, lon = KOTTENFORST
    payload = mission("M1", "2025-06-04", [
        observation("in", lat, lon, "2025-06-04T09:30:00+02:00"),
        observation("out", 50.690, 7.000, "2025-06-04T09:31:00+02:00"),  # Alfter
    ])
    r = client.post("/api/missions", json=payload).json()
    assert r["accepted"] == 1
    assert r["rejected"] == [{"uid": "out", "reason": "outside the Bonn city boundary"}]


def test_schema_validation(client):
    lat, lon = KOTTENFORST
    bad_prob = mission("M1", "2025-06-04", [observation("x1", lat, lon, "2025-06-04T09:30:00+02:00", p=1.4)])
    assert client.post("/api/missions", json=bad_prob).status_code == 422
    naive_time = mission("M2", "2025-06-04", [observation("x2", lat, lon, "2025-06-04T09:30:00")])
    assert client.post("/api/missions", json=naive_time).status_code == 422
    no_sim_info = mission("M3", "2025-06-04", [])
    no_sim_info["simulator"] = None
    assert client.post("/api/missions", json=no_sim_info).status_code == 422


def test_quality_flags(client):
    lat, lon = KOTTENFORST
    client.post("/api/missions", json=mission("M1", "2025-06-04", [
        observation("q1", lat, lon, "2025-06-04T15:00:00+02:00", p=0.55, accuracy=14.0, image=False),
    ]))
    o = client.get("/api/observations").json()[0]
    flags = client.get(f"/api/observations/{o['id']}").json()["qc_flags"]
    assert set(flags) == {"poor_gnss", "ambiguous_prediction", "no_image", "timestamp_outside_mission"}


def test_stand_linking_respects_gnss_uncertainty(client):
    lat, lon = KOTTENFORST
    a = offset(lat, lon, 0, 0)
    near = offset(lat, lon, 12, 0)     # 12 m, accuracy 3 m each: limit 8 + 2*4.2 = 16.5 m -> same stand
    far = offset(lat, lon, 45, 0)      # 45 m -> new stand
    sloppy = offset(lat, lon, 45, 25)  # 25 m from `far`, but 12 m accuracy: limit 8 + 2*12.4 = 32.7 m -> joins `far`
    client.post("/api/missions", json=mission("M1", "2025-06-04", [
        observation("a", *a, "2025-06-04T09:30:00+02:00"),
        observation("near", *near, "2025-06-04T09:31:00+02:00"),
        observation("far", *far, "2025-06-04T09:32:00+02:00"),
        observation("sloppy", *sloppy, "2025-06-04T09:33:00+02:00", accuracy=12.0),
    ]))
    stand = {o["uid"]: o["stand_id"] for o in client.get("/api/observations").json()}
    assert stand["a"] == stand["near"]
    assert stand["far"] != stand["a"]
    assert stand["sloppy"] == stand["far"]


def test_svg_images_are_served_with_restrictive_headers(client):
    lat, lon = KOTTENFORST
    client.post("/api/missions", json=mission("M1", "2025-06-04", [observation("i1", lat, lon, "2025-06-04T09:30:00+02:00")]))
    r = client.get("/api/images/M1/i1.svg")
    assert r.status_code == 200
    assert "default-src 'none'" in r.headers["content-security-policy"]
    assert client.get("/api/images/../t.db").status_code == 404


def test_ingest_is_logged(client, settings):
    from forestcare.db import connect
    lat, lon = KOTTENFORST
    client.post("/api/missions", json=mission("M1", "2025-06-04", [observation("l1", lat, lon, "2025-06-04T09:30:00+02:00")]))
    ev = client.get("/api/events").json()[0]
    assert ev["kind"] == "ingest" and ev["actor"] == "robot:TEST-UGV"
    assert json.loads(ev["detail_json"])["accepted"] == 1
    assert connect(settings.db_path).execute("SELECT COUNT(*) FROM stands").fetchone()[0] == 1
