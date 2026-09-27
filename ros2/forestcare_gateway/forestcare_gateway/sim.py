"""A small simulated rover, world and sensor set, for demos, tests and teleoperation practice.

No physics engine: a unicycle rover, a flat 2D forest of tree trunks and simple error
models. Everything here is invented. The world is anchored in real Bonn woodland
(Kottenforst) only so that Forest Care's context lookups behave as for a real mission.

The error models matter for the localization experiment (loc/). They are plausible,
literature-level ASSUMPTIONS, not measurements; the field test replaces them:
  - GNSS: a slowly wandering, time-correlated bias plus noise, both larger under canopy,
    where the receiver also under-reports its error; occasional multipath jumps and
    dropouts under dense canopy.
  - RTK GNSS (optional second receiver): centimetres when fixed in the open, decimetres
    when float under partial canopy, falls back to plain GNSS under dense canopy.
  - Wheel odometry: 2 % scale error and a slip-induced yaw-rate bias.
  - IMU: gyro bias and noise; orientation integrated without magnetometer (yaw drifts).
  - LiDAR odometry: slow drift, worse in open areas with few trunks to match.

Messages are produced as plain dicts in ROS 2 message layout; bagio.build() turns them
into bag messages and nodes/common.fill() into rclpy messages.
"""

from __future__ import annotations

import io
import json
import math
from dataclasses import dataclass, field

import numpy as np

from .geo import from_local, wrap_angle

NS = 1_000_000_000
ORIGIN = (50.65127, 7.07336)   # Kottenforst, Bonn: inside the city's mapped woodland (tested)
TOPICS = {
    "gnss": "/gnss/fix", "gnss_rtk": "/gnss_rtk/fix", "odom": "/odom", "odom_alt": "/odom_lidar",
    "imu": "/imu/data", "camera": "/camera/image_raw/compressed", "detections": "/forestcare/detections",
    "marks": "/forestcare/mark", "lidar": "/scan", "cmd_vel": "/cmd_vel", "tf": "/tf", "tf_static": "/tf_static",
    "truth": "/sim/ground_truth",   # simulator only: true pose, metres east/north of the world origin
}


@dataclass
class Plant:
    id: str
    taxon: str
    e: float
    n: float


@dataclass
class Reference:
    id: str
    e: float
    n: float


@dataclass
class Stop:
    s: float          # distance along the route where the operator stops
    kind: str         # 'plant' or 'reference'
    target: str
    dwell_s: float


@dataclass
class World:
    name: str
    route: list[tuple[float, float]]
    plants: list[Plant]
    references: list[Reference]
    stops: list[Stop]
    open_zones: list[tuple[float, float, float, float]]   # rectangles (e0, n0, e1, n1) with little canopy
    trees: np.ndarray = field(default_factory=lambda: np.zeros((0, 3)))
    origin: tuple[float, float] = ORIGIN

    def __post_init__(self):
        pts = np.asarray(self.route, dtype=float)
        self._pts = pts
        self._cum = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(pts, axis=0).T))])

    @property
    def length(self) -> float:
        return float(self._cum[-1])

    def canopy(self, e: float, n: float) -> float:
        """0 = open sky ... 1 = closed canopy. Partial canopy within 6 m of an open zone."""
        d = min((math.hypot(max(e0 - e, 0, e - e1), max(n0 - n, 0, n - n1)) for e0, n0, e1, n1 in self.open_zones),
                default=99.0)
        return 0.15 if d == 0 else (0.5 if d < 6 else 0.85)

    def point_at(self, s: float) -> tuple[float, float]:
        s = min(max(s, 0.0), self.length)
        i = int(np.searchsorted(self._cum, s, side="right") - 1)
        i = min(i, len(self._pts) - 2)
        a, b = self._pts[i], self._pts[i + 1]
        seg = self._cum[i + 1] - self._cum[i]
        t = 0.0 if seg == 0 else (s - self._cum[i]) / seg
        return float(a[0] + t * (b[0] - a[0])), float(a[1] + t * (b[1] - a[1]))

    def tangent_at(self, s: float) -> float:
        (e0, n0), (e1, n1) = self.point_at(s - 0.5), self.point_at(s + 0.5)
        return math.atan2(n1 - n0, e1 - e0)

    def project(self, e: float, n: float, near_s: float | None = None, window: float = 15.0) -> float:
        """Distance along the route of the closest route point (searched near `near_s` if given)."""
        best_s, best_d = 0.0, math.inf
        for i in range(len(self._pts) - 1):
            if near_s is not None and (self._cum[i] > near_s + window or self._cum[i + 1] < near_s - window):
                continue
            a, b = self._pts[i], self._pts[i + 1]
            ab = b - a
            L2 = float(ab @ ab)
            t = 0.0 if L2 == 0 else max(0.0, min(1.0, float((np.array([e, n]) - a) @ ab) / L2))
            p = a + t * ab
            d = math.hypot(e - p[0], n - p[1])
            if d < best_d:
                best_d, best_s = d, float(self._cum[i] + t * math.sqrt(L2))
        return best_s


# The operator stops this far before a plant so that it is inside the camera's field of view
# (plants stand 2-3 m beside the route; the camera looks ahead with a 70 degree field of view).
PLANT_STOP_BEFORE_M = 4.5


def _stop_for(world: World, e: float, n: float, kind: str, target: str, dwell: float, before: float) -> Stop:
    return Stop(max(0.5, world.project(e, n) - before), kind, target, dwell)


def _plant_trees(world: World, rng: np.random.Generator, density: float = 1 / 22) -> None:
    pts = world._pts
    e0, n0 = pts.min(axis=0) - 25
    e1, n1 = pts.max(axis=0) + 25
    count = int((e1 - e0) * (n1 - n0) * density)
    cand = np.column_stack([rng.uniform(e0, e1, count), rng.uniform(n0, n1, count), rng.uniform(0.12, 0.45, count)])
    keep = []
    for te, tn, tr in cand:
        near_route = min(math.hypot(te - px, tn - py) for px, py in
                         (world.point_at(s) for s in np.arange(0, world.length, 2.0))) < 2.8
        near_plant = any(math.hypot(te - p.e, tn - p.n) < 1.5 for p in world.plants)
        if near_route or near_plant or (world.canopy(te, tn) < 0.2 and rng.random() > 0.08):
            continue
        keep.append((te, tn, tr))
    world.trees = np.array(keep) if keep else np.zeros((0, 3))


def make_world(kind: str = "survey", seed: int = 7) -> World:
    """'survey': a 260 m U-shaped route for the gateway demo (plants, a look-alike, operator stops).
    'loc': a 200 m closed loop with four reference points and six tagged plants for the
    repeated-run localization experiment (half open ride, half closed canopy)."""
    rng = np.random.default_rng(seed)
    if kind == "survey":
        route = [(0, 0), (90, 0), (90, 25), (0, 25), (0, 50), (60, 50)]
        plants = [Plant("A", "Prunus serotina", 30, 2.2), Plant("B", "Prunus serotina", 70, -2.0),
                  Plant("C", "Prunus padus", 88.0, 12), Plant("D", "Prunus serotina", 45, 27.2),
                  Plant("E", "Prunus serotina", 49, 28.3), Plant("F", "Frangula alnus", 15, 22.8),
                  Plant("G", "Prunus serotina", 30, 52.3)]
        world = World(kind, route, plants, [], [], open_zones=[(-6, -7, 96, 7)])
        for p in plants:
            if p.id in ("A", "B", "C", "D", "G"):
                world.stops.append(_stop_for(world, p.e, p.n, "plant", p.id, 4.0, before=PLANT_STOP_BEFORE_M))
    elif kind == "loc":
        route = [(0, 0), (60, 0), (60, 40), (0, 40), (0, 0)]
        plants = [Plant("P1", "Prunus serotina", 25, 2.0), Plant("P2", "Prunus serotina", 58.0, 20),
                  Plant("P3", "Prunus serotina", 40, 42.0), Plant("P4", "Prunus serotina", 28, 42.0),
                  Plant("P5", "Prunus serotina", 2.0, 22), Plant("P6", "Prunus padus", 12, -2.0)]
        refs = [Reference("R1", 0, 0), Reference("R2", 60, 0), Reference("R3", 60, 40), Reference("R4", 0, 40)]
        world = World(kind, route, plants, refs, [], open_zones=[(-10, -10, 70, 14)])
        for r in refs[1:]:
            world.stops.append(_stop_for(world, r.e, r.n, "reference", r.id, 5.0, before=0.0))
        world.stops.append(Stop(0.3, "reference", "R1", 5.0))      # start and end on R1: loop closure
        world.stops.append(Stop(world.length - 0.3, "reference", "R1", 5.0))
        for p in plants:
            world.stops.append(_stop_for(world, p.e, p.n, "plant", p.id, 3.0, before=PLANT_STOP_BEFORE_M))
    else:
        raise ValueError(f"unknown world {kind!r}")
    world.stops.sort(key=lambda s: s.s)
    _plant_trees(world, rng)
    return world


# -- sensor error models -------------------------------------------------------------------

class GnssModel:
    """Standard single-frequency-like receiver, or RTK (`rtk=True`)."""

    def __init__(self, rng: np.random.Generator, rtk: bool = False):
        self.rng, self.rtk = rng, rtk
        self.bias = rng.normal(0, 0.8, 2)
        self.fast = np.zeros(2)
        self.tau, self.tau_fast = 90.0, 3.0
        self.mode = None
        self.jump = np.zeros(2)
        self.jump_left = 0.0
        self.outage_left = 0.0

    def measure(self, dt: float, e: float, n: float, canopy: float) -> dict:
        rng = self.rng
        if self.rtk and canopy < 0.35:
            mode, sb, sw = "fixed", 0.01, 0.012
        elif self.rtk and canopy < 0.7:
            mode, sb, sw = "float", 0.25, 0.08
        else:
            mode, sb, sw = "single", 0.8 + 3.5 * canopy, 0.3 + 1.2 * canopy
        if mode != self.mode:                  # RTK fixed/float solutions do not inherit the previous error
            if mode in ("fixed", "float"):
                self.bias = rng.normal(0, sb, 2)
            self.mode = mode
        a = math.exp(-dt / self.tau)                  # slow bias (first-order Gauss-Markov): geometry, multipath
        self.bias = a * self.bias + math.sqrt(1 - a * a) * rng.normal(0, sb, 2)
        a = math.exp(-dt / self.tau_fast)             # fast wander: receivers filter internally, so it is smooth
        self.fast = a * self.fast + math.sqrt(1 - a * a) * rng.normal(0, sw, 2)
        if self.jump_left > 0:
            self.jump_left -= dt
        elif canopy > 0.5 and mode == "single" and rng.random() < dt / 60:        # ~1 per minute under canopy
            self.jump, self.jump_left = rng.normal(0, 7, 2), rng.uniform(3, 8)     # multipath excursion
        if self.outage_left > 0:
            self.outage_left -= dt
            return {"status": -1, "e": e, "n": n, "sigma": 0.0, "mode": "no fix"}
        if canopy > 0.8 and rng.random() < dt / 70:
            self.outage_left = rng.uniform(2, 5)
        err = self.bias + self.fast + (self.jump if self.jump_left > 0 else 0)
        true_sigma = math.hypot(sb, sw)
        reported = true_sigma * (1.0 - 0.45 * canopy) if mode == "single" else true_sigma   # over-confident under canopy
        reported *= rng.uniform(0.85, 1.15)                                                  # receivers' estimates vary
        status = 2 if mode in ("fixed", "float") else (1 if canopy < 0.3 else 0)
        return {"status": status, "e": e + err[0], "n": n + err[1], "sigma": max(reported, 0.01), "mode": mode}


class OdometryModel:
    """Integrates measured wheel speeds (or, with `lidar=True`, a scan-matching odometry)."""

    def __init__(self, rng: np.random.Generator, lidar: bool = False):
        self.rng, self.lidar = rng, lidar
        self.x = self.y = self.yaw = 0.0
        self.scale = 1.0 + (rng.normal(0.003, 0.002) if lidar else rng.normal(0.02, 0.005))
        self.w_bias = rng.normal(0, 0.0006 if lidar else 0.004)
        self.acc = [0.0, 0.0, 0.0]                        # distance, yaw change, time since the last message

    def step(self, dt: float, v: float, w: float, canopy: float) -> tuple[float, float]:
        rng = self.rng
        if self.lidar:
            open_area = canopy <= 0.4                         # few trunks to match: scan matching gets jumpy
            self.w_bias += rng.normal(0, 0.00005) * math.sqrt(dt)
            vm = v * self.scale + rng.normal(0, 0.03 if open_area else 0.005)
            wm = w + self.w_bias + rng.normal(0, 0.04 if open_area else 0.004)
        else:
            self.w_bias += rng.normal(0, 0.0004) * math.sqrt(dt)                 # slip on rough ground
            vm = v * self.scale + rng.normal(0, 0.02)
            wm = w * 1.03 + self.w_bias + rng.normal(0, 0.01)
        self.yaw = wrap_angle(self.yaw + wm * dt)
        self.x += vm * dt * math.cos(self.yaw)
        self.y += vm * dt * math.sin(self.yaw)
        self.acc = [self.acc[0] + vm * dt, self.acc[1] + wm * dt, self.acc[2] + dt]
        return vm, wm

    def take_twist(self) -> tuple[float, float]:
        """Mean speed and yaw rate since the last message, so the twist matches the pose."""
        d, dyaw, t = self.acc
        self.acc = [0.0, 0.0, 0.0]
        return (d / t, dyaw / t) if t > 0 else (0.0, 0.0)


class ImuModel:
    def __init__(self, rng: np.random.Generator):
        self.rng = rng
        self.bias = rng.normal(0, 0.002)
        self.yaw = 0.0

    def step(self, dt: float, w: float) -> float:
        self.bias += self.rng.normal(0, 0.00005) * math.sqrt(dt)        # in-run bias drift, consumer MEMS
        wm = w + self.bias + self.rng.normal(0, 0.005)
        self.yaw = wrap_angle(self.yaw + wm * dt)
        return wm


# -- message helpers (plain dicts in ROS 2 layout) -----------------------------------------------

def header(stamp_ns: int, frame_id: str) -> dict:
    return {"stamp": {"sec": stamp_ns // NS, "nanosec": stamp_ns % NS}, "frame_id": frame_id}


def quat(yaw: float, roll: float = 0.0, pitch: float = 0.0) -> dict:
    cy, sy = math.cos(yaw / 2), math.sin(yaw / 2)
    cp, sp = math.cos(pitch / 2), math.sin(pitch / 2)
    cr, sr = math.cos(roll / 2), math.sin(roll / 2)
    return {"w": cr * cp * cy + sr * sp * sy, "x": sr * cp * cy - cr * sp * sy,
            "y": cr * sp * cy + sr * cp * sy, "z": cr * cp * sy - sr * sp * cy}


STATIC_TF = [  # base_link -> sensor mounting (metres); the real rover's values go in forestcare_rover
    ("gnss_link", (0.0, 0.0, 1.10)), ("camera_link", (0.35, 0.0, 0.85)),
    ("imu_link", (0.0, 0.0, 0.30)), ("laser", (0.25, 0.0, 0.55)),
]


def static_tf_msg(stamp_ns: int) -> dict:
    return {"transforms": [{"header": header(stamp_ns, "base_link"), "child_frame_id": child,
                            "transform": {"translation": {"x": x, "y": y, "z": z}, "rotation": quat(0.0)}}
                           for child, (x, y, z) in STATIC_TF]}


@dataclass
class Visible:
    plant: Plant
    distance: float
    bbox: dict


def render_frame(world: World, e: float, n: float, yaw: float, stamp_ns: int, caption: str,
                 width: int = 320, height: int = 240, fov_deg: float = 70.0, max_range: float = 9.0) -> tuple[bytes, list[Visible]]:
    """A simple drawn camera view (not a photo): trunks and the plants within range."""
    from PIL import Image, ImageDraw

    half = math.radians(fov_deg) / 2
    canopy = world.canopy(e, n)
    img = Image.new("RGB", (width, height), (150, 185, 120) if canopy < 0.3 else (70, 95, 60))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, int(height * 0.62), width, height], fill=(92, 78, 52))
    items = []
    for te, tn, tr in world.trees:
        d = math.hypot(te - e, tn - n)
        rel = wrap_angle(math.atan2(tn - n, te - e) - yaw)
        if 0.5 < d < 18 and abs(rel) < half:
            items.append((d, "tree", rel, tr, None))
    for p in world.plants:
        d = math.hypot(p.e - e, p.n - n)
        rel = wrap_angle(math.atan2(p.n - n, p.e - e) - yaw)
        if 0.8 < d < max_range and abs(rel) < half:
            items.append((d, "plant", rel, 0.0, p))
    visible = []
    for d, kind, rel, r, plant in sorted(items, key=lambda it: -it[0]):
        cx = width / 2 - rel / half * width / 2
        horizon = height * 0.6
        if kind == "tree":
            w = max(2.0, 2 * r / d * width / (2 * math.tan(half)))
            draw.rectangle([cx - w / 2, horizon - 400 / d, cx + w / 2, horizon + 60 / d], fill=(60, 45, 30))
            continue
        size = min(height * 0.9, 260 / d)
        cy = horizon + 30 / d - size / 2
        box = [cx - size / 2, cy - size / 2, cx + size / 2, cy + size / 2]
        leaf = {"Prunus serotina": (40, 95, 40), "Prunus padus": (110, 155, 75)}.get(plant.taxon, (90, 140, 60))
        draw.ellipse(box, fill=leaf, outline=(20, 40, 20))
        if plant.taxon == "Prunus serotina":            # glossy leaves, drooping black raceme
            draw.line([cx - size / 4, cy - size / 5, cx + size / 6, cy - size / 3], fill=(235, 245, 235), width=2)
            for k in range(6):
                draw.ellipse([cx - 3 + k, cy + k * size / 14 - 3, cx + 3 + k, cy + k * size / 14 + 3], fill=(20, 12, 16))
        elif plant.taxon == "Frangula alnus":
            for k in range(4):
                draw.ellipse([cx - size / 3 + k * size / 5, cy, cx - size / 3 + k * size / 5 + 5, cy + 5], fill=(180, 40, 30))
        visible.append(Visible(plant, d, {"cx": round(cx, 1), "cy": round(cy, 1), "w": round(size, 1), "h": round(size, 1)}))
    draw.rectangle([0, 0, width, 14], fill=(0, 0, 0))
    draw.text((4, 2), f"SIMULATED  {caption}", fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=70)
    return buf.getvalue(), visible


def scan_ranges(world: World, e: float, n: float, yaw: float, beams: int = 360, max_range: float = 25.0) -> np.ndarray:
    """2D LiDAR: ray/circle intersection with the trunks (vectorised)."""
    ranges = np.full(beams, np.inf, dtype=np.float32)
    if len(world.trees) == 0:
        return ranges
    rel = world.trees[:, :2] - np.array([e, n])
    near = np.hypot(rel[:, 0], rel[:, 1]) < max_range + 1
    trees, rel = world.trees[near], rel[near]
    if len(trees) == 0:
        return ranges
    ang = yaw + np.linspace(-math.pi, math.pi, beams, endpoint=False)
    d = np.stack([np.cos(ang), np.sin(ang)], axis=1)                   # (B, 2)
    proj = d @ rel.T                                                    # (B, T)
    perp2 = (rel ** 2).sum(axis=1)[None, :] - proj ** 2
    r2 = (trees[:, 2] ** 2)[None, :]
    hit = (proj > 0) & (perp2 <= r2)
    dist = np.where(hit, proj - np.sqrt(np.clip(r2 - perp2, 0, None)), np.inf)
    ranges = dist.min(axis=1).astype(np.float32)
    ranges[ranges > max_range] = np.inf
    return ranges


# -- the rover ---------------------------------------------------------------------------------

class SimRover:
    """Moves on (v, w) commands and emits sensor messages on their own schedules.

    `step(dt, v_cmd, w_cmd, t_ns)` returns a list of (topic, msgtype, header_stamp_ns, arrival_ns, data).
    """

    RATES = {"gnss": 5.0, "gnss_rtk": 5.0, "odom": 20.0, "odom_alt": 10.0, "imu": 50.0, "camera": 3.0,
             "lidar": 10.0, "cmd_vel": 10.0, "truth": 10.0}

    def __init__(self, world: World, seed: int, start_ns: int, rtk: bool = False, lidar_odom: bool = False,
                 detections: bool = False, caption: str = ""):
        self.world, self.rng = world, np.random.default_rng(seed)
        self.t_ns = start_ns
        (e0, n0), (e1, n1) = world.route[0], world.route[1]
        self.e, self.n, self.yaw = float(e0), float(n0), math.atan2(n1 - n0, e1 - e0)
        self.v = self.w = 0.0
        self.distance = 0.0
        self.gnss = GnssModel(self.rng)
        self.rtk = GnssModel(self.rng, rtk=True) if rtk else None
        self.wheel = OdometryModel(self.rng)
        self.lio = OdometryModel(self.rng, lidar=True) if lidar_odom else None
        self.imu = ImuModel(self.rng)
        self.detections, self.caption = detections, caption
        self.next = {k: start_ns for k in self.RATES}
        self.static_sent = False
        self.last_cmd = (0.0, 0.0)

    def truth(self) -> dict:
        lat, lon = from_local(self.e, self.n, *self.world.origin)
        return {"t_ns": self.t_ns, "e": self.e, "n": self.n, "yaw": self.yaw, "lat": lat, "lon": lon}

    def step(self, dt: float, v_cmd: float, w_cmd: float) -> list[tuple]:
        # rover: acceleration-limited unicycle
        self.v += max(-0.8 * dt, min(0.8 * dt, v_cmd - self.v))
        self.w += max(-2.0 * dt, min(2.0 * dt, w_cmd - self.w))
        self.yaw = wrap_angle(self.yaw + self.w * dt)
        self.e += self.v * dt * math.cos(self.yaw)
        self.n += self.v * dt * math.sin(self.yaw)
        self.distance += abs(self.v) * dt
        self.t_ns += int(dt * NS)
        canopy = self.world.canopy(self.e, self.n)
        self.wheel.step(dt, self.v, self.w, canopy)
        if self.lio:
            self.lio.step(dt, self.v, self.w, canopy)
        gyro = self.imu.step(dt, self.w)
        self.last_cmd = (v_cmd, w_cmd)
        out = []
        if not self.static_sent:
            out.append((TOPICS["tf_static"], "tf2_msgs/msg/TFMessage", self.t_ns, self.t_ns, static_tf_msg(self.t_ns)))
            self.static_sent = True
        for kind, rate in self.RATES.items():
            if self.t_ns < self.next[kind]:
                continue
            self.next[kind] += int(NS / rate)
            msg = self._emit(kind, canopy, gyro)
            if msg:
                out.extend(msg)
        return out

    def _emit(self, kind: str, canopy: float, gyro: float) -> list[tuple] | None:
        t, rng = self.t_ns, self.rng
        lag = lambda ms: t + int(rng.uniform(0.5, ms) * 1e6)   # noqa: E731  arrival after the header stamp
        if kind in ("gnss", "gnss_rtk"):
            model = self.gnss if kind == "gnss" else self.rtk
            if model is None:
                return None
            m = model.measure(1 / self.RATES[kind], self.e, self.n, canopy)
            lat, lon = from_local(m["e"], m["n"], *self.world.origin)
            var = m["sigma"] ** 2
            data = {"header": header(t, "gnss_link"), "status": {"status": m["status"], "service": 1},
                    "latitude": lat, "longitude": lon, "altitude": 92.0 + rng.normal(0, 1.5),
                    "position_covariance": [var, 0, 0, 0, var, 0, 0, 0, 4 * var],
                    "position_covariance_type": 2 if m["status"] >= 0 else 0}
            return [(TOPICS[kind], "sensor_msgs/msg/NavSatFix", t, lag(8), data)]
        if kind in ("odom", "odom_alt"):
            model = self.wheel if kind == "odom" else self.lio
            if model is None:
                return None
            v, w = model.take_twist()
            data = {"header": header(t, "odom" if kind == "odom" else "lidar_odom"), "child_frame_id": "base_link",
                    "pose": {"pose": {"position": {"x": model.x, "y": model.y}, "orientation": quat(model.yaw)},
                             "covariance": [0.0] * 36},
                    "twist": {"twist": {"linear": {"x": v}, "angular": {"z": w}}, "covariance": [0.0] * 36}}
            out = [(TOPICS[kind], "nav_msgs/msg/Odometry", t, lag(3), data)]
            if kind == "odom":
                out.append((TOPICS["tf"], "tf2_msgs/msg/TFMessage", t, lag(3), {"transforms": [{
                    "header": header(t, "odom"), "child_frame_id": "base_link",
                    "transform": {"translation": {"x": model.x, "y": model.y}, "rotation": quat(model.yaw)}}]}))
            return out
        if kind == "imu":
            roll, pitch = rng.normal(0, 0.03), rng.normal(0, 0.03)
            data = {"header": header(t, "imu_link"), "orientation": quat(self.imu.yaw, roll, pitch),
                    "orientation_covariance": [0.01, 0, 0, 0, 0.01, 0, 0, 0, 0.05],
                    "angular_velocity": {"x": rng.normal(0, 0.01), "y": rng.normal(0, 0.01), "z": gyro},
                    "angular_velocity_covariance": [2.5e-5, 0, 0, 0, 2.5e-5, 0, 0, 0, 2.5e-5],
                    "linear_acceleration": {"x": rng.normal(0, 0.05), "y": self.v * self.w + rng.normal(0, 0.05),
                                            "z": 9.81 + rng.normal(0, 0.05)},
                    "linear_acceleration_covariance": [0.0025, 0, 0, 0, 0.0025, 0, 0, 0, 0.0025]}
            return [(TOPICS["imu"], "sensor_msgs/msg/Imu", t, lag(2), data)]
        if kind == "camera":
            jpeg, visible = render_frame(self.world, self.e, self.n, self.yaw, t, self.caption)
            out = [(TOPICS["camera"], "sensor_msgs/msg/CompressedImage", t, lag(40),
                    {"header": header(t, "camera_link"), "format": "jpeg", "data": jpeg})]
            if self.detections:
                out.append(self._detections(t, visible))
            return out
        if kind == "lidar":
            ranges = scan_ranges(self.world, self.e, self.n, self.yaw)
            ranges = np.where(np.isfinite(ranges), ranges + rng.normal(0, 0.02, len(ranges)), ranges)
            data = {"header": header(t, "laser"), "angle_min": -math.pi, "angle_max": math.pi,
                    "angle_increment": 2 * math.pi / len(ranges), "time_increment": 0.0, "scan_time": 0.1,
                    "range_min": 0.1, "range_max": 25.0, "ranges": ranges.astype(np.float32), "intensities": []}
            return [(TOPICS["lidar"], "sensor_msgs/msg/LaserScan", t, lag(6), data)]
        if kind == "truth":
            return [(TOPICS["truth"], "nav_msgs/msg/Odometry", t, t, {
                "header": header(t, "sim_world"), "child_frame_id": "base_link",
                "pose": {"pose": {"position": {"x": self.e, "y": self.n}, "orientation": quat(self.yaw)}},
                "twist": {"twist": {"linear": {"x": self.v}, "angular": {"z": self.w}}}})]
        if kind == "cmd_vel":
            v, w = self.last_cmd
            return [(TOPICS["cmd_vel"], "geometry_msgs/msg/Twist", t, t, {"linear": {"x": v}, "angular": {"z": w}})]
        return None

    def _detections(self, t: int, visible: list[Visible]) -> tuple:
        """A mock perception node: noisy class scores for plants in view, published 150 ms after the frame."""
        rng = self.rng
        dets = []
        for vis in visible:
            if vis.plant.taxon == "Prunus serotina":
                p = float(np.clip(rng.beta(8, 2) - 0.03 * vis.distance, 0.05, 0.99))
                other = "prunus_padus"
            else:
                p = float(np.clip(rng.beta(3, 5), 0.02, 0.9))
                other = {"Prunus padus": "prunus_padus", "Frangula alnus": "frangula_alnus"}.get(vis.plant.taxon, "unknown_plant")
            results = [{"hypothesis": {"class_id": "prunus_serotina", "score": round(p, 3)}},
                       {"hypothesis": {"class_id": other, "score": round((1 - p) * 0.8, 3)}}]
            dets.append({"header": header(t, "camera_link"), "results": results, "id": vis.plant.id,
                         "bbox": {"center": {"position": {"x": vis.bbox["cx"], "y": vis.bbox["cy"]}, "theta": 0.0},
                                  "size_x": vis.bbox["w"], "size_y": vis.bbox["h"]}})
        return (TOPICS["detections"], "vision_msgs/msg/Detection2DArray", t, t + int(0.15 * NS),
                {"header": header(t, "camera_link"), "detections": dets})


# -- a scripted operator (for demos and repeatable experiment runs) -------------------------------

class ScriptedOperator:
    """Drives the route like a careful teleoperator: pure pursuit with a small, run-specific
    lateral wander, stopping at plants and reference points to press the mark button."""

    def __init__(self, world: World, rng: np.random.Generator, run: int = 0, speed: float = 0.7,
                 tag_marks: bool = False):
        self.world, self.rng, self.run, self.speed, self.tag_marks = world, rng, run, speed, tag_marks
        self.phase, self.amp = rng.uniform(0, 2 * math.pi), rng.uniform(0.15, 0.45)
        self.stops = [Stop(s.s + rng.uniform(-0.4, 0.4), s.kind, s.target, s.dwell_s) for s in world.stops]
        self.s = 0.0
        self.dwell_until: float | None = None
        self.marked = False
        self.count = 0
        self.done = False

    def command(self, rover: SimRover) -> tuple[float, float, str | None]:
        """(v, w, mark payload or None) for the current rover state."""
        world = self.world
        self.s = world.project(rover.e, rover.n, near_s=self.s)
        t_s = rover.t_ns / NS
        if self.stops and self.dwell_until is None and self.stops[0].s - self.s < 0.15:
            self.dwell_until, self.marked = t_s + self.stops[0].dwell_s, False
        if self.dwell_until is not None:
            payload = None
            if not self.marked and t_s > self.dwell_until - self.stops[0].dwell_s + 1.0:
                payload, self.marked = self._mark(self.stops[0], rover.t_ns), True
            if t_s >= self.dwell_until:
                self.stops.pop(0)
                self.dwell_until = None
            return 0.0, 0.0, payload
        if self.s >= world.length - 0.4 and not self.stops:
            self.done = True
            return 0.0, 0.0, None
        target_s = min(self.s + 2.0, world.length)
        te, tn = world.point_at(target_s)
        lateral = self.amp * math.sin(2 * math.pi * target_s / 35.0 + self.phase)
        tang = world.tangent_at(target_s)
        te, tn = te - lateral * math.sin(tang), tn + lateral * math.cos(tang)
        alpha = wrap_angle(math.atan2(tn - rover.n, te - rover.e) - rover.yaw)
        v = self.speed * max(0.3, math.cos(alpha))
        slow = min(1.0, max(0.25, (self.stops[0].s - self.s) / 2.0)) if self.stops else 1.0
        v *= slow
        w = max(-0.8, min(0.8, 2 * v * math.sin(alpha) / 2.0))
        return v, w, None

    def _mark(self, stop: Stop, t_ns: int) -> str:
        self.count += 1
        if stop.kind == "reference":
            label = f"REF:{stop.target}"
        else:
            plant = next(p for p in self.world.plants if p.id == stop.target)
            guess = "Prunus serotina?" if plant.taxon == "Prunus serotina" else "look-alike? check"
            label = f"PLANT:{plant.id} {guess}" if self.tag_marks else guess
        return json.dumps({"id": f"run{self.run}-{self.count}", "stamp_ns": t_ns, "label": label,
                           "source": "scripted operator (simulated)"})
