"""Operator marks: "look here" while driving.

Joystick (runs next to teleop_twist_joy):
    ros2 run forestcare_gateway mark --ros-args -p buttons:="[0, 1, 3]" \
        -p labels:="['Prunus serotina?', 'other plant of interest', 'REF']"
  Each configured button publishes one mark on /forestcare/mark when pressed. A label 'REF'
  becomes REF:R1, REF:R2, ... (surveyed reference points for the localization test).

Keyboard (a second terminal next to teleop_twist_keyboard):
    ros2 run forestcare_gateway mark_console
  Enter = default label; 'ref R3' = reference point R3; 'plant P2 Prunus serotina?' = tagged
  plant; any other text = that label.

A mark is a std_msgs/String with a small JSON payload:
    {"id": "<node start>-<n>", "stamp_ns": <time of the button press>, "label": "...", "source": "..."}
The id and stamp travel with the message, so the live gateway and a later bag conversion
turn it into the same Forest Care observation (no duplicates). Plain text also works:
    ros2 topic pub --once /forestcare/mark std_msgs/msg/String "{data: 'Prunus serotina?'}"
"""

from __future__ import annotations

import json
import sys

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy
from std_msgs.msg import String
from std_srvs.srv import Trigger

from .common import hold_signals

MARK_QOS = QoSProfile(depth=50, reliability=ReliabilityPolicy.RELIABLE)


class MarkPublisher(Node):
    def __init__(self, name: str = "forestcare_mark"):
        super().__init__(name)
        self.declare_parameter("mark_topic", "/forestcare/mark")
        self.declare_parameter("default_label", "Prunus serotina?")
        self.pub = self.create_publisher(String, self.get_parameter("mark_topic").value, MARK_QOS)
        self.session = self.get_clock().now().nanoseconds // 1_000_000_000
        self.count = 0
        self.refs = 0

    def mark(self, label: str | None = None, source: str = "", stamp_ns: int | None = None) -> str:
        label = (label or self.get_parameter("default_label").value).strip()
        if label.upper() == "REF":
            self.refs += 1
            label = f"REF:R{self.refs}"
        self.count += 1
        payload = {"id": f"{self.session}-{self.count}", "stamp_ns": stamp_ns or self.get_clock().now().nanoseconds,
                   "label": label, "source": source}
        self.pub.publish(String(data=json.dumps(payload)))
        self.get_logger().info(f"mark {self.count}: {label}")
        return label


class JoyMarkNode(MarkPublisher):
    def __init__(self):
        super().__init__("forestcare_mark")
        self.declare_parameter("joy_topic", "/joy")
        self.declare_parameter("buttons", [0, 1, 3])
        self.declare_parameter("labels", ["Prunus serotina?", "other plant of interest", "REF"])
        self.buttons = list(self.get_parameter("buttons").value)
        self.labels = list(self.get_parameter("labels").value)
        if len(self.buttons) != len(self.labels):
            raise ValueError("'buttons' and 'labels' must have the same length")
        self.previous: list[int] = []
        from sensor_msgs.msg import Joy

        self.create_subscription(Joy, self.get_parameter("joy_topic").value, self._on_joy, 10)
        self.create_service(Trigger, "~/mark", self._srv)
        self.get_logger().info("mark buttons: " + ", ".join(f"{b} = {l!r}" for b, l in zip(self.buttons, self.labels)))

    def _on_joy(self, msg) -> None:
        pressed = list(msg.buttons)
        stamp = msg.header.stamp.sec * 1_000_000_000 + msg.header.stamp.nanosec
        for button, label in zip(self.buttons, self.labels):
            was = self.previous[button] if button < len(self.previous) else 0
            if button < len(pressed) and pressed[button] and not was:
                self.mark(label, f"joystick button {button}", stamp or None)
        self.previous = pressed

    def _srv(self, _req, resp):
        resp.message = self.mark(source="service")
        resp.success = True
        return resp


def main(args=None) -> None:
    rclpy.init(args=args)
    node = JoyMarkNode()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        hold_signals()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


def console(args=None) -> None:
    rclpy.init(args=args)
    node = MarkPublisher("forestcare_mark_console")
    print("Forest Care marks. Enter = default label, 'ref R3' = reference point, "
          "'plant P2 <label>' = tagged plant, other text = label, Ctrl-D = quit.", flush=True)
    try:
        for line in sys.stdin:
            text = line.strip()
            words = text.split(maxsplit=2)
            if words and words[0].lower() == "ref" and len(words) >= 2:
                label = f"REF:{words[1]}"
            elif words and words[0].lower() == "plant" and len(words) >= 2:
                label = f"PLANT:{words[1]} {words[2] if len(words) > 2 else ''}".strip()
            else:
                label = text or None
            print(f"  -> {node.mark(label, 'keyboard console')}", flush=True)
            rclpy.spin_once(node, timeout_sec=0.05)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
