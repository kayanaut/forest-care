"""Run the live gateway.

    ros2 launch forestcare_gateway gateway.launch.py params:=/path/to/gateway.yaml
    ros2 launch forestcare_gateway gateway.launch.py use_sim_time:=true   # with `ros2 bag play --clock`
"""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare


def generate_launch_description():
    default_params = PathJoinSubstitution([FindPackageShare("forestcare_gateway"), "config", "gateway.yaml"])
    return LaunchDescription([
        DeclareLaunchArgument("params", default_value=default_params, description="gateway parameter file"),
        DeclareLaunchArgument("use_sim_time", default_value="false"),
        Node(package="forestcare_gateway", executable="gateway", name="forestcare_gateway", output="screen",
             parameters=[LaunchConfiguration("params"), {"use_sim_time": LaunchConfiguration("use_sim_time")}]),
    ])
