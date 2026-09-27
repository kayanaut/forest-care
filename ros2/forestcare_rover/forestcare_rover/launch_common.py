"""Building blocks shared by the rover launch files."""

from __future__ import annotations

from pathlib import Path

import yaml
from ament_index_python.packages import get_package_share_directory
from launch_ros.actions import Node


def share(package: str, *parts: str) -> str:
    return str(Path(get_package_share_directory(package), *parts))


def driver_nodes(drivers_file: str) -> list[Node]:
    """One Node per entry of drivers.yaml (package, executable, parameters, remappings)."""
    entries = (yaml.safe_load(Path(drivers_file).read_text()) or {}).get("drivers") or []
    return [Node(package=d["package"], executable=d["executable"], name=d.get("name"), output="screen",
                 parameters=[d.get("parameters") or {}], remappings=list((d.get("remappings") or {}).items()))
            for d in entries]


def mount_nodes(mounts_file: str) -> list[Node]:
    """A static transform publisher per sensor mount in mounts.yaml."""
    import math

    data = yaml.safe_load(Path(mounts_file).read_text()) or {}
    base = data.get("base_frame", "base_link")
    nodes = []
    for child, m in (data.get("mounts") or {}).items():
        args = ["--x", str(m.get("x", 0)), "--y", str(m.get("y", 0)), "--z", str(m.get("z", 0)),
                "--roll", str(math.radians(m.get("roll", 0))), "--pitch", str(math.radians(m.get("pitch", 0))),
                "--yaw", str(math.radians(m.get("yaw", 0))), "--frame-id", base, "--child-frame-id", child]
        nodes.append(Node(package="tf2_ros", executable="static_transform_publisher",
                          name=f"mount_{child}", arguments=args, output="log"))
    return nodes


def teleop_nodes(teleop_params: str) -> list[Node]:
    """Gamepad -> /cmd_vel with a deadman button, plus the mark buttons."""
    return [
        Node(package="joy", executable="joy_node", name="joy_node", parameters=[teleop_params]),
        Node(package="teleop_twist_joy", executable="teleop_node", name="teleop_twist_joy_node",
             parameters=[teleop_params]),
        Node(package="forestcare_gateway", executable="mark", name="forestcare_mark", parameters=[teleop_params],
             output="screen"),
    ]
