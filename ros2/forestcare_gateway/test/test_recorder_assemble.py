"""Recorder + mission assembly on small, hand-made message streams."""

import json
import math

import pytest

from forestcare_gateway.assemble import build_track, map_hypotheses
from forestcare_gateway.config import DEFAULTS
from forestcare_gateway.geo import from_local, haversine_m, to_local
from forestcare_gateway.messages import NS
from forestcare_gateway.package import MissionPackage

from gateway_testlib import LAT0, LON0, T0, drive_east, fix_msg, frame_msg, mark_msg


def test_package_layout_and_mark_resolution(recorder):
    rec = recorder
    pkg = rec.start(T0)
    drive_east(rec, mark_at=10.0)
    summary = rec.finish()
    assert summary["status"] == "complete" and summary["observations"] == 1
    for name in ("header.json", "gnss.jsonl", "odom.jsonl", "frames.jsonl", "marks.jsonl", "stats.json", "mission.json"):
        assert (pkg.root / name).exists(), name
    (mark,) = pkg.read("marks")
    assert mark["frame_stamp_ns"] == T0 + 10 * NS          # the last frame before the button press
    assert mark["frame_dt_s"] == -0.1
    odom = pkg.read("odom")
    assert 19 * 20 <= len(odom) <= 21 * 20 + 2             # 50 Hz decimated to 20 Hz
    assert len(pkg.read("frames")) == 1                      # only the frame an observation needs is kept


def test_mark_observation_is_placed_ahead_of_the_robot(recorder):
    rec = recorder
    pkg = rec.start(T0)
    drive_east(rec, mark_at=10.0, sigma=1.0)
    rec.finish()
    mission = pkg.read_json("mission.json")
    (obs,) = mission["observations"]
    e, n = to_local(obs["lat"], obs["lon"], LAT0, LON0)
    assert e == pytest.approx(10.0 + 3.0, abs=0.05)          # robot at 10 m + 3 m camera offset, heading east
    assert n == pytest.approx(0.0, abs=0.05)
    assert obs["gnss_accuracy_m"] == pytest.approx(math.hypot(1.0, 2.0), abs=0.01)
    assert obs["predicted_taxon"] is None and obs["plant_count_est"] is None
    assert obs["uid"].endswith("-Mm1") and obs["image_file"].startswith("frames/")
    md = obs["metadata"]
    assert md["robot"]["heading_deg"] == pytest.approx(90.0, abs=1)
    assert md["capture"]["frame"]["sha256"] and md["mark"]["label"] == "Prunus serotina?"
    assert mission["protocol"] == "opportunistic" and mission["model"] is None
    assert mission["track"][0][0] == [round(LON0, 7), round(LAT0, 7)]


def test_detections_are_matched_to_frames_and_merged(recorder):
    rec = recorder
    pkg = rec.start(T0)
    drive_east(rec, dets_at=(4.0, 4.5, 5.0, 5.5))
    rec.finish()
    mission = pkg.read_json("mission.json")
    (obs,) = mission["observations"]                          # 4 detections of track 'trk1' -> one observation
    assert obs["target_probability"] == pytest.approx(0.655)  # the best frame (t = 5.5 s)
    assert obs["metadata"]["detection"]["merged"]["frames"] == 4
    assert obs["metadata"]["capture"]["frame"]["file"] == f"frames/{T0 + int(5.5 * NS)}.jpg"
    assert mission["model"]["name"] == "unnamed detector"
    assert len(pkg.read("frames")) == 4


def test_standing_robot_uses_heading_from_before_it_stopped(recorder):
    rec = recorder
    pkg = rec.start(T0)
    drive_east(rec, seconds=10)
    for k in range(1, 60):                                     # then 12 s standing still at e = 10 m
        now = T0 + 10 * NS + k * NS // 5
        lat, lon = from_local(10.0, 0.0, LAT0, LON0)
        rec.handle("gnss", "sensor_msgs/msg/NavSatFix", fix_msg(now, lat, lon, 1.0), now)
        if k % 5 == 0:
            rec.handle("camera", "sensor_msgs/msg/CompressedImage", frame_msg(now), now)
    rec.handle("marks", "std_msgs/msg/String", mark_msg("stopped here"), T0 + 21 * NS)
    rec.finish()
    obs = pkg.read_json("mission.json")["observations"][0]
    assert obs["metadata"]["robot"]["heading_deg"] == pytest.approx(90.0, abs=1)
    assert "earlier" in obs["metadata"]["robot"]["heading_source"]


def test_no_gnss_means_invalid_package_not_a_crash(recorder):
    rec = recorder
    pkg = rec.start(T0)
    rec.handle("camera", "sensor_msgs/msg/CompressedImage", frame_msg(T0), T0)
    rec.handle("marks", "std_msgs/msg/String", mark_msg("x"), T0 + NS)
    assert rec.finish()["status"] == "invalid"
    assert "no usable GNSS track" in pkg.state["error"]


def test_bad_message_does_not_stop_recording(recorder):
    rec = recorder
    rec.start(T0)
    rec.handle("gnss", "sensor_msgs/msg/NavSatFix", object(), T0)      # not a NavSatFix
    assert rec.counts["gnss_errors"] == 1
    drive_east(rec, seconds=2)
    assert rec.finish()["status"] == "complete"


def test_clock_before_2020_is_flagged_and_skipped(recorder):
    rec = recorder
    pkg = rec.start(T0)
    rec.handle("gnss", "sensor_msgs/msg/NavSatFix", fix_msg(5 * NS, LAT0, LON0), T0)
    drive_east(rec, seconds=2)
    rec.finish()
    stats = pkg.read_json("stats.json")
    assert any("not wall-clock" in w for w in stats["warnings"])
    assert stats["counts"]["gnss_bad_stamps"] == 1 and stats["first_ns"] == T0
    assert pkg.read_json("mission.json")["started_at"].startswith("2026-09-24")


def test_interrupted_recording_can_be_recovered(recorder):
    from forestcare_gateway.cli import main

    rec = recorder
    pkg = rec.start(T0)
    drive_east(rec, seconds=12, mark_at=10.0)
    rec._resolve(final=True)
    rec.pkg.close()                                              # the process dies here: no finish()
    with open(pkg.root / "gnss.jsonl", "a") as f:
        f.write('{"stamp_ns": 17902368')                         # a line cut off by the crash
    assert pkg.state["status"] == "recording"
    with pytest.raises(SystemExit) as done:
        main(["recover", str(pkg.root)])
    assert done.value.code == 0
    assert MissionPackage(pkg.root).state["status"] == "complete"
    assert len(pkg.read_json("mission.json")["observations"]) == 1


def test_track_is_split_at_gaps_and_jumps():
    lat = lambda n: from_local(0, n, LAT0, LON0)  # noqa: E731
    samples = [(T0 + i * NS, *lat(i * 2.0)) for i in range(10)]          # 2 m steps
    samples += [(T0 + 30 * NS + i * NS, *lat(18 + i * 2.0)) for i in range(5)]   # after a 20 s gap
    samples += [(T0 + 40 * NS, *lat(200.0)), (T0 + 41 * NS, *lat(202.0))]      # a 170 m jump
    track = build_track(samples, DEFAULTS["track"])
    assert [len(seg) for seg in track] == [10, 5, 2]


def test_track_thinning_keeps_endpoints():
    samples = [(T0 + i * NS // 10, *from_local(i * 0.1, 0, LAT0, LON0)) for i in range(101)]   # 10 m in 0.1 m steps
    (seg,) = build_track(samples, DEFAULTS["track"])
    assert len(seg) == 11
    assert haversine_m(seg[-1][1], seg[-1][0], *from_local(10.0, 0, LAT0, LON0)) < 0.01


def test_hypotheses_mapping():
    dcfg = DEFAULTS["detections"]
    m = map_hypotheses([{"class_id": "prunus_padus", "score": 0.5}, {"class_id": "prunus_serotina", "score": 0.4}], dcfg)
    assert m == {"predicted_taxon": "Prunus padus", "confidence": 0.5, "target_probability": 0.4,
                 "alternatives": [{"taxon": "Prunus serotina", "probability": 0.4}]}
    assert map_hypotheses([{"class_id": "prunus_serotina", "score": 0.2}], dcfg) is None
    assert map_hypotheses([{"class_id": "prunus_serotina", "score": 1.7}], dcfg)["confidence"] == 1.0
