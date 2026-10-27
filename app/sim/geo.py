"""Small geo helpers. Kept dependency-free so a fresh clone needs no shapely."""

from __future__ import annotations

import math
from collections.abc import Sequence

Point = tuple[float, float]  # (lat, lon)

EARTH_RADIUS_MILES = 3958.8


def haversine_miles(a: Point, b: Point) -> float:
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(h))


def point_in_polygon(point: Point, polygon: Sequence[Point]) -> bool:
    """Ray casting. `polygon` is a sequence of (lat, lon), implicitly closed."""
    if len(polygon) < 3:
        return False
    lat, lon = point
    inside = False
    n = len(polygon)
    for i in range(n):
        lat_i, lon_i = polygon[i]
        lat_j, lon_j = polygon[(i + 1) % n]
        straddles = (lon_i > lon) != (lon_j > lon)
        if straddles:
            lat_at_lon = lat_i + (lon - lon_i) * (lat_j - lat_i) / (lon_j - lon_i)
            if lat < lat_at_lon:
                inside = not inside
    return inside


def distance_to_nearest(point: Point, candidates: Sequence[Point]) -> float:
    if not candidates:
        return float("inf")
    return min(haversine_miles(point, c) for c in candidates)
