from glob import glob

from setuptools import setup

package_name = "forestcare_rover"

setup(
    name=package_name,
    version="0.1.0",
    packages=[package_name],
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
        ("share/" + package_name + "/config", glob("config/*")),
        ("share/" + package_name + "/launch", glob("launch/*.launch.py")),
        ("lib/" + package_name, ["scripts/record_mission.sh"]),   # `ros2 run forestcare_rover record_mission.sh`
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Forest Care Bonn",
    maintainer_email="maintainer@example.org",
    description="Rover bringup for Forest Care field data collection.",
    license="TODO",
)
