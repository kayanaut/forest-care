"""`fc_gateway loc-eval` and `fc_gateway loc-experiment` (registered into cli.py)."""

from __future__ import annotations

import json
from pathlib import Path


def run_evaluation(experiment: Path, out: Path, log=print) -> dict:
    import yaml

    from ..geo import to_local
    from .evaluate import evaluate, load_experiment, prepare_runs, recommended_config
    from .report import render

    exp = load_experiment(experiment)
    out.mkdir(parents=True, exist_ok=True)
    runs = prepare_runs(exp, out, log)
    result = evaluate(exp, runs, log)
    origin = result["origin"]
    result["_refs_xy"] = {k: to_local(v["lat"], v["lon"], *origin) for k, v in (exp.get("references") or {}).items()}
    (out / "results.json").write_text(json.dumps({k: v for k, v in result.items() if not k.startswith("_")}, indent=1))
    (out / "report.html").write_text(render(result, str(experiment)))
    cfg = recommended_config(result)
    if cfg:
        (out / "recommended_gateway.yaml").write_text(
            "# Apply the localization experiment's result: merge into your gateway configuration.\n"
            + yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))
    log(f"report: {out / 'report.html'}")
    return result


def simulated_experiment(out: Path, runs: int = 5, seed: int = 3, log=print) -> Path:
    """Write N simulated runs of the 'loc' loop (RTK + LiDAR odometry + tagged marks) and an experiment file."""
    import yaml

    from ..demo import DEMO_START_NS, write_demo_bag
    from ..geo import from_local
    from ..messages import NS
    from ..sim import make_world

    out.mkdir(parents=True, exist_ok=True)
    for i in range(1, runs + 1):
        bag = out / f"run{i}" / "bag"
        if not bag.exists():
            log(f"simulating run {i} of {runs} ...")
            # runs 90 min apart: different satellite geometry, so the GNSS bias differs between runs
            write_demo_bag(bag, "loc", seed=seed, run=i, start_ns=DEMO_START_NS + i * 5400 * NS, rtk=True,
                           lidar_odom=True, detections=False, tag_marks=True)
    world = make_world("loc")

    def ll(e, n):
        lat, lon = from_local(e, n, *world.origin)
        return round(lat, 8), round(lon, 8)

    exp = {
        "name": "simulated woodland loop",
        "runs": [{"bag": f"run{i}/bag"} for i in range(1, runs + 1)],
        "current_method": "gnss",                 # what the gateway runs today (localization.method)
        "ground_truth": "ground_truth.json",      # simulation only; a field test uses surveyed references
        "gateway_config": {"source_kind": "simulated", "simulator": {"name": "forestcare_gateway.sim"},
                           "topics": {"gnss_rtk": "/gnss_rtk/fix", "odom_alt": "/odom_lidar"}},
        "references": {r.id: dict(zip(("lat", "lon"), ll(r.e, r.n))) for r in world.references},
        "canopy_zones": {"open": [[list(reversed(ll(e, n))) for e, n in
                                   [(z[0], z[1]), (z[2], z[1]), (z[2], z[3]), (z[0], z[3])]] for z in world.open_zones]},
        "criteria": {"reid_rate": 0.95, "coverage_95": 0.9, "required_separation_m": 20.0},
    }
    path = out / "experiment.yaml"
    path.write_text("# Simulated localization experiment (written by fc_gateway loc-experiment)\n"
                    + yaml.safe_dump(exp, sort_keys=False))
    return path


def register(sub) -> None:
    p = sub.add_parser("loc-eval", help="evaluate a repeated-run localization experiment")
    p.add_argument("experiment", help="experiment YAML (see docs/LOCALIZATION_VALIDATION.md)")
    p.add_argument("--out", required=True, help="output folder (report.html, results.json, recommended_gateway.yaml)")
    p.set_defaults(func=lambda a: (run_evaluation(Path(a.experiment), Path(a.out)), 0)[1])
    p = sub.add_parser("loc-experiment", help="simulate a repeated-run experiment and evaluate it")
    p.add_argument("--out", required=True)
    p.add_argument("--runs", type=int, default=5)
    p.add_argument("--seed", type=int, default=3)
    p.set_defaults(func=lambda a: (run_evaluation(simulated_experiment(Path(a.out), a.runs, a.seed), Path(a.out) / "evaluation"), 0)[1])
