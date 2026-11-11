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


def format_date_to_dd_mm_yy(value: Optional[str]) -> str:
    """Format date to dd/mm/yy format (2-digit year) and strip time if present"""
    if not value or value == "Not Found":
        return "Not Found"

    value = normalize_field_text(value)
    if not value or value.lower() == "not found":
        return "Not Found"

    # Strip time if present (e.g., "01/01/2024 00:00" or "01/01/2024 12:30:00")
    # Remove time patterns like " 00:00", " 12:30", " 12:30:00", etc.
    value = re.sub(r"\s+\d{1,2}:\d{2}(?::\d{2})?\s*$", "", value).strip()

    try:
        date_formats = [
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
        ]

        parsed_date = None
        for fmt in date_formats:
            try:
                parsed_date = datetime.strptime(
                    value.split()[0] if " " in value else value, fmt
                )
                break
            except ValueError:
                continue

        if parsed_date:
            return parsed_date.strftime("%d/%m/%y")

        if "/" in value:
            parts = value.split("/")
            if len(parts) == 3:
                day, month, year = parts
                day = day.strip().zfill(2)
                month = month.strip().zfill(2)
                year = year.strip()
                if len(year) == 4:
                    year = year[2:]
                return f"{day}/{month}/{year}"

        if "-" in value and len(value) >= 8:
            parts = value.split("-")
            if len(parts) == 3:
                try:
                    if len(parts[0]) == 4:
                        year, month, day = parts
                        year = year[2:] if len(year) == 4 else year
                        return f"{day.zfill(2)}/{month.zfill(2)}/{year}"
                    else:
                        day, month, year = parts
                        month_lower = month.lower()
                        month_map = {
                            "jan": "01",
                            "january": "01",
                            "feb": "02",
                            "february": "02",
                            "mar": "03",
                            "march": "03",
                            "apr": "04",
                            "april": "04",
                            "may": "05",
                            "jun": "06",
                            "june": "06",
                            "jul": "07",
                            "july": "07",
                            "aug": "08",
                            "august": "08",
                            "sep": "09",
                            "september": "09",
                            "oct": "10",
                            "october": "10",
                            "nov": "11",
                            "november": "11",
                            "dec": "12",
                            "december": "12",
                        }
                        if month_lower in month_map:
                            month = month_map[month_lower]
                        year = year[2:] if len(year) == 4 else year
                        return f"{day.zfill(2)}/{month.zfill(2)}/{year}"
                except Exception:
                    pass

    except Exception:
        pass

    return value


def to_date(value: Any):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, datetime):
        return value.date()

    text = str(value).strip()
    if not text or text.lower() in ("nan", "none", "not found", "nat"):
        return None
    try:
        normalized = format_date_to_dd_mm_yy(text)
        if not normalized or normalized == "Not Found":
            return None
        return datetime.strptime(normalized, "%d/%m/%y").date()
    except Exception:
        return None


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
