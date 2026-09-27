"""Command line for the offline parts (no ROS needed).

    fc_gateway convert BAG [--config gateway.yaml]      rosbag -> mission package in the outbox
    fc_gateway upload [--api http://host:8000]          send finished packages, resumable
    fc_gateway status                                   what is in the outbox
    fc_gateway inspect BAG                              topics, rates, GNSS quality, clock offsets
    fc_gateway recover PACKAGE_DIR                      rebuild mission.json after a crash
    fc_gateway demo-bag OUT [--world survey|loc]        write a simulated mission bag
    fc_gateway loc-eval ...                             localization experiment (see loc/)

Run with `python -m forestcare_gateway ...` from a checkout, or `ros2 run forestcare_gateway fc_gateway ...`.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .config import load_config, outbox_path
from .package import MissionPackage, list_packages


def _cfg(args) -> dict:
    overrides = {}
    if getattr(args, "outbox", None):
        overrides["outbox_dir"] = args.outbox
    if getattr(args, "api", None):
        overrides["api_url"] = args.api
    return load_config(getattr(args, "config", None), overrides)


def cmd_convert(args) -> int:
    from .bagio import convert_bag

    cfg = _cfg(args)
    if args.method:
        cfg["localization"]["method"] = args.method
    bag = Path(args.bag)
    if args.force:
        from .bagio import bag_summary
        from .recorder import make_mission_id

        mid = args.mission_id or make_mission_id(cfg["robot_id"], bag_summary(bag)["start_ns"])
        old = outbox_path(cfg) / mid
        if old.exists():
            old.rename(old.with_name(f"{mid}.replaced-{time.strftime('%Y%m%dT%H%M%S')}"))
    info = {k: v for k, v in {"area_name": args.area_name, "operator": args.operator}.items() if v}
    try:
        result = convert_bag(bag, cfg, args.mission_id, info)
    except FileExistsError as exc:
        print(f"{exc}\nThis bag was already converted. Use --force to rebuild (the old package is kept, renamed).")
        return 2
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "complete" else 1


def cmd_upload(args) -> int:
    from .uploader import upload_outbox

    cfg = _cfg(args)
    results = upload_outbox(outbox_path(cfg), cfg["api_url"], args.batch_size or cfg["upload_batch_size"],
                            retry_for_s=args.retry_for, interval_s=args.interval)
    print(json.dumps(results, indent=2) if results else "nothing to upload")
    return 0 if all(r["status"] == "uploaded" for r in results) else 1


def cmd_status(args) -> int:
    packages = list_packages(outbox_path(_cfg(args)))
    if not packages:
        print("the outbox is empty")
    for pkg in packages:
        st = pkg.state
        extra = st.get("error") or (f"{st.get('observations', '?')} observation(s)" if st["status"] != "recording" else "")
        print(f"{pkg.mission_id:40s} {st['status']:14s} {extra}")
    return 0


def cmd_inspect(args) -> int:
    from .health import inspect_bag

    report = inspect_bag(Path(args.bag), _cfg(args))
    print(json.dumps(report, indent=2) if args.json else report["text"])
    return 0 if report["ok"] else 1


def cmd_recover(args) -> int:
    from .assemble import build_mission

    pkg = MissionPackage(Path(args.package))
    stats = pkg.read_json("stats.json")
    if stats is None:   # recording never finished: derive what the recorder would have written
        times = [r["stamp_ns"] for s in ("gnss", "odom", "imu", "frames", "marks") for r in pkg.read(s)]
        pkg.write_json("stats.json", {"first_ns": min(times) if times else None, "last_ns": max(times) if times else None,
                                       "counts": {}, "clock_offsets_ms": {},
                                       "warnings": ["Recovered after an interrupted recording; the last seconds may be missing."]})
    print(json.dumps(build_mission(pkg), indent=2))
    return 0


def cmd_demo_bag(args) -> int:
    from .demo import write_demo_bag

    print(json.dumps(write_demo_bag(Path(args.out), args.world, args.seed, args.run, rtk=args.rtk,
                                    lidar_odom=args.lidar_odom, detections=not args.no_detections,
                                    tag_marks=args.tag_marks), indent=2))
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="fc_gateway", description="Forest Care gateway tools (no ROS needed)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p, bag=False):
        if bag:
            p.add_argument("bag", help="rosbag2 directory (MCAP or sqlite3)")
        p.add_argument("--config", help="gateway YAML (ROS parameter format or plain)")
        p.add_argument("--outbox", help="override outbox_dir")
        return p

    p = common(sub.add_parser("convert", help="convert a mission bag into a mission package"), bag=True)
    p.add_argument("--mission-id")
    p.add_argument("--area-name")
    p.add_argument("--operator")
    p.add_argument("--method", help="localization method (overrides the config)")
    p.add_argument("--force", action="store_true", help="rebuild even if a package exists (the old one is renamed)")
    p.set_defaults(func=cmd_convert)
    p = common(sub.add_parser("upload", help="upload finished packages"))
    p.add_argument("--api", help="Forest Care base URL, e.g. http://127.0.0.1:8000")
    p.add_argument("--batch-size", type=int)
    p.add_argument("--retry-for", type=float, default=0.0, help="keep retrying failed uploads for this many seconds")
    p.add_argument("--interval", type=float, default=30.0)
    p.set_defaults(func=cmd_upload)
    common(sub.add_parser("status", help="list the outbox")).set_defaults(func=cmd_status)
    p = common(sub.add_parser("inspect", help="check a bag before converting it"), bag=True)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_inspect)
    p = sub.add_parser("recover", help="rebuild mission.json of an interrupted package")
    p.add_argument("package")
    p.set_defaults(func=cmd_recover)
    p = sub.add_parser("demo-bag", help="write a simulated mission bag")
    p.add_argument("out")
    p.add_argument("--world", default="survey", choices=["survey", "loc"])
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--run", type=int, default=0)
    p.add_argument("--rtk", action="store_true", help="add a second, RTK-capable GNSS receiver")
    p.add_argument("--lidar-odom", action="store_true", help="add a LiDAR odometry topic")
    p.add_argument("--no-detections", action="store_true", help="no mock perception output")
    p.add_argument("--tag-marks", action="store_true", help="marks carry plant ids (for repeated-run experiments)")
    p.set_defaults(func=cmd_demo_bag)
    try:
        from .loc.cli import register as register_loc
        register_loc(sub)
    except ImportError:
        pass
    args = parser.parse_args(argv)
    sys.exit(args.func(args))
