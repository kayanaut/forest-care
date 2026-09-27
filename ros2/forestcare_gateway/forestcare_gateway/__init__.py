"""Forest Care ROS 2 gateway: rover data in, Forest Care missions out.

Only `nodes/` imports ROS (rclpy). Everything else is plain Python so the same code
converts recorded bags on a laptop without ROS, and can be tested without a robot.
"""

VERSION = "0.1.0"
