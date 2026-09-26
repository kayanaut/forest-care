"""Command line:

    uv run python -m forestcare seed                      # reset data/runtime and load the simulated demo
    uv run python -m forestcare serve                     # web UI on http://127.0.0.1:8000
    uv run python -m forestcare import-photos FOLDER --photographer "Name" [--area-name ...] [--mission-id ...]
    uv run python -m forestcare verify-originals          # re-hash all stored original photos
    uv run python -m forestcare make-sample-photos FOLDER # synthetic geotagged JPEGs for trying the import
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="forestcare")
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed", help="reset the runtime database and load simulated robot missions")
    s.add_argument("--seed", type=int, default=42)
    s.add_argument("--review-latest", action="store_true", help="also apply simulated reviews to the latest round")
    v = sub.add_parser("serve", help="run the API and web UI")
    v.add_argument("--host", default="127.0.0.1")
    v.add_argument("--port", type=int, default=8000)
    p = sub.add_parser("import-photos", help="import a folder of geotagged field photos as one mission")
    p.add_argument("folder", type=Path)
    p.add_argument("--photographer", required=True, help="who took the photos (recorded in the provenance)")
    p.add_argument("--area-name")
    p.add_argument("--mission-id", help="default: PHOTO-<date>-<hash of the photo set>")
    p.add_argument("--notes")
    p.add_argument("--no-recursive", action="store_true", help="ignore sub-folders")
    p.add_argument("--simulated", action="store_true", help="mark as test data (never shown as real field data)")
    sub.add_parser("verify-originals", help="check stored original photos against their import hashes")
    m = sub.add_parser("make-sample-photos", help="write synthetic geotagged test JPEGs (marked as synthetic)")
    m.add_argument("folder", type=Path)
    args = parser.parse_args()
    settings = Settings()

    if args.cmd == "seed":
        from .demo import seed_demo
        print(json.dumps(seed_demo(settings, args.seed, args.review_latest), indent=2))
    elif args.cmd == "serve":
        import uvicorn
        uvicorn.run("forestcare.api:create_app", factory=True, host=args.host, port=args.port)
    elif args.cmd == "import-photos":
        from .db import connect, init_db
        from .models import PhotoImportMeta
        from .photos import folder_sources, import_photos
        from .reference import ReferenceData
        if not args.folder.is_dir():
            sys.exit(f"not a folder: {args.folder}")
        meta = PhotoImportMeta(photographer=args.photographer, area_name=args.area_name, mission_id=args.mission_id,
                               notes=args.notes, simulated=args.simulated, source_label=args.folder.resolve().name)
        conn = connect(settings.db_path)
        init_db(conn)
        report = import_photos(conn, ReferenceData(settings.reference_dir, settings.thresholds), settings.image_dir,
                               settings.thresholds, folder_sources(args.folder, recursive=not args.no_recursive),
                               meta, actor=f"cli:{args.photographer}")
        print(json.dumps(report, indent=2, ensure_ascii=False))
    elif args.cmd == "verify-originals":
        from .db import connect, init_db
        from .photos import verify_originals
        conn = connect(settings.db_path)
        init_db(conn)
        result = verify_originals(conn, settings.image_dir)
        print(json.dumps(result, indent=2))
        sys.exit(1 if result["problems"] else 0)
    elif args.cmd == "make-sample-photos":
        from simulator.photos import demo_set, write_folder
        from simulator.world import build_world
        write_folder(args.folder, demo_set(build_world(settings.reference_dir)))
        print(f"Synthetic test photos written to {args.folder}")


if __name__ == "__main__":
    main()
