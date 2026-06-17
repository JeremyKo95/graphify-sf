"""Salesforce Release synchronization for Governor Limits.

Detects staleness of Governor Limits (set in constants.py via ADR-006, ADR-026)
and offers manual sync with latest Salesforce release notes.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def sync_release_notes(out_dir: Path | str, dry_run: bool = False) -> dict:
    """Sync Governor Limits from Salesforce Release Notes.

    Flow:
    1. Fetch (or mock) latest release notes
    2. Parse Governor Limits changes
    3. Generate version file with metadata

    Args:
        out_dir: Output directory for version file
        dry_run: If True, only preview without writing

    Returns:
        {"changed": int, "new_limits": {...}, "version": "Spring '25"}
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Mock: In production, fetch from Salesforce official docs
    new_limits = {
        "soql_queries_per_transaction": 100,
        "dml_statements_per_transaction": 150,
        "heap_size_bytes_sync": 6_000_000,
        "heap_size_bytes_async": 12_000_000,
        "cpu_time_ms_sync": 10_000,
        "cpu_time_ms_batch": 60_000,
        "callouts_per_transaction": 100,
    }

    version_info = {
        "version": "spring-2025",
        "api_version": "63.0",
        "released_date": "2025-03-01",
        "last_synced": datetime.now().isoformat(),
        "days_stale": 0,
        "limits": new_limits,
    }

    version_file = out_dir / "sf_governor_limits_version.json"

    if not dry_run:
        with open(version_file, "w") as f:
            json.dump(version_info, f, indent=2)

    return {
        "changed": 0,
        "new_limits": new_limits,
        "version": "spring-2025",
        "dry_run": dry_run,
    }


def check_staleness(limits_file: Path | str) -> dict:
    """Check if Governor Limits are stale.

    Args:
        limits_file: Path to sf_governor_limits_version.json

    Returns:
        {"days_stale": int, "status": "FRESH|WARNING|ERROR"}
    """
    limits_file = Path(limits_file)

    if not limits_file.exists():
        return {"days_stale": -1, "status": "MISSING"}

    with open(limits_file, "r") as f:
        data = json.load(f)

    last_sync = datetime.fromisoformat(data["last_synced"])
    days_stale = (datetime.now() - last_sync).days

    if days_stale > 90:
        status = "ERROR"
    elif days_stale > 30:
        status = "WARNING"
    else:
        status = "FRESH"

    return {"days_stale": days_stale, "status": status}
