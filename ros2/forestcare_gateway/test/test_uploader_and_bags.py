"""Upload behaviour (with a fake server) and bag conversion (real rosbag2 files written by rosbags)."""

import json

import pytest

from forestcare_gateway.bagio import convert_bag
from forestcare_gateway.demo import demo_config, write_demo_bag
from forestcare_gateway.health import inspect_bag
from forestcare_gateway.package import list_packages
from forestcare_gateway.recorder import MissionRecorder
from forestcare_gateway.uploader import UploadError, upload_outbox, upload_package

from gateway_testlib import T0, drive_east, mark_msg


class FakeServer:
    """Accepts batches like POST /api/missions (idempotent by uid); can fail on demand."""

    def __init__(self, fail_on=(), status_error=None):
        self.missions, self.uids, self.calls = {}, set(), 0
        self.fail_on, self.status_error = set(fail_on), status_error

    def __call__(self, url, payload):
        self.calls += 1
        if self.status_error:
            raise UploadError(self.status_error, retryable=False)
        if self.calls in self.fail_on:
            raise UploadError("cannot reach server", retryable=True)
        self.missions.setdefault(payload["mission_id"], payload["track"])
        new = [o for o in payload["observations"] if o["uid"] not in self.uids]
        self.uids |= {o["uid"] for o in new}
        return {"accepted": len(new), "duplicates": len(payload["observations"]) - len(new), "rejected": []}


def finished_package(cfg, marks=5):
    """A finished mission with `marks` operator marks in the last seconds (frames still buffered)."""
    rec = MissionRecorder(cfg, {"kind": "test"}, log=lambda *_: None)
    pkg = rec.start(T0)
    seconds = 10
    drive_east(rec, seconds=seconds)
    for i in range(marks):
        stamp = T0 + (seconds - 4) * 1_000_000_000 + i * 700_000_000
        rec.handle("marks", "std_msgs/msg/String",
                   mark_msg(json.dumps({"label": f"plant {i}", "id": f"m{i}", "stamp_ns": stamp})), stamp)
    rec.finish()
    return pkg


def test_upload_in_batches(cfg):
    pkg = finished_package(cfg)
    server = FakeServer()
    result = upload_package(pkg, "http://x", batch_size=2, post=server, log=lambda *_: None)
    assert result["status"] == "uploaded" and result["accepted"] == 5
    assert server.calls == 3 and len(server.uids) == 5
    first = next(iter(server.missions.values()))
    assert first == pkg.read_json("mission.json")["track"]


def test_interrupted_upload_resumes_without_duplicates(cfg):
    pkg = finished_package(cfg)
    server = FakeServer(fail_on={2})                               # the 2nd batch hits a dead connection
    first = upload_package(pkg, "http://x", batch_size=2, post=server, log=lambda *_: None)
    assert first["status"] == "upload_failed" and first["sent"] == 2
    second = upload_package(pkg, "http://x", batch_size=2, post=server, log=lambda *_: None)
    assert second["status"] == "uploaded" and second["sent"] == 3   # only what was missing
    assert len(server.uids) == 5
    assert json.loads((pkg.root / "upload.json").read_text())["attempts"] == 2


def test_server_rejection_is_not_retried(cfg):
    pkg = finished_package(cfg)
    result = upload_package(pkg, "http://x", post=FakeServer(status_error="HTTP 422: bad"), log=lambda *_: None)
    assert result["status"] == "rejected"
    assert upload_package(pkg, "http://x", post=FakeServer(), log=lambda *_: None)["sent"] == 0


def test_changed_frame_is_not_uploaded(cfg):
    pkg = finished_package(cfg, marks=1)
    frame = next((pkg.root / "frames").iterdir())
    frame.write_bytes(frame.read_bytes() + b"x")
    result = upload_package(pkg, "http://x", post=FakeServer(), log=lambda *_: None)
    assert result["status"] == "rejected" and "changed on disk" in result["error"]


def test_mission_without_observations_is_still_sent(recorder):
    rec = recorder
    pkg = rec.start(T0)
    drive_east(rec, seconds=5)
    rec.finish()
    server = FakeServer()
    assert upload_package(pkg, "http://x", post=server, log=lambda *_: None)["status"] == "uploaded"
    assert server.calls == 1 and pkg.read_json("mission.json")["mission_id"] in server.missions


def test_upload_outbox_retries_until_success(cfg):
    finished_package(cfg)
    server = FakeServer(fail_on={1})
    results = upload_outbox(cfg["outbox_dir"], "http://x", retry_for_s=5, interval_s=0.01, post=server, log=lambda *_: None)
    assert [r["status"] for r in results] == ["uploaded"]


@pytest.fixture(scope="module")
def short_bag(tmp_path_factory):
    path = tmp_path_factory.mktemp("bags") / "survey"
    write_demo_bag(path, "survey", seed=5, max_duration_s=125)     # covers the first two plant stops
    return path


def test_demo_bag_is_healthy(short_bag):
    report = inspect_bag(short_bag, demo_config())
    assert report["ok"], report["text"]
    assert report["summary"]["topics"]["/gnss/fix"]["type"] == "sensor_msgs/msg/NavSatFix"
    assert {"/tf_static", "/cmd_vel", "/scan"} <= set(report["summary"]["topics"])
    assert (short_bag / "mission_info.yaml").exists() and (short_bag / "ground_truth.json").exists()


def test_bag_conversion_is_deterministic(short_bag, tmp_path):
    a = convert_bag(short_bag, demo_config(outbox_dir=str(tmp_path / "a")), log=lambda *_: None)
    b = convert_bag(short_bag, demo_config(outbox_dir=str(tmp_path / "b")), log=lambda *_: None)
    assert a["status"] == b["status"] == "complete"
    ma = json.loads((tmp_path / "a" / a["mission_id"] / "mission.json").read_text())
    mb = json.loads((tmp_path / "b" / b["mission_id"] / "mission.json").read_text())
    assert a["mission_id"] == b["mission_id"]
    assert [o["uid"] for o in ma["observations"]] == [o["uid"] for o in mb["observations"]]
    assert ma["observations"] == mb["observations"]
    marks = [o for o in ma["observations"] if o["target_probability"] is None]
    detections = [o for o in ma["observations"] if o["target_probability"] is not None]
    assert len(marks) == 2 and detections
    assert ma["metadata"]["operator_info"]["operator"] == "Simulated operator"
    assert ma["metadata"]["source"]["kind"] == "bag"


def test_converted_marks_land_near_the_true_plants(short_bag, tmp_path):
    from forestcare_gateway.geo import haversine_m

    result = convert_bag(short_bag, demo_config(outbox_dir=str(tmp_path)), log=lambda *_: None)
    mission = json.loads((tmp_path / result["mission_id"] / "mission.json").read_text())
    truth = json.loads((short_bag / "ground_truth.json").read_text())
    for obs in (o for o in mission["observations"] if o["target_probability"] is None):
        nearest = min(haversine_m(obs["lat"], obs["lon"], p["lat"], p["lon"]) for p in truth["plants"])
        assert nearest < 3 * obs["gnss_accuracy_m"]                # within the stated uncertainty (3 sigma)


def test_convert_cli_refuses_to_overwrite(short_bag, tmp_path):
    from forestcare_gateway.cli import main

    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(f"outbox_dir: {tmp_path / 'out'}\nsource_kind: simulated\nsimulator: {{name: t}}\n")
    args = ["convert", str(short_bag), "--config", str(cfg_path)]
    for extra, code in (([], 0), ([], 2), (["--force"], 0)):
        with pytest.raises(SystemExit) as done:
            main(args + extra)
        assert done.value.code == code
    assert len(list_packages(tmp_path / "out")) == 2              # the old package was kept, renamed


def test_simulator_bags_are_always_labelled_simulated(short_bag, tmp_path):
    """Converting a simulator bag with a real-rover config must not pass it off as real data."""
    from forestcare_gateway.config import DEFAULTS, merge

    real_rover_cfg = merge(DEFAULTS, {"outbox_dir": str(tmp_path), "source_kind": "robot"})
    result = convert_bag(short_bag, real_rover_cfg, log=lambda *_: None)
    mission = json.loads((tmp_path / result["mission_id"] / "mission.json").read_text())
    assert mission["source_kind"] == "simulated"
    assert "only the simulator publishes" in mission["simulator"]["detected_by"]


def test_one_receiver_can_feed_gnss_and_rtk(short_bag, tmp_path):
    """A rover with a single RTK receiver maps /gnss/fix to both roles, so 'rtk' methods can be evaluated."""
    from forestcare_gateway.assemble import read_streams
    from forestcare_gateway.package import MissionPackage

    cfg = demo_config(outbox_dir=str(tmp_path), topics={"gnss_rtk": "/gnss/fix"})
    streams = read_streams(MissionPackage(convert_bag(short_bag, cfg, log=lambda *_: None)["package"]))
    assert streams["gnss"] and [f.stamp_ns for f in streams["gnss_rtk"]] == [f.stamp_ns for f in streams["gnss"]]
