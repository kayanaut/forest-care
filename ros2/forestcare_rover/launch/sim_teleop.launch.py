"""Practise a field mission without hardware: the simulated rover (same topics as the real one)
driven with a gamepad, mark buttons, and optionally the live gateway.

    ros2 launch forestcare_rover sim_teleop.launch.py                 # gamepad
    ros2 launch forestcare_rover sim_teleop.launch.py teleop:=none    # then, in other terminals:
        ros2 run teleop_twist_keyboard teleop_twist_keyboard
        ros2 run forestcare_gateway mark_console
    ros2 run forestcare_rover record_mission.sh --site "sim practice" --operator "Me"   # record it

Everything recorded from the simulated rover is labelled 'simulated' by the gateway (sim.yaml).
"""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

from forestcare_rover.launch_common import share, teleop_nodes


def _setup(context):
    get = lambda name: LaunchConfiguration(name).perform(context)  # noqa: E731
    actions = [Node(package="forestcare_gateway", executable="sim_rover", name="sim_rover", output="screen",
                    parameters=[{"world": get("world"), "rtk": get("rtk").lower() == "true",
                                 "lidar_odom": get("lidar_odom").lower() == "true"}])]
    if get("teleop") == "joy":
        actions += teleop_nodes(share("forestcare_rover", "config", "teleop_joy.yaml"))
    if get("gateway").lower() == "true":
        actions.append(Node(package="forestcare_gateway", executable="gateway", name="forestcare_gateway",
                            output="screen", parameters=[share("forestcare_gateway", "config", "sim.yaml"),
                                                         {"api_url": get("api_url"), "outbox_dir": get("outbox_dir")}]))
    return actions


def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument("world", default_value="survey", description="'survey' or 'loc'"),
        DeclareLaunchArgument("teleop", default_value="joy", description="'joy' or 'none'"),
        DeclareLaunchArgument("gateway", default_value="false"),
        DeclareLaunchArgument("rtk", default_value="false"),
        DeclareLaunchArgument("lidar_odom", default_value="false"),
        DeclareLaunchArgument("api_url", default_value="http://127.0.0.1:8000"),
        DeclareLaunchArgument("outbox_dir", default_value="~/forestcare_outbox"),
        OpaqueFunction(function=_setup),
    ])
