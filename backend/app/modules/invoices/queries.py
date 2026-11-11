#!/usr/bin/env python3
"""
Export MySQL invoice_data table to Excel file filtered by dashboard date range.

Through 31 Aug 2026 the filter uses created_at (processing date).
From 1 Sep 2026 it uses email_date. A range that crosses the cutover is split.
"""

import pandas as pd
from sqlalchemy import text
from datetime import date, datetime, timedelta
import os
import numpy as np
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl import load_workbook
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from backend.app.shared.dates import EMAIL_DATE_CUTOVER

_STATS_MATCHED_EXPR = "SUM(LOWER(TRIM(IFNULL(matched_status,''))) = 'matched')"
_STATS_UNMATCHED_EXPR = (
    "SUM(LOWER(TRIM(IFNULL(matched_status,''))) = 'unmatched' "
    "AND LOWER(TRIM(IFNULL(flag,''))) <> '0')"
)
_STATS_FOOD_EXPR = "SUM(LOWER(TRIM(IFNULL(matched_status,''))) = 'food_invoice')"
_STATS_HUMMINGBIRD_EXPR = (
    "SUM(LOWER(TRIM(IFNULL(matched_status,''))) = 'hummingbird_mail')"
)
_STATS_B2B_EXPR = "SUM(LOWER(TRIM(IFNULL(matched_status,''))) = 'b2b_invoice')"


# ---------------- DB CONFIG ---------------- 
db_config = {
    "host": "localhost",
    "user": "root",
    "password": "Irahirs11@ittak!",
    "database": "bsr"
}

# UI source keys → mailbox env file + default table
_INVOICE_SOURCES = {
    "bills": {
        "label": "Bills",
        "env_file": ".env.mailbox_a",
        "env_override": "INVOICE_TABLE_BILLS",
        "default_table": "invoice_data",
    },
    "xtract": {
        "label": "XtractBills",
        "env_file": ".env.mailbox_b",
        "env_override": "INVOICE_TABLE_XTRACT",
        "default_table": "invoice_data_b",
    },
    "btcbills": {
        "label": "BTCBills",
        "env_file": ".env.mailbox_c",
        "env_override": "INVOICE_TABLE_BTCBILLS",
        "default_table": "invoice_btcbills",
    },
}


def get_db_engine():
    """Get MySQL database engine connection"""
    from backend.app.database import get_engine

    return get_engine()


def _sanitize_table_name(name: Optional[str], fallback: str) -> str:
    t = (name or "").strip()
    if not t or not all(c.isalnum() or c == "_" for c in t):
        return fallback
    return t


def _get_invoice_table_name():
    """Return invoice table name from env INVOICE_TABLE_NAME, default invoice_data. Safe for SQL."""
    return _sanitize_table_name(os.getenv("INVOICE_TABLE_NAME"), "invoice_data")


def _read_env_file_key(path: Path, key: str) -> Optional[str]:
    """Read KEY=value from a dotenv-style file (no export / quotes stripped lightly)."""
    try:
        if not path.is_file():
            return None
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() != key:
                continue
            val = v.strip().strip("'").strip('"')
            return val or None
    except OSError:
        return None
    return None


def list_invoice_sources() -> List[Dict[str, str]]:
    """Return available UI sources with resolved table names."""
    out: List[Dict[str, str]] = []
    for key, meta in _INVOICE_SOURCES.items():
        table = resolve_source_table(key)
        out.append(
            {
                "id": key,
                "label": meta["label"],
                "table": table or meta["default_table"],
            }
        )
    return out


def resolve_source_table(source: Optional[str]) -> Optional[str]:
    """Map source id (bills|xtract|btcbills) to a safe MySQL table name, or None if invalid."""
    key = (source or "bills").strip().lower()
    meta = _INVOICE_SOURCES.get(key)
    if not meta:
        return None

    # 1) Explicit process env override
    override = os.getenv(meta["env_override"], "").strip()
    if override:
        return _sanitize_table_name(override, meta["default_table"])

    # 2) Mailbox env file next to this package / repo root
    root = Path(__file__).resolve().parent
    from_file = _read_env_file_key(root / meta["env_file"], "INVOICE_TABLE_NAME")
    if from_file:
        return _sanitize_table_name(from_file, meta["default_table"])

    # 3) Default
    return meta["default_table"]


def normalize_invoice_source(source: Optional[str]) -> Tuple[str, str]:
    """Return (source_id, table_name). Invalid source falls back to bills."""
    key = (source or "bills").strip().lower()
    table = resolve_source_table(key)
    if table is None:
        key = "bills"
        table = resolve_source_table(key) or "invoice_data"
    return key, table


# Hidden from Invoice Database UI + date-range Excel export only (not export_mysql_to_excel.py).
INVOICE_DB_HIDDEN_COLUMNS = frozenset({
    "s_no",
    "message_id",
    "multi_page_pdf",
    "pushed_at",
})


def _drop_invoice_db_hidden_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Drop columns excluded from Invoice Database preview/export."""
    to_drop = [c for c in INVOICE_DB_HIDDEN_COLUMNS if c in df.columns]
    if to_drop:
        return df.drop(columns=to_drop)
    return df


def normalize_match_filter(match: str) -> str:
    """Return one of: all | matched | unmatched | others."""
    m = (match or "all").strip().lower()
    if m in ("matched", "unmatched", "others", "all"):
        return m
    return "all"


def _sql_matched_clause(match_filter: str) -> str:
    """SQL fragment for matched_status (match_filter must be normalized)."""
    m = normalize_match_filter(match_filter)
    if m == "matched":
        return " AND LOWER(TRIM(IFNULL(matched_status, ''))) = 'matched' "
    if m == "unmatched":
        return " AND LOWER(TRIM(IFNULL(matched_status, ''))) = 'unmatched' "
    if m == "others":
        return (
            " AND LOWER(TRIM(IFNULL(matched_status, ''))) IN "
            "('food_invoice', 'hummingbird_mail', 'b2b_invoice') "
        )
    return " "


def _as_date(value: Union[date, datetime]) -> date:
    return value.date() if isinstance(value, datetime) else value


def _dashboard_date_filter(
    from_dt: Union[date, datetime],
    to_dt: Union[date, datetime],
) -> Tuple[str, Dict[str, Any]]:
    """SQL AND-fragment + bind params for dashboard date windows.

    Through 31 Aug 2026: created_at, inclusive of the last day's 23:59:59.
    From 1 Sep 2026: email_date (DATE column, inclusive).
    A selected range that crosses 2026-09-01 is split and OR'd.
    """
    from_d = _as_date(from_dt)
    to_d = _as_date(to_dt)
    cutoff = EMAIL_DATE_CUTOVER
    clauses: List[str] = []
    params: Dict[str, Any] = {}

    if from_d < cutoff:
        legacy_end = min(to_d, cutoff - timedelta(days=1))
        params["legacy_from"] = datetime(from_d.year, from_d.month, from_d.day)
        params["legacy_to"] = datetime(
            legacy_end.year, legacy_end.month, legacy_end.day, 23, 59, 59
        )
        clauses.append("(created_at >= :legacy_from AND created_at <= :legacy_to)")

    if to_d >= cutoff:
        email_start = max(from_d, cutoff)
        params["email_from"] = email_start
        params["email_to"] = to_d
        clauses.append("(email_date >= :email_from AND email_date <= :email_to)")

    if not clauses:
        return " AND 1=0 ", {}
    return " AND (" + " OR ".join(clauses) + ") ", params


def _trend_day_select_sql(
    table_name: str,
    from_dt: Union[date, datetime],
    to_dt: Union[date, datetime],
) -> Tuple[str, Dict[str, Any]]:
    """Daily matched/unmatched aggregates using the dashboard date cutover."""
    from_d = _as_date(from_dt)
    to_d = _as_date(to_dt)
    cutoff = EMAIL_DATE_CUTOVER
    parts: List[str] = []
    params: Dict[str, Any] = {}

    if from_d < cutoff:
        legacy_end = min(to_d, cutoff - timedelta(days=1))
        params["legacy_from"] = datetime(from_d.year, from_d.month, from_d.day)
        params["legacy_to"] = datetime(
            legacy_end.year, legacy_end.month, legacy_end.day, 23, 59, 59
        )
        parts.append(
            f"""
        SELECT DATE(created_at) AS day,
          {_STATS_MATCHED_EXPR} AS matched,
          {_STATS_UNMATCHED_EXPR} AS unmatched
        FROM {table_name}
        WHERE created_at >= :legacy_from AND created_at <= :legacy_to
        GROUP BY DATE(created_at)
            """.strip()
        )

    if to_d >= cutoff:
        email_start = max(from_d, cutoff)
        params["email_from"] = email_start
        params["email_to"] = to_d
        parts.append(
            f"""
        SELECT email_date AS day,
          {_STATS_MATCHED_EXPR} AS matched,
          {_STATS_UNMATCHED_EXPR} AS unmatched
        FROM {table_name}
        WHERE email_date >= :email_from AND email_date <= :email_to
        GROUP BY email_date
            """.strip()
        )

    if not parts:
        return "SELECT NULL AS day, 0 AS matched, 0 AS unmatched WHERE 1=0", {}
    if len(parts) == 1:
        return parts[0] + " ORDER BY day", params
    return (
        "SELECT day, SUM(matched) AS matched, SUM(unmatched) AS unmatched "
        "FROM (\n" + "\nUNION ALL\n".join(parts) + "\n) t "
        "GROUP BY day ORDER BY day"
    ), params


def _serialize_cell(v: Any) -> Union[str, int, float, bool, None]:
    if v is None:
        return None
    if isinstance(v, float) and np.isnan(v):
        return None
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        if np.isnan(v):
            return None
        return float(v)
    if isinstance(v, np.bool_):
        return bool(v)
    if hasattr(v, "isoformat"):
        try:
            return v.isoformat()
        except Exception:
            return str(v)
    if isinstance(v, (pd.Timestamp,)):
        return v.isoformat()
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace")
    return v


def fetch_invoice_rows_for_api(
    from_date: str,
    to_date: str,
    match_filter: str = "all",
    limit: int = 500,
    offset: int = 0,
    table_name: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Load invoice rows for JSON API: dashboard date range, optional matched filter, LIMIT/OFFSET.

    Through 31 Aug 2026 the range is on created_at; from 1 Sep 2026 it is on email_date.
    Returns dict with columns, rows, total (full count in range), or None on validation / DB error.
    """
    match_filter = normalize_match_filter(match_filter)
    limit = max(1, min(int(limit), 2000))
    offset = max(0, int(offset))

    try:
        from_dt = datetime.strptime(from_date, "%Y-%m-%d")
        to_dt = datetime.strptime(to_date, "%Y-%m-%d")
        if from_dt > to_dt:
            return None
    except ValueError:
        return None

    table_name = _sanitize_table_name(table_name, _get_invoice_table_name())
    match_sql = _sql_matched_clause(match_filter)
    date_sql, date_params = _dashboard_date_filter(from_dt, to_dt)
    query = text(
        f"SELECT * FROM {table_name} "
        f"WHERE 1=1 {date_sql}{match_sql}"
        "ORDER BY id DESC LIMIT :lim OFFSET :off"
    )
    count_query = text(
        f"SELECT COUNT(*) FROM {table_name} "
        f"WHERE 1=1 {date_sql}{match_sql}"
    )

    try:
        engine = get_db_engine()
        with engine.connect() as conn:
            total = int(
                conn.execute(count_query, date_params).scalar()
                or 0
            )
            df = pd.read_sql(
                query,
                conn,
                params={**date_params, "lim": limit, "off": offset},
            )
    except Exception as e:
        print(f"❌ fetch_invoice_rows_for_api: {e}")
        return None

    df = _drop_invoice_db_hidden_columns(df)

    columns: List[str] = [str(c) for c in df.columns.tolist()]
    rows: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        rec: Dict[str, Any] = {}
        for col in columns:
            rec[col] = _serialize_cell(row[col])
        rows.append(rec)

    return {
        "columns": columns,
        "rows": rows,
        "count": len(rows),
        "total": total,
        "match_filter": match_filter,
        "from_date": from_date,
        "to_date": to_date,
        "limit": limit,
        "offset": offset,
        "table": table_name,
    }


def fetch_invoice_stats(
    from_date: str,
    to_date: str,
    table_name: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Aggregate dashboard counts over the dashboard date range.

    Through 31 Aug 2026 the range is on created_at; from 1 Sep 2026 it is on email_date.

    - matched:        matched_status = 'matched'
    - unmatched:      matched_status = 'unmatched' AND flag <> '0'
                      (keeps '1', 'not found', and unique-bill NULLs; drops only '0')
    - others:         food_invoice + hummingbird_mail + b2b_invoice (total and separate)
    - match_rate:     matched / (matched + unmatched) * 100
    Returns dict, or None on validation / DB error.
    """
    table_name = _sanitize_table_name(table_name, _get_invoice_table_name())
    try:
        from_dt = datetime.strptime(from_date, "%Y-%m-%d")
        to_dt = datetime.strptime(to_date, "%Y-%m-%d")
        if from_dt > to_dt:
            return None
    except (ValueError, TypeError):
        return None

    date_sql, date_params = _dashboard_date_filter(from_dt, to_dt)
    query = text(
        f"""
        SELECT
          {_STATS_MATCHED_EXPR} AS matched,
          {_STATS_UNMATCHED_EXPR} AS unmatched,
          {_STATS_FOOD_EXPR} AS food,
          {_STATS_HUMMINGBIRD_EXPR} AS hummingbird,
          {_STATS_B2B_EXPR} AS b2b
        FROM {table_name}
        WHERE 1=1 {date_sql}
        """
    )

    try:
        engine = get_db_engine()
        with engine.connect() as conn:
            row = conn.execute(query, date_params).fetchone()
    except Exception as e:
        print(f"❌ fetch_invoice_stats: {e}")
        return None

    matched = int(row[0] or 0)
    unmatched = int(row[1] or 0)  # already excludes flag = '0'
    food = int(row[2] or 0)
    hummingbird = int(row[3] or 0)
    b2b = int(row[4] or 0)
    denom = matched + unmatched
    match_rate = round(matched / denom * 100, 1) if denom else 0.0

    return {
        "matched": matched,
        "unmatched": unmatched,
        "others_total": food + hummingbird + b2b,
        "food_invoices": food,
        "hummingbird_mails": hummingbird,
        "b2b_invoices": b2b,
        "match_rate": match_rate,
        "from_date": from_date,
        "to_date": to_date,
        "table": table_name,
    }


def fetch_invoice_match_rate_trend(
    from_date: str,
    to_date: str,
    table_name: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Daily match rate over the dashboard date range (one point per calendar day).

    Through 31 Aug 2026 days are DATE(created_at); from 1 Sep 2026 they are email_date.
    Uses the same matched / unmatched rules as fetch_invoice_stats.
    Returns all days in range, including days with zero invoices (0% match rate).
    """
    table_name = _sanitize_table_name(table_name, _get_invoice_table_name())
    try:
        from_dt = datetime.strptime(from_date, "%Y-%m-%d")
        to_dt = datetime.strptime(to_date, "%Y-%m-%d")
        if from_dt > to_dt:
            return None
    except (ValueError, TypeError):
        return None

    inner_sql, date_params = _trend_day_select_sql(table_name, from_dt, to_dt)
    query = text(inner_sql)

    try:
        engine = get_db_engine()
        with engine.connect() as conn:
            rows = conn.execute(query, date_params).fetchall()
    except Exception as e:
        print(f"❌ fetch_invoice_match_rate_trend: {e}")
        return None

    by_day: Dict[str, Dict[str, int]] = {}
    for row in rows:
        day_val = row[0]
        if hasattr(day_val, "strftime"):
            day_key = day_val.strftime("%Y-%m-%d")
        else:
            day_key = str(day_val)[:10]
        by_day[day_key] = {
            "matched": int(row[1] or 0),
            "unmatched": int(row[2] or 0),
        }

    points: List[Dict[str, Any]] = []
    cur = from_dt
    while cur <= to_dt:
        day_key = cur.strftime("%Y-%m-%d")
        counts = by_day.get(day_key, {"matched": 0, "unmatched": 0})
        matched = counts["matched"]
        unmatched = counts["unmatched"]
        denom = matched + unmatched
        rate = round(matched / denom * 100, 1) if denom else 0.0
        points.append(
            {
                "date": day_key,
                "matched": matched,
                "unmatched": unmatched,
                "match_rate": rate,
            }
        )
        cur += timedelta(days=1)

    return {
        "from_date": from_date,
        "to_date": to_date,
        "points": points,
        "table": table_name,
    }


MAILBOX_DAILY_COUNTS_TABLE = "mailbox_daily_counts"


def fetch_mailbox_daily_counts(
    from_date: str,
    to_date: str,
    source: Optional[str] = "bills",
) -> Optional[Dict[str, Any]]:
    """Daily mailbox email/PDF counts for a dashboard source (bills|xtract|btcbills).

    Missing days in the range are filled with zeros. Returns None on invalid dates.
    """
    source_id = (source or "bills").strip().lower()
    if source_id not in _INVOICE_SOURCES:
        source_id = "bills"
    try:
        from_dt = datetime.strptime(from_date, "%Y-%m-%d")
        to_dt = datetime.strptime(to_date, "%Y-%m-%d")
        if from_dt > to_dt:
            return None
    except (ValueError, TypeError):
        return None

    try:
        from backend.app.modules.invoices.mysql import ensure_mailbox_daily_counts_table_exists

        ensure_mailbox_daily_counts_table_exists()
    except Exception:
        pass

    by_day: Dict[str, Dict[str, int]] = {}
    query = text(
        f"""
        SELECT count_date, emails, pdfs, emails_found, pdfs_found
        FROM {MAILBOX_DAILY_COUNTS_TABLE}
        WHERE source = :source
          AND count_date BETWEEN :from_date AND :to_date
        ORDER BY count_date
        """
    )
    try:
        engine = get_db_engine()
        with engine.connect() as conn:
            rows = conn.execute(
                query,
                {"source": source_id, "from_date": from_date, "to_date": to_date},
            ).fetchall()
        for row in rows:
            day_val = row[0]
            if hasattr(day_val, "strftime"):
                day_key = day_val.strftime("%Y-%m-%d")
            else:
                day_key = str(day_val)[:10]
            by_day[day_key] = {
                "emails": int(row[1] or 0),
                "pdfs": int(row[2] or 0),
                "emails_found": int(row[3] or 0) if len(row) > 3 else 0,
                "pdfs_found": int(row[4] or 0) if len(row) > 4 else 0,
            }
    except Exception as e:
        print(f"⚠️  fetch_mailbox_daily_counts: {e}")

    days: List[Dict[str, Any]] = []
    emails_total = 0
    pdfs_total = 0
    emails_found_total = 0
    pdfs_found_total = 0
    empty = {"emails": 0, "pdfs": 0, "emails_found": 0, "pdfs_found": 0}
    cur = from_dt
    while cur <= to_dt:
        day_key = cur.strftime("%Y-%m-%d")
        counts = by_day.get(day_key, empty)
        emails_total += counts["emails"]
        pdfs_total += counts["pdfs"]
        emails_found_total += counts["emails_found"]
        pdfs_found_total += counts["pdfs_found"]
        days.append(
            {
                "date": day_key,
                "emails": counts["emails"],
                "pdfs": counts["pdfs"],
                "emails_found": counts["emails_found"],
                "pdfs_found": counts["pdfs_found"],
            }
        )
        cur += timedelta(days=1)

    return {
        "source": source_id,
        "from_date": from_date,
        "to_date": to_date,
        "emails": emails_total,
        "pdfs": pdfs_total,
        "emails_found": emails_found_total,
        "pdfs_found": pdfs_found_total,
        "days": days,
    }


def export_invoice_data_by_date_range(
    from_date: str,
    to_date: str,
    output_file: Optional[str] = None,
    include_all_columns: bool = True,
    match_filter: str = "all",
    table_name: Optional[str] = None,
):
    """
    Export invoice_data table from MySQL to Excel file filtered by dashboard date range.

    Through 31 Aug 2026 the range is on created_at; from 1 Sep 2026 it is on email_date.
    
    Args:
        from_date: Start date in YYYY-MM-DD format (inclusive)
        to_date: End date in YYYY-MM-DD format (inclusive)
        output_file: Optional path for output Excel file. If None, generates filename with date range
        include_all_columns: If True, includes all columns. If False, includes only main columns.
        match_filter: all | matched | unmatched (filters on matched_status column)
        table_name: Optional explicit table (from Bills/Xtract source). Defaults to INVOICE_TABLE_NAME.
    
    Returns:
        str: Path to the created Excel file, or None if error/no data
    """
    match_filter = normalize_match_filter(match_filter)
    table_name = _sanitize_table_name(table_name, _get_invoice_table_name())
    print(f"📊 Exporting MySQL {table_name} to Excel by date range...")
    print(f"   📅 Date Range: {from_date} to {to_date} (match={match_filter})")
    
    # Validate date format
    try:
        from_dt = datetime.strptime(from_date, '%Y-%m-%d')
        to_dt = datetime.strptime(to_date, '%Y-%m-%d')
        
        if from_dt > to_dt:
            print("❌ Error: from_date must be before or equal to to_date")
            return None
        
    except ValueError as e:
        print(f"❌ Error: Invalid date format. Expected YYYY-MM-DD format. Error: {e}")
        return None
    
    # Generate output filename if not provided
    filename = None
    if output_file is None:
        from_date_safe = from_date.replace('-', '')
        to_date_safe = to_date.replace('-', '')
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"{table_name}_export_{from_date_safe}_to_{to_date_safe}_{timestamp}.xlsx"
    else:
        # If output_file was provided with a path, extract just the filename
        filename = os.path.basename(output_file)
        # Ensure .xlsx extension
        if not filename.endswith('.xlsx'):
            filename += '.xlsx'
    
    # Always save to ./invoices folder (similar to test1.py saving to ./api)
    output_dir = "invoices"
    os.makedirs(output_dir, exist_ok=True)
    
    # Build full output path
    output_file = os.path.join(output_dir, filename)
    
    try:
        # Connect to database
        engine = get_db_engine()
        print(f"✅ Connected to MySQL database: {db_config['database']}")
        
        # Query data from invoice table using the dashboard date cutover
        # (created_at through Aug 2026, email_date from Sep 2026).
        match_sql = _sql_matched_clause(match_filter)
        date_sql, date_params = _dashboard_date_filter(from_dt, to_dt)
        query = text(
            f"SELECT * FROM {table_name} "
            f"WHERE 1=1 {date_sql}{match_sql}"
            "ORDER BY id ASC"
        )
        print(
            f"📥 Fetching data from {table_name} table "
            f"(dashboard date filter between {from_date} and {to_date})..."
        )
        
        df = pd.read_sql(
            query,
            engine,
            params=date_params,
        )
        
        if df.empty:
            print(f"⚠️  No data found in {table_name} table for date range {from_date} to {to_date}")
            return None
        
        print(f"✅ Fetched {len(df)} rows from database for the specified date range")
        
        # Remove columns hidden from Invoice Database export (kept in MySQL)
        columns_to_remove = list(INVOICE_DB_HIDDEN_COLUMNS)
        removed_cols = []
        for col in columns_to_remove:
            if col in df.columns:
                df = df.drop(columns=[col])
                removed_cols.append(col)
        
        if removed_cols:
            print(f"   ⚠️  Removed columns from export (kept in MySQL): {', '.join(removed_cols)}")
        
        # Keep created_at as stored in MySQL (same value as frontend "created at")
        if 'created_at' in df.columns:
            df['created_at'] = pd.to_datetime(df['created_at'], errors='coerce')
            if df['created_at'].dt.tz is not None:
                df['created_at'] = df['created_at'].dt.tz_localize(None)
        
        # Format updated_at for display (convert to string format)
        if 'updated_at' in df.columns:
            df['updated_at'] = pd.to_datetime(df['updated_at'], errors='coerce')
            # Format as YYYY-MM-DD HH:MM:SS
            df['updated_at'] = df['updated_at'].dt.strftime('%Y-%m-%d %H:%M:%S')
        
        # Replace NULL/None values with "Not Found"
        print(f"🔄 Converting NULL values to 'Not Found'...")
        # Fill NaN values first
        df = df.fillna('Not Found')
        # Also replace None, numpy.nan, and string representations
        df = df.replace([np.nan, None, 'None', 'null', 'NULL', 'nan', 'NaN'], 'Not Found')
        
        # Map MySQL column names to Excel-friendly names
        column_mapping = {
            'id': 'ID',
            'updated_at': 'Processing Date',
            'email_from': 'Email From',
            'email_date': 'Email Date',
            'assigned_to': 'Assigned To',
            'hotel_name': 'Hotel Name',
            'guest_name': 'Guest Name',
            'booking_code': 'Booking Code',
            'bill_number': 'Bill Number',
            'bill_date': 'Bill Date',
            'check_in_date': 'Check-in Date',
            'check_out_date': 'Check-out Date',
            'amount_paid': 'Amount Paid',
            'hbd_gst_number': 'HBD GST Number',
            'client_gst_number': 'Client GST Number',
            'validation_flag': 'Validation Flag',
            'cgst_number': 'CGST Number',
            'sgst_number': 'SGST Number',
            'total_cgst': 'Total CGST',
            'total_sgst': 'Total SGST',
            'pdf_filename': 'PDF Filename',
            'blob_url': 'Blob URL',
            'matched_status': 'Matched Status',
            'amount_match': 'Amount Match',
            'amount_difference': 'Amount Difference',
            'created_at': 'Created At'
        }
        
        # Rename columns
        df.rename(columns=column_mapping, inplace=True)
        
        # Select columns based on include_all_columns flag
        if not include_all_columns:
            # Main columns only
            main_columns = [
                'ID', 'Processing Date', 'Created At', 'Email Date', 'Email From', 'Assigned To',
                'Hotel Name', 'Guest Name', 'Booking Code', 'Bill Number', 'Bill Date',
                'Check-in Date', 'Check-out Date', 'Amount Paid',
                'HBD GST Number', 'Client GST Number', 'Validation Flag', 'CGST Number', 'SGST Number',
                'Total CGST', 'Total SGST', 'PDF Filename', 'Blob URL',
                'Matched Status', 'Amount Match', 'Amount Difference'
            ]
            # Only include columns that exist in dataframe
            available_columns = [col for col in main_columns if col in df.columns]
            df = df[available_columns]
        
        # Reorder columns to match Excel format
        desired_column_order = [
            'ID', 'Processing Date', 'Created At', 'Email Date', 'Email From', 'Assigned To',
            'Hotel Name', 'Guest Name', 'Booking Code', 'Bill Number', 'Bill Date',
            'Check-in Date', 'Check-out Date', 'Amount Paid',
            'HBD GST Number', 'Client GST Number', 'Validation Flag', 'CGST Number', 'SGST Number',
            'Total CGST', 'Total SGST', 'PDF Filename', 'Blob URL',
            'Matched Status', 'Amount Match', 'Amount Difference'
        ]
        
        # Add any additional columns at the end
        if include_all_columns:
            additional_cols = [col for col in df.columns if col not in desired_column_order]
            desired_column_order = desired_column_order + additional_cols
        
        # Reorder columns (only include columns that exist)
        existing_columns = [col for col in desired_column_order if col in df.columns]
        df = df[existing_columns]
        
        # Save to Excel
        print(f"💾 Saving to Excel file: {output_file}")
        df.to_excel(output_file, index=False, engine='openpyxl')
        
        # Apply formatting
        try:
            wb = load_workbook(output_file)
            ws = wb.active
            
            # Header row formatting
            header_fill = PatternFill(start_color="366092", end_color="366092", fill_type="solid")
            header_font = Font(bold=True, color="FFFFFF", size=11)
            header_alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            border = Border(
                left=Side(style='thin'),
                right=Side(style='thin'),
                top=Side(style='thin'),
                bottom=Side(style='thin')
            )
            
            # Apply header formatting
            for cell in ws[1]:
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = header_alignment
                cell.border = border
            
            # Set column widths
            # Keyed by header name (not column letter) so inserting/removing a column
            # can't leave widths attached to the wrong column.
            from openpyxl.utils import get_column_letter

            column_widths_by_header = {
                'ID': 8,
                'Processing Date': 20,
                'Created At': 20,
                'Email Date': 14,
                'Email From': 25,
                'Assigned To': 15,
                'Hotel Name': 35,
                'Guest Name': 25,
                'Booking Code': 18,
                'Bill Number': 20,
                'Bill Date': 15,
                'Check-in Date': 15,
                'Check-out Date': 15,
                'Amount Paid': 15,
                'HBD GST Number': 20,
                'Client GST Number': 20,
                'Validation Flag': 12,
                'CGST Number': 18,
                'SGST Number': 18,
                'Total CGST': 15,
                'Total SGST': 15,
                'PDF Filename': 30,
                'Blob URL': 50,
                'Flag': 8,
                'Matched Status': 15,
            }

            for col_idx, header_cell in enumerate(ws[1], start=1):
                width = column_widths_by_header.get(header_cell.value)
                if width:
                    ws.column_dimensions[get_column_letter(col_idx)].width = width
            
            # Freeze header row
            ws.freeze_panes = 'A2'
            
            # Save formatted workbook
            wb.save(output_file)
            wb.close()
            
        except Exception as format_error:
            print(f"⚠️  Warning: Could not apply Excel formatting: {format_error}")
            print(f"   File saved without formatting")
        
        print(f"✅ Successfully exported {len(df)} rows to: {output_file}")
        print(f"   📅 Date Range: {from_date} to {to_date}")
        return output_file
        
    except Exception as e:
        print(f"❌ Error exporting data: {e}")
        import traceback
        traceback.print_exc()
        return None


def export_recent_invoice_data(days: int = 30, output_file: Optional[str] = None):
    """
    Export invoice data for the last N days
    
    Args:
        days: Number of days to look back (default: 30)
        output_file: Optional path for output Excel file
    
    Returns:
        str: Path to the created Excel file, or None if error/no data
    """
    to_date = datetime.now().date()
    from_date = (datetime.now() - pd.Timedelta(days=days)).date()
    
    return export_invoice_data_by_date_range(
        from_date.strftime('%Y-%m-%d'),
        to_date.strftime('%Y-%m-%d'),
        output_file
    )


def get_date_input(prompt: str, default: Optional[str] = None) -> str:
    """
    Get date input from user with validation
    
    Args:
        prompt: Prompt message to display
        default: Optional default date value (YYYY-MM-DD format)
    
    Returns:
        str: Valid date string in YYYY-MM-DD format
    """
    while True:
        if default:
            user_input = input(f"{prompt} (default: {default}): ").strip()
            if not user_input:
                user_input = default
        else:
            user_input = input(f"{prompt}: ").strip()
        
        # Validate date format
        try:
            datetime.strptime(user_input, '%Y-%m-%d')
            return user_input
        except ValueError:
            print("❌ Invalid date format. Please use YYYY-MM-DD format (e.g., 2025-01-15)")
            continue


if __name__ == "__main__":
    import sys
    
    print("=" * 70)
    print("📊 EXPORT INVOICE DATA BY DATE RANGE")
    print("=" * 70)
    
    # Check if dates are provided as command-line arguments
    if len(sys.argv) >= 3:
        # Use command-line arguments
        from_date = sys.argv[1]
        to_date = sys.argv[2]
        output_file = sys.argv[3] if len(sys.argv) > 3 else None
        
        print(f"From Date: {from_date}")
        print(f"To Date: {to_date}")
        if output_file:
            print(f"Output File: {output_file}")
        print("=" * 70)
        
    else:
        # Interactive mode - prompt user for dates
        print("📅 Please enter the date range for export")
        print("   Date format: YYYY-MM-DD (e.g., 2025-01-15)")
        print()
        
        # Get today's date as default for "to_date"
        today = datetime.now().strftime('%Y-%m-%d')
        
        # Get date range from user
        from_date = get_date_input("Enter Start Date (From)")
        to_date = get_date_input("Enter End Date (To)", default=today)
        
        # Ask for custom output filename (optional)
        print()
        custom_file = input("Enter custom output filename (press Enter for auto-generated): ").strip()
        output_file = custom_file if custom_file else None
        
        print("=" * 70)
        print(f"From Date: {from_date}")
        print(f"To Date: {to_date}")
        if output_file:
            print(f"Output File: {output_file}")
        print("=" * 70)
        print()
    
    # Export the data
    result = export_invoice_data_by_date_range(from_date, to_date, output_file)
    
    if result:
        print(f"\n✅ Export completed successfully!")
        print(f"📁 File saved at: {os.path.abspath(result)}")
    else:
        print(f"\n❌ Export failed!")
        sys.exit(1)

