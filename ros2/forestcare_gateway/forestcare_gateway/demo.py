"""Write simulated mission bags (real rosbag2 / MCAP files) without ROS.

    fc_gateway demo-bag out/survey_demo                  # gateway demo: marks + mock detections
    fc_gateway demo-bag out/loc_run1 --world loc --run 1 --rtk --lidar-odom --tag-marks

Each bag directory also gets
  mission_info.yaml   what the recording script writes on a real rover (operator, site, notes)
  ground_truth.json   the simulated true trajectory, plants and reference points (simulation only!)
"""

from __future__ import annotations

import heapq
import json
from pathlib import Path

import numpy as np

from .bagio import BagWriter
from .messages import NS
from .sim import TOPICS, ScriptedOperator, SimRover, make_world

DEMO_START_NS = 1_790_236_800 * NS     # 2026-09-24 08:00:00 UTC, fixed so demo output is reproducible


def write_demo_bag(out: Path, world_kind: str = "survey", seed: int = 1, run: int = 0, start_ns: int | None = None,
                   rtk: bool = False, lidar_odom: bool = False, detections: bool = True, tag_marks: bool = False,
                   max_duration_s: float = 1200.0, dt: float = 0.02) -> dict:
    out = Path(out)
    if out.exists():
        raise FileExistsError(f"{out} already exists")
    world = make_world(world_kind)                        # same world for every run
    start_ns = start_ns if start_ns is not None else DEMO_START_NS + run * 45 * 60 * NS
    rover = SimRover(world, seed * 1000 + run, start_ns, rtk=rtk, lidar_odom=lidar_odom,
                     detections=detections, caption=f"{world_kind} run {run}")
    operator = ScriptedOperator(world, np.random.default_rng(seed * 1000 + run + 1), run, tag_marks=tag_marks)
    queue: list = []            # messages sorted by arrival time before writing
    truth = []
    counter = 0
    with BagWriter(out) as writer:
        while not operator.done and (rover.t_ns - start_ns) / NS < max_duration_s:
            v, w, mark = operator.command(rover)
            messages = rover.step(dt, v, w)
            if mark:
                messages.append((TOPICS["marks"], "std_msgs/msg/String", rover.t_ns, rover.t_ns + 2_000_000, {"data": mark}))
            for topic, msgtype, _stamp, arrival, data in messages:
                counter += 1
                heapq.heappush(queue, (arrival, counter, topic, msgtype, data))
            while queue and queue[0][0] < rover.t_ns - NS // 2:
                arrival, _, topic, msgtype, data = heapq.heappop(queue)
                writer.write(topic, msgtype, arrival, data)
            if not truth or rover.t_ns - truth[-1]["t_ns"] >= 0.2 * NS:
                truth.append({k: (round(v, 7) if isinstance(v, float) else v) for k, v in rover.truth().items()})
        while queue:
            arrival, _, topic, msgtype, data = heapq.heappop(queue)
            writer.write(topic, msgtype, arrival, data)
    from .geo import from_local

    def ll(e, n):
        lat, lon = from_local(e, n, *world.origin)
        return {"lat": round(lat, 8), "lon": round(lon, 8)}

    (out / "ground_truth.json").write_text(json.dumps({
        "note": "SIMULATED ground truth for evaluating the gateway and localization; not real data.",
        "world": world_kind, "seed": seed, "run": run,
        "plants": [{"id": p.id, "taxon": p.taxon, **ll(p.e, p.n)} for p in world.plants],
        "references": [{"id": r.id, **ll(r.e, r.n)} for r in world.references],
        "trajectory": truth,
    }))
    (out / "mission_info.yaml").write_text(
        f"operator: Simulated operator\nsite: Kottenforst (simulated demo route '{world_kind}')\n"
        f"rover: simulated rover (forestcare_gateway sim)\nweather: n/a (simulation)\n"
        f"notes: Simulated run {run}; every value in this bag is invented.\n")
    return {"bag": str(out), "duration_s": round((rover.t_ns - start_ns) / NS, 1), "marks": operator.count,
            "distance_m": round(rover.distance, 1)}


def demo_config(world_kind: str = "survey", seed: int = 1, run: int = 0, **overrides) -> dict:
    """Gateway settings matching the demo bags (simulated source, the sim's topic names)."""
    from .config import DEFAULTS, merge

    cfg = merge(DEFAULTS, {
        "robot_id": "sim-rover", "source_kind": "simulated", "survey_protocol": "opportunistic",
        "detections": {"model_name": "mock-perception (simulated)", "model_version": "0.1"},
        "simulator": {"name": "forestcare_gateway.sim", "version": "0.1.0", "seed": seed, "run": run, "world": world_kind},
    })
    return merge(cfg, overrides)
