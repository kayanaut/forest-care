"""Simulated survey missions in the same JSON shape a real robot would send.

The simulated robot drives serpentine transects through real Bonn woodland
twice a year: early June (P. serotina flowering) and mid-September (ripe
fruit). On-board detection and classification are modelled statistically:
  - detection probability falls with distance and is lower for seedlings;
  - the classifier gives P(Prunus serotina); look-alikes get lower but
    overlapping scores, so some are sent as candidates;
  - GNSS error under canopy is 2-5 m with occasional multipath outliers.
The robot sends a detection if P(Prunus serotina) >= REPORT_THRESHOLD.
"""

from __future__ import annotations

import base64
import math
import random
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from forestcare.geo import from_local_xy

from .images import render
from .world import BLOCKED_RADIUS_M, YEARS, World, distance_to_route, local_to_lonlat

TARGET = "Prunus serotina"
LOOKALIKES = ["Prunus padus", "Frangula alnus", "Prunus avium"]
REPORT_THRESHOLD = 0.35
DETECTION_RANGE_M = 10.0
SPEED_MPS = 0.8
BERLIN = ZoneInfo("Europe/Berlin")
WINDOWS = {"june": (6, 3), "september": (9, 16)}
SIM_INFO = {"name": "forestcare-sim", "version": "0.1.0"}
MODEL = {"name": "ps-detector-sim", "version": "0.3.0-sim"}

# (taxon, window) -> (phenology the robot reports, what the frame shows)
PHENOLOGY = {
    (TARGET, "june"): ("flowering", "raceme_white"),
    (TARGET, "september"): ("fruiting", "raceme_black"),
    ("Prunus padus", "june"): ("fruiting", "raceme_green"),
    ("Prunus padus", "september"): ("autumn_colour", "autumn"),
    ("Frangula alnus", "june"): ("flowering", "axillary_flowers"),
    ("Frangula alnus", "september"): ("fruiting", "axillary_berries"),
    ("Prunus avium", "june"): ("fruiting", "stalked_cherries"),
    ("Prunus avium", "september"): ("autumn_colour", "autumn"),
}
_TARGET_MEAN = {"flowering": 0.84, "fruiting": 0.88, "vegetative": 0.64}
_LOOKALIKE_MEAN = {("Prunus padus", "june"): 0.5, ("Prunus padus", "september"): 0.38,
                   ("Frangula alnus", "june"): 0.36, ("Frangula alnus", "september"): 0.42,
                   ("Prunus avium", "june"): 0.44, ("Prunus avium", "september"): 0.36}


def _beta(rng: random.Random, mean: float, concentration: float) -> float:
    mean = min(max(mean, 0.02), 0.98)
    return rng.betavariate(mean * concentration, (1 - mean) * concentration)


def classify(rng: random.Random, taxon: str, window: str, phenology: str, distance_m: float) -> dict:
    if taxon == TARGET:
        p = _beta(rng, _TARGET_MEAN[phenology] - 0.12 * distance_m / DETECTION_RANGE_M, 12)
    else:
        p = _beta(rng, _LOOKALIKE_MEAN[(taxon, window)], 10)
    rest = 1 - p
    others = LOOKALIKES[:]
    rng.shuffle(others)
    if taxon != TARGET:
        others.remove(taxon)
        others.insert(0, taxon)  # the true look-alike usually gets most of the remaining mass
    shares = [0.7, 0.2, 0.1] if taxon != TARGET else sorted([rng.random() for _ in range(3)], reverse=True)
    total = sum(shares)
    alts = [{"taxon": TARGET, "probability": round(p, 3)}] + [
        {"taxon": t, "probability": round(rest * s / total, 3)} for t, s in zip(others, shares)]
    alts.sort(key=lambda a: -a["probability"])
    return {"predicted_taxon": alts[0]["taxon"], "confidence": alts[0]["probability"],
            "target_probability": round(p, 3), "alternatives": alts[1:]}


def _clumps(plants: list, main_taxon: str, height: str | None) -> list[dict]:
    cells: dict[tuple, list] = defaultdict(list)
    for p in plants:
        cells[(round(p.x / 3), round(p.y / 3), p.taxon)].append(p)
    return [{"taxon": k[2], "x": sum(p.x for p in v) / len(v), "y": sum(p.y for p in v) / len(v),
             "count": len(v), "height": height if k[2] == main_taxon and height else "shrub"} for k, v in cells.items()]


def _cut(route: list[list[tuple[float, float]]], centre: tuple[float, float], radius: float) -> list[list[tuple[float, float]]]:
    """Remove route points near `centre`, splitting transects where the path is blocked."""
    out = []
    for line in route:
        run: list[tuple[float, float]] = []
        for pt in line:
            if math.dist(pt, centre) > radius:
                run.append(pt)
            else:
                if len(run) >= 2:
                    out.append(run)
                run = []
        if len(run) >= 2:
            out.append(run)
    return out


def _route_times(route: list[list[tuple[float, float]]]) -> list[list[float]]:
    """Seconds since mission start at each sampled route point."""
    times, elapsed, prev = [], 0.0, None
    for line in route:
        row = []
        for pt in line:
            if prev is not None:
                elapsed += math.dist(prev, pt) / SPEED_MPS
            row.append(elapsed)
            prev = pt
        times.append(row)
    return times


def simulate(world: World, seed: int = 42) -> tuple[list[dict], dict[str, dict]]:
    """Return (mission payloads, ground truth per observation uid)."""
    rng = random.Random(seed)
    missions, truth = [], {}
    for year in YEARS:
        for window, (month, day) in WINDOWS.items():
            for ai, area in enumerate(world.areas.values()):
                stands = [s for s in world.stands if s.area.key == area.key]
                route = [list(line) for line in world.transects[area.key]]
                notes = []
                for s in stands:
                    if year in s.spec.skip_in:
                        route = _cut(route, (s.x, s.y), BLOCKED_RADIUS_M)
                        notes.append("Part of a transect skipped: path blocked by fallen trees (simulated).")
                times = _route_times(route)
                start = datetime(year, month, day + ai, 9, rng.randint(0, 40), tzinfo=BERLIN)
                end = start + timedelta(seconds=times[-1][-1] + 60)
                mission_id = f"SIM-{year}{month:02d}-{area.key}"
                observations = []

                def emit(taxon: str, x: float, y: float, count: int, height: str, truth_key: str) -> None:
                    dist, li, pi = distance_to_route(x, y, route)
                    phenology, visual = PHENOLOGY[(taxon, window)]
                    if height == "seedling":
                        phenology, visual = "vegetative", "none"
                    cls = classify(rng, taxon, window, phenology, dist)
                    if cls["target_probability"] < REPORT_THRESHOLD:
                        return
                    sigma = area.gnss_sigma_m * rng.uniform(0.8, 1.3)
                    reported = sigma * rng.uniform(0.9, 1.4)
                    if rng.random() < 0.04:  # multipath outlier; the receiver reports it only partly
                        sigma *= 3
                        reported *= 2.5
                    lat, lon = from_local_xy(x + rng.gauss(0, sigma), y + rng.gauss(0, sigma), area.lat, area.lon)
                    at = start + timedelta(seconds=times[li][pi])
                    uid = f"{mission_id}-{len(observations) + 1:04d}"
                    est = count if count <= 2 else max(1, round(count * rng.uniform(0.75, 1.1)))
                    svg = render(taxon, visual, height, dist, uid, at.isoformat(), rng.randint(0, 10**9))
                    observations.append({
                        "uid": uid, "observed_at": at.isoformat(), "lat": round(lat, 7), "lon": round(lon, 7),
                        "gnss_accuracy_m": round(reported, 1), **cls, "plant_count_est": est,
                        "height_class": height, "phenology": phenology,
                        "image": {"media_type": "image/svg+xml",
                                  "data_base64": base64.b64encode(svg.encode()).decode()},
                    })
                    truth[uid] = {"taxon": taxon, "stand_key": truth_key, "height": height}

                for s in stands:
                    height = s.spec.height.get(year)
                    for c in _clumps(s.alive(year), s.spec.taxon, height):
                        dist = distance_to_route(c["x"], c["y"], route)[0]
                        if dist > DETECTION_RANGE_M:
                            continue
                        p_detect = 0.92 * (1 - 0.6 * (dist / DETECTION_RANGE_M) ** 2) * (0.6 if c["height"] == "seedling" else 1.0)
                        if rng.random() < p_detect:
                            emit(c["taxon"], c["x"], c["y"], c["count"], c["height"], s.spec.key)
                if rng.random() < 0.35:  # stray look-alike somewhere along the route
                    line = rng.choice(route)
                    x, y = rng.choice(line)
                    emit(rng.choice(LOOKALIKES), x + rng.uniform(-6, 6), y + rng.uniform(-6, 6), 1, "shrub", "noise")

                missions.append({
                    "mission_id": mission_id, "robot_id": "SIM-UGV-01", "source_kind": "simulated",
                    "area_name": area.name, "started_at": start.isoformat(), "ended_at": end.isoformat(),
                    "track": [local_to_lonlat(area, line) for line in route],
                    "detection_range_m": DETECTION_RANGE_M,
                    "sensors": {"camera": "RGB camera (simulated)", "gnss": "GNSS without RTK (simulated)"},
                    "model": MODEL, "simulator": {**SIM_INFO, "seed": seed, "window": window},
                    "notes": " ".join(notes) or None,
                    "observations": observations,
                })
    return missions, truth
