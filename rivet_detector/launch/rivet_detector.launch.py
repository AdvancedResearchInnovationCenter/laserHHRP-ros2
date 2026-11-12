"""ros2 launch rivet_detector rivet_detector.launch.py [rviz:=true] [out_dir:=/path/for/results]

Starts the rivet detector alone with config/params.yaml. To pick holes, rivets or both, use
`ros2 launch laser_hhrp detectors.launch.py` (mode from laser_hhrp's hhrp.yaml).
"""

from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def detector(context):
    """The node with config/params.yaml (a non-empty out_dir argument overrides the file's), and RViz."""
    share = Path(get_package_share_directory("rivet_detector"))
    params = [str(share / "config" / "params.yaml")]
    out_dir = LaunchConfiguration("out_dir").perform(context)
    if out_dir:
        params.append({"out_dir": out_dir})
    actions = [Node(package="rivet_detector", executable="rivet_detector", output="screen", parameters=params)]
    if LaunchConfiguration("rviz").perform(context).lower() in ("true", "1", "yes"):
        actions.append(Node(package="rviz2", executable="rviz2", arguments=["-d", str(share / "rviz" / "rivets.rviz")]))
    return actions


def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument("rviz", default_value="false", description="also start RViz"),
        DeclareLaunchArgument("out_dir", default_value="",
                              description="where the result folders go (empty: out_dir in config/params.yaml)"),
        OpaqueFunction(function=detector),
    ])
