"""Gateway settings.

One YAML file serves the ROS node (as ROS 2 parameters) and the offline tools. It may be
written in ROS parameter format (`forestcare_gateway: {ros__parameters: {...}}`) or as a
plain mapping. Anything not set keeps the default below. Values marked FIELD are
assumptions that must be checked on the real rover (see docs/ROS2_GATEWAY.md).
"""

from __future__ import annotations

import copy
from pathlib import Path

DEFAULTS: dict = {
    "robot_id": "rover-01",
    # 'robot' for real hardware. 'simulated' for the sim rover and demo bags: Forest Care then
    # labels everything from this mission as simulated.
    "source_kind": "robot",
    "area_name": "",
    # 'opportunistic': operator marks what they notice (presence only, the usual case).
    # 'transect': a systematic survey where the track may be used to infer absence.
    "survey_protocol": "opportunistic",
    "detection_range_m": 8.0,          # FIELD: how far from the track the camera can see plants
    "outbox_dir": "~/forestcare_outbox",
    "api_url": "http://127.0.0.1:8000",
    "upload_on_finish": True,          # live node: try to upload right after a mission ends
    "upload_batch_size": 20,
    "topics": {
        "gnss": "/gnss/fix",                         # sensor_msgs/NavSatFix
        "gnss_rtk": "",                              # optional second (RTK) receiver, NavSatFix
        "odom": "/odom",                             # nav_msgs/Odometry, wheel odometry
        "odom_alt": "",                              # optional LiDAR/visual odometry, nav_msgs/Odometry
        "imu": "/imu/data",                          # sensor_msgs/Imu
        "camera": "/camera/image_raw/compressed",    # sensor_msgs/CompressedImage (or Image)
        "detections": "/forestcare/detections",      # vision_msgs/Detection2DArray (optional)
        "marks": "/forestcare/mark",                 # std_msgs/String, operator marks
        "lidar": "/scan",                            # LaserScan or PointCloud2 (only summarised)
    },
    "lidar_type": "scan",              # 'scan' (sensor_msgs/LaserScan) or 'points' (sensor_msgs/PointCloud2)
    "camera": {
        "name": "front camera",
        "time_offset_s": 0.0,        # FIELD: added to image stamps (e.g. -0.08 if stamps are 80 ms late)
        "forward_offset_m": 3.0,     # FIELD: typical distance from the antenna to the plants in view
        "yaw_offset_deg": 0.0,       # FIELD: camera direction relative to driving direction (+ = left)
        "placement_sigma_m": 2.0,    # FIELD: uncertainty of that simple projection
        "keep_every_s": 0.0,         # also keep a frame at this interval (0 = only frames used by observations)
    },
    "gnss": {
        "name": "GNSS receiver",
        "max_fix_gap_s": 2.0,        # frames further than this from a fix are not placed
        "assumed_sigma_m": 5.0,      # if the receiver reports no covariance
        "accuracy_scale": 1.0,       # multiply the receiver's reported sigma before localization (known bias only)
        "min_status": 0,             # NavSatStatus: -1 no fix, 0 fix, 1 SBAS, 2 GBAS/RTK
    },
    "localization": {
        # gnss | gnss_imu | gnss_odom | gnss_odom_imu | gnss_altodom | rtk | rtk_odom (see loc/estimators.py)
        "method": "gnss",
        # FIELD: multiplies the robot position uncertainty of any method so that 95 % of the errors fall
        # inside the 95 % circle (fc_gateway loc-eval measures it: recommended_gateway.yaml)
        "sigma_scale": 1.0,
        # filled from a localization experiment (fc_gateway loc-eval writes it); shown in the dashboard
        "validated": {},
    },
    "track": {"min_step_m": 1.0, "max_gap_s": 10.0, "max_jump_m": 50.0},
    "detections": {
        "target_class": "prunus_serotina",
        "class_map": {
            "prunus_serotina": "Prunus serotina",
            "prunus_padus": "Prunus padus",
            "frangula_alnus": "Frangula alnus",
            "prunus_avium": "Prunus avium",
        },
        "min_target_score": 0.35,    # detections below this are not sent to Forest Care
        "frame_tolerance_s": 0.05,   # detection stamp must match an image stamp this closely
        # A detector sees the same plant in many consecutive frames. Detections with the same
        # tracker id (Detection2D.id), or without id but within `merge_radius_m`, are merged when
        # less than `merge_window_s` apart; the best-scoring frame is sent.
        "merge_radius_m": 4.0,
        "merge_window_s": 30.0,
        "model_name": "",
        "model_version": "",
    },
    "marks": {"frame_window_s": 1.0},   # a mark uses the last frame before it (or the next one) within this window
    "recording": {"odom_rate_hz": 20.0, "imu_rate_hz": 20.0, "lidar_rate_hz": 1.0, "fsync_interval_s": 5.0},
    "simulator": {},                    # filled by the demo tools for simulated missions
}


def merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str | Path | None = None, overrides: dict | None = None) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    if path:
        import yaml  # only needed when a file is given

        data = yaml.safe_load(Path(path).read_text()) or {}
        if len(data) == 1:
            (only,) = data.values()
            if isinstance(only, dict) and "ros__parameters" in only:
                data = only["ros__parameters"]
        cfg = merge(cfg, data)
    return merge(cfg, overrides or {})


def flatten(d: dict, prefix: str = "") -> dict:
    """{'topics': {'gnss': '/x'}} -> {'topics.gnss': '/x'} (ROS parameter names)."""
    out = {}
    for key, value in d.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            out.update(flatten(value, name + "."))
        elif not isinstance(value, dict):
            out[name] = value
    return out


def unflatten(flat: dict) -> dict:
    out: dict = {}
    for name, value in flat.items():
        node = out
        *parents, leaf = name.split(".")
        for p in parents:
            node = node.setdefault(p, {})
        node[leaf] = value
    return out


def outbox_path(cfg: dict) -> Path:
    return Path(cfg["outbox_dir"]).expanduser()
