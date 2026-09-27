"""Localization (milestone 3): estimators, the copy of Forest Care's stand rule, experiment metrics.

No ROS and no backend needed. The whole experiment (simulated runs -> evaluation -> API)
runs in tests/test_localization_journey.py.
"""

from __future__ import annotations

import numpy as np
import pytest
from gateway_testlib import LAT0, LON0, T0

from forestcare_gateway.config import merge
from forestcare_gateway.geo import from_local, haversine_m
from forestcare_gateway.localize import build_trajectory
from forestcare_gateway.loc.evaluate import resolvable_separation, track_offsets
from forestcare_gateway.loc.standmatch import Obs, link_distance, link_stands, match_report
from forestcare_gateway.messages import NS, GnssFix


def drive(seconds=120.0, speed=1.0, sigma=2.0, bias=(0.0, 0.0), outage=None, seed=1) -> dict:
    """Straight drive east: GNSS 5 Hz (reported sigma, constant bias, 0.3 m noise), odometry 10 Hz, IMU 50 Hz."""
    rng = np.random.default_rng(seed)
    gnss, odom, imu = [], [], []
    for i in range(int(seconds * 50) + 1):
        t = i / 50
        stamp = T0 + round(t * NS)
        if i % 10 == 0 and not (outage and outage[0] <= t < outage[1]):
            lat, lon = from_local(speed * t + bias[0] + rng.normal(0, 0.3), bias[1] + rng.normal(0, 0.3), LAT0, LON0)
            gnss.append(GnssFix(stamp, lat, lon, 90.0, 0, 1, 2, sigma))
        if i % 5 == 0:
            odom.append({"stamp_ns": stamp, "x": speed * t, "y": 0.0, "yaw": 0.0, "v": speed, "w": 0.0})
        imu.append({"stamp_ns": stamp, "wz": 0.0})
    return {"gnss": gnss, "gnss_rtk": [], "odom": odom, "odom_alt": [], "imu": imu}


def truth(t_s: float, speed=1.0):
    return from_local(speed * t_s, 0.0, LAT0, LON0)


def with_method(cfg, method, **loc):
    return merge(cfg, {"localization": {"method": method, **loc}})


def test_fusion_bridges_a_gnss_outage(cfg):
    streams = drive(outage=(50.0, 70.0))
    raw = build_trajectory(with_method(cfg, "gnss"), streams)
    fused = build_trajectory(with_method(cfg, "gnss_odom_imu"), streams)
    mid = T0 + 60 * NS
    assert raw.at(mid) is None                          # 20 s without a fix: GNSS alone has no position
    pose = fused.at(mid)
    assert haversine_m(pose.lat, pose.lon, *truth(60)) < 1.5
    assert pose.sigma_m > fused.at(T0 + 30 * NS).sigma_m      # and it says it is less sure there


def test_fusion_does_not_remove_gnss_bias_and_says_so(cfg):
    """A constant 3 m GNSS bias looks like a correct position: no filter can see it. The claimed
    uncertainty must stay large enough to cover it instead of shrinking with every fix."""
    fused = build_trajectory(with_method(cfg, "gnss_odom_imu"), drive(bias=(0.0, 3.0)))
    pose = fused.at(T0 + 60 * NS)
    error = haversine_m(pose.lat, pose.lon, *truth(60))
    assert error > 2.0
    assert pose.sigma_m > 1.2


def test_sigma_scale_calibrates_any_method(cfg):
    streams = drive()
    for method in ("gnss", "gnss_odom_imu"):
        base = build_trajectory(with_method(cfg, method), streams)
        scaled = build_trajectory(with_method(cfg, method, sigma_scale=2.5), streams)
        assert scaled.e == base.e
        assert scaled.sigma == pytest.approx([2.5 * s for s in base.sigma])


def test_fusion_needs_the_sensors_it_names(cfg):
    streams = {**drive(), "odom": []}
    with pytest.raises(ValueError, match="needs odom data"):
        build_trajectory(with_method(cfg, "gnss_odom"), streams)


def test_track_offsets_see_shifts_not_jitter():
    t = np.arange(0, 200, 0.2)                           # 5 Hz, 1 m/s
    line = np.column_stack([t, np.zeros_like(t)])
    jittery = line + np.random.default_rng(0).normal(0, 1.5, line.shape)
    shifted = line + [0.0, 3.0]
    assert np.median(track_offsets([(t, line), (t, jittery)])) < 0.5
    assert np.median(track_offsets([(t, line), (t, shifted)])) == pytest.approx(3.0, abs=0.1)


def test_stand_separation_cannot_beat_the_stand_rule():
    """With perfect positions, two stands still merge within Forest Care's link distance."""
    floor = link_distance(2.0, 2.0)                      # 8 m spread + 2 x sqrt(2^2 + 2^2) = 13.7 m
    exact = resolvable_separation(np.zeros((10, 2)), np.full(10, 2.0))
    assert floor < exact <= floor + 0.5
    errors = np.random.default_rng(3).normal(0, 3.0, (40, 2))
    assert resolvable_separation(errors, np.full(40, 2.0)) > exact + 3


def test_link_stands_follows_forest_cares_rule():
    def obs(key, t, e, n, tag, sigma=2.0):
        return Obs(key, t, *from_local(e, n, LAT0, LON0), sigma, tag)

    plants = [obs("a1", 1, 0, 0, "A"), obs("a2", 2, 3, 1, "A"),       # same plant, 3 m apart
              obs("b1", 3, 40, 0, "B"), obs("b2", 4, 41, -2, "B"),    # another plant, 40 m away
              obs("c1", 5, 52, 0, "C")]                               # 12 m from B: inside the link distance
    report = match_report(plants, link_stands(plants))
    assert report["plants"] == 3 and report["reidentified"] == 3
    assert report["merged_groups"] == [["B", "C"]] and report["stands"] == 2


def test_pose_only_odometry_gets_a_twist():
    """LiDAR odometry nodes often publish poses only; speed and yaw rate are derived from them."""
    from forestcare_gateway.assemble import with_twist

    rows, x, y = [], 0.0, 0.0
    for i in range(20):                                  # an arc: 1 m/s, 0.2 rad/s, poses at 10 Hz
        rows.append({"stamp_ns": T0 + i * NS // 10, "x": x, "y": y, "yaw": 0.02 * i, "v": 0.0, "w": 0.0})
        x, y = x + 0.1 * np.cos(0.02 * i), y + 0.1 * np.sin(0.02 * i)
    derived = with_twist(rows)
    assert all(r["v"] == pytest.approx(1.0, rel=0.01) and r["w"] == pytest.approx(0.2) for r in derived[1:])
    assert with_twist([dict(r, v=0.5) for r in rows])[5]["v"] == 0.5          # a real twist is kept
