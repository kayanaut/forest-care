"""The real rover: sensor drivers, sensor mounts, gamepad teleop with deadman, mark buttons and
(optionally) the live Forest Care gateway. Record with `record_mission.sh` in a second terminal.

    ros2 launch forestcare_rover rover.launch.py
    ros2 launch forestcare_rover rover.launch.py drivers:=/path/my_drivers.yaml gateway:=true

Arguments
  drivers         YAML list of driver nodes (config/drivers.yaml; empty by default)
  mounts          sensor positions on the rover (config/mounts.yaml)
  teleop          'joy' (gamepad) or 'none' (use teleop_twist_keyboard + mark_console in terminals)
  gateway         'true' to run the live gateway as well (the bag is always the full record)
  gateway_params  gateway settings (config/forestcare_gateway.yaml)
"""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

from forestcare_rover.launch_common import driver_nodes, mount_nodes, share, teleop_nodes


def _setup(context):
    get = lambda name: LaunchConfiguration(name).perform(context)  # noqa: E731
    actions = driver_nodes(get("drivers")) + mount_nodes(get("mounts"))
    if get("teleop") == "joy":
        actions += teleop_nodes(share("forestcare_rover", "config", "teleop_joy.yaml"))
    if get("gateway").lower() == "true":
        actions.append(Node(package="forestcare_gateway", executable="gateway", name="forestcare_gateway",
                            parameters=[get("gateway_params")], output="screen"))
    return actions


def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument("drivers", default_value=share("forestcare_rover", "config", "drivers.yaml")),
        DeclareLaunchArgument("mounts", default_value=share("forestcare_rover", "config", "mounts.yaml")),
        DeclareLaunchArgument("teleop", default_value="joy"),
        DeclareLaunchArgument("gateway", default_value="false"),
        DeclareLaunchArgument("gateway_params", default_value=share("forestcare_rover", "config", "forestcare_gateway.yaml")),
        OpaqueFunction(function=_setup),
    ])
