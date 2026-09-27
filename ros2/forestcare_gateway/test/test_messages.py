import json
import math

import pytest

from forestcare_gateway.messages import (detections_from_msg, frame_from_compressed, gnss_from_msg, image_size,
                                         imu_from_msg, mark_from_text, odom_from_msg)
from forestcare_gateway.sim import header, quat

from gateway_testlib import T0, fix_msg, frame_msg, jpeg, ros


def test_navsatfix():
    fix = gnss_from_msg(fix_msg(T0, 50.65, 7.07, sigma=3.0, status=1))
    assert (fix.stamp_ns, fix.lat, fix.lon, fix.status) == (T0, 50.65, 7.07, 1)
    assert fix.sigma_m == pytest.approx(3.0)
    assert fix.valid


def test_navsatfix_without_covariance_or_fix():
    assert gnss_from_msg(fix_msg(T0, 50.65, 7.07, cov_type=0)).sigma_m is None
    assert not gnss_from_msg(fix_msg(T0, 50.65, 7.07, status=-1)).valid
    assert not gnss_from_msg(fix_msg(T0, 0.0, 0.0)).valid


def test_odometry_and_imu():
    odom = odom_from_msg(ros("nav_msgs/msg/Odometry", {
        "header": header(T0, "odom"), "child_frame_id": "base_link",
        "pose": {"pose": {"position": {"x": 1.5, "y": -2.0}, "orientation": quat(math.radians(30))}},
        "twist": {"twist": {"linear": {"x": 0.6}, "angular": {"z": 0.1}}}}))
    assert (odom.x, odom.y, odom.v, odom.w) == (1.5, -2.0, 0.6, 0.1)
    assert math.degrees(odom.yaw) == pytest.approx(30)
    imu = imu_from_msg(ros("sensor_msgs/msg/Imu", {"header": header(T0, "imu"), "orientation": quat(0, 0.1, -0.2),
                                                  "orientation_covariance": [0.01] + [0] * 8, "angular_velocity": {"z": 0.2}}))
    assert imu.roll == pytest.approx(0.1) and imu.pitch == pytest.approx(-0.2) and imu.wz == 0.2
    no_orientation = imu_from_msg(ros("sensor_msgs/msg/Imu", {"header": header(T0, "imu"), "orientation_covariance": [-1] + [0] * 8}))
    assert no_orientation.yaw is None


def test_image_size_from_headers():
    assert image_size(jpeg(320, 240)) == (320, 240)
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (17, 9)).save(buf, "PNG")
    assert image_size(buf.getvalue()) == (17, 9)
    assert image_size(b"not an image") == (None, None)


def test_compressed_image():
    data = jpeg(80, 60)
    frame = frame_from_compressed(frame_msg(T0, data))
    assert frame.data == data and (frame.width, frame.height, frame.fmt) == (80, 60, "jpeg")


def test_detections_vision_msgs_4():
    arr = detections_from_msg(ros("vision_msgs/msg/Detection2DArray", {"header": header(T0, "cam"), "detections": [{
        "header": header(T0, "cam"), "id": "track-7",
        "results": [{"hypothesis": {"class_id": "prunus_serotina", "score": 0.8}},
                    {"hypothesis": {"class_id": "prunus_padus", "score": 0.15}}],
        "bbox": {"center": {"position": {"x": 100, "y": 60}}, "size_x": 40, "size_y": 30}}]}))
    (det,) = arr.detections
    assert arr.stamp_ns == T0 and det.det_id == "track-7"
    assert [(h.class_id, h.score) for h in det.hypotheses] == [("prunus_serotina", 0.8), ("prunus_padus", 0.15)]
    assert det.bbox == {"cx": 100.0, "cy": 60.0, "w": 40.0, "h": 30.0}


@pytest.mark.parametrize("text,label,kind,tag", [
    ("Prunus serotina?", "Prunus serotina?", "observation", ""),
    ("REF:R3", "reference point R3", "reference", "R3"),
    ("PLANT:P2 Prunus serotina?", "Prunus serotina?", "observation", "P2"),
    (json.dumps({"label": "odd shrub", "note": "near path", "id": "s1-4", "stamp_ns": T0 + 5}), "odd shrub", "observation", ""),
    ("{broken json", "{broken json", "observation", ""),
])
def test_mark_text(text, label, kind, tag):
    m = mark_from_text(text, recv_ns=T0)
    assert (m.label, m.kind, m.tag) == (label, kind, tag)


def test_mark_json_carries_its_own_time_and_id():
    m = mark_from_text(json.dumps({"label": "x", "id": "s1-4", "stamp_ns": T0 + 5}), recv_ns=T0 + 999)
    assert (m.stamp_ns, m.mark_id) == (T0 + 5, "s1-4")
