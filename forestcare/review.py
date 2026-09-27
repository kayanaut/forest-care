"""Human verification: review queue ordering and recording decisions.

The queue order only decides what a reviewer sees first. Every reason for
the position is shown next to the item, so the ordering can be checked
and corrected.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict

from .db import log_event, now_iso
from .models import ReviewIn


class NotFound(Exception):
    pass


def _stand_facts(conn: sqlite3.Connection) -> dict[str, dict]:
    facts: dict[str, dict] = defaultdict(lambda: {"status": Counter(), "rejected_as": Counter(), "last_action": None})
    for r in conn.execute(
        "SELECT o.stand_id, o.review_status, (SELECT corrected_taxon FROM reviews WHERE observation_id = o.id"
        " ORDER BY id DESC LIMIT 1) AS corrected FROM observations o"
    ):
        f = facts[r["stand_id"]]
        f["status"][r["review_status"]] += 1
        if r["review_status"] == "rejected":
            f["rejected_as"][r["corrected"] or "unspecified"] += 1
    for r in conn.execute("SELECT stand_id, MAX(action_date) AS d FROM stand_notes WHERE kind = 'management_action' GROUP BY stand_id"):
        facts[r["stand_id"]]["last_action"] = r["d"]
    return facts


def priority(o: sqlite3.Row, facts: dict) -> tuple[int, list[str]]:
    score, reasons = 0, []
    ctx = json.loads(o["context_json"])
    flags = json.loads(o["qc_flags_json"])
    if facts["last_action"] and o["observed_at"][:10] > facts["last_action"]:
        score += 3
        reasons.append(f"Detected after management recorded on {facts['last_action']} (possible resprouting)")
    protected = [a["name"] for a in ctx["nsg"] + ctx["ffh"]]
    if protected:
        score += 2
        reasons.append("In or near " + ", ".join(protected))
    elif ctx["protected_biotopes"]:
        score += 2
        reasons.append("In or near a legally protected biotope")
    confirmed = facts["status"]["confirmed"]
    rejected = sum(facts["rejected_as"].values())
    if confirmed == 0 and rejected == 0:
        score += 2
        reasons.append("No reviewed record at this location yet")
    elif confirmed == 0:
        score += 1
        top = ", ".join(f"{k} ×{v}" for k, v in facts["rejected_as"].most_common())
        reasons.append(f"Known look-alike location: {rejected} earlier detection(s) rejected ({top})")
    else:
        reasons.append(f"Stand already has {confirmed} confirmed observation(s)")
    if "ambiguous_prediction" in flags:
        score += 1
        reasons.append(f"Ambiguous model output (p = {o['target_probability']:.2f})")
    if o["phenology"] == "fruiting":
        score += 1
        reasons.append("Fruiting reported (relevant to LANUK's 'fruiting specimens' criterion)")
    if o["target_probability"] is None:
        if o["original_path"]:
            reasons.append("Field photo without model prediction: label it from the image")
        else:
            label = mark_label(o)
            as_label = f" as “{label}”" if label else ""
            reasons.append(f"Marked by the robot operator{as_label}; no model prediction")
    return score, reasons


def mark_label(o: sqlite3.Row) -> str | None:
    """The operator's label for a robot observation marked by hand (stored in its metadata)."""
    if not o["metadata_json"]:
        return None
    mark = json.loads(o["metadata_json"]).get("mark") or {}
    return mark.get("label")


def review_queue(conn: sqlite3.Connection, limit: int = 500) -> list[dict]:
    facts = _stand_facts(conn)
    items = []
    for o in conn.execute("SELECT * FROM observations WHERE review_status = 'pending'"):
        score, reasons = priority(o, facts[o["stand_id"]])
        items.append({
            "id": o["id"], "uid": o["uid"], "observed_at": o["observed_at"], "mission_id": o["mission_id"],
            "stand_id": o["stand_id"], "lat": o["lat"], "lon": o["lon"],
            "predicted_taxon": o["predicted_taxon"], "target_probability": o["target_probability"],
            "phenology": o["phenology"], "plant_count_est": o["plant_count_est"],
            "image_url": f"/api/images/{o['image_path']}" if o["image_path"] else None,
            "qc_flags": json.loads(o["qc_flags_json"]), "source_kind": o["source_kind"],
            "original_filename": o["original_filename"], "mark_label": mark_label(o),
            "priority_score": score, "priority_reasons": reasons,
        })
    items.sort(key=lambda i: (-i["priority_score"], i["observed_at"]))
    return items[:limit]


def observation_detail(conn: sqlite3.Connection, obs_id: int) -> dict:
    o = conn.execute("SELECT * FROM observations WHERE id = ?", (obs_id,)).fetchone()
    if o is None:
        raise NotFound(obs_id)
    reviews = [dict(r) for r in conn.execute("SELECT * FROM reviews WHERE observation_id = ? ORDER BY id", (obs_id,))]
    mission = conn.execute("SELECT id, robot_id, area_name, started_at, notes, source_kind, protocol FROM missions"
                           " WHERE id = ?", (o["mission_id"],)).fetchone()
    siblings = conn.execute(
        "SELECT o.id, o.observed_at, o.review_status, o.target_probability, o.image_path, o.phenology,"
        " (SELECT corrected_taxon FROM reviews WHERE observation_id = o.id ORDER BY id DESC LIMIT 1) AS corrected_taxon"
        " FROM observations o WHERE o.stand_id = ? AND o.id != ? ORDER BY o.observed_at DESC LIMIT 40",
        (o["stand_id"], obs_id),
    ).fetchall()
    d = dict(o)
    for k in ("alternatives_json", "qc_flags_json", "context_json", "provenance_json", "metadata_json"):
        raw = d.pop(k)
        d[k.removesuffix("_json")] = json.loads(raw) if raw else None
    d["image_url"] = f"/api/images/{o['image_path']}" if o["image_path"] else None
    d["original_url"] = f"/api/observations/{obs_id}/original" if o["original_path"] else None
    d["reviews"] = reviews
    d["mission"] = dict(mission)
    d["stand_history"] = [
        {**dict(s), "image_url": f"/api/images/{s['image_path']}" if s["image_path"] else None} for s in siblings
    ]
    return d


def add_review(conn: sqlite3.Connection, obs_id: int, review: ReviewIn, source_kind: str = "human",
               at: str | None = None) -> dict:
    if conn.execute("SELECT 1 FROM observations WHERE id = ?", (obs_id,)).fetchone() is None:
        raise NotFound(obs_id)
    at = at or now_iso()
    cur = conn.execute(
        "INSERT INTO reviews (observation_id, decision, corrected_taxon, note, reviewer, reviewer_role, reviewed_at,"
        " source_kind, plant_count, height_class, phenology) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (obs_id, review.decision, review.corrected_taxon, review.note, review.reviewer, review.reviewer_role, at,
         source_kind, review.plant_count, review.height_class, review.phenology),
    )
    conn.execute("UPDATE observations SET review_status = ? WHERE id = ?", (review.decision, obs_id))
    log_event(conn, review.reviewer, "review", str(obs_id), json.dumps({"decision": review.decision, "source_kind": source_kind}))
    conn.commit()
    return {"review_id": cur.lastrowid, "observation_id": obs_id, "decision": review.decision, "reviewed_at": at}
