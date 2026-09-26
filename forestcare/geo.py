"""Small, dependency-free geometry helpers.

Bonn spans roughly 15 x 20 km, so a local equirectangular projection is
accurate to well under a metre for the distances this prototype cares about
(stand linking, coverage, "within 100 m of a reserve"). GeoJSON order is
always (lon, lat); function arguments are (lat, lon) unless named otherwise.
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence

EARTH_RADIUS_M = 6_371_008.8


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def to_local_xy(lat: float, lon: float, lat0: float, lon0: float) -> tuple[float, float]:
    """Metres east/north of (lat0, lon0)."""
    x = math.radians(lon - lon0) * EARTH_RADIUS_M * math.cos(math.radians(lat0))
    y = math.radians(lat - lat0) * EARTH_RADIUS_M
    return x, y


def from_local_xy(x: float, y: float, lat0: float, lon0: float) -> tuple[float, float]:
    """Inverse of to_local_xy; returns (lat, lon)."""
    lat = lat0 + math.degrees(y / EARTH_RADIUS_M)
    lon = lon0 + math.degrees(x / (EARTH_RADIUS_M * math.cos(math.radians(lat0))))
    return lat, lon


# ---------------------------------------------------------------------------
# Polygons (GeoJSON coordinate arrays)
# ---------------------------------------------------------------------------

def _ring_contains(lon: float, lat: float, ring: Sequence[Sequence[float]]) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat):
            x_cross = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if lon < x_cross:
                inside = not inside
        j = i
    return inside


def _polygon_contains(lon: float, lat: float, rings: Sequence) -> bool:
    if not rings or not _ring_contains(lon, lat, rings[0]):
        return False
    return not any(_ring_contains(lon, lat, hole) for hole in rings[1:])


def _polygons(geometry: dict) -> Iterable[Sequence]:
    if geometry["type"] == "Polygon":
        yield geometry["coordinates"]
    elif geometry["type"] == "MultiPolygon":
        yield from geometry["coordinates"]


def geometry_bbox(geometry: dict) -> tuple[float, float, float, float]:
    """(min_lon, min_lat, max_lon, max_lat)."""
    xs, ys = [], []
    for poly in _polygons(geometry):
        for ring in poly:
            for x, y, *_ in ring:
                xs.append(x)
                ys.append(y)
    return min(xs), min(ys), max(xs), max(ys)


def geometry_contains(geometry: dict, lat: float, lon: float) -> bool:
    return any(_polygon_contains(lon, lat, poly) for poly in _polygons(geometry))


def _segment_distance(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    t = 0.0 if seg2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def distance_to_geometry_m(geometry: dict, lat: float, lon: float) -> float:
    """0 inside the polygon, otherwise distance to the nearest edge in metres."""
    if geometry_contains(geometry, lat, lon):
        return 0.0
    best = math.inf
    for poly in _polygons(geometry):
        for ring in poly:
            pts = [to_local_xy(p[1], p[0], lat, lon) for p in ring]
            for (ax, ay), (bx, by) in zip(pts, pts[1:]):
                best = min(best, _segment_distance(0.0, 0.0, ax, ay, bx, by))
    return best


def distance_to_polyline_m(coords: Sequence[Sequence[float]], lat: float, lon: float) -> float:
    """Distance from a point to a GeoJSON LineString coordinate list."""
    if len(coords) == 1:
        return haversine_m(lat, lon, coords[0][1], coords[0][0])
    pts = [to_local_xy(p[1], p[0], lat, lon) for p in coords]
    return min(_segment_distance(0.0, 0.0, ax, ay, bx, by) for (ax, ay), (bx, by) in zip(pts, pts[1:]))


# ---------------------------------------------------------------------------
# ETRS89 / UTM zone 32N (EPSG:25832) — the CRS used by LANUK and Geobasis NRW
# ---------------------------------------------------------------------------

_GRS80_A = 6_378_137.0
_GRS80_F = 1 / 298.257222101


def wgs84_to_utm32(lat: float, lon: float) -> tuple[float, float]:
    """Forward transverse Mercator (Krüger series), mm-level within the zone.

    Treats WGS84 and ETRS89 as identical; they differ by well under a metre in
    Germany, far below robot GNSS error under canopy.
    """
    k0, lon0, false_easting = 0.9996, math.radians(9.0), 500_000.0
    n = _GRS80_F / (2 - _GRS80_F)
    big_a = _GRS80_A / (1 + n) * (1 + n**2 / 4 + n**4 / 64)
    alpha = (n / 2 - 2 * n**2 / 3 + 5 * n**3 / 16, 13 * n**2 / 48 - 3 * n**3 / 5, 61 * n**3 / 240)
    phi, dlam = math.radians(lat), math.radians(lon) - lon0
    c = 2 * math.sqrt(n) / (1 + n)
    t = math.sinh(math.atanh(math.sin(phi)) - c * math.atanh(c * math.sin(phi)))
    xi = math.atan2(t, math.cos(dlam))
    eta = math.atanh(math.sin(dlam) / math.sqrt(1 + t * t))
    easting = eta + sum(a * math.cos(2 * j * xi) * math.sinh(2 * j * eta) for j, a in enumerate(alpha, 1))
    northing = xi + sum(a * math.sin(2 * j * xi) * math.cosh(2 * j * eta) for j, a in enumerate(alpha, 1))
    return false_easting + k0 * big_a * easting, k0 * big_a * northing


def douglas_peucker(points: list[list[float]], tolerance_m: float) -> list[list[float]]:
    """Simplify a (lon, lat) line/ring; used only to shrink reference snapshots."""
    if len(points) < 3:
        return points
    lat0, lon0 = points[0][1], points[0][0]
    xy = [to_local_xy(p[1], p[0], lat0, lon0) for p in points]
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        i, j = stack.pop()
        best, idx = 0.0, -1
        for k in range(i + 1, j):
            d = _segment_distance(*xy[k], *xy[i], *xy[j])
            if d > best:
                best, idx = d, k
        if best > tolerance_m and idx > 0:
            keep[idx] = True
            stack += [(i, idx), (idx, j)]
    return [p for p, k in zip(points, keep) if k]
