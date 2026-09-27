"""Read and write rosbag2 files without a ROS installation (uses the `rosbags` library).

- `convert_bag` replays a recorded mission bag through the same MissionRecorder the live node
  uses, and writes a mission package.
- `BagWriter` writes messages given as plain dicts (used by the simulator's demo bags).
- Types are taken from the definitions stored in the bag itself (MCAP bags always carry
  them). vision_msgs (Apache-2.0, v4.1) is registered here as a fallback for bags that lack
  them, and for writing demo detections.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from pathlib import Path

SIM_TOPIC = "/sim/ground_truth"   # published only by the simulator; its presence marks simulated data

VISION_MSGS = {  # vision_msgs 4.1 (ROS 2 Humble..Kilted), comments removed
    "vision_msgs/msg/Point2D": "float64 x\nfloat64 y\n",
    "vision_msgs/msg/Pose2D": "vision_msgs/Point2D position\nfloat64 theta\n",
    "vision_msgs/msg/BoundingBox2D": "vision_msgs/Pose2D center\nfloat64 size_x\nfloat64 size_y\n",
    "vision_msgs/msg/ObjectHypothesis": "string class_id\nfloat64 score\n",
    "vision_msgs/msg/ObjectHypothesisWithPose": "ObjectHypothesis hypothesis\ngeometry_msgs/PoseWithCovariance pose\n",
    "vision_msgs/msg/Detection2D": "std_msgs/Header header\nObjectHypothesisWithPose[] results\nBoundingBox2D bbox\nstring id\n",
    "vision_msgs/msg/Detection2DArray": "std_msgs/Header header\nDetection2D[] detections\n",
}


def typestore():
    from rosbags.typesys import Stores, get_types_from_msg, get_typestore

    store = get_typestore(Stores.ROS2_KILTED)
    types = {}
    for name, text in VISION_MSGS.items():
        types.update(get_types_from_msg(text, name))
    store.register(types)
    return store


# -- dict -> rosbags message (so the simulator can describe messages as plain dicts) -------

def build(store, msgtype: str, data: dict | None):
    import numpy as np
    from rosbags.interfaces import Nodetype

    _, fields = store.fielddefs[msgtype]
    data = data or {}
    kwargs = {}
    for name, (node, detail) in fields:
        value = data.get(name)
        if node == Nodetype.NAME:
            kwargs[name] = build(store, detail, value)
        elif node == Nodetype.BASE:
            kwargs[name] = _base(detail[0], value)
        else:  # ARRAY (fixed length) or SEQUENCE
            (sub_node, sub_detail), length = detail
            if sub_node == Nodetype.BASE and sub_detail[0] not in ("string", "wstring"):
                dtype = {"bool": np.bool_, "byte": np.uint8, "char": np.uint8, "octet": np.uint8}.get(sub_detail[0], sub_detail[0])
                if isinstance(value, (bytes, bytearray)):
                    arr = np.frombuffer(bytes(value), dtype=np.uint8)
                else:
                    arr = np.asarray(value if value is not None else ([0] * length if node == Nodetype.ARRAY else []), dtype=dtype)
                kwargs[name] = arr
            elif sub_node == Nodetype.BASE:
                kwargs[name] = list(value or [])
            else:
                kwargs[name] = [build(store, sub_detail, v) for v in (value or [])]
    return store.types[msgtype](**kwargs)


def _base(typ: str, value):
    if typ in ("string", "wstring"):
        return "" if value is None else str(value)
    if typ == "bool":
        return bool(value)
    if typ.startswith("float"):
        return float(value or 0.0)
    return int(value or 0)


class BagWriter:
    """Write a rosbag2 (MCAP) from plain-dict messages: `write(topic, msgtype, stamp_ns, data)`."""

    def __init__(self, path: Path):
        from rosbags.rosbag2 import StoragePlugin, Writer

        self.store = typestore()
        self.writer = Writer(Path(path), version=9, storage_plugin=StoragePlugin.MCAP)
        self.connections: dict[str, object] = {}

    def __enter__(self):
        self.writer.open()
        return self

    def __exit__(self, *exc):
        self.writer.close()

    def write(self, topic: str, msgtype: str, stamp_ns: int, data: dict) -> None:
        conn = self.connections.get(topic)
        if conn is None:
            conn = self.connections[topic] = self.writer.add_connection(topic, msgtype, typestore=self.store)
        msg = build(self.store, msgtype, data)
        self.writer.write(conn, stamp_ns, self.store.serialize_cdr(msg, msgtype))


# -- reading ------------------------------------------------------------------------------------

def read_messages(bag: Path, topics: list[str] | None = None):
    """Yield (topic, msgtype, bag_time_ns, message) in recording order."""
    from rosbags.highlevel import AnyReader

    with AnyReader([Path(bag)], default_typestore=typestore()) as reader:
        conns = [c for c in reader.connections if topics is None or c.topic in topics]
        for conn, t, raw in reader.messages(connections=conns):
            yield conn.topic, conn.msgtype, t, reader.deserialize(raw, conn.msgtype)


def bag_summary(bag: Path) -> dict:
    from rosbags.highlevel import AnyReader

    bag = Path(bag)
    with AnyReader([bag], default_typestore=typestore()) as reader:
        counts = Counter()
        types = {}
        for conn in reader.connections:
            counts[conn.topic] += conn.msgcount
            types[conn.topic] = conn.msgtype
        meta = bag / "metadata.yaml" if bag.is_dir() else None
        return {"name": bag.name, "start_ns": reader.start_time, "end_ns": reader.end_time,
                "duration_s": round(reader.duration / 1e9, 1), "topics": {t: {"type": types[t], "messages": counts[t]} for t in sorted(counts)},
                "metadata_sha256": hashlib.sha256(meta.read_bytes()).hexdigest() if meta and meta.exists() else None}


def mission_info(bag: Path) -> dict:
    """Operator notes written by the recording script: mission_info.yaml inside the bag
    directory, or in the mission folder that contains it (<mission>/bag + <mission>/mission_info.yaml)."""
    bag = Path(bag)
    candidates = [bag / "mission_info.yaml", bag.parent / "mission_info.yaml"] if bag.is_dir() else \
                 [bag.with_name("mission_info.yaml")]
    for path in candidates:
        if path.exists():
            import yaml

            return yaml.safe_load(path.read_text()) or {}
    return {}


def convert_bag(bag: Path, cfg: dict, mission_id: str | None = None, info: dict | None = None, log=print) -> dict:
    """Replay a mission bag through the recorder and build the mission package."""
    from .recorder import MissionRecorder

    summary = bag_summary(bag)
    roles: dict[str, list[str]] = {}         # a topic may feed several roles (one RTK receiver: gnss and gnss_rtk)
    for role, topic in cfg["topics"].items():
        if topic:
            roles.setdefault(topic, []).append(role)
    found = set(summary["topics"])
    for role, topic in cfg["topics"].items():
        if topic and topic not in found and role in ("gnss", "camera"):
            log(f"WARNING: the bag has no {role} topic {topic!r} (topics: {', '.join(sorted(found))})")
    rec = MissionRecorder(cfg, {"kind": "bag", **summary}, log)
    if SIM_TOPIC in found:
        rec.mark_simulated(f"the bag contains {SIM_TOPIC}, which only the simulator publishes")
    rec.start(summary["start_ns"], mission_id, {**mission_info(bag), **(info or {})})
    for topic, msgtype, t, msg in read_messages(bag, list(roles)):
        for role in roles[topic]:
            rec.handle(role, msgtype, msg, t)
    return rec.finish()
