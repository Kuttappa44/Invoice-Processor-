"""
Excel matching helpers (reading generated excel, column mapping utilities).
"""

from __future__ import annotations

import os

import pandas as pd

from backend.app.shared.text import (  # noqa: F401
    normalize_booking_code_value,
    normalize_column_name,
)


def _matching_excel_path():
    excel_path = os.getenv("EXCEL_FILE_PATH")
    if excel_path:
        return excel_path
    from backend.app.modules.matching import constants as _config

    return _config.MATCHING_EXCEL_FILE_PATH


def find_matching_column(df, field_name):
    """Find the matching column in DataFrame for a given field (same as email_to_excel_mapper.py)"""
    # Explicit mappings for common field names to Excel column names (priority order)
    # These are checked FIRST before any other matching logic
    explicit_mappings = {
        "Booking Code": ["bookingCode", "booking_code", "booking code", "booking id", "bookingid"],
        "Guest Name": ["guestName", "guest_name", "guest name", "customer name", "customer"],
        "Hotel Name": [
            "propertyName",
            "property_name",
            "property name",
            "hotel name",
            "hotel_name",
            "hotel",
            "propertyReference",
        ],
        "Check-In Date": [
            "checkInDate",
            "check_in_date",
            "check-in date",
            "checkin date",
            "checkin",
            "arrival date",
            "arrival",
        ],
        "Check-Out Date": [
            "checkOutDate",
            "check_out_date",
            "check-out date",
            "checkout date",
            "checkout",
            "departure date",
            "departure",
        ],
    }

    # Try explicit mappings first (case-insensitive, exact field name match)
    if field_name in explicit_mappings:
        for mapping in explicit_mappings[field_name]:
            # Check if this exact column name exists (case-insensitive)
            for col in df.columns:
                if normalize_column_name(col) == normalize_column_name(mapping):
                    return col

    # Normalize field name for matching
    normalized_field = normalize_column_name(field_name)

    # Try exact match first
    for col in df.columns:
        if normalize_column_name(col) == normalized_field:
            return col

    # Try partial match
    for col in df.columns:
        col_normalized = normalize_column_name(col)
        if normalized_field in col_normalized or col_normalized in normalized_field:
            return col

    # Try fuzzy matching with common variations
    field_variations = {
        "paid on": ["paid", "payment date", "date paid"],
        "booking code": ["booking", "code", "booking id", "reference", "confirmation"],
        "guest name": ["guest", "name", "customer"],
        "hotel name": ["hotel", "property", "venue"],
        "check-in date": ["check in", "arrival", "checkin"],
        "check-out date": ["check out", "departure", "checkout"],
        "payment": ["payment", "amount", "total", "paid"],
        "advance paid amount": ["advance paid amount", "advance amount", "advance paid"],
    }

    for col in df.columns:
        col_normalized = normalize_column_name(col)
        for key, variations in field_variations.items():
            if key in normalized_field:
                for variation in variations:
                    if variation in col_normalized:
                        return col

    return None


def read_matching_excel_file():
    """Read Excel file from local path for matching (same as email_to_excel_mapper.py)"""
    try:
        excel_path = _matching_excel_path()
        print(f"\n📊 Reading matching Excel file: {excel_path}")

        # Check if file exists
        if not excel_path or not os.path.exists(excel_path):
            print(f"❌ Excel file not found: {excel_path}")
            print(f"   Please check the file path in your .env file (EXCEL_FILE_PATH)")
            return None

        # Check if file is readable
        if not os.access(excel_path, os.R_OK):
            print(f"❌ Excel file is not readable: {excel_path}")
            print(f"   Please check file permissions")
            return None

        # Check file size
        file_size = os.path.getsize(excel_path)
        if file_size == 0:
            print(f"❌ Excel file is empty (0 bytes): {excel_path}")
            return None

        # Try reading with pandas
        print(f"   📄 File size: {file_size:,} bytes")
        df = pd.read_excel(excel_path, engine="openpyxl", header=0)
        print(f"✅ Read Excel file: {len(df)} rows, {len(df.columns)} columns")
        print(f"📋 Columns: {list(df.columns)}")
        return df

    except PermissionError:
        print(f"❌ Permission denied: Cannot read Excel file")
        print(f"   The file might be open in Excel. Please close it and try again.")
        return None
    except FileNotFoundError:
        excel_path = _matching_excel_path()
        print(f"❌ Excel file not found: {excel_path}")
        return None
    except Exception as e:
        print(f"❌ Error reading Excel file: {e}")
        return None


def __getattr__(name: str):
    # Lazy: legacy_matching imports find_matching_column from this module.
    if name in ("extract_all_invoice_fields", "match_invoice_data_with_excel"):
        from backend.app.modules.matching import master as _lm

        return getattr(_lm, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
