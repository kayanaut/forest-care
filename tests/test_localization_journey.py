"""Milestone 3 end to end, without a robot:

    repeated simulated runs (rosbags) -> localization experiment -> report + recommended settings
    -> gateway with those settings -> Forest Care API -> stands and dashboard metadata

It also checks that the experiment's copy of the stand rule (loc/standmatch.py) forms exactly
the stands the backend forms from the same observations.
"""

from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest
import yaml

from forestcare_gateway.bagio import convert_bag
from forestcare_gateway.config import merge
from forestcare_gateway.loc.cli import run_evaluation, simulated_experiment
from forestcare_gateway.loc.evaluate import ORDER, gateway_config, load_experiment, recommended_config
from forestcare_gateway.loc.standmatch import Obs, link_stands
from forestcare_gateway.messages import NS
from forestcare_gateway.package import MissionPackage
from forestcare_gateway.uploader import upload_package

QUIET = dict(log=lambda *_: None)


@pytest.fixture(scope="module")
def experiment(tmp_path_factory):
    out = tmp_path_factory.mktemp("locexp")
    path = simulated_experiment(out, runs=2, seed=5, **QUIET)
    result = run_evaluation(path, out / "evaluation", **QUIET)
    return path, out / "evaluation", result


def test_experiment_compares_every_setup_and_answers_the_question(experiment):
    _, out, result = experiment
    methods = result["methods"]
    assert result["simulated"] and result["current_method"] == "gnss"
    assert list(methods) == ORDER and all(m["available"] for m in methods.values())
    # the answer starts with the setup the rover runs today
    assert result["recommendation"]["summary"][0].startswith("Current localization (GNSS receiver only")
    # RTK + odometry + IMU beats the raw receiver; after correction every setup's uncertainty is honest
    assert methods["rtk_odom"]["trajectory_error_m"]["median"] < methods["gnss"]["trajectory_error_m"]["median"]
    assert all(m["coverage_95"] >= 0.9 for m in methods.values())
    # each plant was seen once per run; drift comes from the loop closure on R1 (the field method)
    assert all(m["stands"]["plants"] == 6 and len(m["observations"]) == 12 for m in methods.values())
    assert result["drift"]["odom"]["loop_closure_per_100m"]["n"] == 2
    report = (out / "report.html").read_text()
    assert "Simulated data" in report and report.count("<svg") >= 10
    assert json.loads((out / "results.json").read_text())["methods"].keys() == methods.keys()


def upload_with_recommended_settings(experiment, server_url, outbox, runs=None) -> dict:
    """Convert runs with the settings the experiment recommends for 'rtk_odom' and upload them."""
    path, _, result = experiment
    exp = load_experiment(path)
    settings = recommended_config(result, method="rtk_odom")["forestcare_gateway"]["ros__parameters"]
    cfg = merge(gateway_config(exp), {**settings, "outbox_dir": str(outbox)})
    for run in exp["runs"][:runs]:                       # in time order, as a field team would upload
        pkg = MissionPackage(convert_bag(path.parent / run["bag"], cfg, **QUIET)["package"])
        assert upload_package(pkg, server_url, **QUIET)["status"] == "uploaded"
    return settings


def test_recommended_settings_reach_the_dashboard_and_stands_match_the_backend(experiment, empty_server_url, tmp_path):
    _, _, result = experiment
    settings = upload_with_recommended_settings(experiment, empty_server_url, tmp_path)
    assert settings["localization"]["validated"]["simulated"] is True
    yaml.safe_load(yaml.safe_dump(settings))            # plain YAML, ready for a ROS 2 params file

    rows = httpx.get(f"{empty_server_url}/api/observations").json()
    assert len(rows) == 12 and {r["kind"] for r in rows} == {"mark"}
    # same stands as the backend, from the same observations
    obs = [Obs(r["uid"], int(dt.datetime.fromisoformat(r["observed_at"]).timestamp() * NS), r["lat"], r["lon"],
               r["gnss_accuracy_m"]) for r in rows]
    ours = link_stands(obs)
    partition = lambda key: sorted(sorted(r["uid"] for r in rows if key(r) == k) for k in {key(r) for r in rows})  # noqa: E731
    assert partition(lambda r: ours[r["uid"]]) == partition(lambda r: r["stand_id"])

    # the dashboard gets the method, the calibrated uncertainty and the validation note
    detail = httpx.get(f"{empty_server_url}/api/observations/{rows[0]['id']}").json()
    loc = detail["metadata"]["localization"]
    assert loc["method"] == "rtk_odom" and loc["sigma_scale"] == settings["localization"]["sigma_scale"]
    assert loc["validated"]["experiment"] == result["experiment"] and loc["validated"]["runs"] == 2


@pytest.mark.browser
def test_dashboard_shows_the_localization_test(experiment, empty_server_url, tmp_path):
    from playwright.sync_api import expect, sync_playwright

    _, _, result = experiment
    settings = upload_with_recommended_settings(experiment, empty_server_url, tmp_path, runs=1)
    mark = httpx.get(f"{empty_server_url}/api/observations").json()[0]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(empty_server_url) else route.abort())
        page.goto(f"{empty_server_url}/#obs/{mark['id']}")
        panel = page.locator(".panel-body")
        check = panel.locator('dt:has-text("Localization test") + dd')
        expect(check).to_contain_text(result["experiment"])
        expect(check.locator(".badge.sim")).to_have_count(1)
        expect(panel).to_contain_text(f"uncertainty ×{settings['localization']['sigma_scale']} (calibrated)")
        expect(page.locator("path.uncertainty-selected")).to_have_count(1)
        browser.close()
    assert errors == []
