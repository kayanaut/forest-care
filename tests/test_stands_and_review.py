"""Stand status logic and the human review workflow on small, hand-built data."""

import pytest

from forestcare.config import Thresholds
from forestcare.stands import _classify

from .conftest import KOTTENFORST, mission, observation, offset

T = Thresholds()


def row(year, surveyed=True, confirmed=0, rejected=0, pending=0, uncertain=0, plants=None):
    return {"year": year, "surveyed": surveyed, "confirmed": confirmed, "rejected": rejected, "pending": pending,
            "uncertain": uncertain, "plants_confirmed": plants if plants is not None else (0 if surveyed else None)}


@pytest.mark.parametrize("rows,expected", [
    ([row(2025), row(2026, confirmed=2, plants=3)], "new"),
    ([row(2025, confirmed=3, plants=4), row(2026, confirmed=6, plants=9)], "expanding"),
    ([row(2025, confirmed=3, plants=4), row(2026, confirmed=3, plants=5)], "stable"),
    ([row(2025, confirmed=6, plants=10), row(2026, confirmed=2, plants=3)], "declining"),
    ([row(2025, confirmed=3, plants=4), row(2026)], "not_redetected"),
    ([row(2025, confirmed=3, plants=4), row(2026, surveyed=False)], "not_surveyed"),
    ([row(2025, confirmed=3, plants=4), row(2026, pending=2)], "awaiting_confirmation"),
    ([row(2025, confirmed=3, plants=4), row(2026, uncertain=1)], "awaiting_confirmation"),
    ([row(2025), row(2026, pending=1)], "unverified"),
    ([row(2025, rejected=2), row(2026, pending=1)], "not_target"),
    # Growth is measured against the last year with confirmed plants, skipping gaps.
    ([row(2024, confirmed=2, plants=2), row(2025, surveyed=False), row(2026, confirmed=4, plants=8)], "expanding"),
])
def test_classify(rows, expected):
    assert _classify(rows, T)[0] == expected


def test_small_changes_are_stable():
    # 1 -> 2 plants is +100 % but only one plant: not called "expanding"
    assert _classify([row(2025, confirmed=1, plants=1), row(2026, confirmed=1, plants=2)], T)[0] == "stable"


def _review(client, obs_id, decision, **kw):
    r = client.post(f"/api/observations/{obs_id}/reviews", json={"decision": decision, "reviewer": "Test Expert", **kw})
    assert r.status_code == 200, r.text
    return r.json()


def _two_year_stand(client, second_year_track=None, second_year_obs=True):
    lat, lon = KOTTENFORST
    client.post("/api/missions", json=mission("M25", "2025-06-04", [
        observation(f"a{i}", *offset(lat, lon, i * 2, 0), "2025-06-04T09:30:00+02:00", count=2) for i in range(3)]))
    obs26 = [observation("b0", lat, lon, "2026-06-04T09:30:00+02:00", count=2)] if second_year_obs else []
    client.post("/api/missions", json=mission("M26", "2026-06-04", obs26, track=second_year_track))
    for o in client.get("/api/observations").json():
        if o["uid"].startswith("a"):
            _review(client, o["id"], "confirmed")
    return client.get("/api/stands").json()[0]["id"]


def test_unreviewed_detections_do_not_change_the_trend(client):
    sid = _two_year_stand(client)
    s = client.get(f"/api/stands/{sid}").json()
    assert s["status"] == "awaiting_confirmation"
    assert s["years"][0]["plants_confirmed"] == 6


def test_confirming_updates_status(client):
    sid = _two_year_stand(client)
    b0 = next(o for o in client.get("/api/observations").json() if o["uid"] == "b0")
    _review(client, b0["id"], "confirmed")
    s = client.get(f"/api/stands/{sid}").json()
    assert s["status"] == "declining"  # 6 confirmed plants in 2025, 2 in 2026


def test_absence_needs_coverage(client):
    lat, lon = KOTTENFORST
    # 2026 mission drives 300 m further north: the stand was not surveyed, so no inference.
    a, b = offset(lat, lon, -60, 300), offset(lat, lon, 60, 300)
    sid = _two_year_stand(client, second_year_track=[[[a[1], a[0]], [b[1], b[0]]]], second_year_obs=False)
    assert client.get(f"/api/stands/{sid}").json()["status"] == "not_surveyed"


def test_covered_absence_is_not_redetected_and_needs_inspection(client):
    sid = _two_year_stand(client, second_year_obs=False)
    s = client.get(f"/api/stands/{sid}").json()
    assert s["status"] == "not_redetected"
    assert s["needs_inspection"]
    assert s["inspection_reasons"][0]["code"] == "not_redetected"


def test_review_history_is_append_only(client):
    lat, lon = KOTTENFORST
    client.post("/api/missions", json=mission("M1", "2025-06-04", [observation("r1", lat, lon, "2025-06-04T09:30:00+02:00")]))
    oid = client.get("/api/observations").json()[0]["id"]
    _review(client, oid, "uncertain", note="leaf underside not visible")
    _review(client, oid, "rejected", corrected_taxon="Prunus padus")
    d = client.get(f"/api/observations/{oid}").json()
    assert [r["decision"] for r in d["reviews"]] == ["uncertain", "rejected"]
    assert d["review_status"] == "rejected"
    assert all(r["source_kind"] == "human" for r in d["reviews"])
    assert client.get("/api/stands").json()[0]["status"] == "not_target"


def test_review_validation(client):
    lat, lon = KOTTENFORST
    client.post("/api/missions", json=mission("M1", "2025-06-04", [observation("v1", lat, lon, "2025-06-04T09:30:00+02:00")]))
    oid = client.get("/api/observations").json()[0]["id"]
    post = lambda body: client.post(f"/api/observations/{oid}/reviews", json=body).status_code  # noqa: E731
    assert post({"decision": "remove", "reviewer": "X Y"}) == 422               # no such decision exists
    assert post({"decision": "confirmed"}) == 422                              # reviewer is required
    assert post({"decision": "confirmed", "reviewer": "X Y", "corrected_taxon": "Prunus padus"}) == 422
    assert post({"decision": "rejected", "reviewer": "X Y", "corrected_taxon": "Quercus robur"}) == 422
    assert client.post("/api/observations/9999/reviews", json={"decision": "confirmed", "reviewer": "X Y"}).status_code == 404


def test_queue_prioritises_and_explains(client):
    lat, lon = KOTTENFORST
    client.post("/api/missions", json=mission("M1", "2025-06-04", [
        observation("known", lat, lon, "2025-06-04T09:30:00+02:00", p=0.95),
        observation("fresh", *offset(lat, lon, 50, 0), "2025-06-04T09:31:00+02:00", p=0.6, phenology="fruiting"),
    ]))
    known = next(o for o in client.get("/api/observations").json() if o["uid"] == "known")
    _review(client, known["id"], "confirmed")
    client.post("/api/missions", json=mission("M2", "2025-09-10", [
        observation("again", lat, lon, "2025-09-10T09:30:00+02:00", p=0.95)]))
    q = client.get("/api/review-queue").json()
    assert [i["uid"] for i in q] == ["fresh", "again"]
    assert any("No reviewed record" in r for r in q[0]["priority_reasons"])
    assert any("Fruiting" in r for r in q[0]["priority_reasons"])
    assert any("already has 1 confirmed" in r for r in q[1]["priority_reasons"])


def test_management_is_only_recorded_by_people(client):
    sid = _two_year_stand(client)
    bad = client.post(f"/api/stands/{sid}/notes", json={"kind": "management_action", "text": "ringed", "recorded_by": "A B"})
    assert bad.status_code == 422  # needs the date the action happened
    ok = client.post(f"/api/stands/{sid}/notes", json={"kind": "management_action", "text": "Stems ringed",
                                                        "recorded_by": "A B", "action_date": "2026-02-01"})
    assert ok.status_code == 200
    s = client.get(f"/api/stands/{sid}").json()
    assert s["notes"][0]["source_kind"] == "human"
    assert any(r["code"] == "after_management" for r in s["inspection_reasons"])
    assert client.post("/api/stands/PS-9999/notes", json={"kind": "note", "text": "x y z", "recorded_by": "A B"}).status_code == 404


def test_system_never_recommends_management(seeded_client):
    """The API describes and flags; it has no field that proposes an intervention."""
    words = ("recommend", "remove", "eradicat", "control_now", "action_required")
    for s in seeded_client.get("/api/stands").json():
        keys = " ".join(s.keys()) + " " + " ".join(r["code"] for r in s["inspection_reasons"])
        assert not any(w in keys.lower() for w in words)
        assert all(c["code"] in {"near_protected", "early_invasion", "low_infestation", "fruiting_solitary"} for c in s["lanuk_criteria"])
