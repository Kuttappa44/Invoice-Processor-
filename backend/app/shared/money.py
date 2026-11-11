"""Amount parsing and the Step-7 amount-gate tolerance."""

from __future__ import annotations

import re
from typing import Any, Optional

import pandas as pd

AMOUNT_GATE_TOLERANCE_INR = 10.0


def normalize_amount(value: Any) -> Optional[float]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(",", "").strip()
    if not text or text.lower() in ("not found", "nan", "none"):
        return None
    match = re.search(r"[\d.]+", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except Exception:
        return None
