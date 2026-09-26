"""Simulated expert decisions, used ONLY to give the demo a review history.

Stored with source_kind='simulated' and a reviewer name that says so. The
latest survey round is left unreviewed so a person can try the real workflow.
"""

from __future__ import annotations

import random

from .robot import TARGET

REVIEWER = "Demo reviewer (simulated)"
ROLE = "simulated expert — not a real person"


def decide(rng: random.Random, truth: dict) -> dict:
    r = rng.random()
    if truth["taxon"] == TARGET:
        if truth["height"] == "seedling" and r < 0.15:
            return {"decision": "uncertain", "note": "Seedling too small to confirm from the image."}
        if r < 0.03:
            return {"decision": "field_visit", "note": "Image partly occluded; please check on site."}
        return {"decision": "confirmed", "note": None}
    if r < 0.06:
        return {"decision": "uncertain", "note": "Leaf gloss not visible; could be a native Prunus."}
    return {"decision": "rejected", "corrected_taxon": truth["taxon"], "note": None}
