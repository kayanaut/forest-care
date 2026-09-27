"""A simulated rover that looks like the real one from the ROS side.

    ros2 run forestcare_gateway sim_rover --ros-args -p world:=survey
    ros2 run teleop_twist_keyboard teleop_twist_keyboard          # drive it

It listens to /cmd_vel (geometry_msgs/Twist) and publishes what the rover's drivers would:
/gnss/fix, /odom (wheel odometry), /imu/data, /camera/image_raw/compressed, /scan, /tf, /tf_static,
optionally /gnss_rtk/fix (rtk:=true), /odom_lidar (lidar_odom:=true) and mock detections
(detections:=true, needs vision_msgs). With autodrive:=true it follows the demo route like the
scripted operator and publishes the marks itself.

Also publishes /sim/ground_truth (nav_msgs/Odometry, metres east/north of the world origin) so a
recorded session can be evaluated. A real rover has no such topic. Everything is simulated.
"""

from __future__ import annotations

import json

import numpy as np
import rclpy
from geometry_msgs.msg import Twist
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile
from std_msgs.msg import String

from ..messages import NS
from ..sim import TOPICS, ScriptedOperator, SimRover, make_world
from .common import fill, hold_signals, message_class


class SimRoverNode(Node):
    def __init__(self):
        super().__init__("sim_rover")
        p = {name: self.declare_parameter(name, default).value for name, default in {
            "world": "survey", "seed": 1, "run": 0, "rtk": False, "lidar_odom": False, "detections": False,
            "autodrive": False, "max_speed": 1.0}.items()}
        self.params = p
        detections = p["detections"]
        if detections:
            try:
                message_class("vision_msgs/msg/Detection2DArray")
            except ImportError:
                self.get_logger().warning("vision_msgs is not installed: mock detections disabled")
                detections = False
        world = make_world(p["world"])
        now = self.get_clock().now().nanoseconds
        self.rover = SimRover(world, p["seed"] * 1000 + p["run"], now, rtk=p["rtk"], lidar_odom=p["lidar_odom"],
                              detections=detections, caption=f"{p['world']} live")
        self.operator = (ScriptedOperator(world, np.random.default_rng(p["seed"] * 1000 + p["run"] + 1), p["run"],
                                          tag_marks=p["world"] == "loc") if p["autodrive"] else None)
        self.cmd, self.cmd_ns = (0.0, 0.0), 0
        self.create_subscription(Twist, TOPICS["cmd_vel"], self._on_cmd, 10)
        self.pubs: dict = {}
        self.mark_pub = self.create_publisher(String, TOPICS["marks"], 50)
        self.last_ns: int | None = None
        self.dt = 0.02
        self.create_timer(self.dt, self._tick)
        self.get_logger().info(f"simulated rover in world '{p['world']}' at {world.origin} "
                               f"({'autodrive' if self.operator else 'waiting for /cmd_vel'})")

    def _on_cmd(self, msg: Twist) -> None:
        v = max(-self.params["max_speed"], min(self.params["max_speed"], msg.linear.x))
        self.cmd, self.cmd_ns = (v, msg.angular.z), self.get_clock().now().nanoseconds

    def _publisher(self, topic: str, msgtype: str):
        if topic not in self.pubs:
            qos = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL) if topic == "/tf_static" else 10
            self.pubs[topic] = (self.create_publisher(message_class(msgtype), topic, qos), message_class(msgtype))
        return self.pubs[topic]

    def _tick(self) -> None:
        now = self.get_clock().now().nanoseconds
        dt = self.dt if self.last_ns is None else min(max((now - self.last_ns) / NS, 0.001), 0.1)
        self.last_ns = now
        self.rover.t_ns = now - int(dt * NS)
        if self.operator:
            v, w, mark = self.operator.command(self.rover)
            if mark:
                self.mark_pub.publish(String(data=mark))
            if self.operator.done:
                v = w = 0.0
        else:
            fresh = now - self.cmd_ns < 0.5 * NS           # stop if teleop goes quiet
            v, w = self.cmd if fresh else (0.0, 0.0)
        for topic, msgtype, _stamp, _arrival, data in self.rover.step(dt, v, w):
            if topic == TOPICS["cmd_vel"]:
                continue                                     # the teleop node publishes the real one
            pub, cls = self._publisher(topic, msgtype)
            pub.publish(fill(cls(), data))


def main(args=None) -> None:
    rclpy.init(args=args)
    node = SimRoverNode()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        hold_signals()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
