"""Per-mailbox folders for the booking download and the Step 7 report."""

from __future__ import annotations

import os
from pathlib import Path


def scheduled_instance_id() -> str:
    """Mailbox instance from SCHEDULED_INSTANCE (a, b, or c). Defaults to a."""
    raw = (os.getenv("SCHEDULED_INSTANCE") or "a").strip()
    cleaned = "".join(ch for ch in raw if ch.isalnum() or ch == "_")
    return cleaned or "a"


def run_excel_dir(kind: str) -> Path:
    """Working-directory folder for this mailbox: api_a or push_b, and so on."""
    if kind not in ("api", "push"):
        raise ValueError(f"Unknown run excel folder kind: {kind}")
    return Path(f"{kind}_{scheduled_instance_id()}")


def clear_run_excels() -> None:
    """Delete .xlsx files this mailbox wrote. Other instances' folders are left alone."""
    for kind in ("api", "push"):
        directory = run_excel_dir(kind)
        if not directory.is_dir():
            continue
        for path in directory.glob("*.xlsx"):
            try:
                path.unlink()
                print(f"   🗑️  Deleted {path}")
            except OSError as err:
                print(f"   ⚠️  Could not delete {path}: {err}")
