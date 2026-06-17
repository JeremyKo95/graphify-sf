"""
graphify-sf: Salesforce Release sync (Governor Limit refresh).

Keeps `constants.GOVERNOR_LIMITS` honest against the current Salesforce release
without ever blocking an analysis run (ADR-006, ADR-026, ADR-010).

Design rules baked in here:

- OFFLINE-FIRST. `_fetch_release_limits()` is a deterministic, network-free
  source (mockable). A real online fetch can replace it later, but its failure
  must never abort analysis — the caller falls back to the hardcoded limits.
- NO AUTO-APPLY. `sync_release_notes()` writes a *version file*
  (`sf_governor_limits_version.json`) and reports a diff; it NEVER rewrites
  `constants.py`. Promoting new values into `constants.GOVERNOR_LIMITS` is a
  manual, human-reviewed step (CLAUDE.md / ADR-026 forbid auto-edit).
- STAGED STALENESS (ADR-026): 0-30d FRESH, 30-90d WARNING, 90d+ ERROR. Staleness
  is a *warning*, not a hard stop — analysis keeps running.

`sync_release_notes()` measures staleness from `constants.GOVERNOR_LIMITS_SYNCED_AT`
(the last in-code sync). `check_staleness()` measures it from a previously written
version file's `last_synced`.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from graphify.salesforce.constants import (
    GOVERNOR_LIMITS,
    GOVERNOR_LIMITS_SYNCED_AT,
    GOVERNOR_LIMITS_VERSION,
)

#: Staleness thresholds in days (ADR-026).
_STALE_WARNING_DAYS = 30
_STALE_ERROR_DAYS = 90

#: Metadata for the current Salesforce release the limits track.
_CURRENT_RELEASE = {
    "version": GOVERNOR_LIMITS_VERSION,  # e.g. "spring-2025"
    "api_version": "63.0",
    "released_date": "2025-03-01",
}


def _fetch_release_limits() -> dict[str, int]:
    """Return the Governor Limits for the current Salesforce release.

    OFFLINE/MOCK source (ADR-010): this is network-free and deterministic so
    that CI and air-gapped environments work. Tests / a future online backend
    monkeypatch this function. On a real fetch failure the caller keeps the
    hardcoded `constants.GOVERNOR_LIMITS` — sync must never abort analysis.

    Governor Limits have not actually changed since 2018 (PRD release table), so
    the offline default mirrors `constants.GOVERNOR_LIMITS`.
    """
    return dict(GOVERNOR_LIMITS)


def _staleness_status(days_stale: int) -> str:
    """Map an age in days to a staleness status (ADR-026 thresholds)."""
    if days_stale > _STALE_ERROR_DAYS:
        return "ERROR"
    if days_stale > _STALE_WARNING_DAYS:
        return "WARNING"
    return "FRESH"


def sync_release_notes(out_dir: Path, dry_run: bool = False) -> dict:
    """Sync Governor Limits from the current Salesforce release.

    Flow:
        1. Fetch the latest release limits (offline-first, see
           `_fetch_release_limits`).
        2. Diff them against `constants.GOVERNOR_LIMITS`.
        3. Report staleness measured from `GOVERNOR_LIMITS_SYNCED_AT`.
        4. Write `sf_governor_limits_version.json` (skipped when ``dry_run``).

    Does NOT edit `constants.py` — promoting new values is a manual step
    (ADR-026). On any fetch error the hardcoded limits are kept and analysis
    continues.

    Args:
        out_dir: Directory the version file is written to (created if missing).
        dry_run: When True, preview only — no file is written.

    Returns:
        ``{"changed": int, "new_limits": {...}, "version": str,
           "days_stale": int, "status": "FRESH|WARNING|ERROR",
           "version_file": str, "dry_run": bool}``
    """
    out_dir = Path(out_dir)

    # 1. Fetch (offline-first). A failure must not abort — fall back to current.
    try:
        new_limits = _fetch_release_limits()
    except Exception as exc:  # pragma: no cover - defensive, offline fallback
        print(f"WARNING: release fetch failed ({exc}); keeping hardcoded limits")
        new_limits = dict(GOVERNOR_LIMITS)

    # 2. Diff against the in-code limits (count of differing/new keys).
    changed_keys = [
        key
        for key in set(new_limits) | set(GOVERNOR_LIMITS)
        if new_limits.get(key) != GOVERNOR_LIMITS.get(key)
    ]

    # 3. Staleness from the last in-code sync date (ADR-026).
    last_sync = datetime.fromisoformat(GOVERNOR_LIMITS_SYNCED_AT)
    days_stale = (datetime.now() - last_sync).days
    status = _staleness_status(days_stale)
    if status == "ERROR":
        print(f"ERROR: Governor Limits are {days_stale} days old (run sync-release-notes)")
    elif status == "WARNING":
        print(f"WARNING: Governor Limits are {days_stale} days old")
    else:
        print(f"INFO: Governor Limits are {days_stale} days old (fresh)")

    # 4. Build + (optionally) write the version file.
    now = datetime.now()
    version_file = out_dir / "sf_governor_limits_version.json"
    version_info = {
        **_CURRENT_RELEASE,
        "last_synced": now.isoformat(),
        "days_stale": 0,
        "limits": new_limits,
    }

    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        with open(version_file, "w", encoding="utf-8") as fh:
            json.dump(version_info, fh, indent=2)

    return {
        "changed": len(changed_keys),
        "new_limits": new_limits,
        "version": _CURRENT_RELEASE["version"],
        "days_stale": days_stale,
        "status": status,
        "version_file": str(version_file),
        "dry_run": dry_run,
    }


def check_staleness(limits_file: Path) -> dict:
    """Report how stale a written version file is (ADR-026 thresholds).

    Reads ``last_synced`` from a `sf_governor_limits_version.json` produced by
    `sync_release_notes()` and grades it: 0-30d FRESH, 30-90d WARNING,
    90d+ ERROR. Staleness is advisory only — it never blocks analysis.

    Args:
        limits_file: Path to a version file written by `sync_release_notes`.

    Returns:
        ``{"days_stale": int, "status": "FRESH|WARNING|ERROR"}``
    """
    with open(limits_file, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    last_sync = datetime.fromisoformat(data["last_synced"])
    days_stale = (datetime.now() - last_sync).days
    return {"days_stale": days_stale, "status": _staleness_status(days_stale)}
