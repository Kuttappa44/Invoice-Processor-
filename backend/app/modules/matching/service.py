"""Step-7 matching service: booking-code then GST, amount gate, then push."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from backend.app.integrations import booking_api
from backend.app.modules.matching.amount_gate import (
    _amount_difference_from_gate,
    _amount_gate_for_row,
    _is_mmt_invoice,
)
from backend.app.modules.matching.bill_duplicate import (
    build_bill_number_index,
    compute_duplicate_bill_flag_for_row,
    load_bill_number_index_df,
)
from backend.app.modules.matching.gst import (
    _first_valid_gstin_from_field,
    match_by_gst_numbers,
    normalize_gst_value,
)
from backend.app.modules.matching.property import match_properties
from backend.app.modules.matching.remarks import generate_matching_remarks
from backend.app.modules.push.service import _build_api_json_preview, push_row_to_api
from backend.app.shared.dates import (
    date_delta as _date_delta,
    date_match_pm1 as _date_match_pm1,
    date_passes_row_match_style as _date_passes_row_match_style,
    format_date_to_dd_mm_yy,
    to_date as _to_date,
)
from backend.app.shared.guest_names import guest_names_match_for_merge
from backend.app.shared.money import AMOUNT_GATE_TOLERANCE_INR
from backend.app.shared.money import normalize_amount as _normalize_amount
from backend.app.shared.text import normalize_booking_code_value
from backend.app.integrations.azure_blob import sanitize_blob_url
from backend.app.modules.matching.excel import find_matching_column

MATCHED_STATUS_COLUMN = "matched_status"


def _bad_required_value(v) -> bool:
    """Mirror main pipeline: treat placeholders as missing."""
    if v is None:
        return True
    try:
        if pd.isna(v):
            return True
    except Exception:
        pass
    s = str(v).strip()
    if not s:
        return True
    low = s.lower()
    if low in ("not found", "nan", "none", "null", "string", "-1"):
        return True
    if s == "9999-12-12":
        return True
    return False


def _pick_first_present(row_dict: dict, keys: list[str]):
    for k in keys:
        if k in row_dict and not _bad_required_value(row_dict.get(k)):
            return row_dict.get(k)
    return None


def _bad_amount_paid(v) -> bool:
    """Skip push when amount is missing, placeholder, -1, or zero."""
    if v is None:
        return True
    try:
        if pd.isna(v):
            return True
    except Exception:
        pass
    if _bad_required_value(v):
        return True
    try:
        s = str(v).strip().replace(",", "").replace("₹", "").replace("$", "").strip()
        if not s:
            return True
        num = float(s)
        if num == 0.0 or num == -1.0:
            return True
    except (ValueError, TypeError):
        return True
    return False


def _build_field_map(df: pd.DataFrame) -> dict[str, str]:
    required = ["Booking Code", "Guest Name", "Hotel Name", "Check-In Date", "Check-Out Date"]
    out: dict[str, str] = {}
    for field in required:
        col = find_matching_column(df, field)
        if col:
            out[field] = col
    return out


def _resolve_col(df: pd.DataFrame, aliases: list[str]) -> Optional[str]:
    for alias in aliases:
        col = find_matching_column(df, alias)
        if col:
            return col
    return None


def _is_non_empty(value: Any) -> bool:
    if value is None:
        return False
    try:
        if pd.isna(value):
            return False
    except Exception:
        pass
    text = str(value).strip()
    return text != "" and text.lower() not in ("nan", "none")


def _room_captured_from_matched_api_row(api_df: pd.DataFrame, api_idx: int) -> Any:
    col = _resolve_col(
        api_df,
        ["roomCaptured", "Room Captured", "room_captured", "room captured"],
    )
    if not col:
        return None
    val = api_df.at[api_idx, col]
    if val is None:
        return None
    try:
        if pd.isna(val):
            return None
    except TypeError:
        pass
    s = str(val).strip()
    return s if s and s.lower() not in ("nan", "none", "nat") else None


_EXTRACTED_HBD_GST_KEYS = [
    "HBD GST Number",
    "hbd_gst_number",
    "hbdGstNumber",
    "hb_gst_number",
]
_EXTRACTED_CLIENT_GST_KEYS = [
    "Client GST Number",
    "client_gst_number",
    "hotelGSTNumber",
    "hotel_gst_number",
]


def _gst_value_from_api_cell(val: Any) -> Optional[str]:
    if val is None:
        return None
    try:
        if pd.isna(val):
            return None
    except (TypeError, ValueError):
        pass
    s = str(val).strip()
    if not s or s.lower() in ("nan", "none", "nat", "not found"):
        return None
    return s


def _first_gst_from_api_indices(
    api_df: pd.DataFrame,
    api_indices: list[int],
    col: Optional[str],
) -> Optional[str]:
    """First non-empty GST across API rows (in order)."""
    if not col:
        return None
    for i in api_indices:
        v = _gst_value_from_api_cell(api_df.loc[i].get(col))
        if v:
            return v
    return None


def _api_indices_after_property_filter(
    erow: pd.Series,
    api_df: pd.DataFrame,
    base_indices: list[int],
    extracted_hotel_col: Optional[str],
    api_property_col: Optional[str],
) -> list[int]:
    """Same property filter as the amount gate when multiple API rows matched."""
    if len(base_indices) <= 1 or not extracted_hotel_col or not api_property_col:
        return list(base_indices)
    inv_hotel = str(erow.get(extracted_hotel_col, "") or "").strip()
    if not inv_hotel:
        return list(base_indices)
    filtered: list[int] = []
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
            filtered.append(int(i))
    return filtered


def _resolve_indices_for_push_enrichment(
    erow: pd.Series,
    gate_debug: dict[str, Any],
    gate_indices: list[int],
    fallback_api_idx: int,
    api_df: pd.DataFrame,
    extracted_hotel_col: Optional[str],
    api_property_col: Optional[str],
) -> list[int]:
    """Prefer amount-gate post-filter indices; else re-apply property filter."""
    cached = gate_debug.get("api_indices")
    if isinstance(cached, list) and cached:
        return [int(i) for i in cached]
    base = gate_indices or [fallback_api_idx]
    filtered = _api_indices_after_property_filter(
        erow, api_df, base, extracted_hotel_col, api_property_col
    )
    return filtered if filtered else base


def _enrich_payload_gst_from_matched_api(
    payload: dict[str, Any],
    api_df: pd.DataFrame,
    api_indices: list[int],
    api_hbd_col: Optional[str],
    api_client_col: Optional[str],
) -> dict[str, str]:
    """Fill missing GST on push payload from API rows; never overwrite existing values."""
    filled: dict[str, str] = {}

    if _bad_required_value(_pick_first_present(payload, _EXTRACTED_HBD_GST_KEYS)):
        hbd = _first_gst_from_api_indices(api_df, api_indices, api_hbd_col)
        if hbd:
            payload["HBD GST Number"] = hbd
            filled["HBD GST Number"] = hbd

    if _bad_required_value(_pick_first_present(payload, _EXTRACTED_CLIENT_GST_KEYS)):
        client = _first_gst_from_api_indices(api_df, api_indices, api_client_col)
        if client:
            payload["Client GST Number"] = client
            filled["Client GST Number"] = client

    return filled


def _apply_gst_backfill_before_push(
    payload: dict[str, Any],
    erow: pd.Series,
    extracted_idx: int,
    fallback_api_idx: int,
    api_df: pd.DataFrame,
    gate_debug: dict[str, Any],
    gate_indices_by_idx: dict[int, list[int]],
    extracted_hotel_col: Optional[str],
    api_property_col: Optional[str],
    api_hbd_col: Optional[str],
    api_client_col: Optional[str],
) -> dict[str, str]:
    api_indices = _resolve_indices_for_push_enrichment(
        erow,
        gate_debug,
        gate_indices_by_idx.get(int(extracted_idx)) or [],
        fallback_api_idx,
        api_df,
        extracted_hotel_col,
        api_property_col,
    )
    filled = _enrich_payload_gst_from_matched_api(
        payload, api_df, api_indices, api_hbd_col, api_client_col
    )
    if filled:
        print(
            f"   📋 GST backfill from API rows {api_indices} "
            f"(invoice values kept when already present): {filled}"
        )
    return filled


def _payload_from_extracted_row_only(erow: pd.Series) -> dict[str, Any]:
    """API push uses MySQL/export columns only (no merged booking Excel row)."""
    payload: dict[str, Any] = {}
    for c in erow.index:
        v = erow.get(c)
        if not _is_non_empty(v):
            continue
        payload[str(c)] = v
    return payload


def _resolve_api_balance_col(api_df: pd.DataFrame) -> Optional[str]:
    """Resolve the API DataFrame's balanceAmount column."""
    if "balanceAmount" in api_df.columns:
        return "balanceAmount"
    for col in api_df.columns:
        if str(col).strip().lower() in ("balance amount", "balanceamount"):
            return str(col)
    return None

def run(
    output_path: Optional[str] = None,
    push: bool = False,
    preview_push_json: bool = False,
    immediate_push: bool = False,
    dry_run: bool = False,
):
    print("=" * 70)
    print("STEP7 FLOW (MySQL required) + API EXCEL FROM test1.py")
    if dry_run:
        print("DRY RUN: amount gate runs, no HTTP push, no push-outcome MySQL writes")
    print("=" * 70)
    print("1) Generate API Excel via test1.py")
    print("2) Match extracted rows to API rows")
    print("3) Fill status/flags/remarks/amount_match/amount_difference")
    print("4) Save updated extracted Excel (invoice report)")
    print("5) Optionally push matched rows to API")
    print("=" * 70)

    # 0) Initialize MySQL engine for writing match results back to DB
    _mysql_engine = None
    try:
        from backend.app.modules.invoices.mysql import (
            ensure_invoice_report_table_exists,
            get_db_engine,
            update_matched_status_in_mysql,
            update_excel_match_diagnostics_in_mysql,
            update_push_cancelled_in_mysql,
            update_push_failed_in_mysql,
            update_pushed_at_in_mysql,
            update_room_captured_in_mysql,
            update_flag_in_mysql,
        )
        _mysql_engine = get_db_engine()
        ensure_invoice_report_table_exists()
        print("✅ MySQL engine initialized — match results will be saved to DB")
    except Exception as _db_err:
        raise RuntimeError(
            "MySQL is required for this script but could not be initialized. "
            f"Original error: {_db_err}"
        ) from _db_err

    def _get_row_id(row):
        """Get MySQL primary key from extracted row, or None."""
        rid = row.get("id")
        if rid is None:
            return None
        try:
            if pd.isna(rid):
                return None
        except Exception:
            pass
        try:
            return int(float(str(rid).strip()))
        except (ValueError, TypeError):
            return None

    # 1) Generate API Excel from test1.py
    print("\n📥 Generating API Excel from test1.py ...")
    generated_api_excel = booking_api.main()
    if not generated_api_excel or not Path(generated_api_excel).exists():
        raise FileNotFoundError("test1.py did not return a valid generated Excel path.")
    print(f"✅ Generated API Excel: {generated_api_excel}")

    # 2) Read source files — extracted rows come from MySQL (pushed_at=0 or NULL only)
    from backend.app.modules.invoices.mysql import read_mysql_data_as_dataframe
    extracted_df = read_mysql_data_as_dataframe(only_unpushed=True)
    if extracted_df is None or extracted_df.empty:
        raise ValueError("No unpushed rows found in MySQL (pushed_at=0 or NULL).")
    api_df = pd.read_excel(generated_api_excel)

    extracted_map = _build_field_map(extracted_df)
    api_map = _build_field_map(api_df)

    for needed in ["Booking Code", "Guest Name", "Check-In Date", "Check-Out Date"]:
        if needed not in extracted_map:
            raise ValueError(f"Missing extracted column mapping for: {needed}")
        if needed not in api_map:
            raise ValueError(f"Missing generated-API column mapping for: {needed}")

    # Optional columns
    extracted_hbd_col = _resolve_col(extracted_df, ["HBD GST Number", "hbd gst", "hbdGstNumber"])
    extracted_client_col = _resolve_col(extracted_df, ["Client GST Number", "client gst", "hotelGSTNumber"])
    extracted_amount_col = _resolve_col(extracted_df, ["Amount Paid", "amount paid", "total amount", "amount"])
    extracted_hotel_col = _resolve_col(
        extracted_df,
        ["hotel_name", "Hotel Name", "hotelName", "propertyName", "Hotel"],
    )
    api_property_col = _resolve_col(
        api_df,
        ["propertyName", "Property Name", "property_name", "hotelName", "Hotel Name"],
    )
    api_balance_col = _resolve_col(api_df, ["balanceAmount", "balance amount", "balanceamount"])
    api_hbd_gst_col = _resolve_col(
        api_df,
        ["hbgstNumber", "HBD GST Number", "hbd gst number", "hbd gst", "hbgst"],
    )
    api_client_gst_col = _resolve_col(
        api_df,
        ["hotelGSTNumber", "Client GST Number", "client gst number", "client gst", "hotelGST"],
    )

    print("\n🔎 Building bill_number duplicate index from MySQL (all rows)...")
    bill_index = build_bill_number_index(load_bill_number_index_df(_mysql_engine))
    print(f"   ✅ Indexed {len(bill_index)} distinct bill number(s)")

    # Ensure output columns exist on EXTRACTED excel (invoice report).
    # Also convert to object dtype so string/int values can be written
    # without LossySetitemError (columns from MySQL may be float64 when all NULL).
    for column_name in [
        MATCHED_STATUS_COLUMN,
        "match_booking_code",
        "hbd_gst_match",
        "client_gst_match",
        "amount_match",
        "amount_difference",
        "amount_gate_matched_booking_codes",
        "remarks",
        "excel_match_guest_score",
        "excel_match_checkin_delta",
        "excel_match_checkout_delta",
        "push_failed",
        "push_cancelled",
        "Room Captured",
        "flag",
    ]:
        if column_name not in extracted_df.columns:
            extracted_df[column_name] = None
        extracted_df[column_name] = extracted_df[column_name].astype(object)

    selected_for_push: list[tuple[int, int, dict[str, Any]]] = []
    # Cache of amount-gate results keyed by extracted_idx, populated in the
    # matching loop. Re-used (not recomputed) inside both push branches so the
    # gate verdict that drives `amount_match` is exactly the one that drives
    # the cancel/push decision.
    gate_results_by_idx: dict[int, tuple[bool, Optional[str], dict[str, Any]]] = {}
    gate_indices_by_idx: dict[int, list[int]] = {}
    gate_match_path_by_idx: dict[int, str] = {}
    rows_updated = 0
    unmatched_rows = 0
    push_success = 0
    push_failed = 0
    push_attempts = 0
    skipped_count = 0
    push_cancelled_count = 0

    # 3) Match extracted -> API rows
    for extracted_idx in extracted_df.index:
        erow = extracted_df.loc[extracted_idx]
        _rid = _get_row_id(erow)

        e_code_raw = str(erow.get(extracted_map["Booking Code"], "") or "").strip()
        e_code = normalize_booking_code_value(e_code_raw)
        e_guest = str(erow.get(extracted_map["Guest Name"], "") or "").strip()
        e_checkin = erow.get(extracted_map["Check-In Date"])
        e_checkout = erow.get(extracted_map["Check-Out Date"])

        matched_api_idx: Optional[int] = None
        matched_api_indices: list[int] = []
        match_booking_code: Optional[int] = None
        hbd_gst_match: Optional[int] = None
        client_gst_match: Optional[int] = None
        guest_score: Optional[int] = None
        checkin_delta: Optional[int] = None
        checkout_delta: Optional[int] = None
        match_type = "booking_code"

        print("\n" + "-" * 70)
        _rid_label = f"row ID {_rid} | " if _rid is not None else ""
        print(
            f"🔎 Matching extracted row {int(extracted_idx) + 1} | "
            f"{_rid_label}"
            f"Booking Code='{e_code_raw or ''}' | Guest='{e_guest or ''}' | "
            f"CheckIn='{e_checkin}' | CheckOut='{e_checkout}'"
        )

        if _rid is not None:
            dup_flag = compute_duplicate_bill_flag_for_row(
                erow,
                _rid,
                bill_index,
                tolerance=AMOUNT_GATE_TOLERANCE_INR,
            )
            if dup_flag is not None:
                extracted_df.at[extracted_idx, "flag"] = dup_flag
                update_flag_in_mysql(_rid, dup_flag, _mysql_engine)
                print(
                    f"   🏷️  Duplicate bill_number (row ID {_rid}): flag={dup_flag!r} "
                    f"(amount_paid vs email_amount on same row, ±₹{AMOUNT_GATE_TOLERANCE_INR})"
                )
            erow = extracted_df.loc[extracted_idx]

        # Primary path: only when invoice has a normalized booking code.
        # If code is missing, do not scan guest+dates here — go straight to GST fallback below.
        if e_code:
            booking_matches: list[int] = []
            for api_idx in api_df.index:
                arow = api_df.loc[api_idx]
                a_code = normalize_booking_code_value(str(arow.get(api_map["Booking Code"], "") or ""))
                if a_code != e_code:
                    continue

                a_guest = str(arow.get(api_map["Guest Name"], "") or "").strip()
                if not guest_names_match_for_merge(a_guest, e_guest):
                    continue

                a_checkin = arow.get(api_map["Check-In Date"])
                a_checkout = arow.get(api_map["Check-Out Date"])
                if not (_date_match_pm1(a_checkin, e_checkin) or _date_match_pm1(a_checkout, e_checkout)):
                    continue

                booking_matches.append(int(api_idx))

            if booking_matches:
                matched_api_indices = booking_matches
                matched_api_idx = matched_api_indices[0]
                arow = api_df.loc[matched_api_idx]
                a_code = normalize_booking_code_value(str(arow.get(api_map["Booking Code"], "") or ""))
                a_guest = str(arow.get(api_map["Guest Name"], "") or "").strip()
                a_checkin = arow.get(api_map["Check-In Date"])
                a_checkout = arow.get(api_map["Check-Out Date"])
                match_booking_code = 1
                guest_score = 100 if a_guest.lower() == e_guest.lower() else 85
                checkin_delta = _date_delta(a_checkin, e_checkin)
                checkout_delta = _date_delta(a_checkout, e_checkout)
                print(
                    f"✅ Matched by booking-code path -> API row {matched_api_idx + 1} | "
                    f"api_booking='{a_code}' | api_guest='{a_guest}'"
                )
                if len(matched_api_indices) > 1:
                    print(
                        f"   ℹ️  Amount gate will sum {len(matched_api_indices)} booking-code API row(s)"
                    )
        else:
            print(
                "   ℹ️  No booking code on invoice (missing or not extracted) → "
                "skipping booking-code+guest+date scan; using GST fallback only."
            )

        # Fallback path: GST matching when booking code missing or primary path found no match
        if matched_api_idx is None:
            match_type = "gst"
            hbd_val = str(erow.get(extracted_hbd_col, "") or "").strip() if extracted_hbd_col else ""
            client_val = str(erow.get(extracted_client_col, "") or "").strip() if extracted_client_col else ""

            if hbd_val or client_val:
                entry = {
                    "Guest Name": e_guest,
                    "Check-In Date": str(e_checkin) if e_checkin is not None else "",
                    "Check-Out Date": str(e_checkout) if e_checkout is not None else "",
                }
                try:
                    indices, guest_scores, checkin_deltas, checkout_deltas, hbd_ok, client_ok = match_by_gst_numbers(
                        entry, api_df, api_map, hbd_val, client_val
                    )
                    if indices:
                        matched_api_indices = [int(i) for i in indices]
                        matched_api_idx = matched_api_indices[0]
                        match_booking_code = 0
                        hbd_gst_match = 1 if hbd_ok is True else (0 if hbd_ok is False else None)
                        client_gst_match = 1 if client_ok is True else (0 if client_ok is False else None)

                        if hasattr(guest_scores, "index") and matched_api_idx in guest_scores.index:
                            try:
                                guest_score = int(guest_scores.at[matched_api_idx])
                            except Exception:
                                guest_score = None
                        if hasattr(checkin_deltas, "index") and matched_api_idx in checkin_deltas.index:
                            try:
                                checkin_delta = int(checkin_deltas.at[matched_api_idx])
                            except Exception:
                                checkin_delta = None
                        if hasattr(checkout_deltas, "index") and matched_api_idx in checkout_deltas.index:
                            try:
                                checkout_delta = int(checkout_deltas.at[matched_api_idx])
                            except Exception:
                                checkout_delta = None
                        print(
                            f"✅ Matched by GST fallback -> API row {matched_api_idx + 1} | "
                            f"hbd_gst_match={hbd_gst_match} | client_gst_match={client_gst_match}"
                        )
                        if len(matched_api_indices) > 1:
                            print(
                                f"   ℹ️  Amount gate will sum {len(matched_api_indices)} GST-matched API row(s)"
                            )
                except Exception:
                    pass

        if matched_api_idx is None:
            unmatched_rows += 1
            print("❌ No API match found for this extracted row.")
            extracted_df.at[extracted_idx, MATCHED_STATUS_COLUMN] = "unmatched"
            extracted_df.at[extracted_idx, "amount_gate_matched_booking_codes"] = None
            extracted_df.at[extracted_idx, "amount_difference"] = None
            _rid = _get_row_id(erow)
            if _rid is not None and _mysql_engine is not None:
                update_matched_status_in_mysql(
                    _rid, "unmatched", amount_difference=None, engine=_mysql_engine
                )
                update_pushed_at_in_mysql(_rid, _mysql_engine)
            continue

        # 4) Update output columns next to invoice report fields (EXTRACTED Excel).
        extracted_df.at[extracted_idx, MATCHED_STATUS_COLUMN] = "matched"
        extracted_df.at[extracted_idx, "match_booking_code"] = match_booking_code
        extracted_df.at[extracted_idx, "hbd_gst_match"] = hbd_gst_match
        extracted_df.at[extracted_idx, "client_gst_match"] = client_gst_match
        extracted_df.at[extracted_idx, "excel_match_guest_score"] = guest_score
        extracted_df.at[extracted_idx, "excel_match_checkin_delta"] = checkin_delta
        extracted_df.at[extracted_idx, "excel_match_checkout_delta"] = checkout_delta

        # Run amount gate on the API row(s) from the match above (no duplicate search).
        # Cache result for the push block (same verdict drives cancel/push).
        gate_ok, gate_reason, gate_debug = _amount_gate_for_row(
            erow, api_df, extracted_map, api_map,
            extracted_hbd_col, extracted_client_col,
            extracted_amount_col, api_balance_col,
            extracted_hotel_col, api_property_col,
            matched_api_indices=matched_api_indices,
            match_path=match_type,
        )
        gate_results_by_idx[int(extracted_idx)] = (gate_ok, gate_reason, gate_debug)
        gate_indices_by_idx[int(extracted_idx)] = list(matched_api_indices)
        gate_match_path_by_idx[int(extracted_idx)] = match_type
        if gate_debug.get("skipped") and gate_debug.get("reason") == "mmt_flag":
            amount_match = None
        elif gate_debug.get("mysql") is None:
            amount_match = None
        else:
            amount_match = 1 if gate_ok else 0
        amount_difference = _amount_difference_from_gate(gate_debug)
        extracted_df.at[extracted_idx, "amount_match"] = amount_match
        extracted_df.at[extracted_idx, "amount_difference"] = amount_difference
        _gate_codes = gate_debug.get("gate_matched_booking_codes") or []
        extracted_df.at[extracted_idx, "amount_gate_matched_booking_codes"] = (
            ", ".join(_gate_codes) if _gate_codes else ""
        )
        if gate_debug.get("skipped") and gate_debug.get("reason") == "mmt_flag":
            print(
                f"   🧮 AMOUNT GATE (matching) -> skipped for MMT (mmt_flag=1); "
                f"amount_match={amount_match}"
            )
        else:
            print(
                f"   🧮 AMOUNT GATE (matching) -> ok={gate_ok} path={gate_debug.get('path')} "
                f"matched_api_rows={gate_debug.get('count')} "
                f"property_filter={gate_debug.get('property_filter_applied')} "
                f"(base={gate_debug.get('property_base_count')} after={gate_debug.get('property_after_count')}) "
                f"booking_codes={gate_debug.get('gate_matched_booking_codes')!r} "
                f"sum={gate_debug.get('sum') if gate_debug.get('sum') is not None else 'N/A'} "
                f"mysql_amount={gate_debug.get('mysql') if gate_debug.get('mysql') is not None else 'N/A'} "
                f"diff={gate_debug.get('diff') if gate_debug.get('diff') is not None else 'N/A'} "
                f"tolerance={AMOUNT_GATE_TOLERANCE_INR} -> amount_match={amount_match} "
                f"amount_difference={amount_difference if amount_difference is not None else 'N/A'}"
            )

        remarks = generate_matching_remarks(
            match_type=match_type,
            booking_code_matched=match_booking_code if match_type == "booking_code" else None,
            hbd_gst_matched=hbd_gst_match if match_type == "gst" else None,
            client_gst_matched=client_gst_match if match_type == "gst" else None,
            guest_score=guest_score,
            checkin_delta=checkin_delta,
            checkout_delta=checkout_delta,
        )
        extracted_df.at[extracted_idx, "remarks"] = remarks
        print(
            f"🧾 Updated API row {matched_api_idx + 1} -> "
            f"matched_status='matched', "
            f"match_booking_code={match_booking_code}, amount_match={amount_match}, "
            f"amount_difference={amount_difference if amount_difference is not None else 'N/A'}, "
            f"remarks='{remarks}'"
        )

        _rid = _get_row_id(erow)
        if _rid is not None and _mysql_engine is not None:
            update_matched_status_in_mysql(
                _rid, "matched",
                match_booking_code=match_booking_code,
                hbd_gst_match=hbd_gst_match,
                client_gst_match=client_gst_match,
                amount_match=amount_match,
                amount_difference=amount_difference,
                remarks=remarks,
                engine=_mysql_engine,
            )
            update_excel_match_diagnostics_in_mysql(
                _rid, guest_score, checkin_delta, checkout_delta, _mysql_engine
            )

        room_val = _room_captured_from_matched_api_row(api_df, matched_api_idx)
        if _rid is not None and _mysql_engine is not None:
            update_room_captured_in_mysql(_rid, room_val, _mysql_engine)

        extracted_df.at[extracted_idx, "Room Captured"] = room_val if room_val is not None else ""
        erow = extracted_df.loc[extracted_idx]
        payload = _payload_from_extracted_row_only(erow)
        print(
            "📌 API push payload: MySQL/export row only; "
            "room captured copied from matched booking Excel into DB and this row."
        )

        selected_for_push.append((int(extracted_idx), matched_api_idx, payload))
        rows_updated += 1

        # Immediate push mode: push each matched row right away
        if push and immediate_push:
            push_attempts += 1
            _banner = "DRY RUN (immediate)" if dry_run else "IMMEDIATE PUSH"
            print(
                f"\n📤 {_banner} [{push_attempts}] -> extracted row {int(extracted_idx) + 1}, "
                f"API row {matched_api_idx + 1}"
            )
            # Mirror main pipeline: skip push when required fields are missing.
            invoice_number_keys = [
                "invoice_number", "Invoice Number", "Bill Number", "bill_number", "billNumber", "poNo"
            ]
            invoice_date_keys = [
                "invoice_date", "Invoice Date", "Bill Date", "bill_date", "billDate"
            ]
            invoice_number_val = _pick_first_present(payload, invoice_number_keys)
            invoice_date_val = _pick_first_present(payload, invoice_date_keys)
            amount_paid_val = payload.get("Amount Paid", payload.get("amount_paid", payload.get("total_amount")))

            push_skip_reasons = []
            if _bad_required_value(invoice_number_val):
                push_skip_reasons.append("missing invoice number")
            if _bad_required_value(invoice_date_val):
                push_skip_reasons.append("missing invoice date")
            if _bad_amount_paid(amount_paid_val):
                push_skip_reasons.append("missing amount")

            if push_skip_reasons:
                pf_msg = "; ".join(push_skip_reasons)
                print(
                    f"   ⏭️  Skipping API push for extracted row {int(extracted_idx) + 1}: {pf_msg} "
                    f"(invoice_number={invoice_number_val!r}, invoice_date={invoice_date_val!r}, amount={amount_paid_val!r})"
                )
                extracted_df.at[extracted_idx, "push_failed"] = pf_msg
                skipped_count += 1
                if _rid is not None and _mysql_engine is not None and not dry_run:
                    update_push_failed_in_mysql(_rid, pf_msg, _mysql_engine)
                    update_pushed_at_in_mysql(_rid, _mysql_engine)
            else:
                _cached = gate_results_by_idx.get(int(extracted_idx))
                if _cached is None:
                    _gate_indices = gate_indices_by_idx.get(int(extracted_idx)) or [int(matched_api_idx)]
                    _gate_path = gate_match_path_by_idx.get(int(extracted_idx), "matched_row")
                    gate_ok, gate_reason, gate_debug = _amount_gate_for_row(
                        erow, api_df, extracted_map, api_map,
                        extracted_hbd_col, extracted_client_col,
                        extracted_amount_col, api_balance_col,
                        extracted_hotel_col, api_property_col,
                        matched_api_indices=_gate_indices,
                        match_path=_gate_path,
                    )
                else:
                    gate_ok, gate_reason, gate_debug = _cached
                if not (gate_debug.get("skipped") and gate_debug.get("reason") == "mmt_flag"):
                    _gate_sum = gate_debug.get("sum")
                    _gate_diff = gate_debug.get("diff")
                    _gate_mysql = gate_debug.get("mysql")
                    print(
                        f"   🧮 AMOUNT GATE -> ok={gate_ok} path={gate_debug.get('path')} "
                        f"matched_api_rows={gate_debug.get('count')} "
                        f"property_filter={gate_debug.get('property_filter_applied')} "
                        f"booking_codes={gate_debug.get('gate_matched_booking_codes')!r} "
                        f"sum={_gate_sum if _gate_sum is not None else 'N/A'} "
                        f"mysql_amount={_gate_mysql if _gate_mysql is not None else 'N/A'} "
                        f"diff={_gate_diff if _gate_diff is not None else 'N/A'} "
                        f"tolerance={AMOUNT_GATE_TOLERANCE_INR}"
                    )
                if not gate_ok:
                    _verb = "Would cancel" if dry_run else "Cancelling"
                    print(
                        f"   ⏭️  {_verb} API push for extracted row {int(extracted_idx) + 1}: amount mismatch"
                    )
                    extracted_df.at[extracted_idx, MATCHED_STATUS_COLUMN] = "unmatched"
                    extracted_df.at[extracted_idx, "push_cancelled"] = "amount mismatch"
                    push_cancelled_count += 1
                    if _rid is not None and _mysql_engine is not None and not dry_run:
                        update_matched_status_in_mysql(_rid, "unmatched", engine=_mysql_engine)
                        update_push_cancelled_in_mysql(_rid, "amount mismatch", _mysql_engine)
                        update_pushed_at_in_mysql(_rid, _mysql_engine)
                else:
                    _apply_gst_backfill_before_push(
                        payload,
                        erow,
                        int(extracted_idx),
                        int(matched_api_idx),
                        api_df,
                        gate_debug,
                        gate_indices_by_idx,
                        extracted_hotel_col,
                        api_property_col,
                        api_hbd_gst_col,
                        api_client_gst_col,
                    )
                    if dry_run:
                        print(
                            f"   🟡 DRY RUN -> would push extracted row {int(extracted_idx) + 1}; "
                            f"skipping HTTP call and push-outcome MySQL writes"
                        )
                    else:
                        ok, api_err = push_row_to_api(payload, row_index=int(matched_api_idx))
                        if ok:
                            push_success += 1
                            extracted_df.at[extracted_idx, "push_failed"] = None
                            if _rid is not None and _mysql_engine is not None:
                                update_push_failed_in_mysql(_rid, None, _mysql_engine)
                                update_pushed_at_in_mysql(_rid, _mysql_engine)
                        else:
                            push_failed += 1
                            extracted_df.at[extracted_idx, "push_failed"] = api_err
                            if _rid is not None and _mysql_engine is not None:
                                update_push_failed_in_mysql(_rid, api_err, _mysql_engine)
                                update_pushed_at_in_mysql(_rid, _mysql_engine)
                                update_matched_status_in_mysql(_rid, "unmatched", engine=_mysql_engine)

    # 5) Save output file (updated EXTRACTED excel with matching results)
    if output_path:
        out_file = Path(output_path)
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_file = Path(__file__).resolve().parent / f"invoice_report_step7_no_mysql_{stamp}.xlsx"
    extracted_df.to_excel(out_file, index=False)

    print(f"\n✅ Rows matched: {rows_updated}")
    print(f"ℹ️  Rows unmatched: {unmatched_rows}")
    print(f"✅ Output file saved: {out_file}")

    # 6) Optional preview and/or API push
    if preview_push_json:
        print(f"\n🧪 PREVIEW MODE: Showing API JSON payload(s), no request sent")
        print("=" * 70)
        print(f"Endpoint: {API_PUSH_ENDPOINT}")
        print(f"Payload count: {len(selected_for_push)}")
        print("=" * 70)
        for idx, (_extracted_idx, api_idx, payload) in enumerate(selected_for_push, 1):
            _cached = gate_results_by_idx.get(int(_extracted_idx))
            if _cached:
                _gate_ok, _, _gate_debug = _cached
                if _gate_ok:
                    _apply_gst_backfill_before_push(
                        payload,
                        extracted_df.loc[_extracted_idx],
                        int(_extracted_idx),
                        int(api_idx),
                        api_df,
                        _gate_debug,
                        gate_indices_by_idx,
                        extracted_hotel_col,
                        api_property_col,
                        api_hbd_gst_col,
                        api_client_gst_col,
                    )
            json_payload = _build_api_json_preview(payload)
            print(f"\n   [{idx}/{len(selected_for_push)}] Preview for row {api_idx + 1}")
            print("   " + "-" * 66)
            pretty = json.dumps(json_payload, indent=2, ensure_ascii=False)
            for line in pretty.splitlines():
                print(f"   {line}")

    if push and not immediate_push:
        if dry_run:
            print(f"\n📤 DRY RUN: VALIDATING MATCHED ROWS (no HTTP call, no push-outcome MySQL writes)")
        else:
            print(f"\n📤 PUSHING MATCHED ROWS TO API")
        print("=" * 70)
        print(f"   📊 Found {len(selected_for_push)} matched row(s) to push")
        if dry_run:
            print(f"   🟡 Dry run mode -- gate verdict only, no requests, no MySQL push state\n")
        else:
            print(f"   📤 Pushing rows to API...\n")

        skipped_count = 0
        for idx, (extracted_idx, api_idx, payload) in enumerate(selected_for_push, 1):
            print(f"   [{idx}/{len(selected_for_push)}] Processing extracted row {int(extracted_idx) + 1} (API row {api_idx + 1})...")

            invoice_number_keys = [
                "invoice_number", "Invoice Number", "Bill Number", "bill_number", "billNumber", "poNo"
            ]
            invoice_date_keys = [
                "invoice_date", "Invoice Date", "Bill Date", "bill_date", "billDate"
            ]
            invoice_number_val = _pick_first_present(payload, invoice_number_keys)
            invoice_date_val = _pick_first_present(payload, invoice_date_keys)
            amount_paid_val = payload.get("Amount Paid", payload.get("amount_paid", payload.get("total_amount")))

            push_skip_reasons = []
            if _bad_required_value(invoice_number_val):
                push_skip_reasons.append("missing invoice number")
            if _bad_required_value(invoice_date_val):
                push_skip_reasons.append("missing invoice date")
            if _bad_amount_paid(amount_paid_val):
                push_skip_reasons.append("missing amount")

            _batch_rid = _get_row_id(extracted_df.loc[extracted_idx]) if extracted_idx in extracted_df.index else None

            if push_skip_reasons:
                pf_msg = "; ".join(push_skip_reasons)
                print(
                    f"   ⏭️  Skipping API push for extracted row {int(extracted_idx) + 1}: {pf_msg} "
                    f"(invoice_number={invoice_number_val!r}, invoice_date={invoice_date_val!r}, amount={amount_paid_val!r})"
                )
                extracted_df.at[extracted_idx, "push_failed"] = pf_msg
                skipped_count += 1
                if _batch_rid is not None and _mysql_engine is not None and not dry_run:
                    update_push_failed_in_mysql(_batch_rid, pf_msg, _mysql_engine)
                    update_pushed_at_in_mysql(_batch_rid, _mysql_engine)
            else:
                erow_batch = extracted_df.loc[extracted_idx]
                _cached = gate_results_by_idx.get(int(extracted_idx))
                if _cached is None:
                    _gate_indices = gate_indices_by_idx.get(int(extracted_idx)) or [int(api_idx)]
                    _gate_path = gate_match_path_by_idx.get(int(extracted_idx), "matched_row")
                    gate_ok, gate_reason, gate_debug = _amount_gate_for_row(
                        erow_batch, api_df, extracted_map, api_map,
                        extracted_hbd_col, extracted_client_col,
                        extracted_amount_col, api_balance_col,
                        extracted_hotel_col, api_property_col,
                        matched_api_indices=_gate_indices,
                        match_path=_gate_path,
                    )
                else:
                    gate_ok, gate_reason, gate_debug = _cached
                if not (gate_debug.get("skipped") and gate_debug.get("reason") == "mmt_flag"):
                    _gate_sum = gate_debug.get("sum")
                    _gate_diff = gate_debug.get("diff")
                    _gate_mysql = gate_debug.get("mysql")
                    print(
                        f"   🧮 AMOUNT GATE -> ok={gate_ok} path={gate_debug.get('path')} "
                        f"matched_api_rows={gate_debug.get('count')} "
                        f"property_filter={gate_debug.get('property_filter_applied')} "
                        f"booking_codes={gate_debug.get('gate_matched_booking_codes')!r} "
                        f"sum={_gate_sum if _gate_sum is not None else 'N/A'} "
                        f"mysql_amount={_gate_mysql if _gate_mysql is not None else 'N/A'} "
                        f"diff={_gate_diff if _gate_diff is not None else 'N/A'} "
                        f"tolerance={AMOUNT_GATE_TOLERANCE_INR}"
                    )
                if not gate_ok:
                    _verb = "Would cancel" if dry_run else "Cancelling"
                    print(
                        f"   ⏭️  {_verb} API push for extracted row {int(extracted_idx) + 1}: amount mismatch"
                    )
                    extracted_df.at[extracted_idx, MATCHED_STATUS_COLUMN] = "unmatched"
                    extracted_df.at[extracted_idx, "push_cancelled"] = "amount mismatch"
                    push_cancelled_count += 1
                    if _batch_rid is not None and _mysql_engine is not None and not dry_run:
                        update_matched_status_in_mysql(_batch_rid, "unmatched", engine=_mysql_engine)
                        update_push_cancelled_in_mysql(_batch_rid, "amount mismatch", _mysql_engine)
                        update_pushed_at_in_mysql(_batch_rid, _mysql_engine)
                else:
                    _apply_gst_backfill_before_push(
                        payload,
                        erow_batch,
                        int(extracted_idx),
                        int(api_idx),
                        api_df,
                        gate_debug,
                        gate_indices_by_idx,
                        extracted_hotel_col,
                        api_property_col,
                        api_hbd_gst_col,
                        api_client_gst_col,
                    )
                    if dry_run:
                        print(
                            f"   🟡 DRY RUN -> would push extracted row {int(extracted_idx) + 1} "
                            f"(API row {int(api_idx) + 1}); skipping HTTP call and push-outcome MySQL writes"
                        )
                    else:
                        ok, api_err = push_row_to_api(payload, row_index=int(api_idx))
                        if ok:
                            push_success += 1
                            extracted_df.at[extracted_idx, "push_failed"] = None
                            if _batch_rid is not None and _mysql_engine is not None:
                                update_push_failed_in_mysql(_batch_rid, None, _mysql_engine)
                                update_pushed_at_in_mysql(_batch_rid, _mysql_engine)
                        else:
                            push_failed += 1
                            extracted_df.at[extracted_idx, "push_failed"] = api_err
                            if _batch_rid is not None and _mysql_engine is not None:
                                update_push_failed_in_mysql(_batch_rid, api_err, _mysql_engine)
                                update_pushed_at_in_mysql(_batch_rid, _mysql_engine)
                                update_matched_status_in_mysql(_batch_rid, "unmatched", engine=_mysql_engine)
                        push_attempts += 1
        if dry_run:
            print(f"\n   📊 API Dry-Run Summary:")
            _would_push = max(len(selected_for_push) - skipped_count - push_cancelled_count, 0)
            print(f"      🟡 Would push (gate ok, HTTP skipped): {_would_push} row(s)")
        else:
            print(f"\n   📊 API Push Summary:")
            print(f"      ✅ Successfully pushed: {push_success} row(s)")
        if skipped_count > 0:
            print(f"      ⏭️  Skipped (missing invoice fields or invalid amount): {skipped_count} row(s)")
        if push_cancelled_count > 0:
            _cancel_label = "Would cancel (amount mismatch)" if dry_run else "Cancelled (amount mismatch)"
            print(f"      🚫 {_cancel_label}: {push_cancelled_count} row(s)")
        if not dry_run and push_failed > 0:
            print(f"      ❌ Failed: {push_failed} row(s)")
        print(f"      📈 Total: {len(selected_for_push)} row(s)")
    elif push and immediate_push:
        if dry_run:
            print(f"\n📊 API Dry-Run Summary (Immediate Mode):")
            _would_push = max(push_attempts - skipped_count - push_cancelled_count, 0)
            print(f"   🟡 Would push (gate ok, HTTP skipped): {_would_push} row(s)")
        else:
            print(f"\n📊 API Push Summary (Immediate Mode):")
            print(f"   ✅ Successfully pushed: {push_success} row(s)")
        if skipped_count > 0:
            print(f"   ⏭️  Skipped (missing invoice fields or invalid amount): {skipped_count} row(s)")
        if push_cancelled_count > 0:
            _cancel_label = "Would cancel (amount mismatch)" if dry_run else "Cancelled (amount mismatch)"
            print(f"   🚫 {_cancel_label}: {push_cancelled_count} row(s)")
        if not dry_run and push_failed > 0:
            print(f"   ❌ Failed: {push_failed} row(s)")
        print(f"   📈 Total attempted: {push_attempts} row(s)")
    else:
        print("\nℹ️  API push skipped (use --push to enable).")


def main():
    parser = argparse.ArgumentParser(
        description="Generate API Excel from test1.py and run Step-7 matching flow without MySQL."
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Optional output file path for updated extracted Excel.",
    )
    parser.add_argument(
        "--push",
        action="store_true",
        help="Actually push selected rows to API using legacy push_row_to_api().",
    )
    parser.add_argument(
        "--immediate-push",
        action="store_true",
        help="Push each row immediately after it is matched (match -> push -> next).",
    )
    parser.add_argument(
        "--preview-push-json",
        action="store_true",
        help="Preview final API JSON payload(s) for matched rows without sending requests.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "When combined with --push (or --push --immediate-push), run the amount "
            "gate and print verdicts but do NOT call push_row_to_api(), and skip "
            "push-outcome MySQL writes (push_failed / push_cancelled / pushed_at, "
            "and the matched_status='unmatched' overwrite from gate failures). "
            "Excel writes still reflect the gate verdict so the report file shows "
            "what would have happened. Matching-loop diagnostics "
            "(matched_status='matched', match_*, excel_match_*, room_captured) "
            "remain unaffected. Re-runnable: matched rows stay pushed_at=0/NULL."
        ),
    )
    args = parser.parse_args()

    run(
        output_path=args.out,
        push=args.push,
        preview_push_json=args.preview_push_json,
        immediate_push=args.immediate_push,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()

