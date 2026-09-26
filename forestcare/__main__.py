"""Command line:

    uv run python -m forestcare seed     # reset data/runtime and load the simulated demo
    uv run python -m forestcare serve    # web UI on http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import json

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
    args = parser.parse_args()

    if args.cmd == "seed":
        from .demo import seed_demo
        print(json.dumps(seed_demo(Settings(), args.seed, args.review_latest), indent=2))
    else:
        import uvicorn
        uvicorn.run("forestcare.api:create_app", factory=True, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
