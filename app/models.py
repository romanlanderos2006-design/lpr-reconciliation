"""Pydantic data models for the LPR Reconciliation Engine.

Defines the contract between OCR ingestion, plate validation/normalization, and the
reconciliation result returned to callers and persisted to the audit ledger.

Design note: all business rules that must *never* be bypassed (plate normalization,
minimum confidence threshold) are enforced inside Pydantic validators on `ValidatedPlate`
itself, rather than in application code. That way, `ValidatedPlate(...)` simply cannot be
constructed in a state that violates the rules, regardless of which code path calls it.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

# --------------------------------------------------------------------------------------- #
# Env-tunable business rules (sane defaults matching the spec: 75% confidence threshold).
# --------------------------------------------------------------------------------------- #
MIN_PLATE_CONFIDENCE: float = float(os.getenv("MIN_PLATE_CONFIDENCE", "0.75"))
MIN_PLATE_LENGTH: int = int(os.getenv("MIN_PLATE_LENGTH", "2"))
MAX_PLATE_LENGTH: int = int(os.getenv("MAX_PLATE_LENGTH", "12"))

_NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ScanSource(str, Enum):
    """Where a given OCR read originated from."""

    MANUAL = "manual"
    EXTERNAL_API = "external_api"
    SIMULATED = "simulated"


class ReconciliationStatus(str, Enum):
    """Terminal classification of a reconciliation attempt."""

    PAID_VALID = "PAID_VALID"
    UNPAID_DRIVE_OFF = "UNPAID_DRIVE_OFF"
    REJECTED_LOW_CONFIDENCE = "REJECTED_LOW_CONFIDENCE"
    REJECTED_INVALID_FORMAT = "REJECTED_INVALID_FORMAT"
    ERROR = "ERROR"


class OCRRawInput(BaseModel):
    """Raw, untrusted output straight from an OCR/ANPR source (camera, vendor API, or a
    manual test payload). Nothing here is normalized or threshold-checked yet."""

    model_config = ConfigDict(str_strip_whitespace=True)

    raw_plate: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Unprocessed plate text exactly as read by the OCR engine.",
    )
    confidence: float = Field(
        ..., ge=0.0, le=1.0, description="OCR confidence score in the [0, 1] range."
    )
    camera_id: str = Field(default="UNKNOWN", max_length=64)
    captured_at: datetime = Field(default_factory=_utcnow)
    source: ScanSource = Field(default=ScanSource.MANUAL)


class ValidatedPlate(BaseModel):
    """A normalized, business-rule-enforced plate derived from `OCRRawInput`.

    - Strips every non-alphanumeric character (spaces, hyphens, OCR noise like `*`/`#`)
      and upper-cases the result.
    - Rejects plates below `MIN_PLATE_CONFIDENCE` (default 75%) *at construction time*,
      so no downstream code can accidentally reconcile or log an untrustworthy read.
    - Rejects plates that normalize to an implausible length (empty, or absurdly long),
      which is the usual signature of an OCR misread rather than a real plate.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    plate: str = Field(..., description="Normalized, alphanumeric-only, upper-cased plate.")
    confidence: float = Field(..., ge=0.0, le=1.0)
    source: ScanSource = ScanSource.MANUAL
    captured_at: datetime = Field(default_factory=_utcnow)

    @field_validator("plate", mode="before")
    @classmethod
    def _normalize_plate(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("plate must be a string")
        cleaned = _NON_ALNUM_RE.sub("", value).upper()
        if not (MIN_PLATE_LENGTH <= len(cleaned) <= MAX_PLATE_LENGTH):
            raise ValueError(
                f"normalized plate length must be between {MIN_PLATE_LENGTH} and "
                f"{MAX_PLATE_LENGTH} characters; got {len(cleaned)} for raw input {value!r}"
            )
        return cleaned

    @field_validator("confidence")
    @classmethod
    def _enforce_confidence_threshold(cls, value: float) -> float:
        if value < MIN_PLATE_CONFIDENCE:
            raise ValueError(
                f"confidence {value:.3f} is below the minimum accepted threshold "
                f"of {MIN_PLATE_CONFIDENCE:.2f}"
            )
        return value

    @classmethod
    def from_raw(cls, raw: OCRRawInput) -> "ValidatedPlate":
        """Convenience constructor: applies normalization/threshold rules to a raw OCR read."""
        return cls(
            plate=raw.raw_plate,
            confidence=raw.confidence,
            source=raw.source,
            captured_at=raw.captured_at,
        )


class BoundingBox(BaseModel):
    """Pixel-space bounding box for a detected plate, as returned by PlateRecognizer's
    `box` object (`{xmin, ymin, xmax, ymax}`), in the *original* uploaded image's pixel
    coordinates -- not normalized/percentage coordinates, and not canvas/display pixels.

    This is stored purely for downstream visual evidence (drawing a spotlight over the
    plate on the original image) and is never consulted by any reconciliation logic, so a
    missing or malformed box can never break a scan -- see `main._extract_bounding_box`,
    which treats any construction failure here as "no visual evidence available" (`None`)
    rather than propagating an error.
    """

    xmin: float = Field(..., ge=0)
    ymin: float = Field(..., ge=0)
    xmax: float = Field(..., ge=0)
    ymax: float = Field(..., ge=0)

    @field_validator("xmax")
    @classmethod
    def _xmax_ge_xmin(cls, value: float, info) -> float:
        xmin = info.data.get("xmin")
        if xmin is not None and value < xmin:
            raise ValueError(f"xmax ({value}) must be >= xmin ({xmin})")
        return value

    @field_validator("ymax")
    @classmethod
    def _ymax_ge_ymin(cls, value: float, info) -> float:
        ymin = info.data.get("ymin")
        if ymin is not None and value < ymin:
            raise ValueError(f"ymax ({value}) must be >= ymin ({ymin})")
        return value


class ReconciliationResult(BaseModel):
    """Outcome of matching a validated plate against the payments ledger. This is the
    shape persisted to `reconciliation_ledger.json` and returned by scan endpoints."""

    plate: str
    status: ReconciliationStatus
    confidence: float
    source: ScanSource
    location_metadata: str = Field(
        default="Unknown",
        max_length=128,
        description="Human-readable capture location, e.g. 'North Gate - Level 2'.",
    )
    bounding_box: Optional[BoundingBox] = Field(
        default=None,
        description="Plate bounding box in original-image pixel space, if the vendor "
        "provided one. None for manual scans or when extraction failed.",
    )
    amount_due: Optional[float] = None
    matched_record: Optional[dict] = None
    detail: str = ""
    reconciled_at: datetime = Field(default_factory=_utcnow)

    @field_validator("location_metadata", mode="before")
    @classmethod
    def _normalize_location(cls, value: object) -> str:
        if value is None:
            return "Unknown"
        if not isinstance(value, str):
            raise ValueError("location_metadata must be a string")
        # Strip control characters so free-form gate labels can't smuggle newlines into logs/UI.
        cleaned = "".join(ch for ch in value.strip() if ch.isprintable())
        return cleaned[:128] if cleaned else "Unknown"
