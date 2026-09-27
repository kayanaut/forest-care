"""Real ROS 2 (Kilted): recorded data played with `ros2 bag play` into the live gateway node, which
uploads to Forest Care. Skipped when ROS 2 is not installed.

    uv run pytest -m ros
"""

from __future__ import annotations

import json
import os
import random
import subprocess
from pathlib import Path

import httpx
import pytest

from forestcare_gateway.bagio import convert_bag
from forestcare_gateway.demo import demo_config, write_demo_bag

ROOT = Path(__file__).resolve().parents[1]
ROS_SETUP = Path(os.environ.get("FORESTCARE_ROS_SETUP", "/opt/ros/kilted/setup.bash"))
pytestmark = [pytest.mark.ros, pytest.mark.skipif(not ROS_SETUP.exists(), reason=f"{ROS_SETUP} not found")]


def ros_env() -> dict:
    """ROS 2 needs the system Python it was built for, not this project's virtual environment."""
    env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME")}
    venv_bin = str(ROOT / ".venv" / "bin")
    env["PATH"] = ":".join(["/usr/bin", *(p for p in env.get("PATH", "").split(":") if p and p != venv_bin)])
    env["ROS_DOMAIN_ID"] = str(random.randint(60, 99))   # keep test traffic away from other ROS 2 processes
    return env


def ros_bash(script: str, timeout: float = 240) -> subprocess.CompletedProcess:
    setup = f"source {ROS_SETUP} && source {ROOT / 'ros2' / 'install' / 'setup.bash'}"
    return subprocess.run(["bash", "-c", f"{setup} && {script}"], capture_output=True, text=True,
                          timeout=timeout, env=ros_env())


@pytest.fixture(scope="session")
def ros_build():
    r = subprocess.run(["bash", "-c", f"source {ROS_SETUP} && cd {ROOT / 'ros2'} && colcon build --symlink-install"],
                       capture_output=True, text=True, timeout=600, env=ros_env())
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]


def gateway_params(path: Path, outbox: Path, api_url: str) -> Path:
    path.write_text(json.dumps({"forestcare_gateway": {"ros__parameters": {
        "robot_id": "sim-rover", "source_kind": "simulated", "outbox_dir": str(outbox), "api_url": api_url,
        "simulator": {"name": "forestcare_gateway.sim", "version": "0.1.0"}}}}))   # JSON is valid YAML
    return path


def test_bag_play_through_the_live_gateway(ros_build, tmp_path, empty_server_url):
    bag = tmp_path / "bag"
    write_demo_bag(bag, "survey", seed=21, detections=False, max_duration_s=110)   # Kilted has no vision_msgs
    params = gateway_params(tmp_path / "params.yaml", tmp_path / "live_outbox", empty_server_url)
    log = tmp_path / "gateway.log"
    result = ros_bash(f"""
        set -m     # job control: background jobs keep Ctrl-C (SIGINT) instead of ignoring it
        ros2 launch forestcare_gateway gateway.launch.py params:={params} use_sim_time:=true > {log} 2>&1 &
        LAUNCH=$!
        trap 'kill -INT -$LAUNCH 2>/dev/null' EXIT
        sleep 4
        timeout 90 ros2 bag play {bag} --clock --rate 10 > /dev/null 2>&1
        sleep 1
        timeout 30 ros2 service call /forestcare_gateway/stop_mission std_srvs/srv/Trigger
        sleep 3
        kill -INT -$LAUNCH
        timeout 20 tail --pid=$LAUNCH -f /dev/null
    """)
    text = log.read_text()
    assert "uploaded" in text, result.stdout[-2000:] + text[-3000:]

    live = httpx.get(f"{empty_server_url}/api/missions").json()
    assert len(live) == 1 and live[0]["provenance"]["metadata"]["source"]["kind"] == "live"
    live_obs = httpx.get(f"{empty_server_url}/api/observations").json()
    assert live_obs and {o["kind"] for o in live_obs} == {"mark"}

    # The same bag converted offline gives the same mission and the same observation ids,
    # so uploading both never duplicates anything.
    offline = convert_bag(bag, demo_config(outbox_dir=str(tmp_path / "offline_outbox")), log=lambda *_: None)
    offline_mission = json.loads((Path(offline["package"]) / "mission.json").read_text())
    assert offline["mission_id"] == live[0]["id"]
    assert sorted(o["uid"] for o in offline_mission["observations"]) == sorted(o["uid"] for o in live_obs)


def test_teleoperated_field_mission(ros_build, tmp_path, empty_server_url):
    """Milestone 2 workflow on the simulated rover: teleop commands on /cmd_vel, operator marks from
    the console, record_mission.sh (preflight + rosbag2), then convert and upload without ROS."""
    missions = tmp_path / "missions"
    result = ros_bash(f"""
        set -m
        ros2 launch forestcare_rover sim_teleop.launch.py teleop:=none > {tmp_path}/sim.log 2>&1 &
        SIM=$!
        trap 'kill -INT -$SIM 2>/dev/null' EXIT
        sleep 4
        ros2 run forestcare_rover record_mission.sh --site "Test route" --operator "Test Operator" \\
            --out {missions} > {tmp_path}/record.log 2>&1 &
        REC=$!
        sleep 12
        ros2 topic pub -r 10 /cmd_vel geometry_msgs/msg/Twist "{{linear: {{x: 0.8}}}}" > /dev/null 2>&1 &
        TELEOP=$!
        ( sleep 5; echo "Prunus serotina?"; sleep 4; echo "ref R1"; sleep 3 ) | \\
            ros2 run forestcare_gateway mark_console > {tmp_path}/marks.log 2>&1
        sleep 2
        kill -INT -$TELEOP
        kill -INT -$REC; timeout 30 tail --pid=$REC -f /dev/null
    """, timeout=180)
    record_log = (tmp_path / "record.log").read_text()
    assert "=> ready" in record_log and "Mission folder" in record_log, result.stdout[-2000:] + record_log[-3000:]
    (mission,) = list(missions.iterdir())
    assert {"bag", "config", "mission_info.yaml", "preflight.txt"} <= {p.name for p in mission.iterdir()}
    assert "recorder_exit_code: 0" in (mission / "mission_info.yaml").read_text()

    from forestcare_gateway.config import load_config
    from forestcare_gateway.health import inspect_bag
    from forestcare_gateway.package import MissionPackage
    from forestcare_gateway.uploader import upload_package

    cfg = load_config(mission / "config" / "gateway.yaml", {"outbox_dir": str(tmp_path / "outbox")})
    report = inspect_bag(mission / "bag", cfg)
    assert report["ok"], report["text"]
    converted = convert_bag(mission / "bag", cfg, log=lambda *_: None)
    assert converted["status"] == "complete" and converted["observations"] == 1 and converted["references"] == 1
    pkg = MissionPackage(converted["package"])
    assert upload_package(pkg, empty_server_url, log=lambda *_: None)["status"] == "uploaded"
    (uploaded,) = httpx.get(f"{empty_server_url}/api/missions").json()
    assert uploaded["source_kind"] == "simulated"              # detected from /sim/ground_truth
    assert uploaded["provenance"]["metadata"]["operator_info"]["operator"] == "Test Operator"
    assert sum(len(s) for s in uploaded["track"]) >= 5           # the rover really drove
