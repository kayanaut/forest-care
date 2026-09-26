"""Build the demo database from simulated robot missions.

Everything written here is marked simulated: missions and observations carry
source_kind='simulated', and so do the review history and stand notes. The
latest survey round (September 2026) is left unreviewed on purpose so a person
can try the review workflow.
"""

from __future__ import annotations

import json
import random
import shutil
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from .config import Settings
from .db import connect, init_db, to_utc_iso
from .ingest import ingest_mission
from .models import MissionIn, ReviewIn, StandNoteIn
from .reference import ReferenceData
from .review import add_review
from .stands import add_note


def reset_runtime(settings: Settings) -> None:
    for suffix in ("", "-wal", "-shm"):
        settings.db_path.with_name(settings.db_path.name + suffix).unlink(missing_ok=True)
    if settings.image_dir.exists():
        shutil.rmtree(settings.image_dir)


def seed_demo(settings: Settings, seed: int = 42, review_latest: bool = False) -> dict:
    from simulator import reviewer
    from simulator.robot import simulate
    from simulator.world import build_world

    reset_runtime(settings)
    world = build_world(settings.reference_dir, seed)
    missions, truth = simulate(world, seed)

    runtime = settings.db_path.parent
    payload_dir = runtime / "sim_payloads"
    payload_dir.mkdir(parents=True, exist_ok=True)
    for m in missions:
        (payload_dir / f"{m['mission_id']}.json").write_text(json.dumps(m, indent=1))
    (runtime / "sim_ground_truth.json").write_text(json.dumps({**world.ground_truth(), "observations": truth}, indent=1))

    ref = ReferenceData(settings.reference_dir, settings.thresholds)
    conn = connect(settings.db_path)
    init_db(conn)
    rejected = 0
    for m in missions:
        result = ingest_mission(conn, ref, settings.image_dir, settings.thresholds, MissionIn.model_validate(m), actor="demo-seed")
        rejected += len(result["rejected"])

    latest_start = max(m["started_at"][:7] for m in missions)
    rng = random.Random(seed + 1)
    reviewed = 0
    for o in conn.execute("SELECT o.id, o.uid, o.observed_at, m.started_at FROM observations o"
                          " JOIN missions m ON m.id = o.mission_id ORDER BY o.observed_at").fetchall():
        if o["started_at"][:7] == latest_start and not review_latest:
            continue
        d = reviewer.decide(rng, truth[o["uid"]])
        at = to_utc_iso(datetime.fromisoformat(o["observed_at"]) + timedelta(days=rng.randint(2, 12)))
        add_review(conn, o["id"], ReviewIn(reviewer=reviewer.REVIEWER, reviewer_role=reviewer.ROLE, **d),
                   source_kind="simulated", at=at)
        reviewed += 1

    # Map simulated stand keys to the stand ids the system assigned.
    votes: dict[str, Counter] = defaultdict(Counter)
    for o in conn.execute("SELECT uid, stand_id FROM observations"):
        votes[truth[o["uid"]]["stand_key"]][o["stand_id"]] += 1
    stand_of = {k: c.most_common(1)[0][0] for k, c in votes.items()}
    for s in world.stands:
        if s.spec.managed_on and s.spec.key in stand_of:
            add_note(conn, stand_of[s.spec.key], StandNoteIn(
                kind="management_action", action_date=s.spec.managed_on, recorded_by="Demo forester (simulated)",
                text="Larger stems ringed, young plants pulled incl. roots. Simulated entry for the demo."),
                source_kind="simulated", at=f"{s.spec.managed_on}T15:00:00+00:00")
    if "TB-1" in stand_of:
        add_note(conn, stand_of["TB-1"], StandNoteIn(
            kind="monitoring_decision", recorded_by="Demo ecologist (simulated)",
            text="Keep in annual robot survey; discuss with the reserve's responsible bodies before any intervention. "
                 "Simulated entry for the demo."),
            source_kind="simulated", at="2025-10-02T09:00:00+00:00")
    conn.close()
    return {"missions": len(missions), "observations": len(truth), "rejected_at_ingest": rejected,
            "simulated_reviews": reviewed, "left_for_review": len(truth) - reviewed - rejected,
            "stand_keys": stand_of}
