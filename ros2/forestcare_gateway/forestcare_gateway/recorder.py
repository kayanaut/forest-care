"""MissionRecorder: turns a time-ordered stream of ROS messages into a mission package.

The live node (nodes/gateway_node.py) and bag conversion (bagio.py) both call
`handle(role, msgtype, msg, recv_ns)`, so a mission recorded live and the same mission
converted from its rosbag give the same package.

What is kept (the rosbag, if recorded, keeps everything):
  - every GNSS fix; odometry, IMU and LiDAR summaries at a reduced rate;
  - camera frames only when an observation needs them (or every `camera.keep_every_s`);
  - detections, and operator marks resolved to the frame the operator was looking at.
"""

from __future__ import annotations

import hashlib
import statistics
import time
from collections import Counter, deque
from dataclasses import asdict

from . import VERSION
from .config import outbox_path
from .messages import (NS, Frame, Mark, detections_from_msg, frame_from_compressed, frame_from_raw,
                       gnss_from_msg, imu_from_msg, lidar_from_msg, mark_from_text, odom_from_msg)
from .package import MissionPackage

MIN_WALL_CLOCK_NS = 1_577_836_800 * NS   # 2020-01-01: earlier stamps mean a clock that was never set


def make_mission_id(robot_id: str, stamp_ns: int) -> str:
    safe = "".join(c if c.isalnum() or c in "._-" else "-" for c in robot_id).strip("-") or "rover"
    return f"{safe}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(stamp_ns / NS))}"


class MissionRecorder:
    def __init__(self, cfg: dict, source: dict, log=print):
        self.cfg, self.source, self.log = cfg, source, log
        self.pkg: MissionPackage | None = None
        self.counts: Counter = Counter()
        self.first_ns: int | None = None
        self.last_ns: int | None = None
        self._clock: dict[str, list[float]] = {}
        self._frames: deque[Frame] = deque()
        self._kept: set[int] = set()
        self._last_keep_ns: int | None = None
        self._pending_marks: list[Mark] = []
        self._pending_dets: list = []
        self._next_ns: dict[str, int] = {}
        self._warned: dict[str, str] = {}
        self._latest: dict[str, dict] = {}     # last raw sample per stream, for mark snapshots
        self._offset_ns = int(cfg["camera"]["time_offset_s"] * NS)
        self._fsync_s = cfg["recording"]["fsync_interval_s"]

    @property
    def recording(self) -> bool:
        return self.pkg is not None

    def start(self, stamp_ns: int, mission_id: str | None = None, info: dict | None = None) -> MissionPackage:
        mid = mission_id or make_mission_id(self.cfg["robot_id"], stamp_ns)
        header = {"mission_id": mid, "gateway": {"name": "forestcare_gateway", "version": VERSION},
                  "config": self.cfg, "source": self.source, "info": info or {}, "start_ns": stamp_ns}
        self.pkg = MissionPackage.create(outbox_path(self.cfg), mid, header)
        self.log(f"recording mission {mid} into {self.pkg.root}")
        return self.pkg

    def mark_simulated(self, reason: str) -> None:
        """The data turned out to come from the simulator: label the mission accordingly."""
        if self.cfg["source_kind"] == "simulated":
            return
        self.cfg["source_kind"] = "simulated"
        self.cfg["simulator"] = {**(self.cfg.get("simulator") or {}), "name": "forestcare_gateway.sim",
                                 "detected_by": reason}
        if self.pkg is not None:
            header = self.pkg.read_json("header.json")
            header["config"] = self.cfg
            self.pkg.write_json("header.json", header)
        self._warn("simulated", f"{reason}: this mission is labelled 'simulated'")

    # -- entry point for rclpy and rosbags messages -------------------------------------

    def handle(self, role: str, msgtype: str, msg, recv_ns: int) -> None:
        if self.pkg is None:
            return
        try:
            if role in ("gnss", "gnss_rtk"):
                self.on_gnss(gnss_from_msg(msg), recv_ns, role)
            elif role in ("odom", "odom_alt"):
                self.on_odom(odom_from_msg(msg), recv_ns, role)
            elif role == "imu":
                self.on_imu(imu_from_msg(msg), recv_ns)
            elif role == "camera":
                frame = frame_from_compressed(msg) if msgtype.endswith("CompressedImage") else frame_from_raw(msg)
                if frame is None:
                    self._warn("camera", f"cannot store {msgtype} images with this encoding (or Pillow is missing)")
                    return
                self.on_frame(frame, recv_ns)
            elif role == "detections":
                self.on_detections(detections_from_msg(msg), recv_ns)
            elif role == "marks":
                self.on_mark(mark_from_text(msg.data, recv_ns))
            elif role == "lidar":
                self.on_lidar(lidar_from_msg(msg, msgtype), recv_ns)
        except Exception as exc:  # one malformed message must not end a field mission
            self.counts[f"{role}_errors"] += 1
            self._warn(f"{role}-error", f"could not read a {msgtype} message on the {role} topic: {exc}")

    # -- per stream ------------------------------------------------------------------------

    def on_gnss(self, fix, recv_ns: int, stream: str = "gnss") -> None:
        if not self._seen(stream, fix.stamp_ns, recv_ns):
            return
        self._latest[stream] = asdict(fix)
        self.pkg.append(stream, {**asdict(fix), "recv_ns": recv_ns}, self._fsync_s)

    def on_odom(self, s, recv_ns: int, stream: str = "odom") -> None:
        if self._seen(stream, s.stamp_ns, recv_ns):
            self._latest[stream] = asdict(s)
        else:
            return
        if self._decimate(stream, s.stamp_ns, self.cfg["recording"]["odom_rate_hz"]):
            self.pkg.append(stream, asdict(s), self._fsync_s)

    def on_imu(self, s, recv_ns: int) -> None:
        if self._seen("imu", s.stamp_ns, recv_ns):
            self._latest["imu"] = asdict(s)
        else:
            return
        if self._decimate("imu", s.stamp_ns, self.cfg["recording"]["imu_rate_hz"]):
            self.pkg.append("imu", asdict(s), self._fsync_s)

    def on_lidar(self, info, recv_ns: int) -> None:
        if self._seen("lidar", info.stamp_ns, recv_ns) and self._decimate("lidar", info.stamp_ns, self.cfg["recording"]["lidar_rate_hz"]):
            self.pkg.append("lidar", asdict(info), self._fsync_s)

    def on_frame(self, frame: Frame, recv_ns: int) -> None:
        if not self._seen("camera", frame.stamp_ns, recv_ns):
            return
        frame.stamp_ns += self._offset_ns
        self._frames.append(frame)
        keep_ns = self._buffer_ns()
        while self._frames and frame.stamp_ns - self._frames[0].stamp_ns > keep_ns:
            self._frames.popleft()
        every = self.cfg["camera"]["keep_every_s"]
        if every and (self._last_keep_ns is None or frame.stamp_ns - self._last_keep_ns >= every * NS):
            self._keep(frame)
            self._last_keep_ns = frame.stamp_ns
        self._resolve()

    def on_detections(self, arr, recv_ns: int) -> None:
        if not self._seen("detections", arr.stamp_ns, recv_ns) or not arr.detections:
            return
        arr.stamp_ns += self._offset_ns   # detections carry the stamp of their image
        self._pending_dets.append(arr)
        self._resolve()

    def on_mark(self, mark: Mark) -> None:
        """Operator pressed 'mark'. The snapshot keeps the latest raw GNSS / odometry / IMU values
        with the mark, so the facts at that moment are on disk even before the mission is built."""
        self.counts["marks"] += 1
        self._seen_time(mark.stamp_ns)
        snapshot = {k: v for k, v in self._latest.items() if abs(v["stamp_ns"] - mark.stamp_ns) < 5 * NS}
        self._pending_marks.append((mark, snapshot))
        fix = snapshot.get("gnss")
        gnss = (f"GNSS \u00b1{fix['sigma_m']:.1f} m (status {fix['status']})" if fix and fix["sigma_m"]
                else "no recent GNSS fix!" if not fix else "GNSS without accuracy")
        self.log(f"mark {self.counts['marks']}: {mark.kind} '{mark.label}'{f' [{mark.tag}]' if mark.tag else ''}; {gnss}")
        self._resolve()

    # -- end of mission -------------------------------------------------------------------

    def finish(self) -> dict:
        """Resolve what is still pending, close the logs and build mission.json."""
        from .assemble import build_mission  # local import: assemble pulls in the localization code

        if self.pkg is None:
            raise RuntimeError("no mission is being recorded")
        self._resolve(final=True)
        self.pkg.write_json("stats.json", {
            "first_ns": self.first_ns, "last_ns": self.last_ns, "counts": dict(self.counts),
            "clock_offsets_ms": {k: _summary(v) for k, v in self._clock.items()},
            "warnings": list(self._warned.values()),
        })
        self.pkg.close()
        pkg, self.pkg = self.pkg, None
        return build_mission(pkg)

    # -- internals --------------------------------------------------------------------------

    def _buffer_ns(self) -> int:
        return int((max(self.cfg["marks"]["frame_window_s"], self.cfg["detections"]["frame_tolerance_s"]) + 5) * NS)

    def _keep(self, frame: Frame) -> dict:
        rel = self.pkg.write_frame(frame.stamp_ns, frame.data, frame.fmt)
        record = {"stamp_ns": frame.stamp_ns, "file": rel, "fmt": frame.fmt, "width": frame.width,
                  "height": frame.height, "bytes": len(frame.data), "frame_id": frame.frame_id,
                  "sha256": hashlib.sha256(frame.data).hexdigest()}
        if frame.stamp_ns not in self._kept:
            self._kept.add(frame.stamp_ns)
            self.pkg.append("frames", record, self._fsync_s)
            self.counts["frames_kept"] += 1
        return record

    def _resolve(self, final: bool = False) -> None:
        newest = self._frames[-1].stamp_ns if self._frames else None
        tol = int(self.cfg["detections"]["frame_tolerance_s"] * NS)
        waiting = []
        for arr in self._pending_dets:
            match = min(self._frames, key=lambda f: abs(f.stamp_ns - arr.stamp_ns), default=None)
            if match is not None and abs(match.stamp_ns - arr.stamp_ns) <= tol:
                self._keep(match)
                self._write_detections(arr, match.stamp_ns)
            elif final or (newest is not None and newest > arr.stamp_ns + tol):
                self._write_detections(arr, None)
            else:
                waiting.append(arr)
        self._pending_dets = waiting

        window = int(self.cfg["marks"]["frame_window_s"] * NS)
        waiting = []
        for m, snapshot in self._pending_marks:
            if not final and (newest is None or newest <= m.stamp_ns):
                waiting.append((m, snapshot))   # the frame the operator saw may still be in flight
                continue
            before = [f for f in self._frames if 0 <= m.stamp_ns - f.stamp_ns <= window]
            after = [f for f in self._frames if 0 < f.stamp_ns - m.stamp_ns <= window]
            frame = before[-1] if before else (after[0] if after else None)
            if frame is not None:
                self._keep(frame)
            self.pkg.append("marks", {**asdict(m), "frame_stamp_ns": frame.stamp_ns if frame else None,
                                      "frame_dt_s": round((frame.stamp_ns - m.stamp_ns) / NS, 3) if frame else None,
                                      "snapshot": snapshot},
                            self._fsync_s)
            if frame is None:
                self._warn(f"mark-{m.stamp_ns}", f"mark '{m.label}' has no camera frame within "
                                                 f"{self.cfg['marks']['frame_window_s']} s: is the camera running?")
        self._pending_marks = waiting

    def _write_detections(self, arr, frame_ns: int | None) -> None:
        self.counts["detections"] += len(arr.detections)
        self.pkg.append("detections", {"stamp_ns": arr.stamp_ns, "frame_stamp_ns": frame_ns, "frame_id": arr.frame_id,
                                       "detections": [asdict(d) for d in arr.detections]}, self._fsync_s)

    def _decimate(self, stream: str, stamp_ns: int, rate_hz: float) -> bool:
        """Keep at most `rate_hz` samples per second, on a fixed schedule (no drift)."""
        if not rate_hz:
            return True
        due = self._next_ns.get(stream)
        if due is not None and stamp_ns < due:
            return False
        period = int(NS / rate_hz)
        due = stamp_ns if due is None or stamp_ns - due > period else due
        while due <= stamp_ns:
            due += period
        self._next_ns[stream] = due
        return True

    def _seen(self, role: str, stamp_ns: int, recv_ns: int) -> bool:
        """Count the message; False if its stamp cannot be used (clock not set)."""
        self.counts[role] += 1
        if stamp_ns < MIN_WALL_CLOCK_NS:
            self.counts[f"{role}_bad_stamps"] += 1
            self._warn(f"{role}-clock", f"{role} stamps are not wall-clock time (before 2020), messages skipped; "
                                        "check the driver clock / use_sim_time")
            return False
        self._seen_time(stamp_ns)
        if recv_ns and self.counts[role] % 10 == 1:     # sample the header-vs-arrival offset
            self._clock.setdefault(role, []).append((recv_ns - stamp_ns) / 1e6)
        return True

    def _seen_time(self, stamp_ns: int) -> None:
        self.first_ns = stamp_ns if self.first_ns is None else min(self.first_ns, stamp_ns)
        self.last_ns = stamp_ns if self.last_ns is None else max(self.last_ns, stamp_ns)

    def _warn(self, key: str, text: str) -> None:
        if key not in self._warned:
            self._warned[key] = text
            self.log(f"WARNING: {text}")


def _summary(values: list[float]) -> dict:
    if not values:
        return {}
    ordered = sorted(values)
    return {"n": len(values), "median": round(statistics.median(ordered), 1),
            "p95": round(ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))], 1),
            "min": round(ordered[0], 1), "max": round(ordered[-1], 1)}
