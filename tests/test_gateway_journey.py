"""The full journey without a robot:

    ROS 2 data (a real rosbag2/MCAP file) -> gateway -> mission package -> Forest Care API -> dashboard

The bag comes from the gateway's simulator; everything else is the production code path.
"""

from __future__ import annotations

import json

import httpx
import numpy as np
import pytest

from forestcare.models import MissionIn
from forestcare_gateway.bagio import convert_bag
from forestcare_gateway.demo import demo_config, write_demo_bag
from forestcare_gateway.geo import from_local
from forestcare_gateway.package import MissionPackage
from forestcare_gateway.sim import make_world
from forestcare_gateway.uploader import _encode, upload_package

QUIET = dict(log=lambda *_: None)


@pytest.fixture(scope="module")
def demo_bag(tmp_path_factory):
    path = tmp_path_factory.mktemp("journey") / "survey_bag"
    write_demo_bag(path, "survey", seed=11, max_duration_s=130)   # first two plant stops, with mock detections
    return path


@pytest.mark.parametrize("kind", ["survey", "loc"])
def test_simulated_routes_lie_in_bonn_woodland(ref, kind):
    world = make_world(kind)
    points = [world.point_at(s) for s in np.arange(0, world.length, 5.0)]
    contexts = [ref.context(*from_local(e, n, *world.origin)) for e, n in points]
    assert all(c["in_bonn"] for c in contexts)
    in_woodland = sum(1 for c in contexts if c["landuse"] and c["landuse"]["environment"] == "forest")
    assert in_woodland / len(contexts) > 0.8          # forest roads and rides are cut out of the land-use map


def test_gateway_mission_matches_the_contract(demo_bag, tmp_path):
    result = convert_bag(demo_bag, demo_config(outbox_dir=str(tmp_path)), **QUIET)
    pkg = MissionPackage(result["package"])
    mission = pkg.read_json("mission.json")
    payload = {**mission, "observations": [_encode(pkg, o) for o in mission["observations"]]}
    parsed = MissionIn.model_validate(payload)
    assert parsed.protocol == "opportunistic" and parsed.source_kind == "simulated"
    kinds = {o.target_probability is None for o in parsed.observations}
    assert kinds == {True, False}                      # operator marks and detections
    assert all(o.image is not None and o.metadata for o in parsed.observations)
    assert parsed.model.name == "mock-perception (simulated)"


def test_full_journey_over_http(demo_bag, tmp_path, empty_server_url):
    result = convert_bag(demo_bag, demo_config(outbox_dir=str(tmp_path)), **QUIET)
    pkg = MissionPackage(result["package"])
    upload = upload_package(pkg, empty_server_url, batch_size=3, **QUIET)
    assert upload["status"] == "uploaded" and upload["refused"] == []

    api = httpx.Client(base_url=empty_server_url)
    (mission,) = api.get("/api/missions").json()
    assert mission["id"] == result["mission_id"] and mission["protocol"] == "opportunistic"
    assert mission["source_kind"] == "simulated" and sum(len(s) for s in mission["track"]) > 50
    assert mission["provenance"]["metadata"]["source"]["kind"] == "bag"
    observations = api.get("/api/observations").json()
    assert {o["kind"] for o in observations} == {"mark", "detection"}
    assert len(observations) == result["observations"]

    mark = next(o for o in observations if o["kind"] == "mark")
    detail = api.get(f"/api/observations/{mark['id']}").json()
    assert detail["metadata"]["mark"]["label"] == "Prunus serotina?"
    assert detail["metadata"]["capture"]["frame"]["sha256"] == detail["provenance"]["image_sha256"]
    assert "no_model_prediction" in detail["qc_flags"]
    assert detail["context"]["nsg"][0]["id"] == "BN-003"            # the demo route runs through NSG Kottenforst
    queue = api.get("/api/review-queue").json()
    assert any("Marked by the robot operator" in r for item in queue for r in item["priority_reasons"])

    # An expert confirms the mark: a stand appears, presence-only (an operator mark has no count).
    r = api.post(f"/api/observations/{mark['id']}/reviews", json={"decision": "confirmed", "reviewer": "Test Expert"})
    assert r.status_code == 200
    stand = next(s for s in api.get("/api/stands").json() if s["id"] == mark["stand_id"])
    assert stand["status"] == "new" and stand["latest_plants_confirmed"] is None

    # Uploading again is harmless: the server already has every observation.
    pkg.set_state("complete")
    (pkg.root / "upload.json").unlink()
    again = upload_package(pkg, empty_server_url, **QUIET)
    assert again["status"] == "uploaded" and again["accepted"] == 0
    assert len(api.get("/api/observations").json()) == len(observations)


@pytest.mark.browser
def test_robot_observation_in_the_dashboard(demo_bag, tmp_path, empty_server_url):
    from playwright.sync_api import expect, sync_playwright

    result = convert_bag(demo_bag, demo_config(outbox_dir=str(tmp_path)), **QUIET)
    upload_package(MissionPackage(result["package"]), empty_server_url, **QUIET)
    observations = httpx.get(f"{empty_server_url}/api/observations").json()
    detection = next(o for o in observations if o["kind"] == "detection")
    mark = next(o for o in observations if o["kind"] == "mark")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(empty_server_url) else route.abort())
        page.goto(f"{empty_server_url}/#obs/{detection['id']}")
        expect(page.locator(".panel-body")).to_contain_text("Robot capture")
        expect(page.locator(".panel-body")).to_contain_text("identical to the recorded ROS frame")
        expect(page.locator(".bbox")).to_have_count(1)
        expect(page.locator("path.uncertainty-selected")).to_have_count(1)
        page.goto(f"{empty_server_url}/#obs/{mark['id']}")
        expect(page.locator(".panel-body")).to_contain_text("Marked by the operator")
        expect(page.locator(".bbox")).to_have_count(0)
        page.goto(f"{empty_server_url}/#missions")
        expect(page.locator(".panel-body")).to_contain_text("ROS 2 gateway (converted rosbag)")
        browser.close()
    assert errors == []


def test_uploader_reports_an_unreachable_server(demo_bag, tmp_path):
    result = convert_bag(demo_bag, demo_config(outbox_dir=str(tmp_path)), **QUIET)
    pkg = MissionPackage(result["package"])
    outcome = upload_package(pkg, "http://127.0.0.1:9", **QUIET)          # nothing listens on port 9
    assert outcome["status"] == "upload_failed" and "cannot reach" in outcome["error"]
    assert json.loads((pkg.root / "upload.json").read_text())["done"] == []
