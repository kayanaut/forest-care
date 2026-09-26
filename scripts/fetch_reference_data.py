"""Download real Bonn / NRW reference data into data/reference/ with provenance.

Run:  uv run python scripts/fetch_reference_data.py

Every file written here is REAL third-party data (never simulated). Each gets
an entry in data/reference/manifest.json with publisher, source URL, licence,
retrieval time and SHA-256, so the app can show exactly where context came from.
The snapshots are committed so the prototype works offline; re-run this script
to refresh them.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from forestcare.geo import douglas_peucker, geometry_bbox, geometry_contains  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "data" / "reference"
BONN_BBOX = (7.00, 50.62, 7.22, 50.78)  # lon/lat, generous; filtered by the real boundary afterwards
UA = {"User-Agent": "forestcare-bonn-prototype/0.1 (research prototype)"}

STADTBEZIRKE = {"1": "Bonn", "2": "Bad Godesberg", "3": "Beuel", "4": "Hardtberg"}
WOODED_LANDUSE = {
    2600: "forest",        # Wald
    2645: "urban_green",   # Grün-/ Parkfläche mit Baumbestand
    2670: "urban_green",   # Verkehrsbegleitgrün mit Baumbestand
    2680: "urban_green",   # sonstige (kl.) Gehölzfläche
    2475: "urban_green",   # Grünland (Landwirtschaft) mit Baumbestand
}
LINFOS_WFS = "https://www.wfs.nrw.de/umwelt/linfos"
NEOBIOTA_FS = (
    "https://www.arcgishostedserver.nrw.de/arcgis/rest/services/Hosted/"
    "erfassungen_neobiota_sichtlayer_public/FeatureServer/0"
)
GBIF_TAXON_KEY = 3021850  # Prunus serotina Ehrh. (GBIF backbone, exact match)


def get(url: str, params: dict | None = None, timeout: int = 180) -> bytes:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


def get_json(url: str, params: dict | None = None) -> dict:
    return json.loads(get(url, params))


def simplify(geometry: dict, tol_m: float) -> dict:
    def ring(r):
        pts = douglas_peucker([[round(x, 6), round(y, 6)] for x, y, *_ in r], tol_m)
        return pts if len(pts) >= 4 else [[round(x, 6), round(y, 6)] for x, y, *_ in r]

    if geometry["type"] == "Polygon":
        return {"type": "Polygon", "coordinates": [ring(r) for r in geometry["coordinates"]]}
    return {"type": "MultiPolygon", "coordinates": [[ring(r) for r in p] for p in geometry["coordinates"]]}


def write(name: str, payload: dict) -> tuple[str, int]:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    (OUT / name).write_bytes(data)
    return hashlib.sha256(data).hexdigest(), len(data)


class Bonn:
    """City boundary as the union of the 65 statistical districts."""

    def __init__(self, districts: list[dict]):
        self.districts = districts
        self.bboxes = [geometry_bbox(d["geometry"]) for d in districts]

    def contains(self, lat: float, lon: float) -> bool:
        for d, (x0, y0, x1, y1) in zip(self.districts, self.bboxes):
            if x0 <= lon <= x1 and y0 <= lat <= y1 and geometry_contains(d["geometry"], lat, lon):
                return True
        return False

    def intersects(self, geometry: dict) -> bool:
        """Approximate: any vertex of either shape inside the other."""
        polys = geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
        if any(self.contains(y, x) for p in polys for x, y, *_ in p[0][::5]):
            return True
        return any(
            geometry_contains(geometry, y, x)
            for d in self.districts
            for p in (d["geometry"]["coordinates"] if d["geometry"]["type"] == "MultiPolygon" else [d["geometry"]["coordinates"]])
            for x, y, *_ in p[0][::10]
        )


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    manifest: dict[str, dict] = {}

    def record(key: str, file: str, meta: dict, payload: dict, count: int | None = None) -> None:
        sha, size = write(file, payload)
        manifest[key] = {"file": file, "retrieved_at": now, "sha256": sha, "bytes": size,
                         "feature_count": count, "is_mock": False, **meta}
        print(f"  {file}: {count} features, {size/1e6:.2f} MB")

    # 1. City of Bonn — statistical districts (CC0) --------------------------------
    print("Bonn statistical districts ...")
    raw = get_json("https://stadtplan.bonn.de/geojson?OD=4859")
    districts = []
    for f in raw["features"]:
        code = str(f["properties"]["sta_bezirk"])
        districts.append({"type": "Feature", "geometry": simplify(f["geometry"], 1.0), "properties": {
            "district_code": code,
            "district": f["properties"]["sta_bezirk_bez"],
            "stadtbezirk": STADTBEZIRKE.get(code[0], "unknown"),
        }})
    bonn = Bonn(districts)
    record("bonn_districts", "bonn_districts.geojson", {
        "title": "Statistische Bezirke Bonn",
        "publisher": "Bundesstadt Bonn (Offene Daten Bonn)",
        "source_url": "https://opendata.bonn.de/dataset/statistische-bezirke",
        "download_url": "https://stadtplan.bonn.de/geojson?OD=4859",
        "license": "CC0 1.0 (Creative Commons Zero)",
        "used_for": "Bonn boundary (ingest filter) and district/Stadtbezirk names for reports",
        "processing": "Douglas-Peucker 1 m, coordinates rounded to 6 decimals; Stadtbezirk derived from first digit of district code",
    }, {"type": "FeatureCollection", "features": districts}, len(districts))

    # 2. City of Bonn — land use (Realnutzung), wooded classes only (CC0) ----------
    print("Bonn land use (Realnutzung) ...")
    raw = get_json("https://stadtplan.bonn.de/geojson?OD=4409")
    wooded = []
    for f in raw["features"]:
        p = f["properties"]
        env = WOODED_LANDUSE.get(p["realnutzung_zvs"])
        if env:
            wooded.append({"type": "Feature", "geometry": simplify(f["geometry"], 1.5), "properties": {
                "landuse_id": p["flaechen_id"], "landuse_code": p["realnutzung_zvs"],
                "landuse": p["realnutzung"], "environment": env, "area_m2": p["netto_flaeche"],
            }})
    record("bonn_wooded_landuse", "bonn_wooded_landuse.geojson", {
        "title": "Landnutzungskartierung Realnutzung — wooded classes",
        "publisher": "Bundesstadt Bonn (Offene Daten Bonn)",
        "source_url": "https://opendata.bonn.de/dataset/landnutzungskartierung-realnutzung",
        "download_url": "https://stadtplan.bonn.de/geojson?OD=4409",
        "license": "CC0 1.0 (Creative Commons Zero)",
        "source_last_modified": "2021-09-06 (per portal metadata)",
        "used_for": "Forest / wooded urban green mask; land-use class shown to reviewers; simulator survey areas",
        "processing": "Kept classes 2600 Wald, 2645, 2670, 2680, 2475 (with trees); Douglas-Peucker 1.5 m",
    }, {"type": "FeatureCollection", "features": wooded}, len(wooded))

    # 3/4. LANUK — nature reserves (NSG) and FFH sites (dl-de/zero-2-0) -------------
    def linfos(type_name: str) -> list[dict]:
        x0, y0, x1, y1 = BONN_BBOX
        return get_json(LINFOS_WFS, {
            "SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature",
            "TYPENAMES": f"wfs_linfos:{type_name}", "SRSNAME": "EPSG:4326",
            "BBOX": f"{y0},{x0},{y1},{x1},urn:ogc:def:crs:EPSG::4326", "OUTPUTFORMAT": "GEOJSON",
        })["features"]

    print("LANUK NSG ...")
    nsg = [{"type": "Feature", "geometry": simplify(f["geometry"], 1.0), "properties": {
        "id": p["kennung"], "name": p["gebietsname"].replace("\\\"", "\""),
        "protection_goal": (p.get("schutzziel") or "")[:700],
        "area": p.get("offflaech"), "in_force_since": p.get("inkraft_seit"),
        "factsheet_url": p.get("hyperlink"),
    }} for f in linfos("nsg_polygon") if (p := f["properties"])["kennung"].startswith("BN-")]
    record("lanuk_nsg", "lanuk_nsg_bonn.geojson", {
        "title": "Naturschutzgebiete (NSG) in Bonn",
        "publisher": "LANUK NRW — Landschaftsinformationssammlung @LINFOS",
        "source_url": "https://open.nrw/dataset/ccbfb8e5-541d-438e-9f6c-466f4d4646a2",
        "download_url": LINFOS_WFS + " (WFS 2.0, wfs_linfos:nsg_polygon)",
        "license": "Datenlizenz Deutschland – Zero – 2.0 (dl-de/zero-2-0)",
        "used_for": "Context flag 'inside / near nature reserve' (LANUK prioritisation criterion)",
        "processing": "Kept features with Bonn identifiers (BN-*); Douglas-Peucker 1 m; protection goal truncated",
    }, {"type": "FeatureCollection", "features": nsg}, len(nsg))

    print("LANUK FFH ...")
    ffh = []
    for f in linfos("ffh_polygon"):
        p = f["properties"]
        # Rhine fish protection zones: aquatic, irrelevant for a terrestrial shrub, very large polygon.
        if p["kennung"] == "DE-4405-301" or not bonn.intersects(f["geometry"]):
            continue
        ffh.append({"type": "Feature", "geometry": simplify(f["geometry"], 2.0), "properties": {
            "id": p["kennung"], "name": p["gebietsname"],
            "habitat_types": p.get("lrt_agg"), "threats": p.get("gefahrbeschr_kurz"),
            "caretakers": p.get("betreuer_agg"), "factsheet_url": p.get("hyperlink"),
        }})
    record("lanuk_ffh", "lanuk_ffh_bonn.geojson", {
        "title": "FFH-Gebiete (Natura 2000) intersecting Bonn",
        "publisher": "LANUK NRW — Landschaftsinformationssammlung @LINFOS",
        "source_url": "https://open.nrw/dataset/ca423bb5-95fb-475a-a718-ba6d32647270",
        "download_url": LINFOS_WFS + " (WFS 2.0, wfs_linfos:ffh_polygon)",
        "license": "Datenlizenz Deutschland – Zero – 2.0 (dl-de/zero-2-0)",
        "used_for": "Context flag 'inside / near FFH site'; habitat types shown to reviewers",
        "processing": "Kept sites intersecting Bonn except DE-4405-301 (Rhine fish zones); Douglas-Peucker 2 m",
    }, {"type": "FeatureCollection", "features": ffh}, len(ffh))

    print("LANUK legally protected biotopes ...")
    biotopes = []
    for f in linfos("gbt_polygon"):
        g = f["geometry"]
        x0, y0, x1, y1 = geometry_bbox(g)
        if bonn.contains((y0 + y1) / 2, (x0 + x1) / 2):
            biotopes.append({"type": "Feature", "geometry": simplify(g, 1.0),
                             "properties": {"id": f["properties"].get("localid")}})
    record("lanuk_protected_biotopes", "lanuk_protected_biotopes_bonn.geojson", {
        "title": "Gesetzlich geschützte Biotope (§30 BNatSchG / §42 LNatSchG NRW) in Bonn",
        "publisher": "LANUK NRW — Landschaftsinformationssammlung @LINFOS",
        "source_url": "https://linfos.naturschutzinformationen.nrw.de/atlinfos/de/einleitung",
        "download_url": LINFOS_WFS + " (WFS 2.0, wfs_linfos:gbt_polygon)",
        "license": "Datenlizenz Deutschland – Zero – 2.0 (dl-de/zero-2-0) — assumed same as other LINFOS layers; confirm with LANUK",
        "used_for": "Context flag 'inside / near protected biotope' (LANUK prioritisation criterion)",
        "processing": "Kept polygons whose bbox centre lies in Bonn; Douglas-Peucker 1 m",
    }, {"type": "FeatureCollection", "features": biotopes}, len(biotopes))

    # 5. GBIF occurrences of Prunus serotina inside Bonn ---------------------------
    print("GBIF occurrences ...")
    x0, y0, x1, y1 = BONN_BBOX
    results, offset = [], 0
    while True:
        page = get_json("https://api.gbif.org/v1/occurrence/search", {
            "taxonKey": GBIF_TAXON_KEY, "hasCoordinate": "true", "limit": 300, "offset": offset,
            "decimalLatitude": f"{y0},{y1}", "decimalLongitude": f"{x0},{x1}",
        })
        results += page["results"]
        offset += 300
        if page["endOfRecords"]:
            break
    dataset_titles: dict[str, str] = {}
    gbif = []
    for r in results:
        lat, lon = r["decimalLatitude"], r["decimalLongitude"]
        if not bonn.contains(lat, lon):
            continue
        key = r["datasetKey"]
        if key not in dataset_titles:
            dataset_titles[key] = get_json(f"https://api.gbif.org/v1/dataset/{key}").get("title", key)
        title = dataset_titles[key]
        gbif.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]}, "properties": {
            "gbif_id": r["key"], "dataset": title, "basis_of_record": r.get("basisOfRecord"),
            "event_date": r.get("eventDate"), "year": r.get("year"),
            "coordinate_uncertainty_m": r.get("coordinateUncertaintyInMeters"),
            "license": r.get("license"),
            # Pl@ntNet's "automatically identified" dataset has no human confirmation.
            "automated_identification": "automatically identified" in title.lower(),
            "url": f"https://www.gbif.org/occurrence/{r['key']}",
        }})
    record("gbif_prunus_serotina", "gbif_prunus_serotina_bonn.geojson", {
        "title": "GBIF occurrences of Prunus serotina within Bonn",
        "publisher": "GBIF.org (aggregating iNaturalist, Pl@ntNet, observation.org, NABU|naturgucker, floristic mapping, herbaria)",
        "source_url": f"https://www.gbif.org/species/{GBIF_TAXON_KEY}",
        "download_url": "https://api.gbif.org/v1/occurrence/search (taxonKey=3021850, Bonn bbox, filtered to city boundary)",
        "license": "Per record: CC0 / CC BY 4.0 / CC BY-NC 4.0 (see each feature). Cite as GBIF.org occurrence search.",
        "used_for": "Context only: 'external records nearby' shown to reviewers, with dataset, accuracy and ID method",
        "processing": "Observer names dropped; records outside Bonn boundary dropped",
    }, {"type": "FeatureCollection", "features": gbif}, len(gbif))

    # 6. LANUK Neobiota portal — validated find points ------------------------------
    print("LANUK Neobiota find points ...")
    where = "art LIKE '%Prunus serotina%'"
    nrw = get_json(NEOBIOTA_FS + "/query", {"where": where, "outFields": "*", "outSR": 4326, "f": "geojson"})
    in_bonn = [f for f in nrw["features"] if f["geometry"] and bonn.contains(f["geometry"]["coordinates"][1], f["geometry"]["coordinates"][0])]
    vocab = {}
    for fld in ("individuen", "bed_fl", "stadium", "repro", "lebensraum", "beseitigt"):
        rows = get_json(NEOBIOTA_FS + "/query", {"where": "artgruppe IN ('Gehölze','Landpflanzen')", "outFields": fld,
                                                 "returnDistinctValues": "true", "returnGeometry": "false", "f": "json"})
        vocab[fld] = sorted({r["attributes"][fld] for r in rows["features"] if r["attributes"][fld] not in (None, "", "None")})
    record("lanuk_neobiota", "lanuk_neobiota_prunus_serotina.json", {
        "title": "LANUK Neobiota portal — public validated find points for Prunus serotina",
        "publisher": "LANUK NRW — Neobiota in NRW",
        "source_url": "https://neobiota.naturschutzinformationen.nrw.de/neobiota/de/fundpunkte/webapp",
        "download_url": NEOBIOTA_FS,
        "license": "Not stated on the service — used only as a count and as a field vocabulary; confirm terms with LANUK",
        "used_for": "Shows how many LANUK-validated points exist in Bonn; field vocabulary for the report draft export",
        "processing": "Queried all NRW records for the species; counted those inside Bonn; collected distinct values of plant fields",
    }, {
        "query": where,
        "nrw_record_count": len(nrw["features"]),
        "bonn_record_count": len(in_bonn),
        "bonn_records": in_bonn,
        "field_vocabulary": vocab,
    }, len(in_bonn))

    manifest_out = {"generated_at": now, "note": "All files listed here are real third-party data. "
                    "species_profile_prunus_serotina.json is a hand-written summary of LANUK pages (see its sources).",
                    "sources": manifest}
    (OUT / "manifest.json").write_text(json.dumps(manifest_out, ensure_ascii=False, indent=2))
    print("manifest.json written")


if __name__ == "__main__":
    main()
