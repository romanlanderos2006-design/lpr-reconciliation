"""FastAPI application entrypoint for the LPR Revenue Reconciliation Engine.

Routes:
    GET  /health                    liveness/readiness probe
    GET  /                          serves the static dashboard (index.html), same-origin
    GET  /api/v1/ledger             paginated read of the reconciliation ledger
    POST /api/v1/scan/manual        reconcile a raw OCR payload passed directly as JSON
    POST /api/v1/scan/upload        live multi-vehicle PlateRecognizer upload + reconcile
    POST /api/v1/scan/external      legacy upload path (simulates if vendor call fails)
"""

from __future__ import annotations

import logging
import os
import random
import string
from datetime import datetime, timezone
from pathlib import Path
from typing import List, NamedTuple, Optional

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError

from app.models import (
    BoundingBox,
    OCRRawInput,
    ReconciliationResult,
    ReconciliationStatus,
    ScanSource,
    ValidatedPlate,
)
from app.reconciler import (
    LEDGER_JSON_PATH,
    PAYMENTS_CSV_PATH,
    append_many_to_ledger,
    append_to_ledger,
    load_ledger,
    reconcile_plate,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("lpr.main")

# --------------------------------------------------------------------------------------- #
# External OCR provider configuration (PlateRecognizer: https://platerecognizer.com/)
# Prefer PLATERECOGNIZER_API_KEY from the environment in real deployments. The default
# below satisfies the live-fire wiring requirement; rotate this token if it has been
# shared outside a private channel, and never commit a production secret to a public repo.
# --------------------------------------------------------------------------------------- #
PLATERECOGNIZER_API_URL = os.getenv(
    "PLATERECOGNIZER_API_URL", "https://api.platerecognizer.com/v1/plate-reader/"
)
PLATERECOGNIZER_API_KEY: str = os.getenv(
    "PLATERECOGNIZER_API_KEY", "API Key Goes Here"
)
EXTERNAL_API_TIMEOUT_SECONDS = float(os.getenv("PLATERECOGNIZER_TIMEOUT_SECONDS", "15.0"))

MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(8 * 1024 * 1024)))  # 8 MB default
ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/jpg"}
DEFAULT_LOCATION_METADATA = os.getenv("DEFAULT_LOCATION_METADATA", "North Gate - Level 2")
MAX_PLATES_PER_IMAGE = int(os.getenv("MAX_PLATES_PER_IMAGE", "25"))

# Magic-byte signatures used to reject Content-Type spoofing (e.g. .exe renamed to .jpg).
_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_WEBP_RIFF = b"RIFF"
_WEBP_WEBP = b"WEBP"

# --------------------------------------------------------------------------------------- #
# Dashboard: served same-origin from FastAPI (avoids CORS entirely for the common case of
# `uvicorn app.main:app` + browsing to `/`). CORS is *also* configured below as
# defense-in-depth for anyone who serves index.html from a different origin/port instead.
# --------------------------------------------------------------------------------------- #
FRONTEND_INDEX_PATH = Path(
    os.getenv("LPR_FRONTEND_INDEX", str(Path(__file__).resolve().parent.parent / "index.html"))
)
_allowed_origins_env = os.getenv("ALLOWED_ORIGINS", "*")
ALLOWED_ORIGINS = [o.strip() for o in _allowed_origins_env.split(",") if o.strip()]

LEDGER_DEFAULT_LIMIT = int(os.getenv("LEDGER_DEFAULT_LIMIT", "100"))
LEDGER_MAX_LIMIT = int(os.getenv("LEDGER_MAX_LIMIT", "500"))

app = FastAPI(
    title="LPR Revenue Reconciliation Engine",
    description="Automated License Plate Recognition & Revenue Reconciliation API.",
    version="1.0.0",
)

# No cookies/auth headers are used anywhere in this API, so `allow_credentials=False` is
# safe to pair with a wildcard origin -- browsers reject the credentials+wildcard
# combination anyway. Tighten `ALLOWED_ORIGINS` before deploying this beyond a local demo.
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last-resort handler: never leak internals (stack traces, paths, secrets) to clients."""
    logger.exception("unhandled exception while processing %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "internal server error"})


def _classify_validation_error(exc: ValidationError) -> ReconciliationStatus:
    """Map a `ValidatedPlate` construction failure to the most specific rejection status."""
    for error in exc.errors():
        if error.get("loc") == ("confidence",):
            return ReconciliationStatus.REJECTED_LOW_CONFIDENCE
    return ReconciliationStatus.REJECTED_INVALID_FORMAT


async def _validate_and_reconcile(
    raw: OCRRawInput,
    location_metadata: str = DEFAULT_LOCATION_METADATA,
    bounding_box: Optional[BoundingBox] = None,
    *,
    persist: bool = True,
) -> ReconciliationResult:
    """Shared pipeline: ValidatedPlate -> reconcile_plate -> (optional) ledger append.

    Rejections (bad format / low confidence) are normal, auditable outcomes.
    When `persist=False`, the caller is responsible for a batched atomic ledger write
    (used by multi-vehicle uploads). `bounding_box` is carried through untouched so the
    dashboard's visual-evidence viewer works even for rejected reads.
    """
    try:
        validated = ValidatedPlate.from_raw(raw)
    except ValidationError as exc:
        fallback_plate = raw.raw_plate.strip().upper() if isinstance(raw.raw_plate, str) else ""
        result = ReconciliationResult(
            plate=fallback_plate,
            status=_classify_validation_error(exc),
            confidence=raw.confidence,
            source=raw.source,
            location_metadata=location_metadata,
            bounding_box=bounding_box,
            detail=f"rejected before reconciliation: {exc.errors()[0]['msg']}",
        )
        if persist:
            await append_to_ledger(result)
        return result

    result = await reconcile_plate(
        validated, location_metadata=location_metadata, bounding_box=bounding_box
    )
    if persist:
        await append_to_ledger(result)
    return result


def _looks_like_image(image_bytes: bytes) -> bool:
    """Validate file content by magic bytes, not just the client-supplied Content-Type."""
    if len(image_bytes) < 12:
        return False
    if image_bytes.startswith(_JPEG_MAGIC) or image_bytes.startswith(_PNG_MAGIC):
        return True
    # WEBP: RIFF....WEBP
    if image_bytes.startswith(_WEBP_RIFF) and image_bytes[8:12] == _WEBP_WEBP:
        return True
    return False


def _read_upload_guardrails(image: UploadFile, image_bytes: bytes) -> None:
    """Shared upload checks used by both /upload and /external."""
    content_type = (image.content_type or "").lower().strip()
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                f"unsupported content type '{image.content_type}'; "
                f"expected one of {sorted(ALLOWED_CONTENT_TYPES)}"
            ),
        )
    if len(image_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"upload exceeds the {MAX_UPLOAD_BYTES} byte limit",
        )
    if not image_bytes:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="empty upload")
    if not _looks_like_image(image_bytes):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="file content is not a valid JPEG, PNG, or WEBP image",
        )


async def _fetch_platerecognizer_results(image_bytes: bytes, filename: str) -> list[dict]:
    """POST the image to PlateRecognizer and return the raw `results` array.

    Raises HTTPException with a readable detail for vendor/client failures so the
    production upload endpoint never crashes the server process.
    """
    headers = {"Authorization": f"Token {PLATERECOGNIZER_API_KEY}"}
    files = {"upload": (filename or "upload.jpg", image_bytes)}

    try:
        async with httpx.AsyncClient(timeout=EXTERNAL_API_TIMEOUT_SECONDS) as client:
            response = await client.post(PLATERECOGNIZER_API_URL, headers=headers, files=files)
    except httpx.TimeoutException:
        logger.warning("PlateRecognizer timed out after %.1fs", EXTERNAL_API_TIMEOUT_SECONDS)
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Plate recognition timed out. Please try again with a smaller image.",
        ) from None
    except httpx.RequestError as exc:
        logger.warning("PlateRecognizer network error: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not reach the plate recognition service. Please try again shortly.",
        ) from None

    if response.status_code == 429:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Plate recognition rate limit exceeded. Please wait a moment and try again.",
        )
    if response.status_code == 400:
        logger.warning("PlateRecognizer 400: %s", response.text[:300])
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The recognition service rejected this image. Try a clearer JPEG/PNG photo.",
        )
    if response.status_code in (401, 403):
        logger.error("PlateRecognizer auth failure status=%s", response.status_code)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Plate recognition service authentication failed. Check the API token.",
        )
    if response.status_code >= 400:
        logger.warning(
            "PlateRecognizer unexpected status=%s body=%s",
            response.status_code,
            response.text[:300],
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Plate recognition service returned an error (HTTP {response.status_code}).",
        )

    try:
        payload = response.json()
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Plate recognition service returned an unreadable response.",
        ) from None

    results = payload.get("results") or []
    if not isinstance(results, list):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Plate recognition service returned an unexpected payload shape.",
        )
    if not results:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No license plate was detected in this image. Try a clearer, closer shot.",
        )
    return results


class PlateCandidate(NamedTuple):
    """One detected plate from a vendor response, paired with its (possibly absent)
    bounding box. Kept together so both travel through the same reconcile/ledger path."""

    raw: OCRRawInput
    bounding_box: Optional[BoundingBox]


def _extract_bounding_box(item: dict) -> Optional[BoundingBox]:
    """Best-effort extraction of PlateRecognizer's `box` object ({xmin, ymin, xmax, ymax}).

    Vendor responses are untrusted: `box` may be absent, missing keys, non-numeric, or
    have an inverted/degenerate rectangle. Any of that degrades to `None` -- "no visual
    evidence available" -- rather than ever failing the scan itself (red-team finding:
    "missing bounding boxes" must never crash reconciliation).
    """
    box = item.get("box")
    if not isinstance(box, dict):
        return None
    try:
        return BoundingBox(
            xmin=box.get("xmin"),
            ymin=box.get("ymin"),
            xmax=box.get("xmax"),
            ymax=box.get("ymax"),
        )
    except (ValidationError, TypeError, ValueError) as exc:
        logger.debug("discarding malformed PlateRecognizer bounding box %r: %s", box, exc)
        return None


def _results_to_candidates(results: list[dict]) -> list[PlateCandidate]:
    """Convert every PlateRecognizer candidate into a PlateCandidate (multi-vehicle).

    Skips entries missing a plate string. Caps at MAX_PLATES_PER_IMAGE and prefers
    higher-confidence duplicates when the vendor returns the same plate twice.
    """
    # Sort highest score first so the per-plate dedupe keeps the best read.
    ranked = sorted(results, key=lambda r: float(r.get("score") or 0.0), reverse=True)
    seen_plates: set[str] = set()
    candidates: list[PlateCandidate] = []

    for item in ranked:
        plate = item.get("plate")
        if not plate:
            continue
        key = str(plate).strip().upper()
        if not key or key in seen_plates:
            continue
        seen_plates.add(key)
        raw = OCRRawInput(
            raw_plate=str(plate),
            confidence=float(item.get("score") or 0.0),
            source=ScanSource.EXTERNAL_API,
            camera_id="GATE-UPLOAD",
        )
        candidates.append(PlateCandidate(raw=raw, bounding_box=_extract_bounding_box(item)))
        if len(candidates) >= MAX_PLATES_PER_IMAGE:
            break

    if not candidates:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No license plate was detected in this image. Try a clearer, closer shot.",
        )
    return candidates


@app.get("/health", tags=["ops"])
async def health() -> dict:
    """Liveness/readiness probe. Reports whether the data files backing the engine exist,
    without ever revealing the actual vendor API key."""
    return {
        "status": "ok",
        "payments_csv_found": PAYMENTS_CSV_PATH.exists(),
        "ledger_json_found": LEDGER_JSON_PATH.exists(),
        "platerecognizer_configured": bool(PLATERECOGNIZER_API_KEY),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/", include_in_schema=False)
async def dashboard() -> FileResponse:
    """Serves `index.html` from the project root so the dashboard and API share an origin
    (no CORS needed) when run via `uvicorn app.main:app`."""
    if not FRONTEND_INDEX_PATH.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="index.html not found")
    return FileResponse(FRONTEND_INDEX_PATH)


@app.get("/api/v1/ledger", tags=["ledger"])
async def get_ledger(
    limit: int = Query(
        default=LEDGER_DEFAULT_LIMIT,
        ge=1,
        le=LEDGER_MAX_LIMIT,
        description="Max number of most-recent entries to return.",
    ),
) -> list:
    """Returns the reconciliation ledger, newest entries first, capped at `limit`."""
    return await load_ledger(limit=limit)


@app.post("/api/v1/scan/manual", response_model=ReconciliationResult, tags=["scan"])
async def scan_manual(payload: OCRRawInput) -> ReconciliationResult:
    """Accepts a raw OCR payload directly as JSON -- useful for integration tests and demos
    without needing a real camera feed or the PlateRecognizer API."""
    return await _validate_and_reconcile(payload)


@app.post(
    "/api/v1/scan/upload",
    response_model=List[ReconciliationResult],
    tags=["scan"],
)
async def scan_upload(
    file: UploadFile = File(...),
    location_metadata: str = Form(default=DEFAULT_LOCATION_METADATA),
) -> List[ReconciliationResult]:
    """Live-fire multi-vehicle upload path.

    1. Validate the uploaded image (type, size, magic bytes).
    2. Forward it to PlateRecognizer via multipart field `upload`.
    3. Iterate *every* plate in `results`, run ValidatedPlate / reconcile_plate, carrying
       each plate's bounding box (if the vendor supplied one) through untouched.
    4. Append all outcomes to the ledger in one atomic write (with location_metadata).
    5. Return the full list of ReconciliationResult objects.
    """
    image_bytes = await file.read(MAX_UPLOAD_BYTES + 1)
    _read_upload_guardrails(file, image_bytes)

    vendor_results = await _fetch_platerecognizer_results(
        image_bytes, file.filename or "upload.jpg"
    )
    candidates = _results_to_candidates(vendor_results)

    outcomes: list[ReconciliationResult] = []
    for candidate in candidates:
        outcomes.append(
            await _validate_and_reconcile(
                candidate.raw,
                location_metadata=location_metadata,
                bounding_box=candidate.bounding_box,
                persist=False,
            )
        )

    await append_many_to_ledger(outcomes)
    logger.info(
        "multi-vehicle upload reconciled %d plate(s) at location=%r",
        len(outcomes),
        location_metadata,
    )
    return outcomes


def _simulate_plate_read() -> OCRRawInput:
    """Fallback used only by the legacy `/external` route when the live API call fails."""
    plate = "".join(random.choices(string.ascii_uppercase, k=3)) + "".join(
        random.choices(string.digits, k=3)
    )
    confidence = round(random.uniform(0.70, 0.99), 3)
    logger.warning(
        "PlateRecognizer unavailable; using simulated read plate=%s confidence=%.2f",
        plate,
        confidence,
    )
    return OCRRawInput(raw_plate=plate, confidence=confidence, source=ScanSource.SIMULATED)


@app.post("/api/v1/scan/external", response_model=ReconciliationResult, tags=["scan"])
async def scan_external(
    image: UploadFile = File(...),
    location_metadata: str = Form(default=DEFAULT_LOCATION_METADATA),
) -> ReconciliationResult:
    """Legacy upload path: tries PlateRecognizer (first plate), falls back to simulation."""
    image_bytes = await image.read(MAX_UPLOAD_BYTES + 1)
    _read_upload_guardrails(image, image_bytes)

    bounding_box: Optional[BoundingBox] = None
    try:
        vendor_results = await _fetch_platerecognizer_results(
            image_bytes, image.filename or "upload.jpg"
        )
        candidate = _results_to_candidates(vendor_results)[0]
        raw = candidate.raw
        bounding_box = candidate.bounding_box
    except HTTPException as exc:
        logger.warning("PlateRecognizer call failed (%s); falling back to simulation", exc.detail)
        raw = _simulate_plate_read()

    return await _validate_and_reconcile(
        raw, location_metadata=location_metadata, bounding_box=bounding_box
    )
