"""HTTP for dashboard counts and match-rate trend."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException

from backend.app.modules.invoices.queries import fetch_invoice_match_rate_trend, fetch_invoice_stats

from backend.app.modules.invoices.service import resolve_mysql_source

router = APIRouter()


@router.get("/api/mysql/stats")
async def mysql_invoice_stats(from_date: str, to_date: str, source: str = "bills"):
    """Dashboard counts (matched/unmatched/others/match rate) over the dashboard date range.

    Through 31 Aug 2026 the range is created_at; from 1 Sep 2026 it is email_date.
    """
    source_id, table_name = resolve_mysql_source(source)
    try:
        datetime.strptime(from_date, "%Y-%m-%d")
        datetime.strptime(to_date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid date format. Expected YYYY-MM-DD format.",
        )
    data = fetch_invoice_stats(from_date, to_date, table_name=table_name)
    if data is None:
        raise HTTPException(
            status_code=400,
            detail="Invalid date range (from > to) or database error.",
        )
    data["source"] = source_id
    return data


@router.get("/api/mysql/match-rate-trend")
async def mysql_match_rate_trend(from_date: str, to_date: str, source: str = "bills"):
    """Daily match rate trend over the dashboard date range (for line chart).

    Through 31 Aug 2026 days are DATE(created_at); from 1 Sep 2026 they are email_date.
    """
    source_id, table_name = resolve_mysql_source(source)
    try:
        datetime.strptime(from_date, "%Y-%m-%d")
        datetime.strptime(to_date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid date format. Expected YYYY-MM-DD format.",
        )
    data = fetch_invoice_match_rate_trend(from_date, to_date, table_name=table_name)
    if data is None:
        raise HTTPException(
            status_code=400,
            detail="Invalid date range (from > to) or database error.",
        )
    data["source"] = source_id
    return data
