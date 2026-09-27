"""Build the Forest Care mission (API contract 1.1, see forestcare/models.py) from a package.

How an observation gets its position:
  1. The time t is the stamp of the camera frame it refers to (the detection's image, or the
     frame the operator was looking at when marking).
  2. The trajectory gives the robot position at t and its uncertainty (localize.py).
  3. The plant is assumed `camera.forward_offset_m` in front of the antenna, in the camera's
     direction (driving direction + `camera.yaw_offset_deg`). The reported uncertainty combines
     the position uncertainty with `camera.placement_sigma_m`. If the heading is unknown
     (robot standing still at the start), the robot position is used and the offset is added
     to the uncertainty instead.
Everything used along the way is written to the observation's `metadata`, so a reviewer can
see which fix, which frame and which assumptions produced a point on the map.
"""

from __future__ import annotations

import math
import statistics
import time
from bisect import bisect_left
from collections import Counter

from .geo import from_local, haversine_m, yaw_to_compass_deg
from .localize import METHODS, build_trajectory
from .messages import NS, GnssFix
from .package import MissionPackage


def iso(ns: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ns // NS)) + f".{(ns % NS) // 1_000_000:03d}+00:00"


def _gnss(records: list[dict]) -> list[GnssFix]:
    keys = GnssFix.__dataclass_fields__.keys()
    return [GnssFix(**{k: r[k] for k in keys if k in r}) for r in records]


def _nearest(records: list[dict], stamps: list[int], t_ns: int, max_dt_s: float = 0.5) -> dict | None:
    i = bisect_left(stamps, t_ns)
    best = min((j for j in (i - 1, i) if 0 <= j < len(stamps)), key=lambda j: abs(stamps[j] - t_ns), default=None)
    if best is None or abs(stamps[best] - t_ns) > max_dt_s * NS:
        return None
    return {**records[best], "dt_s": round((stamps[best] - t_ns) / NS, 3)}


def build_track(samples: list[tuple[int, float, float]], tcfg: dict) -> list[list[list[float]]]:
    """MultiLineString [lon, lat]: thinned to `min_step_m`, split at gaps and implausible jumps."""
    segments, current, last = [], [], None
    for t, lat, lon in samples:
        if last is not None:
            gap_s = (t - last[0]) / NS
            jump = haversine_m(last[1], last[2], lat, lon)
            if gap_s > tcfg["max_gap_s"] or jump > tcfg["max_jump_m"]:
                segments.append(current + ([last[1:]] if current and current[-1] != last[1:] else []))
                current = []
        if not current or haversine_m(*current[-1], lat, lon) >= tcfg["min_step_m"]:
            current.append((lat, lon))
        last = (t, lat, lon)
    if last is not None:
        segments.append(current + ([last[1:]] if current and current[-1] != last[1:] else []))
    return [[[round(lon, 7), round(lat, 7)] for lat, lon in seg] for seg in segments if len(seg) >= 2]


def map_hypotheses(hypotheses: list[dict], dcfg: dict) -> dict | None:
    """Detector classes -> Forest Care model fields; None if below the reporting threshold."""
    class_map = dcfg["class_map"]
    scores: dict[str, float] = {}
    for h in hypotheses:
        taxon = class_map.get(h["class_id"], h["class_id"])
        scores[taxon] = max(scores.get(taxon, 0.0), min(1.0, max(0.0, float(h["score"]))))
    target = class_map.get(dcfg["target_class"], dcfg["target_class"])
    p = scores.get(target, 0.0)
    if not scores or p < dcfg["min_target_score"]:
        return None
    best, conf = max(scores.items(), key=lambda kv: kv[1])
    alternatives = [{"taxon": t, "probability": round(s, 4)}
                    for t, s in sorted(scores.items(), key=lambda kv: -kv[1]) if t != best][:10]
    return {"predicted_taxon": best, "confidence": round(conf, 4), "target_probability": round(p, 4),
            "alternatives": alternatives}


def merge_detections(detected: list[tuple[int, str, dict]], dcfg: dict) -> list[dict]:
    """One observation per plant pass: group repeated detections of the same plant, keep the best frame."""
    groups: list[dict] = []
    window = dcfg["merge_window_s"] * NS
    for t, det_id, obs in sorted(detected, key=lambda x: x[0]):
        for g in reversed(groups):
            if t - g["last"] > window:
                continue
            same = det_id and det_id == g["id"]
            close = not det_id and not g["id"] and haversine_m(obs["lat"], obs["lon"], g["best"]["lat"], g["best"]["lon"]) <= dcfg["merge_radius_m"]
            if same or close:
                g["n"] += 1
                g["last"] = t
                if obs["target_probability"] > g["best"]["target_probability"]:
                    g["best"] = obs
                break
        else:
            groups.append({"id": det_id, "first": t, "last": t, "n": 1, "best": obs})
    out = []
    for g in groups:
        best = g["best"]
        best["metadata"]["detection"]["merged"] = {"frames": g["n"], "first": iso(g["first"]), "last": iso(g["last"]),
                                                   "rule": "same tracker id" if g["id"] else "within merge radius"}
        out.append(best)
    return out


class _Context:
    def __init__(self, pkg: MissionPackage, cfg: dict, traj):
        self.cfg, self.traj = cfg, traj
        self.odom = pkg.read("odom")
        self.odom_t = [r["stamp_ns"] for r in self.odom]
        self.imu = pkg.read("imu")
        self.imu_t = [r["stamp_ns"] for r in self.imu]
        self.frames = {r["stamp_ns"]: r for r in pkg.read("frames")}


def _place(ctx: _Context, uid: str, t_ns: int, frame: dict | None, extra: dict, model: dict | None) -> dict | None:
    pose = ctx.traj.at(t_ns)
    if pose is None:
        return None
    cam = ctx.cfg["camera"]
    if pose.yaw is not None:
        bearing = pose.yaw + math.radians(cam["yaw_offset_deg"])
        e = pose.e + cam["forward_offset_m"] * math.cos(bearing)
        n = pose.n + cam["forward_offset_m"] * math.sin(bearing)
        sigma = math.hypot(pose.sigma_m, cam["placement_sigma_m"])
        placement = "robot position + camera offset along the heading"
    else:
        e, n = pose.e, pose.n
        sigma = math.sqrt(pose.sigma_m ** 2 + cam["placement_sigma_m"] ** 2 + cam["forward_offset_m"] ** 2)
        placement = "robot position (heading unknown, offset added to the uncertainty)"
    lat, lon = from_local(e, n, *ctx.traj.origin)
    odom = _nearest(ctx.odom, ctx.odom_t, t_ns)
    imu = _nearest(ctx.imu, ctx.imu_t, t_ns)
    deg = lambda v: None if v is None else round(math.degrees(v), 1)  # noqa: E731
    metadata = {
        "capture": {
            "time": iso(t_ns),
            "frame": None if frame is None else {
                "file": frame["file"], "sha256": frame["sha256"], "fmt": frame["fmt"], "width": frame["width"],
                "height": frame["height"], "frame_id": frame["frame_id"], "camera": cam["name"],
                "time_offset_s": cam["time_offset_s"]},
        },
        "localization": {"method": ctx.traj.method, "method_label": METHODS.get(ctx.traj.method, ctx.traj.method),
                         "robot_sigma_m": round(pose.sigma_m, 2), "observation_sigma_m": round(sigma, 2),
                         "accuracy_scale": ctx.cfg["gnss"]["accuracy_scale"],
                         "sigma_scale": ctx.cfg["localization"].get("sigma_scale", 1.0),
                         "validated": ctx.cfg["localization"].get("validated") or None,
                         **{k: v for k, v in pose.info.items() if v is not None}},
        "robot": {"lat": round(pose.lat, 7), "lon": round(pose.lon, 7),
                  "heading_deg": None if pose.yaw is None else round(yaw_to_compass_deg(pose.yaw), 1),
                  "heading_source": pose.heading_source,
                  "odometry": None if odom is None else {"x": round(odom["x"], 2), "y": round(odom["y"], 2),
                                                         "yaw_deg": deg(odom["yaw"]), "speed_mps": round(odom["v"], 2),
                                                         "frame_id": odom["frame_id"], "dt_s": odom["dt_s"]},
                  "imu": None if imu is None else {"roll_deg": deg(imu["roll"]), "pitch_deg": deg(imu["pitch"]),
                                                   "yaw_deg": deg(imu["yaw"]), "yaw_rate_dps": deg(imu["wz"]),
                                                   "dt_s": imu["dt_s"]}},
        "placement": {"method": placement, "forward_offset_m": cam["forward_offset_m"],
                      "yaw_offset_deg": cam["yaw_offset_deg"], "sigma_m": cam["placement_sigma_m"]},
        **extra,
    }
    obs = {"uid": uid, "observed_at": iso(t_ns), "lat": round(lat, 7), "lon": round(lon, 7),
           "gnss_accuracy_m": round(max(sigma, 0.1), 2),
           "predicted_taxon": None, "confidence": None, "target_probability": None, "alternatives": [],
           "plant_count_est": None, "height_class": None, "phenology": None, "metadata": metadata}
    if model:
        obs.update(model, plant_count_est=1)
    if frame is not None:
        obs["image_file"] = frame["file"]
        obs["image_media_type"] = "image/png" if frame["fmt"] == "png" else "image/jpeg"
    return obs


def _safe_uid(text: str) -> str:
    return "".join(c if c.isalnum() or c in "._:-" else "-" for c in text)[:120]


def _gnss_summary(fixes: list[GnssFix], gcfg: dict) -> dict:
    if not fixes:
        return {"fixes": 0}
    statuses = Counter(str(f.status) for f in fixes)
    sigmas = sorted(f.sigma_m for f in fixes if f.sigma_m)
    stamps = sorted(f.stamp_ns for f in fixes if f.valid)
    gaps = [(b - a) / NS for a, b in zip(stamps, stamps[1:])]
    return {"fixes": len(fixes), "valid": sum(f.valid for f in fixes), "status_counts": dict(statuses),
            "reported_sigma_m": None if not sigmas else {
                "median": round(statistics.median(sigmas), 2), "p90": round(sigmas[int(0.9 * (len(sigmas) - 1))], 2),
                "max": round(sigmas[-1], 2)},
            "no_covariance": sum(1 for f in fixes if f.sigma_m is None),
            "longest_gap_s": round(max(gaps), 1) if gaps else None, "accuracy_scale": gcfg["accuracy_scale"]}


def with_twist(rows: list[dict]) -> list[dict]:
    """Odometry that publishes only poses (many LiDAR/visual odometry nodes leave `twist` empty):
    derive forward speed and yaw rate from consecutive poses. Rows with a twist are kept as they are."""
    if len(rows) < 2 or any(r["v"] or r["w"] for r in rows):
        return rows
    out = [dict(rows[0])]
    for a, b in zip(rows, rows[1:]):
        dt = (b["stamp_ns"] - a["stamp_ns"]) / NS
        if dt <= 0:
            out.append(dict(b, v=out[-1]["v"], w=out[-1]["w"]))
            continue
        dx, dy = b["x"] - a["x"], b["y"] - a["y"]
        forward = dx * math.cos(a["yaw"]) + dy * math.sin(a["yaw"])       # reversing counts negative
        dyaw = math.atan2(math.sin(b["yaw"] - a["yaw"]), math.cos(b["yaw"] - a["yaw"]))
        out.append(dict(b, v=forward / dt, w=dyaw / dt))
    out[0].update(v=out[1]["v"], w=out[1]["w"])
    return out


def read_streams(pkg: MissionPackage) -> dict:
    return {"gnss": _gnss(pkg.read("gnss")), "gnss_rtk": _gnss(pkg.read("gnss_rtk")),
            "odom": with_twist(pkg.read("odom")), "odom_alt": with_twist(pkg.read("odom_alt")), "imu": pkg.read("imu")}


def build_mission(pkg: MissionPackage) -> dict:
    """Write mission.json and return a short summary. Marks the package invalid if nothing can be placed."""
    mission, _traj, summary = assemble_mission(pkg)
    pkg.write_json("mission.json", mission)
    if not mission["track"]:
        pkg.set_state("invalid", error="no usable GNSS track: the mission cannot be placed on the map")
    else:
        pkg.set_state("complete", observations=summary["observations"], unplaced=summary["unplaced"])
    return {"mission_id": mission["mission_id"], "package": str(pkg.root), "status": pkg.state["status"], **summary}


def assemble_mission(pkg: MissionPackage, cfg_overrides: dict | None = None, streams: dict | None = None,
                     traj=None):
    """(mission dict, trajectory, summary) without writing anything; the localization study calls
    this once per method on the same package."""
    from .config import merge

    header = pkg.read_json("header.json")
    stats = pkg.read_json("stats.json", {})
    cfg = merge(header["config"], cfg_overrides or {})
    info, mid = header.get("info") or {}, header["mission_id"]
    streams = streams or read_streams(pkg)
    traj = traj or build_trajectory(cfg, streams)
    track = build_track(traj.samples(), cfg["track"]) if traj else []
    ctx = _Context(pkg, cfg, traj) if traj else None

    observations, unplaced, references = [], [], []
    skipped_low = 0
    detected = []
    for rec in pkg.read("detections"):
        t = rec["frame_stamp_ns"] or rec["stamp_ns"]
        frame = ctx.frames.get(rec["frame_stamp_ns"]) if ctx else None
        for i, det in enumerate(rec["detections"]):
            model = map_hypotheses(det["hypotheses"], cfg["detections"])
            if model is None:
                skipped_low += 1
                continue
            uid = f"{mid}-D{t}-{i}"
            extra = {"detection": {"bbox": det["bbox"], "hypotheses": det["hypotheses"], "id": det["det_id"],
                                   "frame_matched": frame is not None}}
            obs = _place(ctx, uid, t, frame, extra, model) if ctx else None
            if obs:
                detected.append((t, det["det_id"], obs))
            else:
                unplaced.append({"uid": uid, "reason": "no GNSS fix near this time"})
    merged = merge_detections(detected, cfg["detections"])
    observations.extend(merged)
    for m in pkg.read("marks"):
        t = m["frame_stamp_ns"] or m["stamp_ns"]
        if m["kind"] == "reference":
            pose = traj.at(t) if traj else None
            references.append({"tag": m["tag"], "time": iso(m["stamp_ns"]),
                                "lat": None if pose is None else round(pose.lat, 8),
                                "lon": None if pose is None else round(pose.lon, 8),
                                "sigma_m": None if pose is None else round(pose.sigma_m, 3)})
            continue
        uid = _safe_uid(f"{mid}-M{m['mark_id'] or m['stamp_ns']}")
        frame = ctx.frames.get(m["frame_stamp_ns"]) if ctx else None
        extra = {"mark": {"label": m["label"], "note": m["note"], "tag": m["tag"], "source": m["source"],
                          "id": m["mark_id"], "time": iso(m["stamp_ns"]), "frame_dt_s": m["frame_dt_s"]}}
        obs = _place(ctx, uid, t, frame, extra, None) if ctx else None
        (observations.append(obs) if obs else unplaced.append({"uid": uid, "reason": "no GNSS fix near this time"}))

    counts = stats.get("counts", {})
    notes = []
    if unplaced:
        notes.append(f"{len(unplaced)} observation(s) could not be placed (no GNSS fix near their time).")
    if skipped_low:
        notes.append(f"{skipped_low} detection(s) below the reporting threshold were not sent.")
    if len(detected) > len(merged):
        notes.append(f"{len(detected)} detections were merged into {len(merged)} observation(s), one per plant pass.")
    for w in stats.get("warnings", [])[:5]:
        notes.append(w)
    first_ns = stats.get("first_ns") or header["start_ns"]
    last_ns = stats.get("last_ns") or first_ns
    sensors = {"camera": cfg["camera"]["name"], "gnss": cfg["gnss"]["name"],
               **{f"topic_{k}": v for k, v in cfg["topics"].items() if v}}
    has_model_output = any(o["target_probability"] is not None for o in observations)
    dcfg = cfg["detections"]
    mission = {
        "mission_id": mid,
        "robot_id": cfg["robot_id"],
        "source_kind": cfg["source_kind"],
        "area_name": info.get("area_name") or cfg["area_name"] or None,
        "started_at": iso(first_ns),
        "ended_at": iso(max(last_ns, first_ns)),
        "track": track,
        "detection_range_m": cfg["detection_range_m"],
        "protocol": cfg["survey_protocol"],
        "sensors": {k: str(v) for k, v in sensors.items()},
        "model": {"name": dcfg["model_name"] or "unnamed detector", "version": dcfg["model_version"] or "unknown"}
                 if has_model_output else None,
        "simulator": (cfg["simulator"] or {"name": "unspecified simulation"}) if cfg["source_kind"] == "simulated" else None,
        "notes": " ".join(notes)[:2000] or None,
        "metadata": {
            "gateway": header["gateway"],
            "source": header["source"],
            "operator_info": info,
            "localization": {"method": cfg["localization"]["method"],
                             "label": METHODS.get(cfg["localization"]["method"]),
                             "validated": cfg["localization"].get("validated") or None},
            "gnss": _gnss_summary(streams["gnss"], cfg["gnss"]),
            "gnss_rtk": _gnss_summary(streams["gnss_rtk"], cfg["gnss"]) if streams["gnss_rtk"] else None,
            "message_counts": counts,
            "clock_offsets_ms": stats.get("clock_offsets_ms", {}),
            "frames_kept": counts.get("frames_kept", 0),
            "reference_marks": references[:50],
            "unplaced": unplaced[:50],
        },
        "observations": observations,
    }
    summary = {"track_segments": len(track), "observations": len(observations), "unplaced": len(unplaced),
               "references": len(references)}
    return mission, traj, summary
