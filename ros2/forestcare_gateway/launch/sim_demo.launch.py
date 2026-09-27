"""No-hardware demo: a simulated rover drives the demo route by itself, the gateway records it
and uploads the mission to Forest Care when you stop the launch (Ctrl-C).

    ros2 launch forestcare_gateway sim_demo.launch.py api_url:=http://127.0.0.1:8000
"""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare


def generate_launch_description():
    sim_params = PathJoinSubstitution([FindPackageShare("forestcare_gateway"), "config", "sim.yaml"])
    return LaunchDescription([
        DeclareLaunchArgument("world", default_value="survey"),
        DeclareLaunchArgument("autodrive", default_value="true"),
        DeclareLaunchArgument("api_url", default_value="http://127.0.0.1:8000"),
        DeclareLaunchArgument("outbox_dir", default_value="~/forestcare_outbox"),
        Node(package="forestcare_gateway", executable="sim_rover", name="sim_rover", output="screen",
             parameters=[{"world": LaunchConfiguration("world"), "autodrive": LaunchConfiguration("autodrive")}]),
        Node(package="forestcare_gateway", executable="gateway", name="forestcare_gateway", output="screen",
             parameters=[sim_params, {"api_url": LaunchConfiguration("api_url"),
                                      "outbox_dir": LaunchConfiguration("outbox_dir")}]),
    ])
