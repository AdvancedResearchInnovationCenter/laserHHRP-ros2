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
    assert module.file_mode(ROOT / "config" / "hhrp.yaml") == "holes"
    assert set(module.MODES) == set(EXPECTED)
    assert nodes(module) == EXPECTED["holes"]                       # empty mode: hhrp.yaml
    for mode, expected in EXPECTED.items():
        assert nodes(module, mode=mode) == expected, mode
    try:
        nodes(module, mode="slots")
    except RuntimeError:
        pass
    else:
        raise AssertionError("an unknown mode must fail")


if __name__ == "__main__":
    test_modes()
    print("all checks passed")
