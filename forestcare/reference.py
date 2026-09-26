"""Real reference data (Bonn, LANUK, GBIF) and point context lookups.

Context is captured once at ingest and stored with the observation, together
with the retrieval date of each reference layer, so a reviewer can always see
which version of the external data an interpretation was based on.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from .config import Thresholds
from .geo import distance_to_geometry_m, geometry_bbox, geometry_contains, haversine_m

# Roughly 1 km in degrees at Bonn's latitude; used to pre-filter by bounding box.
_DEG_PER_M_LAT = 1 / 111_200
_DEG_PER_M_LON = 1 / 70_400


@dataclass
class _Indexed:
    feature: dict
    bbox: tuple[float, float, float, float]


class ReferenceData:
    def __init__(self, reference_dir: Path, thresholds: Thresholds):
        self.dir = reference_dir
        self.t = thresholds

    def _load(self, name: str) -> dict:
        return json.loads((self.dir / name).read_text())

    def _indexed(self, name: str) -> list[_Indexed]:
        return [_Indexed(f, geometry_bbox(f["geometry"])) for f in self._load(name)["features"]]

    @cached_property
    def manifest(self) -> dict:
        return self._load("manifest.json")

    @cached_property
    def species(self) -> dict:
        return self._load("species_profile_prunus_serotina.json")

    @cached_property
    def districts(self) -> list[_Indexed]:
        return self._indexed("bonn_districts.geojson")

    @cached_property
    def wooded(self) -> list[_Indexed]:
        return self._indexed("bonn_wooded_landuse.geojson")

    @cached_property
    def nsg(self) -> list[_Indexed]:
        return self._indexed("lanuk_nsg_bonn.geojson")

    @cached_property
    def ffh(self) -> list[_Indexed]:
        return self._indexed("lanuk_ffh_bonn.geojson")

    @cached_property
    def biotopes(self) -> list[_Indexed]:
        return self._indexed("lanuk_protected_biotopes_bonn.geojson")

    @cached_property
    def gbif(self) -> list[dict]:
        return self._load("gbif_prunus_serotina_bonn.geojson")["features"]

    @cached_property
    def lanuk_neobiota(self) -> dict:
        return self._load("lanuk_neobiota_prunus_serotina.json")

    def layer_geojson(self, key: str) -> dict:
        files = {
            "districts": "bonn_districts.geojson",
            "wooded": "bonn_wooded_landuse.geojson",
            "nsg": "lanuk_nsg_bonn.geojson",
            "ffh": "lanuk_ffh_bonn.geojson",
            "biotopes": "lanuk_protected_biotopes_bonn.geojson",
            "gbif": "gbif_prunus_serotina_bonn.geojson",
        }
        return self._load(files[key])

    # -- lookups -----------------------------------------------------------

    @staticmethod
    def _candidates(items: list[_Indexed], lat: float, lon: float, buffer_m: float) -> list[_Indexed]:
        dx, dy = buffer_m * _DEG_PER_M_LON, buffer_m * _DEG_PER_M_LAT
        return [i for i in items if i.bbox[0] - dx <= lon <= i.bbox[2] + dx and i.bbox[1] - dy <= lat <= i.bbox[3] + dy]

    def district_at(self, lat: float, lon: float) -> dict | None:
        candidates = self._candidates(self.districts, lat, lon, self.t.boundary_snap_m)
        for i in candidates:
            if geometry_contains(i.feature["geometry"], lat, lon):
                return i.feature["properties"]
        near = [(distance_to_geometry_m(i.feature["geometry"], lat, lon), i) for i in candidates]
        near = [(d, i) for d, i in near if d <= self.t.boundary_snap_m]
        if near:
            d, i = min(near, key=lambda x: x[0])
            return {**i.feature["properties"], "snapped_m": round(d, 1)}
        return None

    def in_bonn(self, lat: float, lon: float) -> bool:
        return self.district_at(lat, lon) is not None

    def landuse_at(self, lat: float, lon: float) -> dict | None:
        for i in self._candidates(self.wooded, lat, lon, 0):
            if geometry_contains(i.feature["geometry"], lat, lon):
                return i.feature["properties"]
        return None

    def _areas_near(self, items: list[_Indexed], lat: float, lon: float, buffer_m: float) -> list[dict]:
        hits = []
        for i in self._candidates(items, lat, lon, buffer_m):
            d = distance_to_geometry_m(i.feature["geometry"], lat, lon)
            if d <= buffer_m:
                p = i.feature["properties"]
                hits.append({"id": p.get("id"), "name": p.get("name"), "distance_m": round(d, 1),
                             "inside": d == 0.0, "factsheet_url": p.get("factsheet_url")})
        return sorted(hits, key=lambda h: h["distance_m"])

    def gbif_near(self, lat: float, lon: float) -> dict:
        precise, grid = [], []
        for f in self.gbif:
            flon, flat = f["geometry"]["coordinates"]
            p = f["properties"]
            d = haversine_m(lat, lon, flat, flon)
            unc = p.get("coordinate_uncertainty_m")
            item = {**{k: p[k] for k in ("gbif_id", "dataset", "year", "coordinate_uncertainty_m",
                                          "automated_identification", "license", "url")}, "distance_m": round(d)}
            if unc and unc >= self.t.external_record_grid_uncertainty_m:
                if d <= unc:
                    grid.append(item)
            elif d <= self.t.external_record_radius_m:
                precise.append(item)
        return {"within_m": self.t.external_record_radius_m,
                "records": sorted(precise, key=lambda r: r["distance_m"]), "grid_records": grid}

    def context(self, lat: float, lon: float) -> dict:
        district = self.district_at(lat, lon)
        buffer_m = self.t.protected_area_buffer_m
        return {
            "in_bonn": district is not None,
            "district": district,
            "landuse": self.landuse_at(lat, lon),
            "nsg": self._areas_near(self.nsg, lat, lon, buffer_m),
            "ffh": self._areas_near(self.ffh, lat, lon, buffer_m),
            "protected_biotopes": self._areas_near(self.biotopes, lat, lon, buffer_m / 2),
            "gbif": self.gbif_near(lat, lon),
            "lanuk_neobiota_points_in_bonn": self.lanuk_neobiota["bonn_record_count"],
            "reference_versions": {k: v["retrieved_at"] for k, v in self.manifest["sources"].items()},
        }
