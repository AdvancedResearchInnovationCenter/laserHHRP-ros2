"""Operator UI: RViz 2 with the laser roller panel (config/hhrp.rviz).

    ros2 launch laser_hhrp ui.launch.py
    ros2 launch laser_hhrp ui.launch.py displays_file:=/path/to/my_displays.yaml

Which displays and marker layers start shown: config/rviz_displays.yaml.
"""

import os
import tempfile

import yaml
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, LogInfo, OpaqueFunction
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare


def read_switches(path):
    """{display name: bool} and {marker namespace: bool} from the displays file."""
    with open(path) as handle:
        switches = yaml.safe_load(handle) or {}
    shown, layers = switches.get("displays") or {}, switches.get("layers") or {}
    for part in (shown, layers):
        if not isinstance(part, dict) or not all(isinstance(v, bool) for v in part.values()):
            raise ValueError("'displays' and 'layers' must map names to true/false")
    return shown, layers


def with_switches(context):
    """hhrp.rviz with each display's Enabled and each marker layer set from the displays file.
    A bad displays file only gives a warning: RViz then starts with hhrp.rviz as it is."""
    rviz = os.path.join(FindPackageShare("laser_hhrp").perform(context), "config", "hhrp.rviz")
    displays_file = LaunchConfiguration("displays_file").perform(context)
    try:
        shown, layers = read_switches(displays_file)
    except (OSError, yaml.YAMLError, AttributeError, ValueError) as error:
        return [LogInfo(msg="rviz_displays: %s unusable (%s); using hhrp.rviz unchanged" % (displays_file, error)),
                Node(package="rviz2", executable="rviz2", arguments=["-d", rviz], output="screen")]
    with open(rviz) as handle:
        config = yaml.safe_load(handle)
    actions, found = [], set()
    for display in config["Visualization Manager"]["Displays"]:
        if display.get("Name") in shown:
            display["Enabled"] = display["Value"] = shown[display["Name"]]
            found.add(display["Name"])
        if display.get("Class") == "rviz_default_plugins/MarkerArray" and layers:
            display["Namespaces"] = dict(layers)
    for name in sorted(set(shown) - found):
        actions.append(LogInfo(msg="rviz_displays: no display named '%s' in hhrp.rviz (ignored)" % name))
    handle, path = tempfile.mkstemp(prefix="hhrp_ui_", suffix=".rviz")   # private (0600), one per launch
    with os.fdopen(handle, "w") as out:
        yaml.safe_dump(config, out, sort_keys=False)
    return actions + [Node(package="rviz2", executable="rviz2", arguments=["-d", path], output="screen")]


def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument("displays_file", default_value=PathJoinSubstitution(
            [FindPackageShare("laser_hhrp"), "config", "rviz_displays.yaml"]),
            description="which RViz displays and marker layers start shown"),
        OpaqueFunction(function=with_switches),
    ])
