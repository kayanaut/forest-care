from glob import glob

from setuptools import find_packages, setup

package_name = "forestcare_gateway"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
        ("share/" + package_name + "/config", glob("config/*.yaml")),
        ("share/" + package_name + "/launch", glob("launch/*.launch.py")),
    ],
    install_requires=["setuptools"],
    # Offline tools (bag conversion, demo bags, localization analysis) run without ROS.
    extras_require={"offline": ["rosbags>=0.10", "pyyaml", "numpy", "pillow"]},
    zip_safe=True,
    maintainer="Forest Care Bonn",
    maintainer_email="maintainer@example.org",
    description="Records rover data into Forest Care missions and uploads them.",
    license="TODO",
    entry_points={
        "console_scripts": [
            "gateway = forestcare_gateway.nodes.gateway_node:main",
            "mark = forestcare_gateway.nodes.mark_node:main",
            "mark_console = forestcare_gateway.nodes.mark_node:console",
            "sim_rover = forestcare_gateway.nodes.sim_rover_node:main",
            "preflight = forestcare_gateway.nodes.preflight_node:main",
            "fc_gateway = forestcare_gateway.cli:main",
        ],
    },
)
