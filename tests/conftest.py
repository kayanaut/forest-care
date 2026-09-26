from __future__ import annotations

import base64
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from forestcare.api import create_app
from forestcare.config import REFERENCE_DIR, Settings, Thresholds
from forestcare.db import connect, init_db
from forestcare.demo import seed_demo
from forestcare.reference import ReferenceData

# A point inside real Bonn woodland: Kottenforst near Röttgen, inside NSG BN-003 and FFH DE-5308-303.
KOTTENFORST = (50.66244, 7.05895)
SVG = base64.b64encode(b'<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4"/>').decode()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(db_path=tmp_path / "t.db", image_dir=tmp_path / "images", reference_dir=REFERENCE_DIR,
                    thresholds=Thresholds())


@pytest.fixture
def ref(settings: Settings) -> ReferenceData:
    return ReferenceData(settings.reference_dir, settings.thresholds)


@pytest.fixture
def conn(settings: Settings):
    c = connect(settings.db_path)
    init_db(c)
    yield c
    c.close()


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


@pytest.fixture(scope="session")
def _seeded_template(tmp_path_factory) -> Path:
    base = tmp_path_factory.mktemp("seeded")
    s = Settings(db_path=base / "demo.db", image_dir=base / "images", reference_dir=REFERENCE_DIR)
    seed_demo(s, seed=42)
    return base


@pytest.fixture
def seeded_settings(_seeded_template: Path, tmp_path: Path) -> Settings:
    """A fresh copy of the seeded demo database per test (seeding once per session)."""
    target = tmp_path / "seeded"
    shutil.copytree(_seeded_template, target)
    return Settings(db_path=target / "demo.db", image_dir=target / "images", reference_dir=REFERENCE_DIR)


@pytest.fixture
def seeded_client(seeded_settings: Settings) -> TestClient:
    return TestClient(create_app(seeded_settings))


def offset(lat: float, lon: float, east_m: float = 0.0, north_m: float = 0.0) -> tuple[float, float]:
    from forestcare.geo import from_local_xy
    return from_local_xy(east_m, north_m, lat, lon)


def observation(uid: str, lat: float, lon: float, when: str, p: float = 0.9, count: int = 1,
                accuracy: float = 3.0, image: bool = True, **extra) -> dict:
    o = {"uid": uid, "observed_at": when, "lat": lat, "lon": lon, "gnss_accuracy_m": accuracy,
         "predicted_taxon": "Prunus serotina" if p >= 0.5 else "Prunus padus", "confidence": max(p, 1 - p),
         "target_probability": p, "alternatives": [{"taxon": "Prunus padus", "probability": round(1 - p, 3)}],
         "plant_count_est": count, "height_class": "shrub", "phenology": "flowering", **extra}
    if image:
        o["image"] = {"media_type": "image/svg+xml", "data_base64": SVG}
    return o


def mission(mission_id: str, day: str, observations: list[dict], centre=KOTTENFORST, half_len_m: float = 60.0,
            track: list | None = None) -> dict:
    lat, lon = centre
    if track is None:
        a, b = offset(lat, lon, -half_len_m), offset(lat, lon, half_len_m)
        track = [[[a[1], a[0]], [b[1], b[0]]]]
    return {
        "mission_id": mission_id, "robot_id": "TEST-UGV", "source_kind": "simulated", "area_name": "Test area",
        "started_at": f"{day}T09:00:00+02:00", "ended_at": f"{day}T11:00:00+02:00",
        "track": track, "detection_range_m": 10.0, "model": {"name": "test-model", "version": "0"},
        "simulator": {"name": "pytest", "version": "0", "seed": 0}, "observations": observations,
    }
