from glob import glob

from setuptools import setup

setup(
    name="hole_detector",
    version="0.1.0",
    packages=["hole_detector", "hole_detector.rivets"],
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/hole_detector"]),
        ("share/hole_detector", ["package.xml"]),
        ("share/hole_detector/config", glob("config/*.yaml")),
        ("share/hole_detector/launch", glob("launch/*.py")),
        ("share/hole_detector/rviz", glob("rviz/*.rviz")),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    entry_points={"console_scripts": ["hole_detector = hole_detector.node:main",
                                    "rivet_detector = hole_detector.rivets.node:main"]},
)
