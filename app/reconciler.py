"""Payment reconciliation against the CSV payments export, plus atomic audit logging.

Two concerns live here, both I/O-bound and both kept off the asyncio event loop via
`asyncio.to_thread`:

1. `reconcile_plate`      -- read-only lookup against `data/mock_payments.csv`.
2. `append_to_ledger`     -- append-only audit trail in `data/reconciliation_ledger.json`,
                             written with a same-directory temp file + `os.replace` so a
                             crash or concurrent reader never observes a half-written file.

Known limitation (documented, not silently ignored): the `asyncio.Lock` below serializes
ledger writes *within a single process*. If this app is ever run with multiple worker
processes (e.g. `uvicorn --workers 4`), each worker gets its own lock and the same
lost-update race reappears across processes. Production fix: an OS-level advisory lock
(e.g. the `filelock` package) around the same read-modify-write, or -- better -- replace
the JSON ledger with a real datastore (Postgres, SQLite with WAL, etc.) once this scaffold
graduates past a single-instance demo.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

import pandas as pd

from app.models import BoundingBox, ReconciliationResult, ReconciliationStatus, ValidatedPlate

logger = logging.getLogger("lpr.reconciler")

# --------------------------------------------------------------------------------------- #
# Paths (env-overridable so tests/deployments can point elsewhere without code changes).
# --------------------------------------------------------------------------------------- #
DATA_DIR = Path(os.getenv("LPR_DATA_DIR", str(Path(__file__).resolve().parent.parent / "data")))
PAYMENTS_CSV_PATH = Path(os.getenv("LPR_PAYMENTS_CSV", str(DATA_DIR / "mock_payments.csv")))
LEDGER_JSON_PATH = Path(os.getenv("LPR_LEDGER_JSON", str(DATA_DIR / "reconciliation_ledger.json")))

_PAYMENTS_COLUMNS = ["plate", "status", "amount", "paid_at", "lot_id"]

# Serializes the ledger's read-modify-write cycle across concurrent asyncio tasks in this
# process. See module docstring for the multi-process caveat.
_LEDGER_LOCK = asyncio.Lock()


# ----------------------------------------------------------------------------- #
# Reconciliation (read side)
# ----------------------------------------------------------------------------- #
def _read_payments_sync() -> pd.DataFrame:
    """Blocking CSV read, isolated so callers can offload it to a worker thread."""
    if not PAYMENTS_CSV_PATH.exists():
        logger.warning("payments CSV not found at %s; treating as an empty ledger", PAYMENTS_CSV_PATH)
        return pd.DataFrame(columns=_PAYMENTS_COLUMNS)

    df = pd.read_csv(
        PAYMENTS_CSV_PATH,
        dtype={"plate": str, "status": str, "lot_id": str},
        keep_default_na=False,  # avoid NaN for blank paid_at, which JSON can't serialize
    )
    df["plate"] = df["plate"].str.strip().str.upper()
    df["status"] = df["status"].str.strip().str.upper()
    return df


async def reconcile_plate(
    validated: ValidatedPlate,
    location_metadata: str = "Unknown",
    bounding_box: Optional[BoundingBox] = None,
) -> ReconciliationResult:
    """Look up a normalized plate in the payments CSV and classify the outcome.

    - Match found with status PAID       -> PAID_VALID
    - No matching row at all             -> UNPAID_DRIVE_OFF ("missing" per spec)
    - Match found but status != PAID     -> UNPAID_DRIVE_OFF (present, but never paid)
    - CSV unreadable/corrupted           -> ERROR (fails safe, never silently drops a scan)

    `location_metadata` and `bounding_box` are stamped onto every result so the atomic
    JSON ledger retains where the scan took place and (if available) exactly where the
    plate was located in the source image, for the dashboard's visual-evidence viewer.
    `bounding_box` is purely informational -- it never affects the reconciliation outcome.
    """
    try:
        df = await asyncio.to_thread(_read_payments_sync)
    except Exception:
        logger.exception("failed to read payments CSV at %s", PAYMENTS_CSV_PATH)
        return ReconciliationResult(
            plate=validated.plate,
            status=ReconciliationStatus.ERROR,
            confidence=validated.confidence,
            source=validated.source,
            location_metadata=location_metadata,
            bounding_box=bounding_box,
            detail="internal error reading the payments ledger",
        )

    matches = df[df["plate"] == validated.plate]

    if matches.empty:
        return ReconciliationResult(
            plate=validated.plate,
            status=ReconciliationStatus.UNPAID_DRIVE_OFF,
            confidence=validated.confidence,
            source=validated.source,
            location_metadata=location_metadata,
            bounding_box=bounding_box,
            detail="no payment record found for this plate",
        )

    paid_matches = matches[matches["status"] == "PAID"]
    if not paid_matches.empty:
        record = paid_matches.iloc[0]
        return ReconciliationResult(
            plate=validated.plate,
            status=ReconciliationStatus.PAID_VALID,
            confidence=validated.confidence,
            source=validated.source,
            location_metadata=location_metadata,
            bounding_box=bounding_box,
            amount_due=float(record.get("amount") or 0.0),
            matched_record=record.to_dict(),
            detail="payment record found and marked PAID",
        )

    record = matches.iloc[0]
    return ReconciliationResult(
        plate=validated.plate,
        status=ReconciliationStatus.UNPAID_DRIVE_OFF,
        confidence=validated.confidence,
        source=validated.source,
        location_metadata=location_metadata,
        bounding_box=bounding_box,
        matched_record=record.to_dict(),
        detail=f"payment record found but status is '{record['status']}', not PAID",
    )


# ----------------------------------------------------------------------------- #
# Atomic ledger logging (write side)
# ----------------------------------------------------------------------------- #
def _atomic_write_json_sync(path: Path, payload: list) -> None:
    """Write `payload` to `path` atomically via a same-directory temp file + `os.replace`.

    `os.replace()` is guaranteed atomic on both POSIX and Windows -- but only when the
    source and destination are on the same filesystem/volume. `tempfile.mkstemp(dir=...)`
    is used (instead of the OS-default temp dir) specifically to guarantee that.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            json.dump(payload, tmp_file, indent=2, default=str)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_name, path)  # atomic swap; readers never see a partial file
    except Exception:
        with contextlib.suppress(OSError):
            os.remove(tmp_name)
        raise


def _load_ledger_sync() -> list:
    """Load the existing ledger, failing safe (empty list) on a missing or corrupt file
    rather than raising and losing the current scan's audit entry."""
    if not LEDGER_JSON_PATH.exists():
        return []
    try:
        content = LEDGER_JSON_PATH.read_text(encoding="utf-8").strip()
        if not content:
            return []
        data = json.loads(content)
        if not isinstance(data, list):
            logger.error("ledger file %s did not contain a JSON list; resetting", LEDGER_JSON_PATH)
            return []
        return data
    except json.JSONDecodeError:
        logger.exception(
            "ledger file %s is corrupted; starting a fresh in-memory ledger "
            "(the corrupt file is left on disk for forensics)",
            LEDGER_JSON_PATH,
        )
        return []


async def append_to_ledger(result: ReconciliationResult) -> None:
    """Append a single result to the JSON ledger using an atomic read-modify-write.

    Guarded by an in-process `asyncio.Lock` so concurrent requests can't interleave their
    read-modify-write cycles and silently drop each other's entries (see module docstring
    for the multi-process caveat this does *not* cover).
    """
    await append_many_to_ledger([result])


async def append_many_to_ledger(results: list[ReconciliationResult]) -> None:
    """Append multiple reconciliation results in one atomic ledger write.

    Used by multi-vehicle image scans so N plates from one frame share a single
    read-modify-write (and still include `location_metadata` via model_dump).
    """
    if not results:
        return

    entries = [r.model_dump(mode="json") for r in results]

    def _do_append() -> None:
        ledger = _load_ledger_sync()
        ledger.extend(entries)
        _atomic_write_json_sync(LEDGER_JSON_PATH, ledger)

    async with _LEDGER_LOCK:
        await asyncio.to_thread(_do_append)


async def load_ledger(limit: Optional[int] = None) -> list:
    """Read the ledger for API consumption (e.g. the dashboard's polling endpoint).

    Reuses `_load_ledger_sync`, so a missing file, an empty file, or a corrupted file all
    fail safe to `[]` rather than raising -- there is no "the ledger doesn't exist yet"
    error case for callers to special-case.

    Returns entries newest-first (most recent scan first), optionally capped to the most
    recent `limit` entries. Capping happens here, server-side, so a long-running demo can't
    grow the response (and the dashboard's DOM) without bound.
    """
    ledger = await asyncio.to_thread(_load_ledger_sync)
    ledger = list(reversed(ledger))
    if limit is not None:
        ledger = ledger[: max(limit, 0)]
    return ledger
