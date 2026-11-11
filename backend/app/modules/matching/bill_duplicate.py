"""
Duplicate bill_number check for Step 7.

When a row's bill_number exists on more than one MySQL row, compare amount_paid vs
email_amount on the same row (± tolerance). Persist result in column `flag`:
  1           — amounts match within tolerance
  0           — both present but outside tolerance
  not found   — duplicate bill exists but amount_paid or email_amount is missing
  (no update) — bill_number is unique in the table
"""

from __future__ import annotations

import re
from typing import Any, Optional

import pandas as pd
from sqlalchemy.engine import Engine

DUPLICATE_AMOUNT_TOLERANCE_INR = 10.0

FLAG_MATCH = 1
FLAG_MISMATCH = 0
FLAG_NOT_FOUND = "not found"


def _normalize_amount(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text_val = str(value).replace(",", "").replace("₹", "").replace("$", "").strip()
    if not text_val or text_val.lower() in ("not found", "nan", "none", "null"):
        return None
    match = re.search(r"[\d.]+", text_val)
    if not match:
        return None
    try:
        return float(match.group(0))
    except (ValueError, TypeError):
        return None


def _normalize_bill_number(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    s = str(value).strip()
    if not s or s.lower() in ("not found", "nan", "none", "null", "-1"):
        return None
    return s.lower()


def _bill_number_from_row(row: pd.Series) -> Optional[str]:
    for key in ("Bill Number", "bill_number", "Bill number", "invoice_number"):
        if key in row.index:
            norm = _normalize_bill_number(row.get(key))
            if norm:
                return norm
    return None


def _amount_paid_from_row(row: pd.Series) -> Any:
    for key in ("Amount Paid", "amount_paid", "total_amount", "Amount"):
        if key in row.index:
            return row.get(key)
    return None


def _email_amount_from_row(row: pd.Series) -> Any:
    for key in ("Email Amount", "email_amount"):
        if key in row.index:
            return row.get(key)
    return None


def load_bill_number_index_df(engine: Engine) -> pd.DataFrame:
    """Load id + bill_number for all rows (duplicate detection across full table)."""
    from backend.app.modules.invoices.mysql import get_invoice_table_name

    table_name = get_invoice_table_name()
    query = f"SELECT id, bill_number FROM {table_name} ORDER BY id ASC"
    return pd.read_sql(query, engine)


def build_bill_number_index(df: pd.DataFrame) -> dict[str, list[int]]:
    """Map normalized bill_number -> list of row ids."""
    index: dict[str, list[int]] = {}
    if df is None or df.empty:
        return index
    for _, row in df.iterrows():
        bill_norm = _normalize_bill_number(row.get("bill_number"))
        if not bill_norm:
            continue
        try:
            row_id = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        index.setdefault(bill_norm, []).append(row_id)
    return index


def compute_duplicate_bill_flag(
    row_id: int,
    bill_number: Optional[str],
    amount_paid: Any,
    email_amount: Any,
    bill_index: dict[str, list[int]],
    *,
    tolerance: float = DUPLICATE_AMOUNT_TOLERANCE_INR,
) -> Optional[Any]:
    """
    Return flag value when bill_number is duplicated; None when unique or bill missing.
    """
    bill_norm = _normalize_bill_number(bill_number)
    if not bill_norm:
        return None

    ids = bill_index.get(bill_norm, [])
    if len(ids) <= 1:
        return None

    try:
        rid = int(row_id)
    except (TypeError, ValueError):
        return None
    if rid not in ids:
        return None

    ap = _normalize_amount(amount_paid)
    ea = _normalize_amount(email_amount)
    if ap is None or ea is None:
        return FLAG_NOT_FOUND
    if abs(ap - ea) <= tolerance:
        return FLAG_MATCH
    return FLAG_MISMATCH


def compute_duplicate_bill_flag_for_row(
    row: pd.Series,
    row_id: int,
    bill_index: dict[str, list[int]],
    *,
    tolerance: float = DUPLICATE_AMOUNT_TOLERANCE_INR,
) -> Optional[Any]:
    """Resolve fields from a Step-7 extracted row and compute duplicate bill flag."""
    return compute_duplicate_bill_flag(
        row_id,
        _bill_number_from_row(row),
        _amount_paid_from_row(row),
        _email_amount_from_row(row),
        bill_index,
        tolerance=tolerance,
    )


def flag_value_for_mysql(flag: Any) -> str:
    """Serialize flag for VARCHAR column."""
    if flag is None:
        return ""
    if flag == FLAG_MATCH:
        return "1"
    if flag == FLAG_MISMATCH:
        return "0"
    if flag == FLAG_NOT_FOUND:
        return FLAG_NOT_FOUND
    return str(flag)
