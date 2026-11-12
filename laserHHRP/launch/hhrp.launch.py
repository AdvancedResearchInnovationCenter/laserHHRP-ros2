"""Starts the laser roller nodes.

    ros2 launch laser_hhrp hhrp.launch.py                       # real hardware
    ros2 launch laser_hhrp hhrp.launch.py simulate:=true        # no hardware
    ros2 launch laser_hhrp hhrp.launch.py hardware_trigger:=true  # Nano D3 wired to laser In1
    ros2 launch laser_hhrp hhrp.launch.py scan_switch:=false      # no GPIO header (not on the Pi)
    ros2 launch laser_hhrp hhrp.launch.py ui:=false               # without the operator UI
                                                                  # (over SSH: RViz needs a display)
    ros2 launch laser_hhrp hhrp.launch.py mode:=rivets            # rivet heads instead of holes
    ros2 launch laser_hhrp hhrp.launch.py mode:=both              # holes and rivets (a panel with both)
    ros2 launch laser_hhrp hhrp.launch.py holes:=false            # without hole/rivet detection

Parameters: config/hhrp.yaml (or params_file:=...). The UI alone (e.g. on a PC): ui.launch.py.
"""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.conditions import IfCondition, UnlessCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare


def generate_launch_description():
    simulate = LaunchConfiguration("simulate")
    params = [
        LaunchConfiguration("params_file"),
        {"sync.hardware_trigger": LaunchConfiguration("hardware_trigger")},
        {"nano.port": LaunchConfiguration("nano_port")},
    ]

    def node(executable, **kwargs):
        return Node(package="laser_hhrp", executable=executable, parameters=params,
                    output="screen", **kwargs)

    return LaunchDescription([
        DeclareLaunchArgument("simulate", default_value="false",
                              description="simulate the Nano and the laser"),
        DeclareLaunchArgument("hardware_trigger", default_value="false",
                              description="true: hardware sync (Nano D3 -> laser In1); false: software sync"),
        DeclareLaunchArgument("nano_port", default_value="/dev/ttyUSB0"),
        DeclareLaunchArgument("holes", default_value="true",
                              description="also run hole/rivet detection (detectors.launch.py)"),
        DeclareLaunchArgument("mode", default_value="",
                              description="holes | rivets | both (empty: detection.mode in params_file)"),
        DeclareLaunchArgument("holes_out_dir",
                              default_value="~/aric_ws/src/laserHHRP-ros2/hole_detector/hole_detector_out",
                              description="where detection saves its scan folders"),
        DeclareLaunchArgument("scan_switch", default_value="true",
                              description="scan button + status LEDs on the Pi GPIO header"),
        DeclareLaunchArgument("ui", default_value="true",
                              description="also start RViz with the operator panel (needs a display)"),
        DeclareLaunchArgument("params_file", default_value=PathJoinSubstitution(
            [FindPackageShare("laser_hhrp"), "config", "hhrp.yaml"])),

        # layer 1: hardware interface (or the simulator in their place)
        node("nano_driver_node", condition=UnlessCondition(simulate)),
        node("laser_driver_node", condition=UnlessCondition(simulate)),
        node("simulator_node", condition=IfCondition(simulate)),
        # layer 2: acquisition, odometry
        node("acquisition_node"),
        node("odometry_node"),
        # layer 3: synchronization
        node("synchronizer_node"),
        # layers 4, 5: point cloud, inspection
        node("point_cloud_node"),
        node("inspection_node"),
        # hole / rivet detection: packages hole_detector, rivet_detector (own parameters); mode from params_file
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(PathJoinSubstitution(
                [FindPackageShare("laser_hhrp"), "launch", "detectors.launch.py"])),
            launch_arguments={"mode": LaunchConfiguration("mode"),
                              "out_dir": LaunchConfiguration("holes_out_dir"),
                              "params_file": LaunchConfiguration("params_file")}.items(),
            condition=IfCondition(LaunchConfiguration("holes"))),
        # layer 6: storage
        node("recorder_node"),
        # calibration, scan control + monitoring (layer 7 logic)
        node("calibration_node"),
        node("scan_controller_node"),
        # scan button + status LEDs (Pi GPIO)
        node("scan_switch_node.py", condition=IfCondition(LaunchConfiguration("scan_switch"))),
        # layer 7: operator UI (RViz + panel), same as ui.launch.py
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(PathJoinSubstitution(
                [FindPackageShare("laser_hhrp"), "launch", "ui.launch.py"])),
            condition=IfCondition(LaunchConfiguration("ui"))),
    ])
