"""Plain-Python samples and converters from ROS 2 messages.

The converters only read message attributes, so they work on messages from rclpy (live)
and from the `rosbags` reader (offline) alike. This is the only module that knows ROS
field names; everything downstream works on the dataclasses below.
"""

from __future__ import annotations

import io
import json
import math
import struct
from dataclasses import dataclass, field

NS = 1_000_000_000


def stamp_ns(stamp) -> int:
    return int(stamp.sec) * NS + int(stamp.nanosec)


def yaw_from_quaternion(q) -> float:
    return math.atan2(2.0 * (q.w * q.z + q.x * q.y), 1.0 - 2.0 * (q.y * q.y + q.z * q.z))


def rpy_from_quaternion(q) -> tuple[float, float, float]:
    roll = math.atan2(2 * (q.w * q.x + q.y * q.z), 1 - 2 * (q.x * q.x + q.y * q.y))
    sinp = 2 * (q.w * q.y - q.z * q.x)
    pitch = math.copysign(math.pi / 2, sinp) if abs(sinp) >= 1 else math.asin(sinp)
    return roll, pitch, yaw_from_quaternion(q)


@dataclass
class GnssFix:
    stamp_ns: int
    lat: float
    lon: float
    alt: float
    status: int            # NavSatStatus: -1 no fix, 0 fix, 1 SBAS, 2 GBAS (differential / RTK)
    service: int
    covariance_type: int   # 0 unknown, 1 approximated, 2 diagonal known, 3 known
    sigma_m: float | None  # horizontal, per axis, 1 sigma; None if the receiver reports no covariance
    frame_id: str = ""

    @property
    def valid(self) -> bool:
        return (self.status >= 0 and math.isfinite(self.lat) and math.isfinite(self.lon)
                and not (self.lat == 0.0 and self.lon == 0.0))


def gnss_from_msg(msg) -> GnssFix:
    cov = [float(c) for c in msg.position_covariance]
    ctype = int(msg.position_covariance_type)
    sigma = math.sqrt(max(0.0, (cov[0] + cov[4]) / 2)) if ctype > 0 else None
    return GnssFix(stamp_ns(msg.header.stamp), float(msg.latitude), float(msg.longitude), float(msg.altitude),
                   int(msg.status.status), int(msg.status.service), ctype, sigma or None, msg.header.frame_id)


@dataclass
class OdomSample:
    stamp_ns: int
    x: float
    y: float
    yaw: float       # radians, in the odometry frame (not geographic)
    v: float         # forward speed, m/s
    w: float         # yaw rate, rad/s
    frame_id: str = ""
    child_frame_id: str = ""


def odom_from_msg(msg) -> OdomSample:
    p, q, tw = msg.pose.pose.position, msg.pose.pose.orientation, msg.twist.twist
    return OdomSample(stamp_ns(msg.header.stamp), float(p.x), float(p.y), yaw_from_quaternion(q),
                      float(tw.linear.x), float(tw.angular.z), msg.header.frame_id, msg.child_frame_id)


@dataclass
class ImuSample:
    stamp_ns: int
    roll: float | None      # radians; None if the IMU publishes no orientation
    pitch: float | None
    yaw: float | None
    wz: float               # yaw rate, rad/s
    ax: float
    ay: float
    az: float


def imu_from_msg(msg) -> ImuSample:
    has_orientation = float(msg.orientation_covariance[0]) != -1.0
    roll, pitch, yaw = rpy_from_quaternion(msg.orientation) if has_orientation else (None, None, None)
    a = msg.linear_acceleration
    return ImuSample(stamp_ns(msg.header.stamp), roll, pitch, yaw, float(msg.angular_velocity.z),
                     float(a.x), float(a.y), float(a.z))


@dataclass
class Frame:
    stamp_ns: int
    frame_id: str
    fmt: str               # 'jpeg' or 'png'
    data: bytes
    width: int | None = None
    height: int | None = None


def image_size(data: bytes) -> tuple[int | None, int | None]:
    """Width/height from a JPEG or PNG header, without decoding the image."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        w, h = struct.unpack(">II", data[16:24])
        return w, h
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker == 0xFF or marker == 0x01 or 0xD0 <= marker <= 0xD8:
                i += 1 if marker == 0xFF else 2
                continue
            length = struct.unpack(">H", data[i + 2:i + 4])[0]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return w, h
            i += 2 + length
    return None, None


def frame_from_compressed(msg) -> Frame:
    fmt = "png" if "png" in msg.format.lower() else "jpeg"
    data = bytes(msg.data)
    w, h = image_size(data)
    return Frame(stamp_ns(msg.header.stamp), msg.header.frame_id, fmt, data, w, h)


def frame_from_raw(msg, quality: int = 90) -> Frame | None:
    """sensor_msgs/Image (rgb8, bgr8, mono8) -> JPEG. Needs Pillow; returns None without it."""
    try:
        from PIL import Image
    except ImportError:
        return None
    modes = {"rgb8": ("RGB", "RGB"), "bgr8": ("RGB", "BGR"), "mono8": ("L", "L")}
    if msg.encoding not in modes:
        return None
    mode, raw_mode = modes[msg.encoding]
    img = Image.frombuffer(mode, (msg.width, msg.height), bytes(msg.data), "raw", raw_mode, int(msg.step), 1)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return Frame(stamp_ns(msg.header.stamp), msg.header.frame_id, "jpeg", buf.getvalue(), msg.width, msg.height)


@dataclass
class Hypothesis:
    class_id: str
    score: float


@dataclass
class Detection:
    hypotheses: list[Hypothesis]
    bbox: dict | None = None     # pixel centre and size: cx, cy, w, h
    det_id: str = ""


@dataclass
class DetectionArray:
    stamp_ns: int                # the stamp of the image the detections were made on
    frame_id: str
    detections: list[Detection] = field(default_factory=list)


def detections_from_msg(msg) -> DetectionArray:
    """vision_msgs/Detection2DArray; handles the field layout of vision_msgs 2.x-4.x."""
    dets = []
    for d in msg.detections:
        hyps = []
        for r in d.results:
            h = getattr(r, "hypothesis", r)                      # >= 3.0 nests ObjectHypothesis
            class_id = getattr(h, "class_id", None)
            hyps.append(Hypothesis(str(class_id if class_id is not None else getattr(h, "id", "")), float(h.score)))
        centre = d.bbox.center
        pos = getattr(centre, "position", centre)                # >= 4.0: Pose2D.position
        bbox = {"cx": float(pos.x), "cy": float(pos.y), "w": float(d.bbox.size_x), "h": float(d.bbox.size_y)}
        dets.append(Detection(hyps, bbox, str(getattr(d, "id", ""))))
    return DetectionArray(stamp_ns(msg.header.stamp), msg.header.frame_id, dets)


@dataclass
class Mark:
    """An operator mark: 'look here'. kind is 'observation' or 'reference' (a surveyed point)."""
    stamp_ns: int
    label: str
    note: str = ""
    kind: str = "observation"
    tag: str = ""                # plant tag or reference-point id, for repeated-run experiments
    source: str = ""
    mark_id: str = ""


def mark_from_text(text: str, recv_ns: int) -> Mark:
    """std_msgs/String payload. Plain text is the label; JSON may add note, kind, tag, id, stamp_ns.

    Conventions: 'REF:<id>' marks a surveyed reference point, 'PLANT:<id> <label>' tags a plant
    so that repeated runs can be compared (localization experiment).
    """
    text = text.strip()
    data: dict = {}
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            data = {}
    if not data:
        data = {"label": text}
    label = str(data.get("label") or "").strip()
    kind = str(data.get("kind") or "observation")
    tag = str(data.get("tag") or "")
    upper = label.upper()
    if upper.startswith("REF:"):
        kind, tag = "reference", label[4:].strip()
        label = f"reference point {tag}"
    elif upper.startswith("PLANT:"):
        tag, _, rest = label[6:].strip().partition(" ")
        label = rest.strip() or "tagged plant"
    stamp = int(data.get("stamp_ns") or recv_ns)
    return Mark(stamp, label or "marked", str(data.get("note") or ""), kind, tag, str(data.get("source") or ""),
                str(data.get("id") or ""))


@dataclass
class LidarInfo:
    stamp_ns: int
    kind: str          # 'scan' or 'points'
    points: int
    frame_id: str = ""


def lidar_from_msg(msg, msgtype: str) -> LidarInfo:
    if msgtype.endswith("LaserScan"):
        lo, hi = float(msg.range_min), float(msg.range_max)
        points = sum(1 for r in msg.ranges if lo <= r <= hi)
        return LidarInfo(stamp_ns(msg.header.stamp), "scan", points, msg.header.frame_id)
    return LidarInfo(stamp_ns(msg.header.stamp), "points", int(msg.width) * int(msg.height), msg.header.frame_id)
