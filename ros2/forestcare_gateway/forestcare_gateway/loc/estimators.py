"""Offline localization: GNSS fused with IMU, wheel odometry or LiDAR/visual odometry.

An extended Kalman filter with a Rauch-Tung-Striebel smoother over the state

    x = [east, north, yaw, speed, gnss_bias_east, gnss_bias_north]    metres, radians (ENU), m/s

- Motion: a yaw rate (IMU gyro or odometry) turns the robot; the speed is a slowly varying
  state, measured by odometry when available.
- Correction: a GNSS fix measures position + GNSS bias + noise. Implausible fixes (multipath
  jumps) are gated out.
- GNSS bias: GNSS error under trees is mostly a slowly wandering bias, not independent noise.
  Modelling it (first-order Gauss-Markov, `GNSS_BIAS_TAU_S`, `GNSS_BIAS_SHARE` of the
  reported variance) keeps the claimed uncertainty honest: averaging many fixes cannot make
  the position better than the bias allows.
- Smoothing: missions are processed after the drive, so the smoother uses every fix before
  and after each moment.

This is the textbook 2D model that robot_localization also uses. It is kept in plain numpy
so the experiment is reproducible and each line can be read.

What it can and cannot do: it removes GNSS noise and outliers and bridges outages. It
cannot remove the slowly wandering GNSS bias under canopy, because a biased fix looks just
like a correct one. Measuring how much that matters is the point of the localization
experiment.

Tried and dropped: estimating the odometry scale and the gyro bias as extra states. With
metre-level GNSS the filter cannot tell "slower" from "slightly off heading", the scale
state absorbed heading errors and accuracy got worse; with RTK it gained nothing measurable.
Its claimed uncertainty is still too small with correlated GNSS errors: the experiment
measures a correction (`localization.sigma_scale`) instead of tuning the model by hand.
"""

from __future__ import annotations

import math

import numpy as np

from ..geo import to_local, wrap_angle
from ..localize import Trajectory, fix_sigma, usable_fixes
from ..messages import NS

# method -> (GNSS stream, speed source, yaw-rate source)
SOURCES = {
    "gnss_imu": ("gnss", None, "imu"),
    "gnss_odom": ("gnss", "odom", "odom"),
    "gnss_odom_imu": ("gnss", "odom", "imu"),
    "gnss_altodom": ("gnss", "odom_alt", "odom_alt"),
    "rtk_odom": ("gnss_rtk", "odom", "imu"),
}
YAW_RATE_NOISE = {"imu": 0.02, "odom": 0.06, "odom_alt": 0.02}   # rad/s/sqrt(s): gyro vs. slipping wheels
SPEED_NOISE = {"odom": 0.05, "odom_alt": 0.03}                   # m/s, per speed measurement
POSITION_NOISE = 0.10                                            # m/sqrt(s): slip, bumps, model error
GATE_CHI2 = 13.8                                                 # 99.9 % for 2 degrees of freedom
GATE_RELEASE_S = 3.0                                             # accept fixes again after this long without one
GNSS_BIAS_TAU_S = 120.0                                          # ASSUMPTION: correlation time of GNSS error
GNSS_BIAS_SHARE = 0.8                                            # ASSUMPTION: share of GNSS variance that is bias
D = 6                                                            # state size


def _initial_heading(e: np.ndarray, n: np.ndarray, min_move_m: float = 5.0) -> tuple[float, float]:
    """Course from the first fixes once the robot has moved `min_move_m`; (yaw, sigma)."""
    d = np.hypot(e - e[0], n - n[0])
    moved = np.nonzero(d >= min_move_m)[0]
    if len(moved) == 0:
        return 0.0, math.pi
    k = moved[0]
    return math.atan2(n[k] - n[0], e[k] - e[0]), 0.35


def ekf_rts(times: np.ndarray, kinds: list, values: list, x0: np.ndarray, P0: np.ndarray,
            yaw_rate_noise: float, speed_noise: float | None, speed_measured: bool):
    """Run the filter over time-ordered events and smooth. Returns (times, states, covariances).

    kinds/values: 'w' (yaw rate, rad/s), 'v' (speed, m/s) or 'gnss' ((east, north), reported sigma).
    """
    N = len(times)
    xf, Pf = np.zeros((N, D)), np.zeros((N, D, D))
    xp, Pp, Fs = np.zeros((N, D)), np.zeros((N, D, D)), np.zeros((N, D, D))
    x, P = x0.astype(float).copy(), P0.astype(float).copy()
    w = 0.0
    q_speed = 0.2 if speed_measured else 0.6
    bias_sigma = math.sqrt(P0[4, 4])
    last_fix_t = times[0]
    H = np.zeros((2, D))
    H[0, 0] = H[1, 1] = H[0, 4] = H[1, 5] = 1.0
    eye = np.eye(D)
    t_prev = times[0]
    for k in range(N):
        dt = (times[k] - t_prev) / NS
        t_prev = times[k]
        F = eye.copy()
        if dt > 0:
            e, n, yaw, v, be, bn = x
            c, s = math.cos(yaw), math.sin(yaw)
            a = math.exp(-dt / GNSS_BIAS_TAU_S)
            x = np.array([e + v * dt * c, n + v * dt * s, wrap_angle(yaw + w * dt), v, a * be, a * bn])
            F[0, 2], F[0, 3] = -v * dt * s, dt * c
            F[1, 2], F[1, 3] = v * dt * c, dt * s
            F[4, 4] = F[5, 5] = a
            qb = bias_sigma ** 2 * (1 - a * a)
            Q = np.diag([POSITION_NOISE ** 2 * dt, POSITION_NOISE ** 2 * dt, yaw_rate_noise ** 2 * dt,
                         q_speed ** 2 * dt, qb, qb])
            P = F @ P @ F.T + Q
        xp[k], Pp[k], Fs[k] = x, P, F
        kind, value = kinds[k], values[k]
        if kind == "w":
            w = value
        elif kind == "v":
            R = speed_noise ** 2 + (0.03 * value) ** 2          # + 3 %: wheel slip, scale error
            K = P[:, 3] / (P[3, 3] + R)
            x = x + K * (value - x[3])
            P = P - np.outer(K, P[3, :])
        elif kind == "gnss":
            z, sigma = value
            bias_sigma = math.sqrt(GNSS_BIAS_SHARE) * sigma
            for i in (4, 5):             # fix quality improved (e.g. RTK fixed): the old bias no longer applies
                if P[i, i] > bias_sigma ** 2:
                    f = bias_sigma / math.sqrt(P[i, i])
                    x[i] *= f
                    P[i, :] *= f
                    P[:, i] *= f
            r_white = (1 - GNSS_BIAS_SHARE) * sigma ** 2
            y = z - H @ x
            S = H @ P @ H.T + np.eye(2) * r_white
            d2 = float(y @ np.linalg.solve(S, y))
            if d2 <= GATE_CHI2 or (times[k] - last_fix_t) / NS > GATE_RELEASE_S:
                K = P @ H.T @ np.linalg.inv(S)
                x = x + K @ y
                x[2] = wrap_angle(x[2])
                IKH = eye - K @ H
                P = IKH @ P @ IKH.T + K @ (np.eye(2) * r_white) @ K.T      # Joseph form
                last_fix_t = times[k]
        xf[k], Pf[k] = x, P
    # Rauch-Tung-Striebel smoother
    xs, Ps = xf.copy(), Pf.copy()
    for k in range(N - 2, -1, -1):
        C = Pf[k] @ Fs[k + 1].T @ np.linalg.inv(Pp[k + 1])
        dx = xs[k + 1] - xp[k + 1]
        dx[2] = wrap_angle(dx[2])
        xs[k] = xf[k] + C @ dx
        xs[k, 2] = wrap_angle(xs[k, 2])
        Ps[k] = Pf[k] + C @ (Ps[k + 1] - Pp[k + 1]) @ C.T
    return times, xs, Ps


def fused_trajectory(method: str, cfg: dict, streams: dict, output_hz: float = 10.0) -> Trajectory | None:
    gnss_stream, speed_src, rate_src = SOURCES[method]
    if method == "rtk_odom" and not streams.get("imu"):
        rate_src = "odom"
    fixes = usable_fixes(streams[gnss_stream], cfg["gnss"])
    if len(fixes) < 2:
        return None
    for src in {speed_src, rate_src} - {None}:
        if not streams.get(src):
            raise ValueError(f"'{method}' needs {src} data, but this mission has none "
                             f"(topic {cfg['topics'].get(src) or '(not configured)'!r})")
    origin = (fixes[0].lat, fixes[0].lon)
    en = np.array([to_local(f.lat, f.lon, *origin) for f in fixes])
    yaw0, yaw_sigma = _initial_heading(en[:, 0], en[:, 1])
    events: list[tuple[int, int, str, object]] = []
    for f, (e, n) in zip(fixes, en):
        events.append((f.stamp_ns, 2, "gnss", (np.array([e, n]), fix_sigma(f, cfg["gnss"]))))
    for r in streams[rate_src]:
        events.append((r["stamp_ns"], 0, "w", float(r["wz"] if rate_src == "imu" else r["w"])))
    if speed_src:
        for r in streams[speed_src]:
            events.append((r["stamp_ns"], 1, "v", float(r["v"])))
    t0, t1 = fixes[0].stamp_ns, fixes[-1].stamp_ns
    events = sorted((ev for ev in events if t0 <= ev[0] <= t1), key=lambda ev: (ev[0], ev[1]))
    times = np.array([ev[0] for ev in events], dtype=np.int64)
    s0 = fix_sigma(fixes[0], cfg["gnss"])
    x0 = np.array([en[0, 0], en[0, 1], yaw0, 0.0, 0.0, 0.0])
    sb2 = GNSS_BIAS_SHARE * s0 ** 2
    P0 = np.diag([s0 ** 2, s0 ** 2, yaw_sigma ** 2, 0.5 ** 2, sb2, sb2])
    P0[0, 4] = P0[4, 0] = P0[1, 5] = P0[5, 1] = -sb2    # first fix = position + bias: their errors are anti-correlated
    t, xs, Ps = ekf_rts(times, [ev[2] for ev in events], [ev[3] for ev in events], x0, P0,
                        YAW_RATE_NOISE[rate_src], SPEED_NOISE.get(speed_src), speed_src is not None)
    keep, last = [], None                  # thin to output_hz for the mission track and lookups
    for i, ti in enumerate(t):
        if last is None or ti - last >= NS / output_hz or i == len(t) - 1:
            keep.append(i)
            last = ti
    sigma = np.sqrt(np.maximum((Ps[keep, 0, 0] + Ps[keep, 1, 1]) / 2, 1e-6))
    info = [{"fused": method} for _ in keep]
    return Trajectory(method, origin, t[keep].tolist(), xs[keep, 0].tolist(), xs[keep, 1].tolist(),
                      sigma.tolist(), xs[keep, 2].tolist(), info, max_gap_s=2.0)


def dead_reckoning(cfg: dict, streams: dict, source: str = "odom",
                   start: tuple[int, float, float, float] | None = None) -> Trajectory | None:
    """Odometry alone (drift study). Starts at `start` = (t_ns, lat, lon, ENU yaw) if given (e.g. the
    true pose in a simulation), otherwise at the first GNSS fix with the initial GNSS course."""
    fixes = usable_fixes(streams["gnss"], cfg["gnss"])
    odom = streams.get(source) or []
    if len(fixes) < 2 or not odom:
        return None
    if start is not None:
        t0, lat0, lon0, yaw = start
        origin = (lat0, lon0)
    else:
        origin, t0 = (fixes[0].lat, fixes[0].lon), fixes[0].stamp_ns
        en = np.array([to_local(f.lat, f.lon, *origin) for f in fixes])
        yaw, _ = _initial_heading(en[:, 0], en[:, 1])
    samples = [r for r in odom if r["stamp_ns"] >= t0]
    if not samples:
        return None
    e = n = 0.0
    ts, es, ns, yaws = [samples[0]["stamp_ns"]], [0.0], [0.0], [yaw]
    for a, b in zip(samples, samples[1:]):
        dt = (b["stamp_ns"] - a["stamp_ns"]) / NS
        yaw = wrap_angle(yaw + a["w"] * dt)
        e += a["v"] * dt * math.cos(yaw)
        n += a["v"] * dt * math.sin(yaw)
        ts.append(b["stamp_ns"])
        es.append(e)
        ns.append(n)
        yaws.append(yaw)
    return Trajectory(f"{source}_only", origin, ts, es, ns, [float("nan")] * len(ts), yaws,
                      [{} for _ in ts], max_gap_s=2.0)
