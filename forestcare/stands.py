"""Stands (Bestände): the unit that makes repeated observations comparable.

A stand groups robot observations whose positions agree within the plausible
clump spread plus their combined GNSS uncertainty (see ingest.link_stand). For each survey year the stand is either not surveyed (no mission track
came close enough), surveyed without detections, or surveyed with detections.
Trends use only expert-confirmed observations; unreviewed detections are shown
as pending work, never as population change.

Nothing in here recommends management. `inspection_reasons` says why a person
should look at a stand again; what to do about it is their decision.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass

from .config import Thresholds
from .db import log_event, now_iso
from .geo import distance_to_polyline_m, haversine_m
from .models import StandNoteIn
from .reference import ReferenceData

STATUS_INFO = {
    "new": ("New", "First confirmed in the latest survey year."),
    "expanding": ("Expanding", "Confirmed plant count increased since the previous confirmed year."),
    "stable": ("Stable", "Confirmed plant count within the stable band."),
    "confirmed_present": ("Present (not counted)",
                          "Confirmed again in the latest year, but without a plant count to compare "
                          "(e.g. field photos without a count label)."),
    "declining": ("Declining", "Confirmed plant count decreased since the previous confirmed year."),
    "not_redetected": ("Not re-detected", "Surveyed in the latest year, confirmed earlier, but not found again."),
    "awaiting_confirmation": ("Awaiting confirmation",
                              "Confirmed earlier; latest detections are pending review or marked uncertain."),
    "not_surveyed": ("Not surveyed recently", "Confirmed earlier; no mission covered it in the latest year."),
    "unverified": ("Unverified candidate", "No reviewed detection yet, or only uncertain ones."),
    "not_target": ("Look-alike location", "Nothing confirmed; reviewers rejected detections here as another species."),
}


@dataclass
class Mission:
    id: str
    year: int
    started_at: str
    track: list
    detection_range_m: float | None
    area_name: str | None
    protocol: str = "transect"

    def covers(self, lat: float, lon: float, extra_m: float) -> bool:
        """Only systematic surveys define coverage. Opportunistic photo walks never do: nobody
        recorded what the photographer looked at and did not photograph."""
        if self.protocol != "transect" or self.detection_range_m is None:
            return False
        return any(distance_to_polyline_m(line, lat, lon) <= self.detection_range_m + extra_m for line in self.track)


def load_missions(conn: sqlite3.Connection) -> list[Mission]:
    return [Mission(r["id"], r["year"], r["started_at"], json.loads(r["track_json"]), r["detection_range_m"],
                    r["area_name"], r["protocol"])
            for r in conn.execute("SELECT * FROM missions ORDER BY started_at")]


def plant_count(o: sqlite3.Row) -> int:
    """Plants a confirmed observation contributes to a count.

    An expert's count label wins. Otherwise transect detections use the device's
    estimate. Observations without a count (unlabelled field photos, operator
    marks) and anything from opportunistic missions prove presence but count 0.
    """
    if o["label_plant_count"] is not None:
        return o["label_plant_count"]
    if o["protocol"] == "transect" and o["plant_count_est"] is not None:
        return o["plant_count_est"]
    return 0


def _centroid(obs: list[sqlite3.Row]) -> tuple[float, float, float]:
    lat = statistics.fmean(o["lat"] for o in obs)
    lon = statistics.fmean(o["lon"] for o in obs)
    radius = max(haversine_m(lat, lon, o["lat"], o["lon"]) for o in obs)
    return lat, lon, radius


def _year_rows(obs: list[sqlite3.Row], missions: list[Mission], years: list[int],
               lat: float, lon: float, radius: float) -> list[dict]:
    rows = []
    for year in years:
        yo = [o for o in obs if int(o["observed_at"][:4]) == year]
        # Count a mission as coverage only if its track came within camera range of the stand's
        # edge (spread capped at 10 m, so a wide GNSS scatter cannot fake coverage).
        covering = [m.id for m in missions if m.year == year and m.covers(lat, lon, min(radius, 10.0))]
        # A detection also shows the place was visited, except a rejected opportunistic photo:
        # a photo of some other shrub says nothing about whether P. serotina is still here.
        evidence = {o["mission_id"] for o in yo if o["protocol"] == "transect" or o["review_status"] != "rejected"}
        mission_ids = sorted(set(covering) | evidence)
        status = Counter(o["review_status"] for o in yo)
        # Plants seen in one mission are summed; across missions take the max to avoid double counting.
        per_mission = defaultdict(int)
        for o in yo:
            if o["review_status"] == "confirmed":
                per_mission[o["mission_id"]] += plant_count(o)
        surveyed = bool(mission_ids)
        counted = max(per_mission.values(), default=0)
        if not surveyed:
            plants = None
        elif status["confirmed"] and not counted:
            plants = None  # present, but only uncounted photos
        else:
            plants = counted
        rows.append({
            "year": year,
            "surveyed": surveyed,
            "missions": mission_ids,
            "detections": len(yo),
            "confirmed": status["confirmed"],
            "rejected": status["rejected"],
            "pending": status["pending"],
            "uncertain": status["uncertain"] + status["field_visit"],
            "plants_confirmed": plants,
            "presence_only": bool(status["confirmed"]) and plants is None,
            "phenology": sorted({o["phenology_eff"] for o in yo if o["phenology_eff"]}),
        })
    return rows


def _classify(rows: list[dict], t: Thresholds) -> tuple[str, dict | None, dict | None]:
    """Return (status, latest_row, previous_confirmed_row)."""
    confirmed_rows = [r for r in rows if r["confirmed"]]
    latest = rows[-1] if rows else None
    if not confirmed_rows:
        return ("not_target" if any(r["rejected"] for r in rows) else "unverified"), latest, None
    if not latest["surveyed"]:
        return "not_surveyed", latest, confirmed_rows[-1]
    if latest["confirmed"]:
        if not any(r["confirmed"] for r in rows[:-1]):
            return "new", latest, None
        prev = next((r for r in reversed(rows[:-1]) if r["confirmed"] and r["plants_confirmed"]), None)
        if prev is None or not latest["plants_confirmed"]:
            return "confirmed_present", latest, confirmed_rows[-2] if len(confirmed_rows) > 1 else None
        a, b = prev["plants_confirmed"], latest["plants_confirmed"]
        if b >= a * t.expand_ratio and b - a >= t.expand_min_plants:
            return "expanding", latest, prev
        if b <= a * t.decline_ratio and a - b >= t.decline_min_plants:
            return "declining", latest, prev
        return "stable", latest, prev
    if latest["pending"] or latest["uncertain"]:
        return "awaiting_confirmation", latest, confirmed_rows[-1]
    return "not_redetected", latest, confirmed_rows[-1]


def summarize_stand(stand_id: str, obs: list[sqlite3.Row], notes: list[sqlite3.Row], missions: list[Mission],
                    years: list[int], ref: ReferenceData, t: Thresholds, with_context: bool = False) -> dict:
    confirmed = [o for o in obs if o["review_status"] == "confirmed"]
    lat, lon, radius = _centroid(confirmed or obs)
    rows = _year_rows(obs, missions, years, lat, lon, radius)
    status, latest, prev = _classify(rows, t)
    ctx = ref.context(lat, lon)
    counts = Counter(o["review_status"] for o in obs)

    reasons: list[dict] = []
    if status == "not_redetected":
        reasons.append({"code": "not_redetected", "text":
                        f"Surveyed in {latest['year']} but not re-detected (confirmed in {prev['year']}). "
                        "Could be removal, die-back or a missed detection."})
    if status == "expanding":
        reasons.append({"code": "expanding", "text":
                        f"Confirmed plants rose from {prev['plants_confirmed']} ({prev['year']}) "
                        f"to {latest['plants_confirmed']} ({latest['year']})."})
    protected = [a["name"] for a in ctx["nsg"] + ctx["ffh"]] + (["protected biotope"] if ctx["protected_biotopes"] else [])
    if status in ("new", "expanding") and protected:
        reasons.append({"code": "protected_area", "text": f"{STATUS_INFO[status][0]} stand in or near: {', '.join(protected)}."})
    unclear = counts["uncertain"] + counts["field_visit"]
    if unclear:
        reasons.append({"code": "uncertain_review", "text": f"{unclear} observation(s) marked uncertain or needing a field visit."})
    mgmt = sorted((n for n in notes if n["kind"] == "management_action"), key=lambda n: n["action_date"])
    if mgmt:
        last_action = mgmt[-1]["action_date"]
        after = [o for o in obs if o["observed_at"][:10] > last_action and o["review_status"] != "rejected"]
        if after:
            reasons.append({"code": "after_management", "text":
                            f"{len(after)} detection(s) after the management action recorded on {last_action} "
                            "(LANUK notes stump sprouting and root suckers; follow-up monitoring is expected)."})
    reviewed_years = [r for r in rows if r["confirmed"] or r["rejected"]]
    if reviewed_years and reviewed_years[-1]["confirmed"] and reviewed_years[-1]["rejected"]:
        reasons.append({"code": "mixed_reviews", "text":
                        f"Confirmed and rejected detections in {reviewed_years[-1]['year']}: "
                        "possibly mixed with a look-alike species."})
    known = [o["gnss_accuracy_m"] for o in obs if o["gnss_accuracy_m"] is not None]
    median_gnss = statistics.median(known) if known else 0.0
    if median_gnss > t.poor_gnss_m:
        reasons.append({"code": "poor_location", "text": f"Median GNSS accuracy {median_gnss:.0f} m: location is imprecise."})

    latest_plants = next((r["plants_confirmed"] for r in reversed(rows) if r["confirmed"] and r["plants_confirmed"]), None)
    fruiting_small = any(o["phenology_eff"] == "fruiting" for o in confirmed) and latest_plants is not None and latest_plants <= 2
    lanuk_criteria = [
        {"code": "near_protected", "applies": bool(protected), "evidence": ", ".join(protected) or "none within buffer"},
        {"code": "early_invasion", "applies": None if latest_plants is None else latest_plants <= t.small_stand_max_plants,
         "evidence": f"{latest_plants} confirmed plants" if latest_plants is not None else "no counted plants"},
        {"code": "low_infestation", "applies": None, "evidence": "not assessable from robot detections alone"},
        {"code": "fruiting_solitary", "applies": fruiting_small,
         "evidence": "fruiting observed on a stand of ≤2 plants" if fruiting_small else "not observed"},
    ]

    summary = {
        "id": stand_id,
        "lat": round(lat, 6), "lon": round(lon, 6),
        "radius_m": round(radius, 1),
        "status": status,
        "status_label": STATUS_INFO[status][0],
        "needs_inspection": bool(reasons),
        "inspection_reasons": reasons,
        "counts": {"observations": len(obs), **{k: counts[k] for k in ("confirmed", "rejected", "pending", "uncertain", "field_visit")}},
        "latest_plants_confirmed": latest_plants,
        "first_confirmed": min((o["observed_at"][:10] for o in confirmed), default=None),
        "last_confirmed": max((o["observed_at"][:10] for o in confirmed), default=None),
        "last_observed": max(o["observed_at"][:10] for o in obs),
        "rejected_as": dict(Counter(o["corrected_taxon"] or "unspecified" for o in obs if o["review_status"] == "rejected")),
        "last_management_date": mgmt[-1]["action_date"] if mgmt else None,
        "area_name": _area_name(obs, missions),
        "district": (ctx["district"] or {}).get("district"),
        "stadtbezirk": (ctx["district"] or {}).get("stadtbezirk"),
        "landuse": (ctx["landuse"] or {}).get("landuse"),
        "environment": (ctx["landuse"] or {}).get("environment"),
        "protected": protected,
        "source_kinds": sorted({o["source_kind"] for o in obs}),
        "years": rows,
        "lanuk_criteria": lanuk_criteria,
    }
    if with_context:
        summary["context"] = ctx
    return summary


def _area_name(obs: list[sqlite3.Row], missions: list[Mission]) -> str | None:
    by_id = {m.id: m.area_name for m in missions}
    names = Counter(by_id.get(o["mission_id"]) for o in obs)
    return names.most_common(1)[0][0] if names else None


def all_stands(conn: sqlite3.Connection, ref: ReferenceData, t: Thresholds) -> list[dict]:
    missions = load_missions(conn)
    years = sorted({m.year for m in missions})
    obs_by_stand: dict[str, list] = defaultdict(list)
    for o in conn.execute(_OBS_WITH_CORRECTION):
        obs_by_stand[o["stand_id"]].append(o)
    notes_by_stand: dict[str, list] = defaultdict(list)
    for n in conn.execute("SELECT * FROM stand_notes"):
        notes_by_stand[n["stand_id"]].append(n)
    return [summarize_stand(sid, obs, notes_by_stand[sid], missions, years, ref, t)
            for sid, obs in sorted(obs_by_stand.items())]


def one_stand(conn: sqlite3.Connection, ref: ReferenceData, t: Thresholds, stand_id: str) -> dict | None:
    obs = conn.execute(_OBS_WITH_CORRECTION + " WHERE o.stand_id = ?", (stand_id,)).fetchall()
    if not obs:
        return None
    missions = load_missions(conn)
    notes = conn.execute("SELECT * FROM stand_notes WHERE stand_id = ? ORDER BY recorded_at", (stand_id,)).fetchall()
    return summarize_stand(stand_id, obs, notes, missions, sorted({m.year for m in missions}), ref, t, with_context=True)


def add_note(conn: sqlite3.Connection, stand_id: str, note: StandNoteIn, source_kind: str = "human",
             at: str | None = None) -> dict:
    """Record a human entry in the stand log. The system itself never writes here."""
    if conn.execute("SELECT 1 FROM stands WHERE id = ?", (stand_id,)).fetchone() is None:
        raise KeyError(stand_id)
    at = at or now_iso()
    cur = conn.execute(
        "INSERT INTO stand_notes (stand_id, kind, text, action_date, recorded_by, recorded_at, source_kind)"
        " VALUES (?,?,?,?,?,?,?)",
        (stand_id, note.kind, note.text, note.action_date, note.recorded_by, at, source_kind),
    )
    log_event(conn, note.recorded_by, "stand_note", stand_id, json.dumps({"kind": note.kind, "source_kind": source_kind}))
    conn.commit()
    return {"note_id": cur.lastrowid, "stand_id": stand_id, "recorded_at": at}


def stand_notes(conn: sqlite3.Connection, stand_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM stand_notes WHERE stand_id = ? ORDER BY recorded_at", (stand_id,))]


# The latest review supplies the corrected taxon and any expert labels; the mission its protocol.
_OBS_WITH_CORRECTION = """
SELECT o.*, r.corrected_taxon, r.plant_count AS label_plant_count,
       COALESCE(r.phenology, o.phenology) AS phenology_eff,
       COALESCE(r.height_class, o.height_class) AS height_eff,
       m.protocol AS protocol
FROM observations o
JOIN missions m ON m.id = o.mission_id
LEFT JOIN reviews r ON r.id = (SELECT MAX(id) FROM reviews WHERE observation_id = o.id)
"""
