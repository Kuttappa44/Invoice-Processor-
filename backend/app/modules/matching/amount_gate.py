"""Step-7 amount gate: MySQL Amount Paid vs summed API balanceAmount (₹10)."""

from __future__ import annotations

from typing import Any, Optional

import pandas as pd

from backend.app.modules.matching.booking import (
    _booking_codes_for_gate_indices,
    _collect_api_rows_by_booking,
    _collect_api_rows_by_gst,
)
from backend.app.modules.matching.property import match_properties
from backend.app.shared.money import AMOUNT_GATE_TOLERANCE_INR
from backend.app.shared.money import normalize_amount as _normalize_amount
from backend.app.shared.text import normalize_booking_code_value

def _is_mmt_invoice(erow) -> bool:
    """True when MySQL row has mmt_flag=1 (MakeMyTrip invoice)."""
    val = erow.get("mmt_flag") if hasattr(erow, "get") else None
    if val is None:
        return False
    try:
        if pd.isna(val):
            return False
    except (TypeError, ValueError):
        pass
    try:
        return int(float(str(val).strip())) == 1
    except (ValueError, TypeError):
        return False


def _amount_gate_for_row(
    erow,
    api_df: pd.DataFrame,
    extracted_map: dict[str, str],
    api_map: dict[str, str],
    extracted_hbd_col: Optional[str],
    extracted_client_col: Optional[str],
    extracted_amount_col: Optional[str],
    api_balance_col: Optional[str],
    extracted_hotel_col: Optional[str],
    api_property_col: Optional[str],
    matched_api_indices: Optional[list[int]] = None,
    match_path: Optional[str] = None,
) -> tuple[bool, Optional[str], dict[str, Any]]:
    """Amount Paid vs API balance gate; skipped (passes) when mmt_flag=1."""
    if _is_mmt_invoice(erow):
        print(
            "   🏷️  MMT invoice (mmt_flag=1): skipping Amount Paid vs API balance comparison"
        )
        return True, None, {"skipped": True, "reason": "mmt_flag", "path": "mmt_skip"}
    return _amount_gate_check(
        erow,
        api_df,
        extracted_map,
        api_map,
        extracted_hbd_col,
        extracted_client_col,
        extracted_amount_col,
        api_balance_col,
        extracted_hotel_col,
        api_property_col,
        matched_api_indices=matched_api_indices,
        match_path=match_path,
    )

def _amount_gate_check(
    erow: pd.Series,
    api_df: pd.DataFrame,
    extracted_map: dict[str, str],
    api_map: dict[str, str],
    extracted_hbd_col: Optional[str],
    extracted_client_col: Optional[str],
    extracted_amount_col: Optional[str],
    api_balance_col: Optional[str],
    extracted_hotel_col: Optional[str],
    api_property_col: Optional[str],
    matched_api_indices: Optional[list[int]] = None,
    match_path: Optional[str] = None,
) -> tuple[bool, Optional[str], dict[str, Any]]:
    """Multi-row amount validation gate that runs right before push.

    When matched_api_indices is provided (from the primary match loop), those
    rows are used directly — no duplicate booking/GST search.

    Returns (ok, reason, debug):
      - ok=True, reason=None: |MySQL Amount Paid - sum(balanceAmount)| <= AMOUNT_GATE_TOLERANCE_INR
      - ok=False, reason='amount mismatch': sum is outside tolerance, OR no
        matching API rows were found, OR no parseable balanceAmount values, OR
        MySQL Amount Paid could not be parsed.
    debug carries diagnostic data for logging.
    """
    inv_amount_raw = erow.get(extracted_amount_col) if extracted_amount_col else None

    if matched_api_indices is not None:
        path = match_path or "matched_row"
        api_indices = [int(i) for i in matched_api_indices]
    else:
        inv_booking_raw = str(erow.get(extracted_map.get("Booking Code", ""), "") or "").strip()
        inv_guest = str(erow.get(extracted_map.get("Guest Name", ""), "") or "").strip()
        inv_in = erow.get(extracted_map.get("Check-In Date", "")) if extracted_map.get("Check-In Date") else None
        inv_out = erow.get(extracted_map.get("Check-Out Date", "")) if extracted_map.get("Check-Out Date") else None

        norm_inv_code = normalize_booking_code_value(inv_booking_raw)
        if norm_inv_code:
            path = "booking"
            api_indices = _collect_api_rows_by_booking(
                api_df, api_map, inv_booking_raw, inv_guest, inv_in, inv_out
            )
        else:
            path = "gst"
            api_indices = _collect_api_rows_by_gst(
                erow, extracted_hbd_col, extracted_client_col,
                api_df, api_map, inv_guest, inv_in, inv_out,
            )

    base_indices = list(api_indices)
    property_base_count = len(base_indices)
    property_filter_applied = False
    if (
        property_base_count > 1
        and extracted_hotel_col
        and api_property_col
    ):
        inv_hotel = str(erow.get(extracted_hotel_col, "") or "").strip()
        if inv_hotel:
            property_filter_applied = True
            filtered_idx: list[int] = []
            for i in base_indices:
                api_prop_raw = api_df.loc[i].get(api_property_col)
                if api_prop_raw is None:
                    api_prop = ""
                else:
                    try:
                        if isinstance(api_prop_raw, float) and pd.isna(api_prop_raw):
                            api_prop = ""
                        else:
                            api_prop = str(api_prop_raw)
                    except (TypeError, ValueError):
                        api_prop = str(api_prop_raw)
                if match_properties(inv_hotel, api_prop):
                    filtered_idx.append(int(i))
            api_indices = filtered_idx

    booking_gate_col = api_map.get("Booking Code")
    gate_booking_codes_before_property: Optional[list[str]] = None
    gate_booking_codes_after_property: Optional[list[str]] = None
    if property_filter_applied and booking_gate_col:
        gate_booking_codes_before_property = _booking_codes_for_gate_indices(
            api_df, booking_gate_col, base_indices
        )
        gate_booking_codes_after_property = _booking_codes_for_gate_indices(
            api_df, booking_gate_col, list(api_indices)
        )
        print(
            "   🏨 AMOUNT GATE booking codes before property filter: "
            f"{', '.join(gate_booking_codes_before_property)}"
        )
        print(
            "   🏨 AMOUNT GATE booking codes after property filter:  "
            f"{', '.join(gate_booking_codes_after_property)}"
        )
    elif property_filter_applied and not booking_gate_col:
        print(
            "   🏨 AMOUNT GATE property filter applied but API has no Booking Code column; "
            "before/after booking code lists skipped."
        )

    gate_matched_booking_codes: list[str] = []
    if booking_gate_col:
        for i in api_indices:
            raw = api_df.loc[i].get(booking_gate_col)
            if raw is None:
                gate_matched_booking_codes.append("(empty)")
                continue
            try:
                if isinstance(raw, float) and pd.isna(raw):
                    gate_matched_booking_codes.append("(empty)")
                    continue
            except (TypeError, ValueError):
                pass
            s = str(raw).strip()
            gate_matched_booking_codes.append(s if s else "(empty)")

    debug: dict[str, Any] = {
        "path": path,
        "count": len(api_indices),
        "api_indices": api_indices,
        "gate_matched_booking_codes": gate_matched_booking_codes,
        "mysql": _normalize_amount(inv_amount_raw),
        "sum": None,
        "diff": None,
        "tolerance": AMOUNT_GATE_TOLERANCE_INR,
        "property_filter_applied": property_filter_applied,
        "property_base_count": property_base_count,
        "property_after_count": len(api_indices),
        "gate_booking_codes_before_property": gate_booking_codes_before_property,
        "gate_booking_codes_after_property": gate_booking_codes_after_property,
    }

    inv_amount = _normalize_amount(inv_amount_raw)
    if inv_amount is None:
        return False, "amount mismatch", debug
    if not api_indices or not api_balance_col:
        return False, "amount mismatch", debug

    raw_balances = [api_df.loc[i].get(api_balance_col) for i in api_indices]
    parsed = [p for p in (_normalize_amount(v) for v in raw_balances) if p is not None]
    if not parsed:
        return False, "amount mismatch", debug

    api_sum = sum(parsed)
    debug["sum"] = api_sum
    debug["diff"] = abs(inv_amount - api_sum)
    if debug["diff"] <= AMOUNT_GATE_TOLERANCE_INR:
        return True, None, debug
    return False, "amount mismatch", debug


def _amount_difference_from_gate(gate_debug: dict[str, Any]) -> Optional[float]:
    """Absolute invoice vs API balance difference when the amount gate ran a comparison."""
    if gate_debug.get("skipped") and gate_debug.get("reason") == "mmt_flag":
        return None
    diff = gate_debug.get("diff")
    if diff is None:
        return None
    try:
        return float(diff)
    except (TypeError, ValueError):
        return None
