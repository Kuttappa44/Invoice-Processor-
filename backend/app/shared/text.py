"""Field and booking-code string normalization used by ingest and matching."""

from __future__ import annotations

import re
from typing import Optional

import pandas as pd


def normalize_field_text(value: Optional[str]) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    if not value:
        return ""
    value = value.replace("\xa0", " ")  # Handle non-breaking spaces from Excel/web data
    value = re.sub(r"\s+", " ", value).strip()
    return value


def normalize_column_name(col_name):
    """Normalize column name for matching (same as email_to_excel_mapper.py)"""
    if pd.isna(col_name):
        return ""
    return str(col_name).strip().lower().replace("_", " ").replace("-", " ")


def normalize_booking_code_value(value):
    """Normalize booking code values for robust comparison"""
    if pd.isna(value):
        return ""
    text = str(value).strip()
    if not text:
        return ""
    text = text.strip("\"'`")
    text = text.replace(",", "").replace(" ", "")
    if re.fullmatch(r"\d+\.0+", text):
        text = text.split(".")[0]
    else:
        try:
            float_val = float(text)
            if float_val.is_integer():
                text = str(int(float_val))
        except Exception:
            pass
    return text.lower()
