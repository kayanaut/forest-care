"""The repeated-run localization experiment.

One question: can the rover place repeated observations close enough that Forest Care
recognises the same plant stand in every run?

Input: an experiment file (YAML) naming the recorded runs (rosbags), the surveyed reference
points, optional surveyed plant positions and open-sky zones, and the methods to compare.
Every run is converted once; every localization method is then applied to the same data,
so the comparison is fair. See docs/LOCALIZATION_VALIDATION.md.
"""

from __future__ import annotations

import copy
import datetime as dt
import itertools
import json
import math
import shutil
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..assemble import assemble_mission, read_streams
from ..bagio import convert_bag
from ..config import DEFAULTS, load_config, merge
from ..geo import from_local, haversine_m, to_local
from ..localize import METHODS, build_trajectory
from ..messages import NS
from ..package import MissionPackage
from .estimators import dead_reckoning
from .standmatch import LINK_SIGMA_FACTOR, Obs, link_distance, link_stands, match_report

ORDER = ["gnss", "gnss_imu", "gnss_odom", "gnss_odom_imu", "gnss_altodom", "rtk", "rtk_odom"]   # simplest first
HARDWARE = {"gnss": "GNSS receiver only", "gnss_imu": "GNSS + IMU", "gnss_odom": "GNSS + wheel odometry",
            "gnss_odom_imu": "GNSS + wheel odometry + IMU", "gnss_altodom": "GNSS + LiDAR/visual odometry",
            "rtk": "RTK GNSS (SAPOS corrections)", "rtk_odom": "RTK GNSS + wheel odometry + IMU"}
NEEDS = {"gnss": {"gnss"}, "gnss_imu": {"gnss", "imu"}, "gnss_odom": {"gnss", "odom"},    # sensors per method
         "gnss_odom_imu": {"gnss", "odom", "imu"}, "gnss_altodom": {"gnss", "odom_alt"},
         "rtk": {"rtk"}, "rtk_odom": {"rtk", "odom", "imu"}}
DEFAULT_CRITERIA = {
    "reid_rate": 0.95,               # share of tagged plants whose repeat observations all join one stand
    "coverage_95": 0.90,             # share of true errors inside the claimed 95 % circle (after scaling)
    "required_separation_m": 20.0,   # two stands this far apart must stay separate
}
TRACK_SMOOTH_S = 10.0                # tracks are smoothed over this time window before comparing runs
CIRCLE_95 = 2.45                     # radius of the 95 % circle in sigmas (2D normal distribution)


@dataclass
class Run:
    name: str
    pkg: MissionPackage
    cfg: dict
    streams: dict
    truth: dict | None       # {'t': ns array, 'lat': array, 'lon': array, 'plants': {...}} (simulation only)


# -- helpers ---------------------------------------------------------------------------------

def _q(values, p) -> float | None:
    return None if len(values) == 0 else round(float(np.percentile(np.asarray(values, dtype=float), p)), 2)


def _stats(values) -> dict:
    v = np.asarray([x for x in values if x is not None and math.isfinite(x)], dtype=float)
    return {"n": int(len(v)), "median": _q(v, 50), "p95": _q(v, 95), "max": None if len(v) == 0 else round(float(v.max()), 2)}


def _point_in_ring(x: float, y: float, ring) -> bool:
    inside, j = False, len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _dist_to_ring(x: float, y: float, ring) -> float:
    best = math.inf
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        dx, dy = x2 - x1, y2 - y1
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / L2))
        best = min(best, math.hypot(x - (x1 + t * dx), y - (y1 + t * dy)))
    return best


class CanopyMap:
    """'open' inside an open-sky zone, 'partial' within `edge_m` of one, else 'closed'."""

    def __init__(self, exp: dict, origin: tuple[float, float], edge_m: float = 6.0):
        self.origin, self.edge_m = origin, edge_m
        self.rings = [[to_local(lat, lon, *origin) for lon, lat in ring] for ring in (exp.get("canopy_zones") or {}).get("open", [])]

    def classify(self, lat: float, lon: float) -> str:
        if not self.rings:
            return "unknown"
        x, y = to_local(lat, lon, *self.origin)
        if any(_point_in_ring(x, y, r) for r in self.rings):
            return "open"
        return "partial" if min(_dist_to_ring(x, y, r) for r in self.rings) < self.edge_m else "closed"


def _resample_path(xy: np.ndarray, step: float = 1.0) -> np.ndarray:
    if len(xy) < 2:
        return xy
    seg = np.hypot(*np.diff(xy, axis=0).T)
    s = np.concatenate([[0.0], np.cumsum(seg)])
    grid = np.arange(0.0, s[-1], step)
    return np.column_stack([np.interp(grid, s, xy[:, 0]), np.interp(grid, s, xy[:, 1])])


def _dist_to_path(points: np.ndarray, path: np.ndarray) -> np.ndarray:
    a, b = path[:-1], path[1:]
    ab = b - a
    L2 = np.maximum((ab ** 2).sum(axis=1), 1e-12)
    out = np.empty(len(points))
    for i, p in enumerate(points):
        t = np.clip(((p - a) * ab).sum(axis=1) / L2, 0, 1)
        out[i] = np.min(np.hypot(*(a + t[:, None] * ab - p).T))
    return out


def _smooth_track(t_s: np.ndarray, xy: np.ndarray, window_s: float) -> np.ndarray:
    """Moving average over a time window (a window in metres would shrink where the track jitters)."""
    c = np.vstack([np.zeros((1, 2)), np.cumsum(xy, axis=0)])
    lo = np.searchsorted(t_s, t_s - window_s / 2)
    hi = np.searchsorted(t_s, t_s + window_s / 2, side="right")
    return (c[hi] - c[lo]) / (hi - lo)[:, None]


def track_offsets(tracks: list[tuple[np.ndarray, np.ndarray]], smooth_s: float = TRACK_SMOOTH_S) -> list[float]:
    """Distances from each run's track to the nearest point of every other run's track.
    tracks: (times in s, positions (N, 2) in m) per run.

    The tracks are smoothed first: a jittery track zig-zags through a wide band, so the
    nearest point of it is always close and raw GNSS would look more repeatable than it is.
    What remains is the systematic shift between runs (GNSS bias), which is what moves a
    plant from one run to the next; per-fix jitter shows up in the observation distances."""
    paths = [_resample_path(_smooth_track(t, xy, smooth_s), 1.0) for t, xy in tracks]
    return [float(d) for a, b in itertools.permutations(range(len(paths)), 2)
            for d in _dist_to_path(paths[a][::2], paths[b])]


# -- loading ---------------------------------------------------------------------------------

def load_experiment(path) -> dict:
    import yaml

    path = Path(path)
    exp = yaml.safe_load(path.read_text()) or {}
    exp["_dir"] = path.parent
    return exp


def _resolve(exp: dict, p) -> Path:
    p = Path(p).expanduser()
    return p if p.is_absolute() else exp["_dir"] / p


def gateway_config(exp: dict) -> dict:
    g = exp.get("gateway_config") or {}
    return load_config(_resolve(exp, g)) if isinstance(g, str) else merge(DEFAULTS, g)


def prepare_runs(exp: dict, workdir: Path, log=print) -> list[Run]:
    cfg = gateway_config(exp)
    packages = Path(workdir) / "packages"
    shutil.rmtree(packages, ignore_errors=True)          # generated output: rebuilt every time
    runs = []
    for i, spec in enumerate(exp["runs"], 1):
        name = spec.get("name") or f"run{i}"
        bag = _resolve(exp, spec["bag"])
        run_cfg = merge(cfg, {"outbox_dir": str(packages / name), "localization": {"method": "gnss"}})
        log(f"{name}: converting {bag}")
        result = convert_bag(bag, run_cfg, log=lambda *_: None)
        pkg = MissionPackage(result["package"])
        truth = None
        gt = spec.get("ground_truth") or (exp.get("ground_truth") and str(bag / exp["ground_truth"]))
        if gt and _resolve(exp, gt).exists():
            data = json.loads(_resolve(exp, gt).read_text())
            tr = data["trajectory"]
            truth = {"t": np.array([p["t_ns"] for p in tr], dtype=np.int64), "lat": np.array([p["lat"] for p in tr]),
                     "lon": np.array([p["lon"] for p in tr]), "yaw": np.array([p["yaw"] for p in tr]),
                     "plants": {p["id"]: p for p in data.get("plants", [])}}
        runs.append(Run(name, pkg, pkg.read_json("header.json")["config"], read_streams(pkg), truth))
    return runs


def resolvable_separation(errors: np.ndarray, sigmas: np.ndarray, max_merge: float = 0.05) -> float | None:
    """Smallest distance at which two stands stay separate under Forest Care's linking rule in
    >= 95 % of cases, given the measured placement errors (2D vectors, metres) and sigmas.

    Every pair of measured errors is applied to two virtual plants `d` metres apart, in 8
    directions; the pair is merged if the placed points are within the link distance."""
    if len(errors) < 2:
        return None
    from .standmatch import LINK_SIGMA_FACTOR, STAND_SPREAD_M

    i, j = np.array([(a, b) for a in range(len(errors)) for b in range(len(errors)) if a != b]).T
    diff = errors[j] - errors[i]
    link = STAND_SPREAD_M + LINK_SIGMA_FACTOR * np.hypot(sigmas[i], sigmas[j])
    angles = np.linspace(0, 2 * math.pi, 8, endpoint=False)
    for d in np.arange(0.0, 80.0, 0.5):
        placed = np.hypot(d * np.cos(angles)[:, None] + diff[None, :, 0], d * np.sin(angles)[:, None] + diff[None, :, 1])
        if np.mean(placed <= link[None, :]) <= max_merge:
            return float(d)
    return None


def _tagged_observations(mission: dict, run: Run, plant_order: list[str] | None) -> list[Obs]:
    marks = sorted((o for o in mission["observations"] if (o.get("metadata") or {}).get("mark")),
                   key=lambda o: o["observed_at"])
    out = []
    for k, o in enumerate(marks):
        tag = o["metadata"]["mark"].get("tag") or (plant_order[k] if plant_order and k < len(plant_order) else "")
        t_ns = int(dt.datetime.fromisoformat(o["observed_at"]).timestamp() * NS)
        out.append(Obs(f"{run.name}/{o['uid']}", t_ns, o["lat"], o["lon"], o["gnss_accuracy_m"], tag,
                       o["metadata"]["localization"]["robot_sigma_m"]))
    return out


def placement_sigma_needed(errors: np.ndarray, robot_sigmas: np.ndarray, coverage: float) -> float | None:
    """Smallest camera.placement_sigma_m for which `coverage` of the observation errors fall inside
    the 95 % circle the dashboard draws around each observation."""
    if len(errors) < 5:
        return None
    for p in np.arange(0.5, 15.01, 0.25):         # 2 % margin: sigmas are rounded to centimetres in the mission
        if np.mean(errors * 1.02 <= CIRCLE_95 * np.hypot(robot_sigmas, p)) >= coverage:
            return float(p)
    return None


# -- evaluation ------------------------------------------------------------------------------------

def evaluate(exp: dict, runs: list[Run], log=print) -> dict:
    refs = exp.get("references") or {}
    criteria = {**DEFAULT_CRITERIA, **(exp.get("criteria") or {})}
    first_fix = next(f for f in runs[0].streams["gnss"] if f.valid)
    origin = next(((r["lat"], r["lon"]) for r in refs.values()), (first_fix.lat, first_fix.lon))
    canopy = CanopyMap(exp, origin)
    plants = {**{k: v for r in runs if r.truth for k, v in r.truth["plants"].items()}, **(exp.get("plants") or {})}
    result = {"experiment": exp.get("name", "localization experiment"),
              "simulated": any(r.truth or r.cfg.get("source_kind") == "simulated" for r in runs),
              "runs": [r.name for r in runs], "criteria": criteria, "origin": origin,
              "current_method": exp.get("current_method") or gateway_config(exp)["localization"]["method"],
              "methods": {}, "gnss": gnss_quality(runs, canopy), "drift": drift(runs)}
    for m in [m for m in (exp.get("methods") or ORDER) if m in METHODS]:
        log(f"method {m} ...")
        result["methods"][m] = evaluate_method(m, runs, refs, plants, canopy, criteria, exp.get("plant_order"), origin)
    result["recommendation"] = recommend(result, criteria)
    return result


def _apply(method: str, runs: list[Run], settings: dict | None = None, trajs: list | None = None):
    """Trajectory and Forest Care mission of every run with one localization method (and optional
    corrected settings: localization.sigma_scale, camera.placement_sigma_m). With `trajs`, the
    given trajectories are used as they are."""
    over = merge({"localization": {"method": method}}, settings or {})
    out_t, out_m = [], []
    for i, run in enumerate(runs):
        traj = trajs[i] if trajs else build_trajectory(merge(run.cfg, over), run.streams)
        if traj is None:
            raise ValueError("no usable fixes for this method")
        mission, _, _ = assemble_mission(run.pkg, over, streams=run.streams, traj=traj)
        out_t.append(traj)
        out_m.append(mission)
    return out_t, out_m


def _position_errors(runs: list[Run], trajs: list, missions: list, refs: dict, canopy: CanopyMap) -> dict:
    """Robot position errors at the surveyed reference points and, if known, along the true
    trajectory; and whether the claimed uncertainty covers them."""
    ref_err, ref_norm, ref_by_canopy = [], [], {}
    for mission in missions:
        for mark in mission["metadata"]["reference_marks"]:
            ref = refs.get(mark["tag"])
            if not ref or mark["lat"] is None:
                continue
            e = haversine_m(mark["lat"], mark["lon"], ref["lat"], ref["lon"])
            ref_err.append(e)
            ref_norm.append(e / max(mark["sigma_m"], 1e-3))
            ref_by_canopy.setdefault(ref.get("canopy") or canopy.classify(ref["lat"], ref["lon"]), []).append(e)
    ate, ate_norm, ate_by_canopy, along = [], [], {}, []
    for run, traj in zip(runs, trajs):
        if not run.truth:
            continue
        tt = run.truth
        for i in range(0, len(tt["t"]), 5):                # truth every 0.2 s -> every 1 s
            pose = traj.at(int(tt["t"][i]))
            if pose is None:
                continue
            e = haversine_m(pose.lat, pose.lon, tt["lat"][i], tt["lon"][i])
            ate.append(e)
            ate_norm.append(e / max(pose.sigma_m, 1e-3))
            ate_by_canopy.setdefault(canopy.classify(tt["lat"][i], tt["lon"][i]), []).append(e)
            along.append([run.name, round((int(tt["t"][i]) - int(tt["t"][0])) / NS, 1), round(e, 2)])
    norm = ate_norm or ref_norm            # the true trajectory if known, else the reference points
    return {"reference_error_m": _stats(ref_err),
            "reference_error_by_canopy": {k: _stats(v) for k, v in ref_by_canopy.items()},
            "trajectory_error_m": _stats(ate),
            "trajectory_error_by_canopy": {k: _stats(v) for k, v in ate_by_canopy.items()},
            "coverage_95": None if not norm else round(float(np.mean(np.asarray(norm) <= CIRCLE_95)), 3),
            # factor that puts 95 % of the errors inside the 95 % circle (never below 1: stay cautious)
            "scale_needed": None if not norm else max(1.0, math.ceil(100 * float(np.percentile(norm, 95)) / CIRCLE_95) / 100),
            "error_along_route": along[::3]}


def _rescaled(traj, factor: float):
    """The same trajectory with its uncertainty multiplied by `factor`."""
    out = copy.copy(traj)
    out.sigma = [s * factor for s in traj.sigma]
    return out


def _observation_errors(obs: list[Obs], plants: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Distance of each tagged observation to its surveyed/true plant, its sigma, its robot sigma."""
    known = [o for o in obs if o.tag in plants]
    return (np.array([haversine_m(o.lat, o.lon, plants[o.tag]["lat"], plants[o.tag]["lon"]) for o in known]),
            np.array([o.sigma for o in known]), np.array([o.robot_sigma or 0.0 for o in known]))


def evaluate_method(method, runs, refs, plants, canopy, criteria, plant_order, origin) -> dict:
    try:
        trajs, missions = _apply(method, runs)
    except ValueError as exc:
        return {"available": False, "reason": str(exc), "hardware": HARDWARE[method]}

    # 1. position errors. If the claimed uncertainty is too small, correct it (localization.sigma_scale)
    #    and redo the missions: stand matching must be judged with the uncertainty Forest Care will show.
    base = float(runs[0].cfg["localization"].get("sigma_scale") or 1.0)
    scale = base
    placement = float(runs[0].cfg["camera"]["placement_sigma_m"])
    settings: dict = {}
    pos = first = _position_errors(runs, trajs, missions, refs, canopy)
    if pos["scale_needed"] and pos["scale_needed"] > 1.0:
        scale = round(base * pos["scale_needed"], 2)
        settings["localization"] = {"sigma_scale": scale}
        trajs = [_rescaled(t, pos["scale_needed"]) for t in trajs]
        trajs, missions = _apply(method, runs, settings, trajs=trajs)
        pos = _position_errors(runs, trajs, missions, refs, canopy)

    # 2. the observation circles (robot + camera placement): do they contain the plant? (needs plant positions)
    obs = [o for run, mission in zip(runs, missions) for o in _tagged_observations(mission, run, plant_order)]
    errs, sig, robot_sig = _observation_errors(obs, plants)
    obs_coverage_first = None if not len(errs) else round(float(np.mean(errs <= CIRCLE_95 * sig)), 3)
    need = placement_sigma_needed(errs, robot_sig, criteria["coverage_95"])
    if need and need > placement:
        placement = need
        settings["camera"] = {"placement_sigma_m": placement}
        trajs, missions = _apply(method, runs, settings, trajs=trajs)
        obs = [o for run, mission in zip(runs, missions) for o in _tagged_observations(mission, run, plant_order)]
        errs, sig, robot_sig = _observation_errors(obs, plants)
    obs_coverage = None if not len(errs) else round(float(np.mean(errs <= CIRCLE_95 * sig)), 3)

    # 3. run-to-run offset of the (smoothed) tracks; the true paths give the operator's own variation
    tracks = []
    for traj in trajs:
        samples = traj.samples()
        tracks.append((np.array([s[0] for s in samples]) / NS, np.array([to_local(s[1], s[2], *origin) for s in samples])))
    offsets = track_offsets(tracks)
    truths = [r.truth for r in runs if r.truth]
    true_offsets = track_offsets([(t["t"] / NS, np.array([to_local(la, lo, *origin) for la, lo in zip(t["lat"], t["lon"])]))
                                  for t in truths]) if len(truths) == len(runs) else []

    # 4. the same tagged plant across runs, and stand matching with Forest Care's rule
    by_tag: dict[str, list[Obs]] = {}
    for o in obs:
        if o.tag:
            by_tag.setdefault(o.tag, []).append(o)
    pairs = [(haversine_m(a.lat, a.lon, b.lat, b.lon), link_distance(a.sigma, b.sigma))
             for group in by_tag.values() for a, b in itertools.combinations(group, 2)]
    stands = link_stands(obs)
    match = match_report(obs, stands)
    sigmas = [o.sigma for o in obs] or [0.0]
    link_med = link_distance(float(np.median(sigmas)), float(np.median(sigmas)))
    # placement error vectors: against the surveyed/true plant if known, else against the mean of its repeats
    vectors, vec_sigma = [], []
    for tag, group in by_tag.items():
        if tag in plants:
            cx, cy = to_local(plants[tag]["lat"], plants[tag]["lon"], *origin)
        else:
            cx, cy = np.array([to_local(o.lat, o.lon, *origin) for o in group]).mean(axis=0)
        for o in group:
            x, y = to_local(o.lat, o.lon, *origin)
            vectors.append((x - cx, y - cy))
            vec_sigma.append(o.sigma)
    separation = resolvable_separation(np.array(vectors), np.array(vec_sigma))
    reid_rate = None if not match["plants"] else round(match["reidentified"] / match["plants"], 3)

    coverages = [c for c in (pos["coverage_95"], obs_coverage) if c is not None]
    checks = {
        "reidentification": reid_rate is not None and reid_rate >= criteria["reid_rate"],
        "separation": separation is not None and separation <= criteria["required_separation_m"],
        "honest_uncertainty": bool(coverages) and min(coverages) >= criteria["coverage_95"],
    }
    return {
        "available": True, "hardware": HARDWARE[method], "label": METHODS[method],
        **{k: v for k, v in pos.items() if k != "scale_needed"},
        "coverage_95_uncorrected": first["coverage_95"], "observation_coverage_95": obs_coverage,
        "observation_coverage_95_uncorrected": obs_coverage_first,
        "settings": settings, "sigma_scale": scale, "placement_sigma_m": placement,
        "claimed_sigma_m": _stats([s for t in trajs for s in t.sigma]),
        "track_offset_m": _stats(offsets), "true_track_offset_m": _stats(true_offsets) if true_offsets else None,
        "observation_repeat_m": _stats([d for d, _ in pairs]),
        "repeat_pairs": [[round(d, 2), round(link, 2)] for d, link in pairs],
        "observation_to_plant_m": _stats(errs), "observation_sigma_m": _stats(sigmas),
        "stand_link_distance_m": round(link_med, 1),
        "stands": match, "reid_rate": reid_rate, "separation_needed_m": separation, "checks": checks,
        "sufficient": all(checks.values()),
        "tracks": [[[round(x, 2), round(y, 2)] for x, y in _resample_path(xy)[::2]] for _, xy in tracks],
        "observations": [{"run": o.key.split("/")[0], "tag": o.tag, "xy": [round(v, 2) for v in to_local(o.lat, o.lon, *origin)],
                          "sigma": round(o.sigma, 2), "stand": stands[o.key]} for o in obs],
    }


def gnss_quality(runs: list[Run], canopy: CanopyMap) -> dict:
    """Per receiver and canopy class: fix availability, reported accuracy, true error (if known)."""
    out = {}
    for stream in ("gnss", "gnss_rtk"):
        classes: dict[str, dict] = {}
        for run in runs:
            for f in run.streams.get(stream) or []:
                lat, lon = f.lat, f.lon
                err = None
                if run.truth is not None:
                    i = int(np.clip(np.searchsorted(run.truth["t"], f.stamp_ns), 0, len(run.truth["t"]) - 1))
                    lat, lon = run.truth["lat"][i], run.truth["lon"][i]
                    err = haversine_m(f.lat, f.lon, lat, lon) if f.valid else None
                c = classes.setdefault(canopy.classify(lat, lon), {"fixes": 0, "valid": 0, "status": {}, "sigma": [], "err": []})
                c["fixes"] += 1
                c["valid"] += f.valid
                c["status"][str(f.status)] = c["status"].get(str(f.status), 0) + 1
                if f.valid and f.sigma_m:
                    c["sigma"].append(f.sigma_m)
                if err is not None:
                    c["err"].append(err)
        if not classes:
            continue
        out[stream] = {}
        for name, c in sorted(classes.items()):
            per_axis = float(np.sqrt(np.mean(np.square(c["err"])) / 2)) if c["err"] else None
            reported = float(np.median(c["sigma"])) if c["sigma"] else None
            out[stream][name] = {"fixes": c["fixes"], "valid_share": round(c["valid"] / c["fixes"], 3), "status": c["status"],
                                 "reported_sigma_m": _stats(c["sigma"]), "true_error_m": _stats(c["err"]),
                                 "true_vs_reported": None if not (per_axis and reported) else round(per_axis / reported, 2)}
    return out


def drift(runs: list[Run]) -> dict:
    """Odometry alone, without GNSS: how fast does the position error grow?

    - loop closure (works in the field): a run that starts and ends on the same reference
      point. The dead-reckoned end point should coincide with the start point; the gap per
      100 m driven is the drift. It does not depend on the unknown start heading.
    - error vs. distance driven, starting from the true pose (needs the true trajectory,
      i.e. the simulation)."""
    out = {}
    for source in ("odom", "odom_alt"):
        closure, rates, curves = [], [], []
        for run in runs:
            if not run.streams.get(source):
                continue
            visits: dict[str, list[int]] = {}
            for m in run.pkg.read("marks"):
                if m["kind"] == "reference" and m["tag"]:
                    visits.setdefault(m["tag"], []).append(m["stamp_ns"])
            for times in visits.values():
                if len(times) < 2:
                    continue
                t0, t1 = min(times), max(times)
                traj = dead_reckoning(run.cfg, run.streams, source, start=(t0, *next(
                    (f.lat, f.lon) for f in run.streams["gnss"] if f.valid), 0.0))
                a, b = (None, None) if traj is None else (traj.at(t0), traj.at(t1))
                if a is None or b is None:
                    continue
                inside = [(e, n) for t, e, n in zip(traj.t, traj.e, traj.n) if t0 <= t <= t1]
                length = sum(math.hypot(e2 - e1, n2 - n1) for (e1, n1), (e2, n2) in zip(inside, inside[1:]))
                if length > 20:
                    closure.append(100 * math.hypot(b.e - a.e, b.n - a.n) / length)
            if not run.truth:
                continue
            tt = run.truth
            traj = dead_reckoning(run.cfg, run.streams, source, start=(int(tt["t"][0]), tt["lat"][0], tt["lon"][0], tt["yaw"][0]))
            if traj is None:
                continue
            dist, last, curve = 0.0, None, []
            for i in range(0, len(tt["t"]), 5):
                if last is not None:
                    dist += haversine_m(tt["lat"][last], tt["lon"][last], tt["lat"][i], tt["lon"][i])
                last = i
                pose = traj.at(int(tt["t"][i]))
                if pose is None:
                    continue
                err = haversine_m(pose.lat, pose.lon, tt["lat"][i], tt["lon"][i])
                if dist > 20:
                    rates.append(100 * err / dist)
                if not curve or dist - curve[-1][0] >= 5:
                    curve.append([round(dist, 1), round(err, 2)])
            curves.append([run.name, curve])
        if closure or rates:
            out[source] = {"loop_closure_per_100m": _stats(closure), "error_per_100m": _stats(rates), "curves": curves}
    return out


def recommend(result: dict, criteria: dict) -> dict:
    methods = result["methods"]
    available = [m for m in ORDER if methods.get(m, {}).get("available")]
    sufficient = [m for m in available if methods[m]["sufficient"]]
    chosen = sufficient[0] if sufficient else None
    current = result.get("current_method")
    req = criteria["required_separation_m"]

    def facts(r: dict) -> str:
        sep = r["separation_needed_m"]
        return (f"{r['stands']['reidentified']} of {r['stands']['plants']} tagged plants were matched to the same stand "
                f"in every run; repeat observations of a plant were up to {r['observation_repeat_m']['p95']} m apart (95 %); "
                f"two stands stay separate from {'?' if sep is None else f'{sep:g}'} m apart (needed: {req:g} m).")

    def missing(r: dict) -> str:
        c = r["checks"]
        why = [text for key, text in (("reidentification", "re-finds too few plants"),
                                      ("separation", "merges stands that should stay separate"),
                                      ("honest_uncertainty", "its uncertainty circles are too small even after correction"))
               if not c[key]]
        return "; ".join(why)

    lines = []
    cur = methods.get(current)
    if cur and cur.get("available"):
        verdict = "sufficient" if cur["sufficient"] else f"NOT sufficient ({missing(cur)})"
        lines.append(f"Current localization ({cur['hardware']}, localization.method '{current}'): {verdict}. {facts(cur)}")
    elif current:
        lines.append(f"The current localization '{current}' could not be evaluated: "
                     f"{cur['reason'] if cur else 'not part of this experiment'}.")
    if chosen and chosen != current:
        m = methods[chosen]
        lines.append(f"Minimum sufficient setup: {m['hardware']} (localization.method '{chosen}'). {facts(m)}")
    elif not chosen:
        lines.append("None of the tested setups met all criteria; see 'improve next'.")
    if chosen and methods[chosen]["settings"]:
        m = methods[chosen]
        parts = []
        if "localization" in m["settings"]:
            parts.append(f"position uncertainty ×{m['sigma_scale']} (only {m['coverage_95_uncorrected']:.0%} of "
                         f"position errors were inside the claimed 95 % circle)")
        if "camera" in m["settings"]:
            parts.append(f"camera placement uncertainty {m['placement_sigma_m']:g} m (only "
                         f"{m['observation_coverage_95_uncorrected']:.0%} of plants were inside the observation's 95 % circle)")
        lines.append("These results use corrected uncertainties, as in recommended_gateway.yaml: " + "; ".join(parts) + ".")

    improve = []
    if chosen and chosen != current:
        names = {"rtk": "RTK corrections", "odom": "wheel odometry", "imu": "the IMU", "odom_alt": "LiDAR/visual odometry"}
        needs = [names[s] for s in sorted(NEEDS[chosen] - NEEDS.get(current, set())) if s in names]
        improve.append(f"Switch the gateway to localization.method '{chosen}' (recommended_gateway.yaml)"
                       + (f": it adds {', '.join(needs)}, all recorded in this experiment." if needs else "."))
        if "rtk" in NEEDS[chosen] and "rtk" not in NEEDS.get(current, set()):
            improve.append("RTK: register for SAPOS-HEPS with Bezirksregierung Köln (Geobasis NRW) and run an NTRIP "
                           "client on the rover (drivers.yaml template); keep a mobile-data fallback plan.")
    if "rtk" in available and methods["rtk"]["trajectory_error_by_canopy"].get("closed", {}).get("median", 0) > 1.0:
        improve.append("RTK falls back to metre-level errors under closed canopy: keep odometry/IMU fusion for those "
                       "segments rather than relying on RTK alone.")
    elif "rtk" not in available:
        improve.append("Test RTK (SAPOS-HEPS corrections): centimetres in the open, float/single under closed canopy.")
    floor = link_distance(0.0, 0.0) + LINK_SIGMA_FACTOR * math.sqrt(2) * min(
        (methods[m]["placement_sigma_m"] for m in available), default=0.0)
    improve.append(f"Even perfect localization cannot separate stands closer than about {floor:.0f} m: Forest Care's "
                   f"8 m stand spread plus the camera placement uncertainty. For closer stands, place each plant from the "
                   f"detection box and depth/LiDAR instead of a fixed camera offset.")
    if "gnss_altodom" not in available:
        improve.append("Record LiDAR and run LiDAR odometry offline (e.g. KISS-ICP) to compare 'gnss_altodom'.")
    improve.append("Only if all of this is not enough: LiDAR SLAM with loop closure between runs (not needed yet).")
    if result["simulated"]:
        improve.insert(0, "These numbers come from the simulator: repeat the experiment with the rover in the field "
                          "before relying on them.")
    return {"current_method": current, "current_sufficient": bool(cur and cur.get("available") and cur["sufficient"]),
            "minimum_stack": chosen, "sufficient_methods": sufficient, "summary": lines, "improve_next": improve}


def recommended_config(result: dict, method: str | None = None) -> dict | None:
    """Gateway settings that apply the result: localization method, corrected uncertainties, and a
    validation note that travels with every observation's metadata to the dashboard. Without
    `method`, for the minimum sufficient setup (None if there is none)."""
    chosen = method or result["recommendation"]["minimum_stack"]
    if not chosen or not result["methods"].get(chosen, {}).get("available"):
        return None
    m = result["methods"][chosen]
    params = {"localization": {"method": chosen, "validated": {
        "experiment": result["experiment"], "simulated": result["simulated"],
        "date": dt.date.today().isoformat(), "runs": len(result["runs"]),
        "reidentified": f"{m['stands']['reidentified']}/{m['stands']['plants']}",
        "observation_repeat_p95_m": m["observation_repeat_m"]["p95"],
        "stand_separation_m": m["separation_needed_m"],
        "reference_error_p95_m": m["reference_error_m"]["p95"],
        "sufficient": m["sufficient"]}}}
    return {"forestcare_gateway": {"ros__parameters": merge(params, m["settings"])}}
