"""HTTP for stored mailbox daily counts."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException

from backend.app.modules.invoices.queries import fetch_mailbox_daily_counts

from backend.app.modules.invoices.service import resolve_mysql_source

router = APIRouter()


@router.get("/api/mysql/mailbox-counts")
async def mysql_mailbox_counts(from_date: str, to_date: str, source: str = "bills"):
    """Daily mailbox email and PDF counts stored before scheduled processing."""
    source_id, _table_name = resolve_mysql_source(source)
    try:
        datetime.strptime(from_date, "%Y-%m-%d")
        datetime.strptime(to_date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid date format. Expected YYYY-MM-DD format.",
        )
    data = fetch_mailbox_daily_counts(from_date, to_date, source=source_id)
    if data is None:
        raise HTTPException(
            status_code=400,
            detail="Invalid date range (from > to) or database error.",
        )
    return data
