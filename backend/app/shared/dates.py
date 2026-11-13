"""Date formatting, ±1-day match, and dashboard cutover. Used by ingest, matching, and stats."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Optional

import pandas as pd

from backend.app.shared.text import normalize_field_text

# Dashboard date-filter cutover: months through August 2026 stay on created_at;
# September 2026 onward uses email_date.
EMAIL_DATE_CUTOVER = date(2026, 9, 1)

_DATE_FORMATS = (
    "%d/%m/%Y",
    "%d/%m/%y",
    "%Y-%m-%d",
    "%d-%m-%Y",
    "%d-%m-%y",
    "%d %m %Y",
    "%d %m %y",
    "%d-%b-%Y",
    "%d-%B-%Y",
    "%d/%b/%Y",
    "%d/%B/%Y",
    "%d %b %Y",
    "%d %B %Y",
    "%d-%b-%y",
    "%d-%B-%y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%y %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%d-%m-%Y %H:%M:%S",
)

_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


def _year_number(year: str) -> Optional[int]:
    digits = re.sub(r"\D", "", year or "")
    if len(digits) == 4:
        return int(digits)
    if len(digits) == 2:
        yy = int(digits)
        return 2000 + yy if yy <= 68 else 1900 + yy
    return None


def _date_from_parts(day: str, month: str, year: str) -> Optional[date]:
    year_n = _year_number(year)
    if year_n is None:
        return None
    month_text = month.strip().lower()
    try:
        month_n = int(month_text) if month_text.isdigit() else _MONTHS.get(month_text)
        if month_n is None:
            return None
        return date(year_n, month_n, int(day))
    except (TypeError, ValueError):
        return None


def _clean_date_text(value: Any) -> str:
    text = normalize_field_text(str(value).strip())
    return re.sub(r"\s+\d{1,2}:\d{2}(?::\d{2})?\s*$", "", text).strip()


def parse_date(value: Any) -> Optional[date]:
    """Parse a date in any supported format. 4-digit and 2-digit years are the same day."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = _clean_date_text(value)
    if not text or text.lower() in ("nan", "none", "not found", "nat"):
        return None

    candidates = [text]
    if " " in text:
        candidates.append(text.split()[0])
    for candidate in candidates:
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(candidate, fmt).date()
            except ValueError:
                continue

    if "/" in text:
        parts = [part.strip() for part in text.split("/")]
        if len(parts) == 3:
            parsed = _date_from_parts(parts[0], parts[1], parts[2])
            if parsed:
                return parsed

    if "-" in text and len(text) >= 8:
        parts = [part.strip() for part in text.split("-")]
        if len(parts) == 3:
            if len(re.sub(r"\D", "", parts[0])) == 4:
                parsed = _date_from_parts(parts[2], parts[1], parts[0])
            else:
                parsed = _date_from_parts(parts[0], parts[1], parts[2])
            if parsed:
                return parsed
    return None


def format_date_to_dd_mm_yy(value: Optional[str]) -> str:
    """Format date to dd/mm/yy format (2-digit year) and strip time if present"""
    if not value or value == "Not Found":
        return "Not Found"

    value = normalize_field_text(value)
    if not value or value.lower() == "not found":
        return "Not Found"

    parsed = parse_date(value)
    if parsed:
        return parsed.strftime("%d/%m/%y")
    return value


def to_date(value: Any):
    return parse_date(value)


def date_delta(a: Any, b: Any) -> Optional[int]:
    da = to_date(a)
    db = to_date(b)
    if not da or not db:
        return None
    diff = (da - db).days
    return diff if diff in (-1, 0, 1) else None


def date_match_pm1(a: Any, b: Any) -> bool:
    return date_delta(a, b) in (-1, 0, 1)


def date_passes_row_match_style(api_in: Any, api_out: Any, inv_in: Any, inv_out: Any) -> bool:
    """Same date OR as booking / GST row match: CI vs CI ±1 or CO vs CO ±1."""
    return date_match_pm1(api_in, inv_in) or date_match_pm1(api_out, inv_out)
