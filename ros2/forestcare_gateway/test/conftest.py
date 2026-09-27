"""Gateway tests: plain Python, no ROS and no Forest Care backend needed."""

from __future__ import annotations

import pytest

from forestcare_gateway.config import DEFAULTS, merge


@pytest.fixture
def cfg(tmp_path):
    return merge(DEFAULTS, {"outbox_dir": str(tmp_path / "outbox"), "robot_id": "test-rover",
                            "source_kind": "simulated", "simulator": {"name": "pytest"},
                            "recording": {"fsync_interval_s": 0}})


@pytest.fixture
def recorder(cfg):
    from forestcare_gateway.recorder import MissionRecorder

    return MissionRecorder(cfg, {"kind": "test"}, log=lambda *_: None)
