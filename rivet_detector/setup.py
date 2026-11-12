from glob import glob

from setuptools import setup

setup(
    name="rivet_detector",
    version="0.1.0",
    packages=["rivet_detector"],
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/rivet_detector"]),
        ("share/rivet_detector", ["package.xml"]),
        ("share/rivet_detector/config", glob("config/*.yaml")),
        ("share/rivet_detector/launch", glob("launch/*.py")),
        ("share/rivet_detector/rviz", glob("rviz/*.rviz")),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    entry_points={"console_scripts": ["rivet_detector = rivet_detector.node:main"]},
)
