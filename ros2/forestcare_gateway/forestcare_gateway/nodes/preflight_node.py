"""Pre-drive check: listen to the rover's topics for a few seconds and say whether it is ready.

    ros2 run forestcare_gateway preflight --ros-args --params-file config/gateway.yaml -p duration_s:=10.0

Prints OK / WARN / FAIL per sensor (rates, GNSS fix and reported accuracy, clock sanity,
camera frames, free disk space) using the same rules as `fc_gateway inspect` for recorded
bags. Exit code 0 = ready to record, 1 = fix the FAIL lines first.
"""

from __future__ import annotations

import os
import shutil
import sys
import time

import rclpy
from rclpy.node import Node

from ..health import TopicStats, checks, render
from .common import declare_config, role_types, subscribe_roles


class Preflight(Node):
    def __init__(self):
        super().__init__("forestcare_preflight", automatically_declare_parameters_from_overrides=True)
        for name, default in {"duration_s": 10.0, "bag_dir": "~/bags"}.items():
            if not self.has_parameter(name):
                self.declare_parameter(name, default)
        self.cfg = declare_config(self)
        self.stats = {role: TopicStats(role, topic) for role, (topic, _) in role_types(self.cfg).items()}
        subscribe_roles(self, self.cfg, self._on)

    def _on(self, role: str, msgtype: str, msg) -> None:
        self.stats[role].add(msgtype, msg, self.get_clock().now().nanoseconds)


def disk_lines(paths: dict[str, str]) -> list[tuple[str, str]]:
    out = []
    for label, path in paths.items():
        path = os.path.expanduser(path)
        probe = path
        while not os.path.exists(probe) and probe not in ("", "/"):
            probe = os.path.dirname(probe)
        free_gb = shutil.disk_usage(probe or "/").free / 1e9
        out.append(("OK" if free_gb >= 20 else ("WARN" if free_gb >= 5 else "FAIL"),
                    f"disk: {free_gb:.0f} GB free for {label} ({path}); a 720p camera needs ~5 GB per hour, 3D LiDAR adds 10-20 GB"))
    return out


def main(args=None) -> None:
    rclpy.init(args=args)
    node = Preflight()
    duration = float(node.get_parameter("duration_s").value)
    node.get_logger().info(f"listening for {duration:.0f} s ...")
    end = time.monotonic() + duration
    while time.monotonic() < end and rclpy.ok():
        rclpy.spin_once(node, timeout_sec=0.1)
    lines = checks(node.stats, node.cfg) + disk_lines({"the outbox": node.cfg["outbox_dir"],
                                                        "bags": node.get_parameter("bag_dir").value})
    text, ok = render(lines, f"preflight ({duration:.0f} s)")
    print(text, flush=True)
    node.destroy_node()
    rclpy.shutdown()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
