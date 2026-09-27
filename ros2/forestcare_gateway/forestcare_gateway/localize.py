"""Where was the robot at time t, and how sure are we?

`Trajectory` is a time-indexed robot track in a local east/north frame with a per-sample
position uncertainty. It is built either from GNSS fixes alone ("gnss", "rtk") or by one
of the fusion estimators in loc/estimators.py (GNSS + IMU / wheel odometry / LiDAR or
visual odometry). The mission builder only calls `at(t)` and `samples()`, so choosing a
localization method is a one-line config change (`localization.method`).
"""

from __future__ import annotations

import math
from bisect import bisect_left
from dataclasses import dataclass, field

from .geo import from_local, to_local
from .messages import NS, GnssFix

METHODS = {
    "gnss": "GNSS only",
    "rtk": "RTK GNSS only (second receiver)",
    "gnss_imu": "GNSS + IMU yaw rate (EKF smoother)",
    "gnss_odom": "GNSS + wheel odometry (EKF smoother)",
    "gnss_odom_imu": "GNSS + wheel odometry + IMU yaw rate (EKF smoother)",
    "gnss_altodom": "GNSS + LiDAR/visual odometry (EKF smoother)",
    "rtk_odom": "RTK GNSS + wheel odometry (EKF smoother)",
}


@dataclass
class Pose:
    t_ns: int
    lat: float
    lon: float
    e: float
    n: float
    sigma_m: float                 # horizontal, per axis, 1 sigma
    yaw: float | None              # ENU radians (counter-clockwise from east); None if unknown
    heading_source: str
    info: dict = field(default_factory=dict)


class Trajectory:
    def __init__(self, method: str, origin: tuple[float, float], t_ns: list[int], e: list[float], n: list[float],
                 sigma: list[float], yaw: list[float] | None = None, info: list[dict] | None = None,
                 max_gap_s: float = 2.0):
        self.method, self.origin = method, origin
        self.t, self.e, self.n, self.sigma = list(t_ns), list(e), list(n), list(sigma)
        self.yaw = list(yaw) if yaw is not None else None
        self.info = info or [{} for _ in self.t]
        self.max_gap_ns = int(max_gap_s * NS)

    def __len__(self) -> int:
        return len(self.t)

    def _interp(self, t_ns: int):
        """(e, n, sigma, alpha, i0, i1) or None if t is outside the data or inside a gap."""
        if not self.t:
            return None
        i = bisect_left(self.t, t_ns)
        if i < len(self.t) and self.t[i] == t_ns:
            return self.e[i], self.n[i], self.sigma[i], 0.0, i, i
        a, b = i - 1, i
        near_a = a >= 0 and t_ns - self.t[a] <= self.max_gap_ns
        near_b = b < len(self.t) and self.t[b] - t_ns <= self.max_gap_ns
        if near_a and near_b:
            alpha = (t_ns - self.t[a]) / (self.t[b] - self.t[a])
            e = self.e[a] + alpha * (self.e[b] - self.e[a])
            n = self.n[a] + alpha * (self.n[b] - self.n[a])
            return e, n, max(self.sigma[a], self.sigma[b]), alpha, a, b
        if near_a or near_b:        # at the start or end of the data: use the nearest sample, no extrapolation
            k = a if near_a else b
            return self.e[k], self.n[k], self.sigma[k], 0.0, k, k
        return None

    def heading(self, t_ns: int, window_s: float = 4.0, min_move_m: float = 2.0,
                look_back_s: float = 30.0) -> tuple[float | None, str]:
        """Course over ground from the positions shortly before and after t.

        Position noise makes this meaningless when the robot barely moves, so the movement
        must exceed the position uncertainty; at a standstill (e.g. while the operator marks
        a plant) the heading of the last real movement is used.
        """
        w = int(window_s * NS)
        for back_s in range(0, int(look_back_s) + 1):
            c = t_ns - back_s * NS
            p0, p1 = self._interp(c - w), self._interp(c + (w if back_s == 0 else 0))
            if p0 is None or p1 is None:
                continue
            de, dn = p1[0] - p0[0], p1[1] - p0[1]
            if math.hypot(de, dn) >= max(min_move_m, p0[2], p1[2]):
                return math.atan2(dn, de), ("course over ground" if back_s == 0 else
                                            f"course over ground {back_s} s earlier (robot was standing)")
        return None, "unknown"

    def at(self, t_ns: int) -> Pose | None:
        p = self._interp(t_ns)
        if p is None:
            return None
        e, n, sigma, alpha, i0, i1 = p
        if self.yaw is not None:
            y0, y1 = self.yaw[i0], self.yaw[i1]
            yaw = y0 + alpha * math.atan2(math.sin(y1 - y0), math.cos(y1 - y0))
            source = self.method
        else:
            yaw, source = self.heading(t_ns)
        lat, lon = from_local(e, n, *self.origin)
        info = {**self.info[i0], "sample_dt_s": round(abs(t_ns - self.t[i0 if alpha < 0.5 else i1]) / NS, 3),
                "interpolated": i0 != i1}
        return Pose(t_ns, lat, lon, e, n, sigma, yaw, source if yaw is not None else "unknown", info)

    def samples(self) -> list[tuple[int, float, float]]:
        return [(t, *from_local(e, n, *self.origin)) for t, e, n in zip(self.t, self.e, self.n)]


def usable_fixes(fixes: list[GnssFix], gcfg: dict) -> list[GnssFix]:
    ok = [f for f in fixes if f.valid and f.status >= gcfg["min_status"]]
    return sorted(ok, key=lambda f: f.stamp_ns)


def fix_sigma(f: GnssFix, gcfg: dict) -> float:
    return (f.sigma_m or gcfg["assumed_sigma_m"]) * gcfg["accuracy_scale"]


def gnss_trajectory(fixes: list[GnssFix], gcfg: dict, method: str = "gnss",
                    origin: tuple[float, float] | None = None) -> Trajectory | None:
    ok = usable_fixes(fixes, gcfg)
    if not ok:
        return None
    origin = origin or (ok[0].lat, ok[0].lon)
    en = [to_local(f.lat, f.lon, *origin) for f in ok]
    info = [{"gnss_stamp_ns": f.stamp_ns, "gnss_status": f.status, "gnss_service": f.service,
             "covariance_type": f.covariance_type, "reported_sigma_m": f.sigma_m} for f in ok]
    return Trajectory(method, origin, [f.stamp_ns for f in ok], [p[0] for p in en], [p[1] for p in en],
                      [fix_sigma(f, gcfg) for f in ok], None, info, gcfg["max_fix_gap_s"])


def build_trajectory(cfg: dict, streams: dict) -> Trajectory | None:
    """streams: 'gnss', 'gnss_rtk' (lists of GnssFix), 'odom', 'odom_alt', 'imu' (lists of dicts)."""
    method = cfg["localization"]["method"]
    if method not in METHODS:
        raise ValueError(f"unknown localization method {method!r}; choose one of {', '.join(METHODS)}")
    if method == "gnss":
        traj = gnss_trajectory(streams["gnss"], cfg["gnss"])
    elif method == "rtk":
        traj = gnss_trajectory(streams["gnss_rtk"], cfg["gnss"], method="rtk")
    else:
        from .loc.estimators import fused_trajectory  # numpy-based; only needed for fusion methods

        traj = fused_trajectory(method, cfg, streams)
    scale = float(cfg["localization"].get("sigma_scale") or 1.0)
    if traj is not None and scale != 1.0:      # calibrated in the localization experiment
        traj.sigma = [s * scale for s in traj.sigma]
    return traj
