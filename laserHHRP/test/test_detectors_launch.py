"""detectors.launch.py: `mode` (launch argument, else detection.mode in hhrp.yaml) picks the detector nodes.

Run in the ROS container with hole_detector and rivet_detector built:
    python3 laserHHRP/test/test_detectors_launch.py
"""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {"holes": [("hole_detector", "hole_detector")], "rivets": [("rivet_detector", "rivet_detector")],
            "both": [("hole_detector", "hole_detector"), ("rivet_detector", "rivet_detector")]}


def load():
    spec = importlib.util.spec_from_file_location("detectors_launch", ROOT / "launch" / "detectors.launch.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def nodes(module, **arguments):
    """(package, executable) of each detector node started with these launch arguments."""
    from launch import LaunchContext
    from launch.actions import OpaqueFunction
    context = LaunchContext()
    context.launch_configurations.update(dict({"params_file": str(ROOT / "config" / "hhrp.yaml")}, **arguments))
    found = []
    for action in module.generate_launch_description().entities:
        if isinstance(action, OpaqueFunction):
            found += action.execute(context) or []
        else:
            action.visit(context)
    return [(n.node_package, n.node_executable) for n in found if n.node_package != "rviz2"]


def test_modes():
    module = load()
    assert module.file_mode(ROOT / "config" / "hhrp.yaml") == "both"
    assert set(module.MODES) == set(EXPECTED)
    assert nodes(module) == EXPECTED["both"]                        # empty mode: hhrp.yaml
    for mode, expected in EXPECTED.items():
        assert nodes(module, mode=mode) == expected, mode
    try:
        nodes(module, mode="slots")
    except RuntimeError:
        pass
    else:
        raise AssertionError("an unknown mode must fail")


def test_network():
    """hhrp.launch.py sets the discovery range before any node: LOCALHOST unless network:=true."""
    from launch import LaunchContext
    from launch.actions import IncludeLaunchDescription, SetEnvironmentVariable
    from launch_ros.actions import Node
    spec = importlib.util.spec_from_file_location("hhrp_launch", ROOT / "launch" / "hhrp.launch.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    entities = module.generate_launch_description().entities
    first = min(i for i, e in enumerate(entities) if isinstance(e, (Node, IncludeLaunchDescription)))
    env = [e for e in entities[:first] if isinstance(e, SetEnvironmentVariable)]
    assert len(env) == 1
    for network, expected in (("false", "LOCALHOST"), ("true", "SUBNET"), ("True", "SUBNET")):
        context = LaunchContext()
        context.launch_configurations["network"] = network
        env[0].execute(context)
        assert context.environment["ROS_AUTOMATIC_DISCOVERY_RANGE"] == expected, network


def test_operator_rviz_light():
    """The operator UI draws the cut-outs from the latched 0.3 mm views and keeps nothing for long."""
    import yaml
    def walk(node):
        if isinstance(node, dict):
            if "Class" in node and "Name" in node:
                yield node
            for value in node.values():
                yield from walk(value)
        elif isinstance(node, list):
            for value in node:
                yield from walk(value)
    displays = {d["Name"]: d for d in walk(yaml.safe_load((ROOT / "config" / "hhrp.rviz").read_text()))}
    for name, topic in (("Boundary", "/holes/boundary_view"), ("Rivet boundary", "/rivets/boundary_view")):
        d = displays[name]
        assert d["Topic"]["Value"] == topic, name
        assert d["Topic"]["Durability Policy"] == "Transient Local" and d["Topic"]["Reliability Policy"] == "Reliable"
        assert d["Decay Time"] == 0, name
    assert max(d.get("Decay Time", 0) for d in displays.values()) <= 600
    shown = yaml.safe_load((ROOT / "config" / "rviz_displays.yaml").read_text())["displays"]
    assert not shown["3D scan"] and not shown["Hole surface"] and not shown["Rivet surface"]
    assert shown["Boundary"] and shown["Rivet boundary"] and shown["Holes"] and shown["Rivets"]


if __name__ == "__main__":
    test_modes()
    test_network()
    test_operator_rviz_light()
    print("all checks passed")
