"""Upload finished mission packages to Forest Care (`POST /api/missions`), resumably.

- A mission is sent in batches of observations. Every batch repeats the mission header and
  track; the server creates the mission once and ignores the repeats.
- The server identifies observations by `uid`, so re-sending a batch after a lost response
  never creates duplicates. Progress is saved after every batch (upload.json), so an upload
  interrupted by a dead field connection resumes where it stopped.
- Network problems and server errors (5xx) are retried later; a 4xx answer means the
  payload itself is wrong. The package is then marked 'rejected' for a person to look at,
  instead of being retried forever.
- Before sending, each frame is re-hashed and compared with the hash recorded when it was
  captured.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from .package import MissionPackage, list_packages

UPLOADABLE = ("complete", "upload_failed", "uploading")


class UploadError(Exception):
    def __init__(self, message: str, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


def post_json(url: str, payload: dict, timeout: float = 120.0) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:2000].decode("utf-8", "replace")
        raise UploadError(f"HTTP {exc.code}: {detail}", retryable=exc.code >= 500 or exc.code in (408, 429)) from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        raise UploadError(f"cannot reach {url}: {exc}", retryable=True) from exc


def _encode(pkg: MissionPackage, obs: dict) -> dict:
    out = {k: v for k, v in obs.items() if k not in ("image_file", "image_media_type")}
    if obs.get("image_file"):
        data = pkg.frame_path(obs["image_file"]).read_bytes()
        expected = ((obs.get("metadata") or {}).get("capture") or {}).get("frame", {}) or {}
        if expected.get("sha256") and hashlib.sha256(data).hexdigest() != expected["sha256"]:
            raise UploadError(f"frame {obs['image_file']} changed on disk since it was recorded", retryable=False)
        out["image"] = {"media_type": obs["image_media_type"], "data_base64": base64.b64encode(data).decode()}
    return out


def upload_package(pkg: MissionPackage, api_url: str, batch_size: int = 20, post=post_json, log=print) -> dict:
    status = pkg.state.get("status")
    if status not in UPLOADABLE:
        return {"mission_id": pkg.mission_id, "status": status, "sent": 0}
    mission = pkg.read_json("mission.json")
    progress = pkg.read_json("upload.json") or {"done": [], "responses": [], "attempts": 0}
    progress["attempts"] += 1
    observations = mission.pop("observations")
    done = set(progress["done"])
    todo = [o for o in observations if o["uid"] not in done]
    batches = [todo[i:i + batch_size] for i in range(0, len(todo), batch_size)]
    if not progress["responses"] and not batches:
        batches = [[]]                   # a mission without observations still has a track worth storing
    url = api_url.rstrip("/") + "/api/missions"
    pkg.set_state("uploading")
    sent = 0
    try:
        for batch in batches:
            payload = {**mission, "observations": [_encode(pkg, o) for o in batch]}
            response = post(url, payload)
            progress["done"] += [o["uid"] for o in batch]
            progress["responses"].append({"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **response})
            pkg.write_json("upload.json", progress)
            sent += len(batch)
    except UploadError as exc:
        progress["last_error"] = str(exc)
        pkg.write_json("upload.json", progress)
        pkg.set_state("upload_failed" if exc.retryable else "rejected", error=str(exc))
        log(f"{pkg.mission_id}: upload {'will be retried' if exc.retryable else 'rejected'}: {exc}")
        return {"mission_id": pkg.mission_id, "status": pkg.state["status"], "sent": sent, "error": str(exc)}
    accepted = sum(r.get("accepted", 0) for r in progress["responses"])
    refused = [x for r in progress["responses"] for x in r.get("rejected", [])]
    pkg.set_state("uploaded", accepted=accepted, refused_by_server=len(refused), error=None)
    log(f"{pkg.mission_id}: uploaded ({accepted} observation(s) accepted"
        f"{f', {len(refused)} refused by the server' if refused else ''})")
    return {"mission_id": pkg.mission_id, "status": "uploaded", "sent": sent, "accepted": accepted,
            "refused": refused}


def upload_outbox(outbox: Path, api_url: str, batch_size: int = 20, retry_for_s: float = 0.0,
                  interval_s: float = 30.0, post=post_json, log=print) -> list[dict]:
    """Upload every finished package; keep retrying failed ones for `retry_for_s` seconds."""
    deadline = time.monotonic() + retry_for_s
    results: dict[str, dict] = {}
    while True:
        pending = [p for p in list_packages(outbox) if p.state.get("status") in UPLOADABLE]
        for pkg in pending:
            results[pkg.mission_id] = upload_package(pkg, api_url, batch_size, post, log)
        if not any(r["status"] == "upload_failed" for r in results.values()) or time.monotonic() >= deadline:
            return list(results.values())
        time.sleep(min(interval_s, max(0.0, deadline - time.monotonic())))
