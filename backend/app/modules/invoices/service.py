"""Excel matching-file helpers and MySQL source query-param validation."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import HTTPException

import main
from backend.app.modules.invoices.queries import normalize_invoice_source, resolve_source_table

UPLOADS_DIR = Path("excel_uploads")
UPLOADS_DIR.mkdir(exist_ok=True)

DEFAULT_EXCEL_PATH = main.MATCHING_EXCEL_FILE_PATH
CURRENT_EXCEL_PATH = DEFAULT_EXCEL_PATH


def get_excel_uploads() -> List[Dict]:
    """Get list of uploaded Excel files"""
    uploads = []
    if not UPLOADS_DIR.exists():
        return uploads

    for file_path in sorted(UPLOADS_DIR.glob("*.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            stat = file_path.stat()
            uploads.append({
                "stored_name": file_path.name,
                "original_name": file_path.stem + ".xlsx",
                "path": str(file_path),
                "uploaded_at": datetime.fromtimestamp(stat.st_mtime).isoformat()
            })
        except Exception:
            continue

    return uploads


def get_current_excel_info() -> Dict:
    """Get current Excel file information"""
    global CURRENT_EXCEL_PATH

    # Sync with main.py's current path
    CURRENT_EXCEL_PATH = main.MATCHING_EXCEL_FILE_PATH

    # Check if current path exists
    exists = os.path.exists(CURRENT_EXCEL_PATH) if CURRENT_EXCEL_PATH else False

    # Get all uploads
    uploads = get_excel_uploads()

    return {
        "path": CURRENT_EXCEL_PATH or "",
        "exists": exists,
        "uploads": uploads
    }


def sync_excel_path_with_main(path: str):
    """Sync Excel path with main.py"""
    global CURRENT_EXCEL_PATH

    CURRENT_EXCEL_PATH = path
    os.environ['EXCEL_FILE_PATH'] = path
    main.MATCHING_EXCEL_FILE_PATH = path


def refresh_excel_path_from_main() -> str:
    """Pull MATCHING_EXCEL_FILE_PATH from main.py after a process run."""
    global CURRENT_EXCEL_PATH
    CURRENT_EXCEL_PATH = main.MATCHING_EXCEL_FILE_PATH
    return CURRENT_EXCEL_PATH


def resolve_mysql_source(source: Optional[str]) -> tuple:
    """Validate source query param; return (source_id, table_name)."""
    key = (source or "bills").strip().lower()
    if key not in ("bills", "xtract", "btcbills"):
        raise HTTPException(
            status_code=400,
            detail="source must be one of: bills, xtract, btcbills",
        )
    table = resolve_source_table(key)
    if not table:
        source_id, table = normalize_invoice_source("bills")
        return source_id, table
    return key, table
