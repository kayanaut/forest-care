"""Forest Care's stand-linking rule, reproduced so the experiment can ask "would these repeat
observations end up in the same stand?" without a server.

The rule (forestcare/ingest.py, link_stand): a new observation joins the stand of the
best-matching earlier observation if their distance is at most

    stand_spread_m + link_sigma_factor * sqrt(sigma_1^2 + sigma_2^2)

(defaults 8 m and 2), otherwise it opens a new stand. A test in the repository checks that
this copy and the backend give the same stands.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..geo import haversine_m

STAND_SPREAD_M = 8.0
LINK_SIGMA_FACTOR = 2.0


@dataclass
class Obs:
    key: str          # unique id (e.g. the observation uid)
    t_ns: int
    lat: float
    lon: float
    sigma: float      # the observation's 1-sigma uncertainty (gnss_accuracy_m)
    tag: str = ""     # the true plant id, if known
    robot_sigma: float | None = None    # the robot-position part of sigma (without camera placement)


def link_distance(sigma_1: float, sigma_2: float, spread: float = STAND_SPREAD_M, factor: float = LINK_SIGMA_FACTOR) -> float:
    return spread + factor * math.hypot(sigma_1, sigma_2)


def link_stands(observations: list[Obs], spread: float = STAND_SPREAD_M, factor: float = LINK_SIGMA_FACTOR) -> dict[str, int]:
    """observation key -> stand number, processing observations in time order like ingest does."""
    placed: list[tuple[Obs, int]] = []
    stand_of: dict[str, int] = {}
    next_stand = 0
    for o in sorted(observations, key=lambda o: o.t_ns):
        best, best_score = None, 1.0
        for other, stand in placed:
            score = haversine_m(o.lat, o.lon, other.lat, other.lon) / link_distance(o.sigma, other.sigma, spread, factor)
            if score <= best_score:
                best, best_score = stand, score
        if best is None:
            best, next_stand = next_stand, next_stand + 1
        stand_of[o.key] = best
        placed.append((o, best))
    return stand_of


def match_report(observations: list[Obs], stand_of: dict[str, int]) -> dict:
    """Per plant tag: did all its observations land in one stand? Which distinct plants were merged?"""
    by_tag: dict[str, set[int]] = {}
    for o in observations:
        if o.tag:
            by_tag.setdefault(o.tag, set()).add(stand_of[o.key])
    tags_in_stand: dict[int, set[str]] = {}
    for o in observations:
        if o.tag:
            tags_in_stand.setdefault(stand_of[o.key], set()).add(o.tag)
    merged = sorted({tuple(sorted(t)) for t in tags_in_stand.values() if len(t) > 1})
    reidentified = [t for t, stands in by_tag.items() if len(stands) == 1]
    return {"plants": len(by_tag), "reidentified": len(reidentified),
            "split": sorted(t for t, stands in by_tag.items() if len(stands) > 1),
            "merged_groups": [list(g) for g in merged], "stands": len(set(stand_of.values()))}
