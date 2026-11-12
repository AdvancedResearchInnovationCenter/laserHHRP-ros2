"""ros2 launch hole_detector hole_detector.launch.py [rviz:=true] [out_dir:=/path/for/results] [mode:=holes|rivets|both]

`mode` (default: `detector_mode.mode` in config/params.yaml) picks the detector: holes (hole_detector),
rivets (rivet_detector) or both (a panel with holes and rivets), with its RViz layout (both: the holes layout). Both nodes read config/params.yaml; each takes its own section.
"""

from pathlib import Path

import yaml
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

MODES = {"holes": (["hole_detector"], "holes.rviz"), "rivets": (["rivet_detector"], "rivets.rviz"),
         "both": (["hole_detector", "rivet_detector"], "holes.rviz")}


def file_mode(params_file):
    """detector_mode.ros__parameters.mode from the params file; holes when the key is missing."""
    with open(params_file) as handle:
        config = yaml.safe_load(handle) or {}
    section = config.get("detector_mode") or {}
    return str((section.get("ros__parameters") or {}).get("mode", "holes"))


def detector(context):
    """The chosen node with config/params.yaml (a non-empty out_dir argument overrides the file's), and RViz."""
    share = Path(get_package_share_directory("hole_detector"))
    params_file = str(share / "config" / "params.yaml")
    mode = LaunchConfiguration("mode").perform(context).strip() or file_mode(params_file)
    if mode not in MODES:
        raise RuntimeError("mode must be one of %s, got %r (launch argument or detector_mode in %s)" % (
            ", ".join(MODES), mode, params_file))
    executables, rviz_file = MODES[mode]
    params = [params_file]
    out_dir = LaunchConfiguration("out_dir").perform(context)
    if out_dir:
        params.append({"out_dir": out_dir})
    actions = [Node(package="hole_detector", executable=e, output="screen", parameters=params) for e in executables]
    if LaunchConfiguration("rviz").perform(context).lower() in ("true", "1", "yes"):
        actions.append(Node(package="rviz2", executable="rviz2", arguments=["-d", str(share / "rviz" / rviz_file)]))
    return actions


def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument("rviz", default_value="false", description="also start RViz"),
        DeclareLaunchArgument("out_dir", default_value="",
                              description="where the result folders go (empty: out_dir in config/params.yaml)"),
        DeclareLaunchArgument("mode", default_value="",
                              description="holes | rivets | both (empty: detector_mode in config/params.yaml)"),
        OpaqueFunction(function=detector),
    ])
