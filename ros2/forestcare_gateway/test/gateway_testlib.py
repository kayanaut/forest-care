"""Helpers shared by the gateway tests (no ROS, no backend: rosbags builds real ROS 2 messages)."""

from __future__ import annotations

import io
import json

from forestcare_gateway.bagio import build, typestore
from forestcare_gateway.geo import from_local
from forestcare_gateway.messages import NS
from forestcare_gateway.sim import header, quat

T0 = 1_790_236_800 * NS          # 2026-09-24 08:00 UTC
LAT0, LON0 = 50.65127, 7.07336
STEP_NS = 20_000_000             # 50 Hz simulation step

_STORE = typestore()


def ros(msgtype: str, data: dict):
    return build(_STORE, msgtype, data)


def fix_msg(t_ns, lat, lon, sigma=2.0, status=0, cov_type=2):
    var = sigma ** 2
    return ros("sensor_msgs/msg/NavSatFix", {"header": header(t_ns, "gnss"), "status": {"status": status, "service": 1},
                                             "latitude": lat, "longitude": lon, "altitude": 90.0,
                                             "position_covariance": [var, 0, 0, 0, var, 0, 0, 0, 4 * var],
                                             "position_covariance_type": cov_type})


def jpeg(width=64, height=48, color=(40, 90, 40)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, "JPEG")
    return buf.getvalue()


def frame_msg(t_ns, data=None):
    return ros("sensor_msgs/msg/CompressedImage", {"header": header(t_ns, "camera"), "format": "jpeg",
                                                   "data": data or jpeg()})


def mark_msg(text):
    return ros("std_msgs/msg/String", {"data": text})


def detection_msg(t_ns, score, track_id="trk1"):
    return ros("vision_msgs/msg/Detection2DArray", {"header": header(t_ns, "cam"), "detections": [{
        "header": header(t_ns, "cam"), "id": track_id,
        "results": [{"hypothesis": {"class_id": "prunus_serotina", "score": score}}],
        "bbox": {"center": {"position": {"x": 30, "y": 20}}, "size_x": 10, "size_y": 10}}]})


def drive_east(rec, seconds=20.0, speed=1.0, sigma=1.0, mark_at=None, dets_at=(), frame_every_steps=25):
    """The robot drives east from the origin: GNSS 5 Hz, camera every 0.5 s, odometry 50 Hz,
    an optional operator mark (100 ms after the frame at `mark_at`) and detections on frames."""
    det_steps = {round(d * 50) for d in dets_at}
    mark_step = None if mark_at is None else round(mark_at * 50)
    for i in range(int(seconds * 50) + 1):
        t, now = i / 50, T0 + i * STEP_NS
        if i % 10 == 0:
            lat, lon = from_local(speed * t, 0.0, LAT0, LON0)
            rec.handle("gnss", "sensor_msgs/msg/NavSatFix", fix_msg(now, lat, lon, sigma), now)
        if i % frame_every_steps == 0:
            rec.handle("camera", "sensor_msgs/msg/CompressedImage", frame_msg(now), now + 30_000_000)
            if i in det_steps:
                rec.handle("detections", "vision_msgs/msg/Detection2DArray", detection_msg(now, 0.6 + t / 100),
                           now + 150_000_000)
        rec.handle("odom", "nav_msgs/msg/Odometry", ros("nav_msgs/msg/Odometry", {
            "header": header(now, "odom"), "pose": {"pose": {"position": {"x": speed * t}, "orientation": quat(0.0)}},
            "twist": {"twist": {"linear": {"x": speed}}}}), now)
        if i == mark_step:
            payload = json.dumps({"label": "Prunus serotina?", "id": "m1", "stamp_ns": now + 100_000_000})
            rec.handle("marks", "std_msgs/msg/String", mark_msg(payload), now + 100_000_000)
