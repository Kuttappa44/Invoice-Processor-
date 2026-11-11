"""
Strict GSTIN (India) validation: structure + state code + PAN segment + 14th 'Z'.
The 15th character is not verified against the mod-36 checksum (see gstin_check_digit if needed).
"""

from __future__ import annotations

import re
from typing import Any, Optional

# 0-9 then A-Z maps to values 0-35 for checksum (GST portal algorithm)
_CODEPOINTS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# 2 state + 5 letters (PAN) + 4 digits + 1 letter (PAN) + entity + Z + check
_GSTIN_FULL = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]$")

_MIN_STATE = 1
_MAX_STATE = 38


def gstin_check_digit(gstin_first_14: str) -> str:
    """Return the 15th check character for the first 14 characters of a GSTIN."""
    s = gstin_first_14.strip().upper()
    if len(s) != 14:
        raise ValueError("gstin_first_14 must be exactly 14 characters")
    factor = 2
    total = 0
    for i in range(len(s) - 1, -1, -1):
        code_point = _CODEPOINTS.index(s[i])
        product = factor * code_point
        digit = (product // 36) + (product % 36)
        total += digit
        factor = 2 if factor == 1 else 1
    check = (36 - (total % 36)) % 36
    return _CODEPOINTS[check]


def is_valid_gstin(value: str | None, *, require_z_at_14: bool = True) -> bool:
    """
    True if value is a valid 15-character GSTIN:
    - State code (01-38)
    - PAN-like block (5 letters + 4 digits + 1 letter)
    - Entity code (alphanumeric)
    - 14th character 'Z' when require_z_at_14 (default)
    - 15th character: any base-36 symbol matching _GSTIN_FULL (checksum not verified)
    """
    if value is None or not isinstance(value, str):
        return False
    s = re.sub(r"[^A-Za-z0-9]", "", value).upper()
    if len(s) != 15:
        return False
    if not _GSTIN_FULL.match(s):
        return False
    if require_z_at_14 and s[13] != "Z":
        return False
    try:
        sc = int(s[:2])
    except ValueError:
        return False
    if sc < _MIN_STATE or sc > _MAX_STATE:
        return False
    return True


def gst_field_value_for_mysql_with_invalid(val: Any) -> tuple[Optional[str], bool]:
    """
    Build MySQL GST column value from raw field text.

    Returns (stored_value, had_invalid_segment):
    - Every non-empty alphanumeric segment is kept (valid or not), deduped in order.
    - had_invalid_segment is True if any segment failed is_valid_gstin.
    """
    if val is None:
        return None, False
    text = str(val).strip()
    if not text or text == "Not Found" or text.lower() in ("n/a", "na"):
        return None, False
    parts = [p.strip() for p in re.split(r"[;\n]+", text) if p.strip()]
    out: list[str] = []
    seen: set[str] = set()
    had_invalid = False
    for p in parts:
        s = re.sub(r"[^A-Za-z0-9]", "", p).upper()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
        if not is_valid_gstin(s):
            had_invalid = True
    if not out:
        return None, False
    return (" ; ".join(out), had_invalid)


def gst_field_value_for_mysql(val: Any) -> Optional[str]:
    """Same stored string as gst_field_value_for_mysql_with_invalid (includes invalid segments)."""
    stored, _ = gst_field_value_for_mysql_with_invalid(val)
    return stored
