"""Hole / rivet detection without the laser hardware (also included by hhrp.launch.py).

    ros2 launch laser_hhrp detectors.launch.py [mode:=holes|rivets|both] [rviz:=true] [out_dir:=/path]
    ros2 bag play <bag>          # with /scan/frame

`mode` (default: detection.mode in config/hhrp.yaml) picks the detectors: holes (hole_detector), rivets
(rivet_detector) or both. Each is its own package with its own config/params.yaml.
"""

from pathlib import Path

import yaml
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

MODES = {"holes": ["hole_detector"], "rivets": ["rivet_detector"], "both": ["hole_detector", "rivet_detector"]}


def file_mode(params_file):
    """detection.ros__parameters.mode from the params file; holes when the key is missing."""
    with open(params_file) as handle:
        config = yaml.safe_load(handle) or {}
    section = config.get("detection") or {}
    return str((section.get("ros__parameters") or {}).get("mode", "holes"))


def detectors(context):
    params_file = LaunchConfiguration("params_file").perform(context) or str(
        Path(get_package_share_directory("laser_hhrp")) / "config" / "hhrp.yaml")
    mode = LaunchConfiguration("mode").perform(context).strip() or file_mode(params_file)
    if mode not in MODES:
        raise RuntimeError("mode must be one of %s, got %r (launch argument or detection.mode in %s)" % (
            ", ".join(MODES), mode, params_file))
    out_dir = LaunchConfiguration("out_dir").perform(context)
    actions = []
    for package in MODES[mode]:
        params = [str(Path(get_package_share_directory(package)) / "config" / "params.yaml")]
        if out_dir:
            params.append({"out_dir": out_dir})
        actions.append(Node(package=package, executable=package, output="screen", parameters=params))
    if LaunchConfiguration("rviz").perform(context).lower() in ("true", "1", "yes"):
        rviz = {"holes": Path(get_package_share_directory("hole_detector")) / "rviz" / "holes.rviz",
                "rivets": Path(get_package_share_directory("rivet_detector")) / "rviz" / "rivets.rviz",
                "both": Path(get_package_share_directory("laser_hhrp")) / "config" / "detectors_both.rviz"}[mode]
        actions.append(Node(package="rviz2", executable="rviz2", arguments=["-d", str(rviz)]))
    return actions


def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument("mode", default_value="",
                              description="holes | rivets | both (empty: detection.mode in params_file)"),
        DeclareLaunchArgument("rviz", default_value="false", description="also start RViz"),
        DeclareLaunchArgument("out_dir", default_value="",
                              description="where the result folders go (empty: each package's out_dir)"),
        DeclareLaunchArgument("params_file", default_value="",
                              description="file holding detection.mode (empty: laser_hhrp config/hhrp.yaml)"),
        OpaqueFunction(function=detectors),
    ])
