"""
Cron and CLI entrypoints. Scheduled scripts import process_invoices from here.
"""

from __future__ import annotations

import os as _os

from backend.app.integrations.invoice_push_api import (
    API_PUSH_ENDPOINT,
    get_api_bearer_token,
)
from backend.app.modules.ingest.service import process_invoices as _ingest_process_invoices
from backend.app.modules.ingest.unread import count_unread_emails_for_date_range
from backend.app.modules.matching import constants as _cfg
from backend.app.modules.matching.master import match_master_sheet_with_excel as _match_master

API_BEARER_TOKEN = get_api_bearer_token()

MATCHING_EXCEL_FILE_PATH = _os.getenv("EXCEL_FILE_PATH", _cfg.MATCHING_EXCEL_FILE_PATH)


def _sync_to_impl() -> None:
    """Push Excel path into env and matching modules before a run."""
    global MATCHING_EXCEL_FILE_PATH

    if MATCHING_EXCEL_FILE_PATH:
        _os.environ["EXCEL_FILE_PATH"] = MATCHING_EXCEL_FILE_PATH
    try:
        _cfg.MATCHING_EXCEL_FILE_PATH = MATCHING_EXCEL_FILE_PATH
    except Exception:
        pass
    try:
        from backend.app.modules.matching import master as _lm

        _lm.MATCHING_EXCEL_FILE_PATH = MATCHING_EXCEL_FILE_PATH
    except Exception:
        pass


def _sync_from_impl() -> None:
    """Pull Excel path back after a run."""
    global MATCHING_EXCEL_FILE_PATH

    cfg_value = getattr(_cfg, "MATCHING_EXCEL_FILE_PATH", None)
    env_value = _os.getenv("EXCEL_FILE_PATH")
    MATCHING_EXCEL_FILE_PATH = cfg_value or env_value or MATCHING_EXCEL_FILE_PATH
    if MATCHING_EXCEL_FILE_PATH:
        _os.environ["EXCEL_FILE_PATH"] = MATCHING_EXCEL_FILE_PATH


def process_invoices(from_date=None, to_date=None, cancel_event=None):
    """Entry point preserved for cron and POST /api/process."""
    _sync_to_impl()
    result = _ingest_process_invoices(
        from_date=from_date, to_date=to_date, cancel_event=cancel_event
    )
    _sync_from_impl()
    return result


def match_master_sheet_with_excel():
    """Entry point preserved for POST /api/match-master."""
    _sync_to_impl()
    result = _match_master()
    _sync_from_impl()
    return result


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--match":
        match_master_sheet_with_excel()
    else:
        process_invoices()
