"""Exports for people outside the tool.

The LANUK draft uses the field names and value classes of LANUK's public
Neobiota find-point layer, so an expert can copy verified stands into the
official reporting form. It is a draft: fields that need a botanist's
judgement (stadium, lebensraum, repro, beseitigt) are left for the expert, with
hints next to them. Nothing is submitted automatically.
"""

from __future__ import annotations

import csv
import io
import math
import sqlite3

from .config import TARGET_LABEL_DE
from .geo import wgs84_to_utm32

_INDIVIDUEN = [(1, "1 Ind."), (5, "2-5 Ind."), (25, "6-25 Ind."), (100, "26-100 Ind."),
               (1000, "> 100 Ind."), (10000, "> 1000 Ind.")]
_BED_FL = [(1, "bis 1 qm"), (5, "1-5 qm"), (25, "6-25 qm"), (100, "26-100 qm"),
           (1000, "über 100 qm"), (10000, "über 1000 qm")]


def individuen_class(n: int) -> str:
    return next((label for limit, label in _INDIVIDUEN if n <= limit), "> 10000 Ind.")


def bed_fl_class(area_m2: float) -> str:
    return next((label for limit, label in _BED_FL if area_m2 <= limit), "über 10.000 qm")


def extent_m2(radius_m: float) -> float:
    """Very rough area: circle around the confirmed positions plus one crown radius."""
    return math.pi * (radius_m + 1.5) ** 2


def verified(stands: list[dict]) -> list[dict]:
    return [s for s in stands if s["counts"]["confirmed"] > 0]


def stands_geojson(stands: list[dict], include_unverified: bool) -> dict:
    chosen = stands if include_unverified else verified(stands)
    return {
        "type": "FeatureCollection",
        "name": "forestcare_bonn_prunus_serotina_stands",
        "note": "Status uses expert-confirmed observations only. Check 'source_kinds' — 'simulated' means demo data.",
        "features": [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [s["lon"], s["lat"]]},
                      "properties": {k: v for k, v in s.items() if k not in ("lat", "lon")}} for s in chosen],
    }


LANUK_COLUMNS = [
    "art", "artgruppe", "funddatum", "anz_abs", "individuen", "bed_fl", "stadium", "repro", "lebensraum",
    "fundort", "bemerkung", "beseitigt", "x_utm32", "y_utm32",
    # helper columns (not LANUK fields)
    "lat", "lon", "stand_id", "stand_status", "confirmed_observations", "reviewers",
    "hint_landuse", "hint_heights", "hint_phenology", "hint_management", "data_status",
]


def lanuk_draft_csv(conn: sqlite3.Connection, stands: list[dict]) -> str:
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=LANUK_COLUMNS, delimiter=";")
    w.writeheader()
    for s in verified(stands):
        rows = conn.execute(
            "SELECT o.observed_at, o.gnss_accuracy_m, o.original_path, r.reviewer, r.source_kind AS review_kind,"
            " COALESCE(r.height_class, o.height_class) AS height_class, COALESCE(r.phenology, o.phenology) AS phenology"
            " FROM observations o JOIN reviews r ON r.id = (SELECT MAX(id) FROM reviews WHERE observation_id = o.id)"
            " WHERE o.stand_id = ? AND o.review_status = 'confirmed'", (s["id"],)).fetchall()
        note = conn.execute("SELECT action_date, text FROM stand_notes WHERE stand_id = ? AND kind = 'management_action'"
                            " ORDER BY action_date DESC LIMIT 1", (s["id"],)).fetchone()
        x, y = wgs84_to_utm32(s["lat"], s["lon"])
        reviewers = sorted({r["reviewer"] for r in rows})
        known = [r["gnss_accuracy_m"] for r in rows if r["gnss_accuracy_m"] is not None]
        accuracy = f"Lagegenauigkeit ca. {max(known):.0f} m" if known else "Lagegenauigkeit nicht angegeben"
        photos = sum(1 for r in rows if r["original_path"])
        method = "Roboter-Monitoring mit Bildklassifikation" if photos < len(rows) else "Feldfotos"
        if 0 < photos < len(rows):
            method += f" und {photos} Feldfoto(s)"
        simulated = "simulated" in s["source_kinds"] or any(r["review_kind"] == "simulated" for r in rows)
        w.writerow({
            "art": TARGET_LABEL_DE,
            "artgruppe": "Gehölze",
            "funddatum": s["last_confirmed"],
            "anz_abs": s["latest_plants_confirmed"] if s["latest_plants_confirmed"] is not None else "",
            "individuen": individuen_class(s["latest_plants_confirmed"]) if s["latest_plants_confirmed"] else "keine Angabe",
            "bed_fl": bed_fl_class(extent_m2(s["radius_m"])),
            "stadium": "", "repro": "", "lebensraum": "",
            "fundort": f"Bonn-{s['stadtbezirk']}, {s['district']}; {s['landuse'] or 'nicht als Gehölzfläche kartiert'}",
            "bemerkung": (f"{method}; {len(rows)} Beobachtung(en) fachlich bestätigt. "
                          f"{accuracy}; Fläche grob aus Positionen geschätzt. Bestand {s['id']}."),
            "beseitigt": "keine Angabe",
            "x_utm32": f"{x:.1f}", "y_utm32": f"{y:.1f}",
            "lat": s["lat"], "lon": s["lon"],
            "stand_id": s["id"], "stand_status": s["status"],
            "confirmed_observations": len(rows), "reviewers": ", ".join(reviewers),
            "hint_landuse": s["landuse"] or "",
            "hint_heights": ", ".join(sorted({r["height_class"] for r in rows if r["height_class"]})),
            "hint_phenology": ", ".join(sorted({r["phenology"] for r in rows if r["phenology"]})),
            "hint_management": f"{note['action_date']}: {note['text']}" if note else "",
            "data_status": "ENTWURF – SIMULIERTE DATEN, NICHT EINREICHEN" if simulated
                           else "ENTWURF – vor Meldung fachlich prüfen",
        })
    return out.getvalue()
