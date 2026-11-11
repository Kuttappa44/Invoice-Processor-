"""Background ingest job. The API starts a thread that calls this; IMAP stays off the request."""

from __future__ import annotations

import threading
from typing import Optional


def run_process_invoices(
    from_date=None,
    to_date=None,
    cancel_event: Optional[threading.Event] = None,
):
    """Thread target for POST /api/process. Goes through main so Excel-path sync still runs."""
    from main import process_invoices

    return process_invoices(
        from_date=from_date, to_date=to_date, cancel_event=cancel_event
    )
