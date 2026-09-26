"""Paths and tunable thresholds.

Every threshold below is a prototype ASSUMPTION, not a validated value. They are
collected here so they can be reviewed with local forest / ecology experts
(see docs/ASSUMPTIONS_AND_VALIDATION.md) and changed in one place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REFERENCE_DIR = ROOT / "data" / "reference"
RUNTIME_DIR = ROOT / "data" / "runtime"
WEB_DIR = ROOT / "web"

TARGET_TAXON = "Prunus serotina"
TARGET_LABEL_DE = "Spätblühende Traubenkirsche (Prunus serotina)"

# Taxa a reviewer can choose when an observation is not the target species.
CORRECTION_TAXA = ["Prunus padus", "Frangula alnus", "Prunus avium", "other / unknown"]

REVIEW_DECISIONS = {
    "confirmed": "Confirmed: Prunus serotina",
    "rejected": "Rejected: not Prunus serotina",
    "uncertain": "Uncertain: evidence insufficient",
    "field_visit": "Needs field visit",
}

# Human-entered stand log. The system never creates these entries itself.
NOTE_KINDS = {
    "note": "Note",
    "monitoring_decision": "Monitoring decision",
    "management_action": "Management action carried out",
}


@dataclass(frozen=True)
class Thresholds:
    # Two observations belong to the same stand if they are closer than
    #   stand_spread_m + link_sigma_factor * sqrt(acc1^2 + acc2^2)
    # i.e. plausible clump spread plus the combined GNSS uncertainty (reported 1 sigma).
    stand_spread_m: float = 8.0
    link_sigma_factor: float = 2.0
    # The statistical districts leave out the Rhine and have tiny slivers; points this close
    # to a district are assigned to it.
    boundary_snap_m: float = 15.0
    # Trend classification between the two most recent years with verified plants.
    expand_ratio: float = 1.5
    expand_min_plants: int = 3
    decline_ratio: float = 0.67
    decline_min_plants: int = 3
    # Context lookups.
    protected_area_buffer_m: float = 100.0
    external_record_radius_m: float = 250.0
    # External records coarser than this are grid cells (e.g. floristic mapping quadrants).
    external_record_grid_uncertainty_m: float = 1000.0
    # Observation quality flags.
    poor_gnss_m: float = 10.0
    ambiguous_band: tuple[float, float] = (0.4, 0.75)
    # "Early invasion / few trees" context (LANUK wording, our numeric reading of it).
    small_stand_max_plants: int = 5


THRESHOLDS = Thresholds()


@dataclass(frozen=True)
class Settings:
    db_path: Path = RUNTIME_DIR / "forestcare.db"
    image_dir: Path = RUNTIME_DIR / "images"
    reference_dir: Path = REFERENCE_DIR
    thresholds: Thresholds = field(default_factory=Thresholds)


# Live map services (not snapshotted). All are official NRW services.
WMS_LAYERS = [
    {
        "id": "dop_rgb", "kind": "base", "title": "Orthophoto NRW (DOP, RGB)",
        "url": "https://www.wms.nrw.de/geobasis/wms_nw_dop", "layers": "nw_dop_rgb", "format": "image/jpeg",
        "publisher": "Geobasis NRW", "license": "dl-de/zero-2-0",
        "attribution": "© Geobasis NRW (dl-de/zero-2-0)",
    },
    {
        "id": "dop_cir", "kind": "base", "title": "Orthophoto NRW (colour infrared)",
        "url": "https://www.wms.nrw.de/geobasis/wms_nw_dop", "layers": "nw_dop_cir", "format": "image/jpeg",
        "publisher": "Geobasis NRW", "license": "dl-de/zero-2-0",
        "attribution": "© Geobasis NRW (dl-de/zero-2-0)",
    },
    {
        "id": "wh_baumarten", "kind": "overlay", "title": "Tree species classification (Wald und Holz NRW)",
        "url": "https://www.wms.nrw.de/umwelt/waldNRW", "layers": "baumartenklassifikation_nrw", "format": "image/png",
        "publisher": "Landesbetrieb Wald und Holz NRW", "license": "WMS states 'Es gelten keine Bedingungen'",
        "attribution": "© Wald und Holz NRW",
    },
    {
        "id": "wh_staatswald", "kind": "overlay", "title": "State forest (Wald und Holz NRW)",
        "url": "https://www.wms.nrw.de/umwelt/waldNRW", "layers": "landeseigener_forstbetrieb_staatswald", "format": "image/png",
        "publisher": "Landesbetrieb Wald und Holz NRW", "license": "WMS states 'Es gelten keine Bedingungen'",
        "attribution": "© Wald und Holz NRW",
    },
    {
        "id": "linfos_biotopkataster", "kind": "overlay", "title": "Biotope register (LANUK @LINFOS)",
        "url": "https://www.wms.nrw.de/umwelt/linfos", "layers": "Biotopkataster", "format": "image/png",
        "publisher": "LANUK NRW", "license": "dl-de/zero-2-0",
        "attribution": "© LANUK NRW (dl-de/zero-2-0)",
    },
]
