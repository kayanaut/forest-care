"""A mission package: one folder per mission in the outbox.

    <outbox>/<mission_id>/
      header.json        config snapshot, data source (live node or bag), operator info
      gnss.jsonl         every GNSS fix                      (append-only while recording)
      odom.jsonl         wheel odometry, decimated
      odom_alt.jsonl     LiDAR/visual odometry, decimated (if configured)
      gnss_rtk.jsonl     second GNSS receiver (if configured)
      imu.jsonl          IMU, decimated
      frames.jsonl       one line per kept camera frame  ->  frames/<stamp_ns>.jpg
      detections.jsonl   detections with the frame they belong to
      marks.jsonl        operator marks with the frame shown at that moment
      lidar.jsonl        LiDAR summary (the raw scans stay in the rosbag)
      stats.json         message counts and clock checks
      mission.json       the Forest Care mission, built when recording ends
      upload.json        upload progress (which observations the server has)
      state.json         recording -> complete -> uploading -> uploaded | upload_failed | rejected | invalid

Logs are append-only and flushed per line, so a crash loses at most the last few messages.
`fc_gateway recover` rebuilds mission.json from whatever was written.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

STREAMS = ("gnss", "gnss_rtk", "odom", "odom_alt", "imu", "frames", "detections", "marks", "lidar")


def write_json_atomic(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class MissionPackage:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._handles: dict = {}
        self._last_sync = time.monotonic()

    @property
    def mission_id(self) -> str:
        return self.root.name

    @classmethod
    def create(cls, outbox: Path, mission_id: str, header: dict) -> "MissionPackage":
        root = Path(outbox).expanduser() / mission_id
        if (root / "state.json").exists():
            raise FileExistsError(f"a mission package already exists: {root}")
        (root / "frames").mkdir(parents=True, exist_ok=True)
        pkg = cls(root)
        write_json_atomic(root / "header.json", header)
        pkg.set_state("recording")
        return pkg

    # -- writing ---------------------------------------------------------------

    def append(self, stream: str, record: dict, fsync_interval_s: float = 5.0) -> None:
        f = self._handles.get(stream)
        if f is None:
            f = self._handles[stream] = open(self.root / f"{stream}.jsonl", "a", encoding="utf-8")
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
        f.flush()
        if fsync_interval_s and time.monotonic() - self._last_sync > fsync_interval_s:
            self.sync()

    def sync(self) -> None:
        for f in self._handles.values():
            os.fsync(f.fileno())
        self._last_sync = time.monotonic()

    def write_frame(self, stamp_ns: int, data: bytes, fmt: str) -> str:
        rel = f"frames/{stamp_ns}.{'png' if fmt == 'png' else 'jpg'}"
        target = self.root / rel
        if not target.exists():
            tmp = target.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, target)
        return rel

    def write_json(self, name: str, data) -> None:
        write_json_atomic(self.root / name, data)

    def close(self) -> None:
        if self._handles:
            self.sync()
        for f in self._handles.values():
            f.close()
        self._handles.clear()

    # -- reading ---------------------------------------------------------------

    def read(self, stream: str) -> list[dict]:
        path = self.root / f"{stream}.jsonl"
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                break  # a line cut off by a crash: keep everything before it
        return out

    def read_json(self, name: str, default=None):
        path = self.root / name
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default

    def frame_path(self, rel: str) -> Path:
        return self.root / rel

    # -- state -----------------------------------------------------------------

    @property
    def state(self) -> dict:
        return self.read_json("state.json", {"status": "unknown"})

    def set_state(self, status: str, **info) -> None:
        state = {**{k: v for k, v in self.state.items() if k not in ("status", "updated_at")},
                 **info, "status": status, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        write_json_atomic(self.root / "state.json", state)


def list_packages(outbox: Path) -> list[MissionPackage]:
    outbox = Path(outbox).expanduser()
    if not outbox.is_dir():
        return []
    return [MissionPackage(p) for p in sorted(outbox.iterdir()) if (p / "state.json").exists()]
