from setuptools import setup

setup(
    name="laser_hhrp_ui",
    version="0.1.0",
    packages=["laser_hhrp_ui"],
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/laser_hhrp_ui"]),
        ("share/laser_hhrp_ui", ["package.xml"]),
    ],
    install_requires=["setuptools"],
    tests_require=["pytest"],
    zip_safe=True,
    entry_points={"console_scripts": ["server = laser_hhrp_ui.server:main"]},
)
