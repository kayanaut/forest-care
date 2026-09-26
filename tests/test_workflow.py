"""End-to-end on the seeded demo: simulated robot data -> review -> stand status -> export."""

import csv
import io
import json

import pytest

from forestcare.export import bed_fl_class, individuen_class
from forestcare.geo import wgs84_to_utm32


def _stand_of(settings, key):
    truth = json.loads((settings.db_path.parent / "sim_ground_truth.json").read_text())["observations"]
    from collections import Counter
    from forestcare.db import connect
    votes = Counter(r["stand_id"] for r in connect(settings.db_path).execute("SELECT uid, stand_id FROM observations")
                    if truth[r["uid"]]["stand_key"] == key)
    return votes.most_common(1)[0][0]


@pytest.mark.parametrize("key,status,reason", [
    ("TB-1", "expanding", "protected_area"),      # grows inside NSG Düne Tannenbusch
    ("KF-1", "stable", "mixed_reviews"),          # native Prunus padus grows inside the stand
    ("KF-2", "declining", "after_management"),    # resprouts after the recorded management
    ("KF-3", "not_surveyed", None),               # transect blocked in 2026
    ("KF-4", "not_redetected", "not_redetected"),
    ("KF-P", "not_target", None),                 # look-alike, all detections rejected
    ("EN-2", "stable", None),
    ("RA-1", "stable", None),
    ("RA-2", "new", None),
])
def test_demo_scenario_statuses(seeded_client, seeded_settings, key, status, reason):
    sid = _stand_of(seeded_settings, key)
    s = seeded_client.get(f"/api/stands/{sid}").json()
    assert s["status"] == status
    if reason:
        assert reason in [r["code"] for r in s["inspection_reasons"]]


def test_everything_in_the_demo_is_marked_simulated(seeded_client):
    rt = seeded_client.get("/api/sources").json()["runtime"]
    assert set(rt["missions"]) == {"simulated"}
    assert set(rt["observations"]) == {"simulated"}
    assert set(rt["reviews"]) == {"simulated"}
    assert set(rt["stand_notes"]) == {"simulated"}
    assert seeded_client.get("/api/summary").json()["simulated"] is True


def test_latest_round_is_left_for_people(seeded_client):
    queue = seeded_client.get("/api/review-queue").json()
    assert queue and all(o["observed_at"].startswith("2026-09") for o in queue)


def test_review_workflow_turns_a_candidate_into_a_new_stand(seeded_client, seeded_settings):
    """EN-1: seedlings appear in NSG Ennert in 2026; only a person's confirmation makes them a stand."""
    sid = _stand_of(seeded_settings, "EN-1")
    before = seeded_client.get(f"/api/stands/{sid}").json()
    assert before["status"] == "unverified"
    pending = [o for o in seeded_client.get(f"/api/observations?stand_id={sid}").json() if o["review_status"] == "pending"]
    for o in pending:
        r = seeded_client.post(f"/api/observations/{o['id']}/reviews",
                               json={"decision": "confirmed", "reviewer": "Test Expert", "reviewer_role": "botanist"})
        assert r.status_code == 200
    after = seeded_client.get(f"/api/stands/{sid}").json()
    assert after["status"] == "new"
    assert "protected_area" in [r["code"] for r in after["inspection_reasons"]]
    assert any("Ennert" in p for p in after["protected"])
    assert sid not in {o["stand_id"] for o in seeded_client.get("/api/review-queue").json()}
    events = seeded_client.get("/api/events?limit=5").json()
    assert events[0]["kind"] == "review" and events[0]["actor"] == "Test Expert"


def test_exports_contain_only_confirmed_stands(seeded_client):
    stands = seeded_client.get("/api/stands").json()
    confirmed_ids = {s["id"] for s in stands if s["counts"]["confirmed"]}
    gj = seeded_client.get("/api/export/stands.geojson").json()
    assert {f["properties"]["id"] for f in gj["features"]} == confirmed_ids
    everything = seeded_client.get("/api/export/stands.geojson?include_unverified=true").json()
    assert len(everything["features"]) == len(stands)

    r = seeded_client.get("/api/export/lanuk-draft.csv")
    assert r.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(r.text.lstrip("﻿")), delimiter=";"))
    assert {row["stand_id"] for row in rows} == confirmed_ids
    for row in rows:
        assert row["art"] == "Spätblühende Traubenkirsche (Prunus serotina)"
        assert row["data_status"].startswith("ENTWURF – SIMULIERTE DATEN")
        assert row["stadium"] == row["lebensraum"] == ""  # left for the expert
        x, y = wgs84_to_utm32(float(row["lat"]), float(row["lon"]))
        assert float(row["x_utm32"]) == pytest.approx(x, abs=0.1)
        assert 360_000 < x < 380_000 and 5_605_000 < y < 5_630_000  # Bonn in UTM32


@pytest.mark.parametrize("n,label", [(1, "1 Ind."), (2, "2-5 Ind."), (25, "6-25 Ind."), (26, "26-100 Ind."), (101, "> 100 Ind.")])
def test_lanuk_count_classes(n, label):
    assert individuen_class(n) == label


def test_lanuk_area_classes_match_portal_vocabulary(ref):
    vocab = set(ref.lanuk_neobiota["field_vocabulary"]["bed_fl"])
    for area in (0.5, 3, 20, 80, 500, 5000, 50000):
        assert bed_fl_class(area) in vocab
    vocab_ind = set(ref.lanuk_neobiota["field_vocabulary"]["individuen"])
    for n in (1, 3, 10, 50, 500, 5000, 50000):
        assert individuen_class(n) in vocab_ind
