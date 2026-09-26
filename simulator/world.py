"""Simulated ground truth for four real Bonn survey areas.

Survey areas and robot transects are placed inside REAL wooded land-use
polygons from the City of Bonn, so context lookups (district, NSG, FFH) behave
as they would for a real robot. The plants themselves are INVENTED: nothing in
this file says anything about where Prunus serotina actually grows in Bonn.

Each scenario stand exists to exercise one part of the workflow (expansion
in a reserve, management follow-up, look-alike false positives, gaps in survey
coverage, ...). The `purpose` field says which.
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path

from forestcare.geo import from_local_xy, geometry_bbox, geometry_contains

YEARS = (2024, 2025, 2026)
TRANSECT_SPACING_M = 25.0
SAMPLE_STEP_M = 5.0
MIN_STAND_SEPARATION_M = 70.0
BLOCKED_RADIUS_M = 30.0


@dataclass
class Area:
    key: str
    name: str
    lat: float
    lon: float
    width_m: float
    height_m: float
    gnss_sigma_m: float  # canopy degrades GNSS; parks are more open


AREAS = [
    Area("KF", "Kottenforst (Röttgen)", 50.66244, 7.05895, 360, 260, 4.0),
    Area("TB", "Düne Tannenbusch", 50.74420, 7.06100, 300, 220, 3.0),
    Area("EN", "Ennert", 50.72394, 7.17260, 320, 240, 4.5),
    Area("RA", "Rheinaue (park)", 50.71370, 7.14810, 280, 200, 2.0),
]


@dataclass
class StandSpec:
    key: str
    area: str
    taxon: str
    plants: dict[int, int]            # true plant count per year
    purpose: str
    height: dict[int, str] = field(default_factory=dict)
    companions: dict[str, int] = field(default_factory=dict)  # look-alike plants growing inside the stand
    managed_on: str | None = None     # a (simulated) human records management on this date
    skip_in: tuple[int, ...] = ()     # years in which missions do not cover this stand


SCENARIO = [
    StandSpec("TB-1", "TB", "Prunus serotina", {2024: 4, 2025: 8, 2026: 20},
              "Expanding stand inside a nature reserve on sand (LANUK: most problematic habitat type)",
              height={2024: "shrub", 2025: "shrub", 2026: "shrub"}),
    StandSpec("KF-1", "KF", "Prunus serotina", {2024: 14, 2025: 15, 2026: 13},
              "Established stable stand with a native look-alike growing inside it",
              height={y: "small_tree" for y in YEARS}, companions={"Prunus padus": 2}),
    StandSpec("KF-2", "KF", "Prunus serotina", {2024: 10, 2025: 11, 2026: 4},
              "Management recorded by a person in Feb 2026; resprouts appear afterwards",
              height={y: "shrub" for y in YEARS}, managed_on="2026-02-12"),
    StandSpec("KF-3", "KF", "Prunus serotina", {2024: 5, 2025: 6, 2026: 6},
              "Not covered in 2026 (transect blocked) — absence of data is not absence of plants",
              height={y: "shrub" for y in YEARS}, skip_in=(2026,)),
    StandSpec("KF-4", "KF", "Prunus serotina", {2024: 0, 2025: 2, 2026: 0},
              "Small stand that disappears (e.g. browsing); surveyed in 2026 but not re-detected",
              height={2025: "seedling"}),
    StandSpec("KF-P", "KF", "Prunus padus", {2024: 6, 2025: 6, 2026: 7},
              "Native look-alike that repeatedly triggers the classifier",
              height={y: "shrub" for y in YEARS}),
    StandSpec("EN-1", "EN", "Prunus serotina", {2024: 0, 2025: 0, 2026: 3},
              "New stand in 2026 inside NSG Ennert / FFH Siebengebirge",
              height={2026: "seedling"}),
    StandSpec("EN-2", "EN", "Prunus serotina", {2024: 8, 2025: 7, 2026: 8},
              "Stable stand", height={y: "shrub" for y in YEARS}),
    StandSpec("EN-F", "EN", "Frangula alnus", {2024: 5, 2025: 5, 2026: 5},
              "Look-alike (alder buckthorn) causing false positives", height={y: "shrub" for y in YEARS}),
    StandSpec("RA-1", "RA", "Prunus serotina", {2024: 1, 2025: 1, 2026: 1},
              "Single fruiting tree in an urban park (LANUK criterion: fruiting solitary specimen)",
              height={y: "tree" for y in YEARS}),
    StandSpec("RA-2", "RA", "Prunus serotina", {2024: 0, 2025: 0, 2026: 4},
              "Bird-dispersed seedlings appearing near the park tree", height={2026: "seedling"}),
    StandSpec("RA-A", "RA", "Prunus avium", {2024: 1, 2025: 1, 2026: 1},
              "Wild cherry tree occasionally confused with the target", height={y: "tree" for y in YEARS}),
]


@dataclass
class Plant:
    taxon: str
    x: float
    y: float
    rank: int  # plants with lower rank exist first; stands grow outward


@dataclass
class StandTruth:
    spec: StandSpec
    area: Area
    x: float
    y: float
    plants: list[Plant]
    lat: float = 0.0
    lon: float = 0.0

    def alive(self, year: int) -> list[Plant]:
        n = self.spec.plants.get(year, 0)
        main = [p for p in self.plants if p.taxon == self.spec.taxon][:n]
        companions = [p for p in self.plants if p.taxon != self.spec.taxon]
        return main + (companions if n else [])


@dataclass
class World:
    areas: dict[str, Area]
    transects: dict[str, list[list[tuple[float, float]]]]   # area -> list of local-xy polylines
    stands: list[StandTruth]
    seed: int

    def ground_truth(self) -> dict:
        return {"note": "SIMULATED ground truth. Invented plants in real Bonn woodland. Not real occurrences.",
                "seed": self.seed,
                "stands": [{"key": s.spec.key, "area": s.area.name, "taxon": s.spec.taxon, "lat": s.lat, "lon": s.lon,
                            "plants_per_year": s.spec.plants, "purpose": s.spec.purpose,
                            "managed_on": s.spec.managed_on, "skip_in": list(s.spec.skip_in)} for s in self.stands]}


def _load_wooded(reference_dir: Path) -> list[tuple[dict, tuple]]:
    feats = json.loads((reference_dir / "bonn_wooded_landuse.geojson").read_text())["features"]
    return [(f["geometry"], geometry_bbox(f["geometry"])) for f in feats]


def _in_wooded(wooded: list, lat: float, lon: float) -> bool:
    return any(b[0] <= lon <= b[2] and b[1] <= lat <= b[3] and geometry_contains(g, lat, lon) for g, b in wooded)


def _transects(area: Area, wooded: list) -> list[list[tuple[float, float]]]:
    """East-west transects every 25 m, cut to the parts that lie in mapped woodland."""
    lines = []
    n_rows = int(area.height_m // TRANSECT_SPACING_M) + 1
    for i in range(n_rows):
        y = -area.height_m / 2 + i * TRANSECT_SPACING_M
        xs = [-area.width_m / 2 + k * SAMPLE_STEP_M for k in range(int(area.width_m // SAMPLE_STEP_M) + 1)]
        if i % 2:
            xs.reverse()  # serpentine route
        run: list[tuple[float, float]] = []
        for x in xs:
            lat, lon = from_local_xy(x, y, area.lat, area.lon)
            if _in_wooded(wooded, lat, lon):
                run.append((x, y))
            else:
                if len(run) >= 4:
                    lines.append(run)
                run = []
        if len(run) >= 4:
            lines.append(run)
    return lines


def build_world(reference_dir: Path, seed: int = 42) -> World:
    rng = random.Random(seed)
    wooded = _load_wooded(reference_dir)
    areas = {a.key: a for a in AREAS}
    transects = {k: _transects(a, wooded) for k, a in areas.items()}
    stands: list[StandTruth] = []
    for spec in SCENARIO:
        area = areas[spec.area]
        candidates = [pt for line in transects[area.key] for pt in line[2:-2]]
        taken = [(s.x, s.y) for s in stands if s.area.key == area.key]
        rng.shuffle(candidates)
        # Leave clear space between stands so they stay separate after GNSS noise and so a
        # locally blocked transect (skip_in) does not also hide a neighbouring stand.
        x, y = next(pt for pt in candidates if all(math.dist(pt, q) > MIN_STAND_SEPARATION_M for q in taken))
        x += rng.uniform(-5, 5)
        n_max = max(spec.plants.values())
        plants = []
        for rank in range(n_max):
            r = 1.2 * math.sqrt(rank) + rng.uniform(0, 1.0)
            a = rng.uniform(0, 2 * math.pi)
            plants.append(Plant(spec.taxon, x + r * math.cos(a), y + r * math.sin(a), rank))
        for taxon, n in spec.companions.items():
            for _ in range(n):
                a = rng.uniform(0, 2 * math.pi)
                plants.append(Plant(taxon, x + 4 * math.cos(a), y + 4 * math.sin(a), 999))
        s = StandTruth(spec, area, x, y, plants)
        s.lat, s.lon = from_local_xy(x, y, area.lat, area.lon)
        stands.append(s)
    return World(areas, transects, stands, seed)


def local_to_lonlat(area: Area, pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out = []
    for x, y in pts:
        lat, lon = from_local_xy(x, y, area.lat, area.lon)
        out.append((round(lon, 7), round(lat, 7)))
    return out


def distance_to_route(x: float, y: float, route: list[list[tuple[float, float]]]) -> tuple[float, int, int]:
    """(distance, line index, point index) of the closest sampled route point."""
    best = (math.inf, -1, -1)
    for li, line in enumerate(route):
        for pi, (px, py) in enumerate(line):
            d = math.hypot(px - x, py - y)
            if d < best[0]:
                best = (d, li, pi)
    return best

