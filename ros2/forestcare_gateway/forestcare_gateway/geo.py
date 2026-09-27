"""Small geodesy helpers. A local east/north projection is accurate to centimetres over the
few kilometres a rover drives, so no projection library is needed."""

from __future__ import annotations

import math

EARTH_RADIUS_M = 6_371_008.8


def to_local(lat: float, lon: float, lat0: float, lon0: float) -> tuple[float, float]:
    """(east, north) in metres from the origin."""
    e = math.radians(lon - lon0) * EARTH_RADIUS_M * math.cos(math.radians(lat0))
    n = math.radians(lat - lat0) * EARTH_RADIUS_M
    return e, n


def from_local(e: float, n: float, lat0: float, lon0: float) -> tuple[float, float]:
    """(lat, lon) of a local east/north offset."""
    lat = lat0 + math.degrees(n / EARTH_RADIUS_M)
    lon = lon0 + math.degrees(e / (EARTH_RADIUS_M * math.cos(math.radians(lat0))))
    return lat, lon


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def yaw_to_compass_deg(yaw_enu: float) -> float:
    """ENU yaw (counter-clockwise from east, radians) -> compass heading (clockwise from north, degrees)."""
    return (90.0 - math.degrees(yaw_enu)) % 360.0


def wrap_angle(a: float) -> float:
    return (a + math.pi) % (2 * math.pi) - math.pi
