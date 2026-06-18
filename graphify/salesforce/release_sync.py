"""Salesforce Release synchronization for Governor Limits.

Detects staleness of Governor Limits (set in constants.py via ADR-006, ADR-026)
and offers manual sync with the latest Salesforce release notes.

Staleness thresholds (ADR-026): 0-30 days FRESH, 31-90 WARNING, 91+ ERROR. The
single source of truth for that mapping is :func:`_staleness_status`; both the
sync entry point and :func:`check_staleness` route through it so the boundaries
can never drift apart.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .constants import (
    GOVERNOR_LIMITS,
    GOVERNOR_LIMITS_SYNCED_AT,
    GOVERNOR_LIMITS_VERSION,
)

#: Staleness boundaries in days (ADR-026). ``> WARNING_AFTER`` is WARNING,
#: ``> ERROR_AFTER`` is ERROR; at or below ``WARNING_AFTER`` is FRESH.
_WARNING_AFTER_DAYS = 30
_ERROR_AFTER_DAYS = 90


def _staleness_status(days_stale: int) -> str:
    """Map an age in days to a staleness status (ADR-026, single source).

    >91 -> ERROR, 31-90 -> WARNING, 0-30 -> FRESH. A negative age (used by
    :func:`check_staleness` for a missing file) never reaches here.
    """
    if days_stale > _ERROR_AFTER_DAYS:
        return "ERROR"
    if days_stale > _WARNING_AFTER_DAYS:
        return "WARNING"
    return "FRESH"


def _fetch_release_limits() -> dict[str, int]:
    """Return the latest Governor Limits (mock backend).

    In production this would fetch + parse the Salesforce Release Notes; offline
    it returns the values baked into :data:`constants.GOVERNOR_LIMITS` so a sync
    with no network access reports zero drift instead of failing (ADR-026 offline
    fallback). Tests monkeypatch this to simulate a release that changed a limit.
    """
    return dict(GOVERNOR_LIMITS)


def sync_release_notes(out_dir: Path | str, dry_run: bool = False) -> dict:
    """Sync Governor Limits from Salesforce Release Notes.

    Flow:
    1. Fetch latest limits via :func:`_fetch_release_limits` (mockable backend).
    2. Diff them against the baseline in :data:`constants.GOVERNOR_LIMITS` to
       count how many limits changed.
    3. Write a version file with metadata (unless ``dry_run``).

    Args:
        out_dir: Output directory for the version file.
        dry_run: If True, only preview without writing.

    Returns:
        ``{"changed": int, "new_limits": {...}, "version": str,
        "days_stale": int, "status": "FRESH|WARNING|ERROR", "dry_run": bool}``.
        The ``days_stale`` / ``status`` keys surface the same staleness signal as
        :func:`check_staleness` (ADR-026) directly from the sync entry point, so
        a caller never has to re-open the version file to learn freshness.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    new_limits = _fetch_release_limits()

    # Drift: count limits whose fetched value differs from the baseline (a key
    # absent from the baseline also counts as a change).
    changed = sum(
        1
        for key, value in new_limits.items()
        if GOVERNOR_LIMITS.get(key) != value
    )

    version_info = {
        "version": GOVERNOR_LIMITS_VERSION,
        "api_version": "63.0",
        "released_date": GOVERNOR_LIMITS_SYNCED_AT,
        "last_synced": datetime.now().isoformat(),
        "days_stale": 0,
        "limits": new_limits,
    }

    version_file = out_dir / "sf_governor_limits_version.json"

    if not dry_run:
        with open(version_file, "w") as f:
            json.dump(version_info, f, indent=2)
        staleness = check_staleness(version_file)
    else:
        # Nothing written: a just-synced version is fresh by definition.
        staleness = {"days_stale": 0, "status": "FRESH"}

    return {
        "changed": changed,
        "new_limits": new_limits,
        "version": GOVERNOR_LIMITS_VERSION,
        "days_stale": staleness["days_stale"],
        "status": staleness["status"],
        "dry_run": dry_run,
    }


def check_staleness(limits_file: Path | str) -> dict:
    """Check if Governor Limits are stale.

    Args:
        limits_file: Path to sf_governor_limits_version.json

    Returns:
        ``{"days_stale": int, "status": "FRESH|WARNING|ERROR|MISSING"}``.
    """
    limits_file = Path(limits_file)

    if not limits_file.exists():
        return {"days_stale": -1, "status": "MISSING"}

    with open(limits_file, "r") as f:
        data = json.load(f)

    last_sync = datetime.fromisoformat(data["last_synced"])
    days_stale = (datetime.now() - last_sync).days

    return {"days_stale": days_stale, "status": _staleness_status(days_stale)}
