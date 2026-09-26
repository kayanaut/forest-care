import json

import pytest

from forestcare.config import REFERENCE_DIR
from forestcare.geo import (distance_to_geometry_m, distance_to_polyline_m, from_local_xy, geometry_contains,
                            haversine_m, to_local_xy, wgs84_to_utm32)

from .conftest import KOTTENFORST


def test_haversine_bonn_city_hall_to_post_tower():
    # Altes Rathaus -> Post Tower, roughly 2.9 km
    assert haversine_m(50.7353, 7.1022, 50.7163, 7.1301) == pytest.approx(2900, rel=0.1)


def test_local_projection_round_trip():
    lat, lon = from_local_xy(120.0, -45.0, *KOTTENFORST)
    x, y = to_local_xy(lat, lon, *KOTTENFORST)
    assert (x, y) == pytest.approx((120.0, -45.0), abs=0.01)


@pytest.mark.parametrize("lat,lon,x,y", [
    # Real records from LANUK's Neobiota layer, which carries both WGS84 geometry and UTM32 x/y.
    (52.150089522000094, 6.995553352000104, 362861.2123, 5779626.328),
    (52.367481695000045, 9.034138831000082, 502324.4102, 5801912.402),
])
def test_utm32_matches_lanuk_records(lat, lon, x, y):
    ex, ny = wgs84_to_utm32(lat, lon)
    assert ex == pytest.approx(x, abs=0.01)
    assert ny == pytest.approx(y, abs=0.01)


def test_polygon_with_hole():
    square = [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]
    hole = [[0.4, 0.4], [0.6, 0.4], [0.6, 0.6], [0.4, 0.6], [0.4, 0.4]]
    geom = {"type": "Polygon", "coordinates": [square, hole]}
    assert geometry_contains(geom, 0.2, 0.2)
    assert not geometry_contains(geom, 0.5, 0.5)
    assert distance_to_geometry_m(geom, 0.2, 0.2) == 0


def test_distance_to_polyline():
    lat, lon = KOTTENFORST
    a, b = from_local_xy(-50, 0, lat, lon), from_local_xy(50, 0, lat, lon)
    p = from_local_xy(10, 7, lat, lon)
    assert distance_to_polyline_m([[a[1], a[0]], [b[1], b[0]]], *p) == pytest.approx(7, abs=0.05)


def test_reference_snapshots_are_real_and_documented():
    manifest = json.loads((REFERENCE_DIR / "manifest.json").read_text())
    for key, entry in manifest["sources"].items():
        assert entry["is_mock"] is False, key
        assert entry["license"] and entry["source_url"] and entry["sha256"], key
        assert (REFERENCE_DIR / entry["file"]).exists(), key


def test_context_in_kottenforst(ref):
    ctx = ref.context(*KOTTENFORST)
    assert ctx["in_bonn"]
    assert ctx["district"]["district"] == "Kottenforst"
    assert ctx["district"]["stadtbezirk"] == "Bonn"
    assert ctx["landuse"]["landuse"] == "Wald"
    assert [a["id"] for a in ctx["nsg"]] == ["BN-003"] and ctx["nsg"][0]["inside"]
    assert any(a["id"] == "DE-5308-303" for a in ctx["ffh"])


def test_context_outside_bonn(ref):
    # Alfter-Witterschlick, just west of the city boundary
    assert not ref.context(50.690, 7.000)["in_bonn"]


def test_rhine_bank_gap_is_snapped(ref):
    # The statistical districts stop at the Rhine bank; a point 0.7 m beyond it still belongs to Bonn.
    d = ref.district_at(50.7144429, 7.1484198)
    assert d["district"] == "Hochkreuz-Bundesviertel" and d["snapped_m"] < 1


def test_gbif_context_separates_grid_records(ref):
    # Near a precise iNaturalist record on the Venusberg
    ctx = ref.context(50.709532, 7.0984)
    assert any(r["distance_m"] < 5 for r in ctx["gbif"]["records"])
    for r in ctx["gbif"]["records"]:
        assert r["coordinate_uncertainty_m"] is None or r["coordinate_uncertainty_m"] < 1000


def test_species_profile_cites_lanuk(ref):
    sp = ref.species
    assert sp["taxon"] == "Prunus serotina"
    cited = {i["source"] for i in sp["identification"] + sp["impacts"] + sp["status_nrw"]}
    assert cited <= set(sp["sources"])
    assert all(s["url"].startswith("https://") for s in sp["sources"].values())
