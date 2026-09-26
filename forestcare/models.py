"""Request schemas. `MissionIn` is the contract between a robot and the system."""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, Field, field_validator, model_validator

from .config import CORRECTION_TAXA

LonLat = tuple[float, float]


class Alternative(BaseModel):
    taxon: str
    probability: float = Field(ge=0, le=1)


class ImageIn(BaseModel):
    media_type: Literal["image/jpeg", "image/png", "image/svg+xml"]
    data_base64: str = Field(max_length=8_000_000)


class ObservationIn(BaseModel):
    """One candidate detection from the on-board classifier."""

    uid: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9._:-]+$")
    observed_at: AwareDatetime
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    gnss_accuracy_m: float = Field(gt=0, le=500, description="Estimated horizontal accuracy (1 sigma)")
    predicted_taxon: str
    confidence: float = Field(ge=0, le=1, description="Probability of predicted_taxon")
    target_probability: float = Field(ge=0, le=1, description="Probability of Prunus serotina")
    alternatives: list[Alternative] = Field(default_factory=list, max_length=10)
    plant_count_est: int = Field(default=1, ge=1, le=10_000)
    height_class: Literal["seedling", "shrub", "small_tree", "tree"] | None = None
    phenology: Literal["vegetative", "flowering", "fruiting", "autumn_colour"] | None = None
    image: ImageIn | None = None


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
    sensors: dict[str, str] = Field(default_factory=dict)
    model: SoftwareInfo
    simulator: dict | None = None
    notes: str | None = Field(default=None, max_length=2000)
    observations: list[ObservationIn] = Field(default_factory=list, max_length=5000)

    @model_validator(mode="after")
    def _check(self) -> "MissionIn":
        if self.ended_at < self.started_at:
            raise ValueError("ended_at is before started_at")
        if self.source_kind == "simulated" and not self.simulator:
            raise ValueError("simulated missions must describe the simulator (version, seed)")
        if any(len(line) < 2 for line in self.track):
            raise ValueError("every track segment needs at least two points")
        return self


class ReviewIn(BaseModel):
    decision: Literal["confirmed", "rejected", "uncertain", "field_visit"]
    reviewer: str = Field(min_length=2, max_length=120)
    reviewer_role: str | None = Field(default=None, max_length=120)
    corrected_taxon: str | None = None
    note: str | None = Field(default=None, max_length=2000)

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

