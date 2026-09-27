"""Live gateway: subscribe to the rover topics and write a mission package while driving.

    ros2 run forestcare_gateway gateway --ros-args --params-file config/gateway.yaml

Services
  ~/start_mission   std_srvs/Trigger   start a new mission (automatic at launch if auto_start)
  ~/stop_mission    std_srvs/Trigger   finish it: build mission.json, then upload if upload_on_finish
Topic
  ~/status          std_msgs/String    JSON once per second: recording?, counts, last GNSS accuracy

The node never changes what the robot does; it only listens. The rosbag (if recorded) stays
the full raw record; this package is what Forest Care needs.
"""

from __future__ import annotations

import json
import os
import threading

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from std_msgs.msg import String
from std_srvs.srv import Trigger

from ..recorder import MIN_WALL_CLOCK_NS, MissionRecorder
from ..uploader import upload_package
from .common import declare_config, hold_signals, subscribe_roles


class GatewayNode(Node):
    def __init__(self):
        super().__init__("forestcare_gateway", automatically_declare_parameters_from_overrides=True)
        if not self.has_parameter("auto_start"):
            self.declare_parameter("auto_start", True)
        self.cfg = declare_config(self)
        self.recorder: MissionRecorder | None = None
        self.waiting_to_start = False
        self.last_summary: dict | None = None
        self.last_fix_sigma: float | None = None
        skipped = subscribe_roles(self, self.cfg, self._on_message)
        # Only the simulator publishes this topic: seeing it labels the mission 'simulated'.
        from nav_msgs.msg import Odometry
        self.create_subscription(Odometry, "/sim/ground_truth", self._on_sim_truth, 1)
        self.create_service(Trigger, "~/start_mission", self._srv_start)
        self.create_service(Trigger, "~/stop_mission", self._srv_stop)
        self.status_pub = self.create_publisher(String, "~/status", 10)
        self.create_timer(1.0, self._publish_status)
        self.get_logger().info(f"outbox: {os.path.expanduser(self.cfg['outbox_dir'])}; API: {self.cfg['api_url']}"
                               + (f"; not subscribed: {', '.join(skipped)}" if skipped else ""))
        if self.get_parameter("auto_start").value:
            self.start_mission()

    # -- mission control -----------------------------------------------------------------------

    def start_mission(self) -> str:
        """Arm the recorder. The mission starts with the first message that carries a valid
        time (with `use_sim_time` the clock is 0 until `ros2 bag play --clock` starts)."""
        if self.recorder and (self.recorder.recording or self.waiting_to_start):
            return "already recording" if self.recorder.recording else "already waiting for data"
        source = {"kind": "live", "node": self.get_fully_qualified_name(), "ros_distro": os.environ.get("ROS_DISTRO", "")}
        self.recorder = MissionRecorder(self.cfg, source, log=self.get_logger().info)
        self.waiting_to_start = True
        return "mission starts with the next sensor message"

    def stop_mission(self) -> str:
        self.waiting_to_start = False
        if not (self.recorder and self.recorder.recording):
            return "not recording"
        summary = self.recorder.finish()
        self.last_summary = summary
        self.get_logger().info(f"mission finished: {json.dumps(summary)}")
        if self.cfg["upload_on_finish"] and summary["status"] == "complete":
            from ..package import MissionPackage

            pkg = MissionPackage(summary["package"])
            threading.Thread(target=upload_package, daemon=True,
                             args=(pkg, self.cfg["api_url"], self.cfg["upload_batch_size"]),
                             kwargs={"log": self.get_logger().info}).start()
        return f"{summary['mission_id']}: {summary['status']}, {summary['observations']} observation(s)"

    def _srv_start(self, _req, resp):
        resp.message = self.start_mission()
        resp.success = True
        return resp

    def _srv_stop(self, _req, resp):
        resp.message = self.stop_mission()
        resp.success = True
        return resp

    # -- data ------------------------------------------------------------------------------------

    def _on_sim_truth(self, _msg) -> None:
        if self.recorder and self.cfg["source_kind"] != "simulated":
            self.recorder.mark_simulated("/sim/ground_truth is being published (simulator)")
            self.cfg = self.recorder.cfg

    def _on_message(self, role: str, msgtype: str, msg) -> None:
        if role == "gnss":
            cov = msg.position_covariance
            self.last_fix_sigma = round(((cov[0] + cov[4]) / 2) ** 0.5, 2) if msg.position_covariance_type else None
        now = self.get_clock().now().nanoseconds
        if self.recorder and self.waiting_to_start:
            header = getattr(msg, "header", None)
            stamp = header.stamp.sec * 1_000_000_000 + header.stamp.nanosec if header is not None else now
            if stamp < MIN_WALL_CLOCK_NS:
                return                       # no valid time yet
            self.recorder.start(stamp)
            self.waiting_to_start = False
        if self.recorder and self.recorder.recording:
            self.recorder.handle(role, msgtype, msg, now)

    def _publish_status(self) -> None:
        rec = self.recorder
        status = {"recording": bool(rec and rec.recording), "waiting_for_data": self.waiting_to_start,
                  "mission_id": rec.pkg.mission_id if rec and rec.recording else None,
                  "counts": dict(rec.counts) if rec else {}, "gnss_sigma_m": self.last_fix_sigma,
                  "last_mission": self.last_summary}
        self.status_pub.publish(String(data=json.dumps(status)))


def main(args=None) -> None:
    rclpy.init(args=args)
    node = GatewayNode()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        # Finish the mission on Ctrl-C / launch shutdown so nothing recorded is lost. The upload
        # may be cut short by the shutdown; `fc_gateway upload` resumes it later.
        hold_signals()
        if node.recorder and node.recorder.recording:
            summary = node.recorder.finish()
            print(f"[forestcare_gateway] mission finished on shutdown: {json.dumps(summary)}", flush=True)
            if node.cfg["upload_on_finish"] and summary["status"] == "complete":
                from ..package import MissionPackage

                upload_package(MissionPackage(summary["package"]), node.cfg["api_url"], node.cfg["upload_batch_size"])
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
