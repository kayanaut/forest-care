"""Request schemas. `MissionIn` is the contract between a robot and the system.

Contract history (all changes are additive; older payloads stay valid):
  1.0  robot missions with classifier detections
  1.1  optional `protocol`, `model` and `metadata` on missions; observations may carry no
       model output (operator marks), no plant count, and a `metadata` dict with sensor,
       pose and time-sync details. Added for the ROS 2 gateway (ros2/forestcare_gateway).
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, Field, field_validator, model_validator

from .config import CORRECTION_TAXA

LonLat = tuple[float, float]
HeightClass = Literal["seedling", "shrub", "small_tree", "tree"]
Phenology = Literal["vegetative", "flowering", "fruiting", "autumn_colour"]


class Alternative(BaseModel):
    taxon: str
    probability: float = Field(ge=0, le=1)


class ImageIn(BaseModel):
    media_type: Literal["image/jpeg", "image/png", "image/svg+xml"]
    data_base64: str = Field(max_length=8_000_000)


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str))


class ObservationIn(BaseModel):
    """One observation: a candidate detection from an on-board classifier, or a point the
    operator marked by hand (then the three model fields are empty)."""

    uid: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9._:-]+$")
    observed_at: AwareDatetime
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    gnss_accuracy_m: float = Field(gt=0, le=500, description="Horizontal position uncertainty of the observation (1 sigma)")
    predicted_taxon: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1, description="Probability of predicted_taxon")
    target_probability: float | None = Field(default=None, ge=0, le=1, description="Probability of Prunus serotina")
    alternatives: list[Alternative] = Field(default_factory=list, max_length=10)
    plant_count_est: int | None = Field(default=1, ge=1, le=10_000, description="null = not estimated")
    height_class: HeightClass | None = None
    phenology: Phenology | None = None
    image: ImageIn | None = None
    metadata: dict[str, Any] | None = Field(default=None, description="Sensor, pose and time-sync details; stored as sent")

    @model_validator(mode="after")
    def _check(self) -> "ObservationIn":
        model_fields = (self.predicted_taxon, self.confidence, self.target_probability)
        if any(v is None for v in model_fields) and any(v is not None for v in model_fields):
            raise ValueError("predicted_taxon, confidence and target_probability are sent together, "
                             "or all left empty for an observation without model output")
        if self.metadata is not None and _json_size(self.metadata) > 32_000:
            raise ValueError("observation metadata is limited to 32 kB of JSON")
        return self


class SoftwareInfo(BaseModel):
    name: str
    version: str


class MissionIn(BaseModel):
    mission_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._:-]+$")
    robot_id: str = Field(min_length=1, max_length=80)
    source_kind: Literal["simulated", "robot"]
    area_name: str | None = None
    started_at: AwareDatetime
    ended_at: AwareDatetime
    track: list[list[LonLat]] = Field(min_length=1, description="Driven path as MultiLineString coordinates [lon, lat]")
    detection_range_m: float = Field(gt=0, le=50, description="Max distance from the track at which the camera detects plants")
    # 'transect': systematic survey; the track may be used to infer that a stand was not re-detected.
    # 'opportunistic': the track is shown, but absence is never inferred from it.
    protocol: Literal["transect", "opportunistic"] = "transect"
    sensors: dict[str, str] = Field(default_factory=dict)
    model: SoftwareInfo | None = Field(default=None, description="The detector used; null if nothing was classified")
    simulator: dict | None = None
    notes: str | None = Field(default=None, max_length=2000)
    metadata: dict[str, Any] | None = Field(default=None, description="Recorder, bag and sensor summary; stored as sent")
    observations: list[ObservationIn] = Field(default_factory=list, max_length=5000)

    @model_validator(mode="after")
    def _check(self) -> "MissionIn":
        if self.ended_at < self.started_at:
            raise ValueError("ended_at is before started_at")
        if self.source_kind == "simulated" and not self.simulator:
            raise ValueError("simulated missions must describe the simulator (version, seed)")
        if any(len(line) < 2 for line in self.track):
            raise ValueError("every track segment needs at least two points")
        if self.model is None and any(o.target_probability is not None for o in self.observations):
            raise ValueError("observations carry model output, so the mission must name the model")
        if self.metadata is not None and _json_size(self.metadata) > 64_000:
            raise ValueError("mission metadata is limited to 64 kB of JSON")
        return self


class ReviewIn(BaseModel):
    decision: Literal["confirmed", "rejected", "uncertain", "field_visit"]
    reviewer: str = Field(min_length=2, max_length=120)
    reviewer_role: str | None = Field(default=None, max_length=120)
    corrected_taxon: str | None = None
    note: str | None = Field(default=None, max_length=2000)
    # Optional labels for confirmed observations. They replace the device's estimate
    # (or fill it in, for field photos that have none).
    plant_count: int | None = Field(default=None, ge=1, le=10_000)
    height_class: HeightClass | None = None
    phenology: Phenology | None = None

    @field_validator("corrected_taxon")
    @classmethod
    def _known_taxon(cls, v: str | None) -> str | None:
        if v is not None and v not in CORRECTION_TAXA:
            raise ValueError(f"corrected_taxon must be one of {CORRECTION_TAXA}")
        return v

    @model_validator(mode="after")
    def _consistent(self) -> "ReviewIn":
        if self.corrected_taxon and self.decision != "rejected":
            raise ValueError("corrected_taxon only applies to rejected observations")
        labelled = self.plant_count is not None or self.height_class or self.phenology
        if labelled and self.decision != "confirmed":
            raise ValueError("plant_count, height_class and phenology describe confirmed Prunus serotina only")
        return self


class StandNoteIn(BaseModel):
    kind: Literal["note", "monitoring_decision", "management_action"]
    text: str = Field(min_length=3, max_length=4000)
    recorded_by: str = Field(min_length=2, max_length=120)
    action_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def _action_needs_date(self) -> "StandNoteIn":
        if self.kind == "management_action" and not self.action_date:
            raise ValueError("management_action entries need the date the action was carried out")
        return self



class PhotoImportMeta(BaseModel):
    """Describes a folder of field photographs imported as one mission."""

    mission_id: str | None = Field(default=None, min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._:-]+$")
    area_name: str | None = Field(default=None, max_length=200)
    photographer: str = Field(min_length=2, max_length=120)
    notes: str | None = Field(default=None, max_length=2000)
    # Mark imports of test/synthetic pictures so they never pass as real field data.
    simulated: bool = False
    source_label: str | None = Field(default=None, max_length=200, description="e.g. the folder name")
