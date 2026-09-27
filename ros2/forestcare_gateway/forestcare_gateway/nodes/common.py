"""rclpy helpers shared by the nodes."""

from __future__ import annotations

import array
import importlib
import signal

import numpy as np
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, qos_profile_sensor_data

from ..config import DEFAULTS, flatten, merge, unflatten

GROUPS_WITH_FREE_KEYS = ("detections.class_map.", "simulator.", "localization.validated.")


def declare_config(node: Node) -> dict:
    """Declare every gateway setting as a ROS parameter (topics.gnss, camera.forward_offset_m, ...).

    Values from a parameter file override the defaults. Returns the nested config dict that the
    plain-Python modules expect. The node must be created with
    `automatically_declare_parameters_from_overrides=True`.
    """
    defaults = flatten(DEFAULTS)
    for name, value in defaults.items():
        if not node.has_parameter(name):
            node.declare_parameter(name, value)
    flat = {name: p.value for name, p in node.get_parameters_by_prefix("").items()
            if name in defaults or name.startswith(GROUPS_WITH_FREE_KEYS)}
    return merge(DEFAULTS, unflatten(flat))


def message_class(type_name: str):
    """'sensor_msgs/msg/NavSatFix', 'sensor_msgs/NavSatFix' or 'sequence<vision_msgs/Detection2D>' -> class."""
    name = type_name.removeprefix("sequence<").split(",")[0].rstrip(">")
    pkg, *_, cls = name.split("/")
    return getattr(importlib.import_module(f"{pkg}.msg"), cls)


def fill(msg, data: dict):
    """Copy a plain dict (ROS 2 message layout, as produced by sim.py) into an rclpy message."""
    types = msg.get_fields_and_field_types()
    for key, value in data.items():
        kind = types.get(key, "")
        floaty = "double" in kind or "float" in kind
        if isinstance(value, list) and floaty:
            value = [float(v) for v in value]
        elif floaty and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        if isinstance(value, dict):
            fill(getattr(msg, key), value)
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            cls = message_class(types[key])
            setattr(msg, key, [fill(cls(), v) for v in value])
        elif isinstance(value, (bytes, bytearray)):
            setattr(msg, key, array.array("B", value))
        elif isinstance(value, np.ndarray):
            setattr(msg, key, value.astype(float).tolist())
        else:
            setattr(msg, key, value)
    return msg


def role_types(cfg: dict) -> dict[str, tuple[str, str]]:
    """role -> (topic, message type) for every configured topic."""
    t = cfg["topics"]
    camera = "sensor_msgs/msg/CompressedImage" if t["camera"].rstrip("/").endswith("compressed") else "sensor_msgs/msg/Image"
    lidar = "sensor_msgs/msg/PointCloud2" if cfg["lidar_type"] == "points" else "sensor_msgs/msg/LaserScan"
    types = {"gnss": "sensor_msgs/msg/NavSatFix", "gnss_rtk": "sensor_msgs/msg/NavSatFix",
             "odom": "nav_msgs/msg/Odometry", "odom_alt": "nav_msgs/msg/Odometry", "imu": "sensor_msgs/msg/Imu",
             "camera": camera, "detections": "vision_msgs/msg/Detection2DArray", "marks": "std_msgs/msg/String",
             "lidar": lidar}
    return {role: (t[role], types[role]) for role in types if t.get(role)}


def subscribe_roles(node: Node, cfg: dict, callback) -> list[str]:
    """Subscribe to every configured topic; callback(role, msgtype, msg). Returns the roles skipped."""
    skipped = []
    for role, (topic, msgtype) in role_types(cfg).items():
        try:
            cls = message_class(msgtype)
        except ImportError:
            node.get_logger().warning(f"{role}: {msgtype} is not installed here; not subscribing to {topic}")
            skipped.append(role)
            continue
        qos = QoSProfile(depth=50, reliability=ReliabilityPolicy.RELIABLE) if role == "marks" else qos_profile_sensor_data
        node.create_subscription(cls, topic, lambda msg, r=role, m=msgtype: callback(r, m, msg), qos)
    return skipped


def hold_signals() -> None:
    """Ignore further Ctrl-C / SIGTERM while shutting down. A terminal Ctrl-C reaches every
    process of the launch at once and `ros2 launch` forwards another SIGINT, which would
    otherwise interrupt the node while it writes the end of the mission."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
