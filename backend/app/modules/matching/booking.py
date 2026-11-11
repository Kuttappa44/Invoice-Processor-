"""Booking-code and GST API-row collectors used by the amount gate."""

from __future__ import annotations

from typing import Any, Optional

import pandas as pd

from backend.app.modules.matching.gst import (
    _first_valid_gstin_from_field,
    normalize_gst_value,
)
from backend.app.shared.dates import date_passes_row_match_style as _date_passes_row_match_style
from backend.app.shared.guest_names import guest_names_match_for_merge
from backend.app.shared.text import normalize_booking_code_value
from backend.app.modules.matching.excel import find_matching_column

def _collect_api_rows_by_booking(
    api_df: pd.DataFrame,
    api_map: dict[str, str],
    invoice_booking: str,
    invoice_guest: str,
    inv_in: Any,
    inv_out: Any,
) -> list[int]:
    """All API row indices where normalized booking == invoice booking,
    guest fuzzy match (rapidfuzz >70 via guest_names_match_for_merge), and
    (check-in OR check-out ±1 day vs invoice) — same date rule as the primary booking row match."""
    norm_inv_code = normalize_booking_code_value(invoice_booking)
    if not norm_inv_code:
        return []
    booking_col = api_map.get("Booking Code")
    guest_col = api_map.get("Guest Name")
    checkin_col = api_map.get("Check-In Date")
    checkout_col = api_map.get("Check-Out Date")
    if not all([booking_col, guest_col, checkin_col, checkout_col]):
        return []
    if not invoice_guest:
        return []

    matches: list[int] = []
    for api_idx in api_df.index:
        api_row = api_df.loc[api_idx]
        api_booking = normalize_booking_code_value(str(api_row.get(booking_col, "") or ""))
        if api_booking != norm_inv_code:
            continue
        api_guest = str(api_row.get(guest_col, "") or "").strip()
        if not guest_names_match_for_merge(api_guest, invoice_guest):
            continue
        if not _date_passes_row_match_style(
            api_row.get(checkin_col), api_row.get(checkout_col), inv_in, inv_out
        ):
            continue
        matches.append(int(api_idx))
    return matches


def _collect_api_rows_by_gst(
    invoice_row: pd.Series,
    extracted_hbd_col: Optional[str],
    extracted_client_col: Optional[str],
    api_df: pd.DataFrame,
    api_map: dict[str, str],
    invoice_guest: str,
    inv_in: Any,
    inv_out: Any,
) -> list[int]:
    """All API row indices where (HBD GST or Client GST) matches the invoice's,
    guest fuzzy match (rapidfuzz >70), and (check-in OR check-out ±1 day vs invoice)
    — same date OR as match_by_gst_numbers / booking row match."""
    if not invoice_guest:
        return []

    hbd_raw = str(invoice_row.get(extracted_hbd_col, "") or "").strip() if extracted_hbd_col else ""
    client_raw = str(invoice_row.get(extracted_client_col, "") or "").strip() if extracted_client_col else ""
    hbd_norm = _first_valid_gstin_from_field(hbd_raw)
    client_norm = _first_valid_gstin_from_field(client_raw)
    if not hbd_norm and not client_norm:
        return []

    api_hbd_col = (
        find_matching_column(api_df, "hbd gst number")
        or find_matching_column(api_df, "hbd gst")
        or find_matching_column(api_df, "hbgstNumber")
        or find_matching_column(api_df, "hbgst")
    )
    api_client_col = (
        find_matching_column(api_df, "client gst number")
        or find_matching_column(api_df, "client gst")
        or find_matching_column(api_df, "hotelGSTNumber")
        or find_matching_column(api_df, "hotelGST")
        or find_matching_column(api_df, "hotel gst number")
    )
    guest_col = api_map.get("Guest Name")
    checkin_col = api_map.get("Check-In Date")
    checkout_col = api_map.get("Check-Out Date")
    if not (api_hbd_col and api_client_col and guest_col and checkin_col and checkout_col):
        return []

    matches: list[int] = []
    for api_idx in api_df.index:
        api_row = api_df.loc[api_idx]
        api_hbd = normalize_gst_value(api_row.get(api_hbd_col))
        api_client = normalize_gst_value(api_row.get(api_client_col))
        gst_ok = (hbd_norm and api_hbd == hbd_norm) or (client_norm and api_client == client_norm)
        if not gst_ok:
            continue
        api_guest = str(api_row.get(guest_col, "") or "").strip()
        if not guest_names_match_for_merge(api_guest, invoice_guest):
            continue
        if not _date_passes_row_match_style(
            api_row.get(checkin_col), api_row.get(checkout_col), inv_in, inv_out
        ):
            continue
        matches.append(int(api_idx))
    return matches


def _booking_codes_for_gate_indices(
    api_df: pd.DataFrame, booking_col: Optional[str], indices: list[int]
) -> list[str]:
    """Raw booking code strings for API row indices (same rules as gate_matched_booking_codes)."""
    if not booking_col:
        return []
    out: list[str] = []
    for i in indices:
        raw = api_df.loc[i].get(booking_col)
        if raw is None:
            out.append("(empty)")
            continue
        try:
            if isinstance(raw, float) and pd.isna(raw):
                out.append("(empty)")
                continue
        except (TypeError, ValueError):
            pass
        s = str(raw).strip()
        out.append(s if s else "(empty)")
    return out
