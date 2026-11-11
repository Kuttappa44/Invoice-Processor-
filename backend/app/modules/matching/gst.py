"""
GST-based matching logic for invoices when booking code is not available.

This module provides matching functionality using GST numbers (HBD and Client),
guest name, and dates when booking code is missing from the invoice.
"""

import pandas as pd
import re
from typing import List, Optional, Tuple
from datetime import datetime
from backend.app.shared.dates import format_date_to_dd_mm_yy
from backend.app.shared.gstin import is_valid_gstin
from backend.app.shared.guest_names import guest_names_match_for_merge
from backend.app.shared.text import normalize_field_text
from backend.app.modules.matching.excel import find_matching_column


def normalize_gst_value(value):
    """Normalize GST number values for robust comparison (similar to normalize_booking_code_value)"""
    if pd.isna(value) or value is None:
        return ''
    text = str(value).strip()
    if not text:
        return ''
    # Remove quotes (same as booking code normalization)
    text = text.strip('"\'`')
    # Remove commas and spaces (same as booking code normalization)
    text = text.replace(',', '').replace(' ', '')
    # Handle numeric edge cases (same as booking code normalization)
    if re.fullmatch(r'\d+\.0+', text):
        text = text.split('.')[0]
    else:
        try:
            float_val = float(text)
            if float_val.is_integer():
                text = str(int(float_val))
        except Exception:
            pass
    # Keep uppercase (GST numbers are typically uppercase, unlike booking codes which are lowercase)
    return text.upper()


def _first_valid_gstin_from_field(raw_value) -> str:
    """Normalize and return the first semicolon/newline-separated substring that is a valid GSTIN."""
    if raw_value is None or (isinstance(raw_value, float) and pd.isna(raw_value)):
        return ""
    s = str(raw_value).strip()
    if not s or s.lower() in ("not found", "n/a", "na"):
        return ""
    parts = [s] + [p.strip() for p in re.split(r"[;\n]+", s) if p.strip()]
    seen = set()
    for part in parts:
        if part in seen:
            continue
        seen.add(part)
        n = normalize_gst_value(part)
        if n and is_valid_gstin(n):
            return n
    return ""


def match_by_gst_numbers(
    entry: dict,
    df: pd.DataFrame,
    field_to_column: dict,
    hbd_gst_value: str,
    client_gst_value: str
) -> Tuple[List, pd.Series, pd.Series, pd.Series, bool, bool]:
    """
    Match invoice entry to Excel rows using GST numbers instead of booking code.
    
    Matching Criteria:
    - HBD GST Number (exact match, case-insensitive)
    - Client GST Number (exact match, case-insensitive)
    - Guest Name (fuzzy match using RapidFuzz > 70)
    - Check-In OR Check-Out Date (with ±1 day tolerance)
    
    Args:
        entry: Dictionary containing invoice entry data
        df: DataFrame containing Excel data
        field_to_column: Mapping of field names to Excel column names
        hbd_gst_value: HBD GST number from invoice
        client_gst_value: Client GST number from invoice
    
    Returns:
        Tuple of (matching_row_indices, guest_scores, checkin_deltas, checkout_deltas, hbd_gst_matched, client_gst_matched)
        where hbd_gst_matched and client_gst_matched are True if at least one matching row has matching GST
    """
    guest_name_value = entry.get('Guest Name', '').strip()
    checkin_value = entry.get('Check-In Date', '').strip()
    checkout_value = entry.get('Check-Out Date', '').strip()
    
    hbd_gst_normalized = _first_valid_gstin_from_field(hbd_gst_value)
    client_gst_normalized = _first_valid_gstin_from_field(client_gst_value)
    if str(hbd_gst_value or "").strip() and not hbd_gst_normalized:
        print(f"   ⚠️  HBD GST value ignored (failed GSTIN validation): {hbd_gst_value!r}")
    if str(client_gst_value or "").strip() and not client_gst_normalized:
        print(f"   ⚠️  Client GST value ignored (failed GSTIN validation): {client_gst_value!r}")
    
    # Initialize result variables
    matching_row_indices = []
    guest_scores = pd.Series(dtype=object)
    checkin_deltas = pd.Series(dtype=object)
    checkout_deltas = pd.Series(dtype=object)
    
    # Check required fields
    required_fields = ['Guest Name', 'Check-In Date', 'Check-Out Date']
    missing_fields = [f for f in required_fields if f not in field_to_column]
    
    if missing_fields:
        print(f"   ⚠️  Cannot match by GST: Missing columns for {missing_fields}")
        return matching_row_indices, guest_scores, checkin_deltas, checkout_deltas, None, None
    
    # Check if GST columns exist in Excel
    # Try multiple variations: API column names (hbgstNumber, hotelGSTNumber) and standard names
    hbd_gst_col = (find_matching_column(df, 'hbd gst number') or 
                   find_matching_column(df, 'hbd gst') or
                   find_matching_column(df, 'hbgstNumber') or
                   find_matching_column(df, 'hbgst'))
    client_gst_col = (find_matching_column(df, 'client gst number') or 
                      find_matching_column(df, 'client gst') or
                      find_matching_column(df, 'hotelGSTNumber') or
                      find_matching_column(df, 'hotelGST') or
                      find_matching_column(df, 'hotel gst number'))
    
    if not hbd_gst_col or not client_gst_col:
        print(f"   ⚠️  Cannot match by GST: GST columns not found in Excel")
        print(f"      HBD GST column: {'✅' if hbd_gst_col else '❌'}")
        print(f"      Client GST column: {'✅' if client_gst_col else '❌'}")
        return matching_row_indices, guest_scores, checkin_deltas, checkout_deltas, None, None
    
    # Validate required values: at least one GST number is required (either is enough)
    if not hbd_gst_normalized and not client_gst_normalized:
        print(f"   ⚠️  Cannot match by GST: Missing GST numbers (need at least HBD or Client GST)")
        print(f"      HBD GST: {'✅' if hbd_gst_normalized else '❌'}")
        print(f"      Client GST: {'✅' if client_gst_normalized else '❌'}")
        return matching_row_indices, guest_scores, checkin_deltas, checkout_deltas, None, None
    
    if not guest_name_value or (not checkin_value and not checkout_value):
        print(f"   ⚠️  Cannot match by GST: Missing required fields")
        print(f"      Guest Name: {'✅' if guest_name_value else '❌'}")
        print(f"      Check-In Date: {'✅' if checkin_value else '❌'}")
        print(f"      Check-Out Date: {'✅' if checkout_value else '❌'}")
        return matching_row_indices, guest_scores, checkin_deltas, checkout_deltas, None, None
    
    print(f"   🔍 Matching by GST Numbers + Guest Name + (Check-In OR Check-Out Date ±1 day):")
    print(f"      HBD GST: '{hbd_gst_normalized}'")
    print(f"      Client GST: '{client_gst_normalized}'")
    print(f"      Guest Name: '{guest_name_value}'")
    print(f"      Check-In Date: '{checkin_value}'")
    print(f"      Check-Out Date: '{checkout_value}'")
    
    try:
        guest_col = field_to_column['Guest Name']
        checkin_col = field_to_column['Check-In Date']
        checkout_col = field_to_column['Check-Out Date']
        
        # Date matching helper functions (same as booking code matching)
        def _to_date_for_match(value):
            """Parse a date value into a date() for matching (supports many formats)."""
            if not value or pd.isna(value) or str(value).strip() in ("", "Not Found"):
                return None
            try:
                # Handle pandas Timestamp
                if hasattr(value, 'strftime'):
                    return value.date()
                # Use existing formatter
                normalized = format_date_to_dd_mm_yy(str(value).strip())
                if not normalized or normalized == 'Not Found':
                    return None
                return datetime.strptime(normalized, '%d/%m/%y').date()
            except Exception:
                return None

        def _remove_titles_only(name: str) -> str:
            """Remove titles from guest name (same logic as guest_names_match_for_merge)."""
            if not name:
                return ""
            titles = {"mr", "mrs", "ms", "miss", "dr", "prof", "sir", "madam"}
            text_value = normalize_field_text(name).strip()
            if not text_value:
                return ""
            tokens = text_value.split()
            while tokens:
                first = tokens[0].strip()
                first_key = first.rstrip(".").lower()
                if first_key in titles:
                    tokens.pop(0)
                    continue
                break
            return " ".join(tokens).strip()

        def _date_matches_or_pm1(excel_value, invoice_value) -> bool:
            """True if dates match exactly or within ±1 day."""
            d1 = _to_date_for_match(excel_value)
            d2 = _to_date_for_match(invoice_value)
            if not d1 or not d2:
                return False
            return abs((d1 - d2).days) <= 1
        
        # Condition 1: HBD GST match (exact, using same normalization as booking codes)
        def normalize_gst_for_match(gst_value):
            """Normalize GST number for matching (same logic as booking code normalization)."""
            return normalize_gst_value(gst_value)
        
        hbd_gst_series = df[hbd_gst_col].apply(normalize_gst_for_match)
        condition1 = hbd_gst_series == hbd_gst_normalized
        
        # Condition 2: Client GST match (exact, case-insensitive)
        client_gst_series = df[client_gst_col].apply(normalize_gst_for_match)
        condition2 = client_gst_series == client_gst_normalized
        
        # Condition 3: Guest Name match (using the same logic as guest_names_match_for_merge - RapidFuzz > 60)
        condition3_list = []
        guest_score_list = []
        for row_idx in df.index:
            excel_guest_name = str(df.at[row_idx, guest_col]).strip()
            # Use the same advanced guest name matching logic as used in merge
            guest_match = guest_names_match_for_merge(excel_guest_name, guest_name_value)
            condition3_list.append(guest_match)
            
            # Compute and store RapidFuzz score
            if excel_guest_name and guest_name_value:
                try:
                    from rapidfuzz import fuzz
                    a = _remove_titles_only(excel_guest_name).lower()
                    b = _remove_titles_only(guest_name_value).lower()
                    if a and b:
                        score = int(max(
                            fuzz.ratio(a, b),
                            fuzz.partial_ratio(a, b),
                            fuzz.token_sort_ratio(a, b),
                            fuzz.token_set_ratio(a, b),
                        ))
                    else:
                        score = None
                except Exception:
                    score = None
            else:
                score = None
            guest_score_list.append(score)
        
        condition3 = pd.Series(condition3_list, index=df.index)
        guest_scores = pd.Series(guest_score_list, index=df.index)
        if condition3.any():
            print(f"      💡 Using advanced guest name matching logic (same as merge)")
        
        # Condition 4: Check-In Date match (with ±1 day tolerance)
        condition4_list = []
        checkin_delta_list = []
        for row_idx in df.index:
            excel_checkin = df.at[row_idx, checkin_col]
            matches = _date_matches_or_pm1(excel_checkin, checkin_value) if checkin_value else False
            condition4_list.append(matches)
            
            # Compute and store check-in date delta
            if checkin_value:
                d_excel = _to_date_for_match(excel_checkin)
                d_invoice = _to_date_for_match(checkin_value)
                if d_excel and d_invoice:
                    delta = (d_invoice - d_excel).days
                    checkin_delta_list.append(delta if delta in (-1, 0, 1) else None)
                else:
                    checkin_delta_list.append(None)
            else:
                checkin_delta_list.append(None)
        
        condition4 = pd.Series(condition4_list, index=df.index)
        checkin_deltas = pd.Series(checkin_delta_list, index=df.index)
        
        # Condition 5: Check-Out Date match (with ±1 day tolerance)
        condition5_list = []
        checkout_delta_list = []
        for row_idx in df.index:
            excel_checkout = df.at[row_idx, checkout_col]
            matches = _date_matches_or_pm1(excel_checkout, checkout_value) if checkout_value else False
            condition5_list.append(matches)
            
            # Compute and store check-out date delta
            if checkout_value:
                d_excel = _to_date_for_match(excel_checkout)
                d_invoice = _to_date_for_match(checkout_value)
                if d_excel and d_invoice:
                    delta = (d_invoice - d_excel).days
                    checkout_delta_list.append(delta if delta in (-1, 0, 1) else None)
                else:
                    checkout_delta_list.append(None)
            else:
                checkout_delta_list.append(None)
        
        condition5 = pd.Series(condition5_list, index=df.index)
        checkout_deltas = pd.Series(checkout_delta_list, index=df.index)
        
        # Use each GST condition only when that GST was provided (avoid matching empty to empty)
        use_condition1 = condition1 if hbd_gst_normalized else pd.Series(False, index=df.index)
        use_condition2 = condition2 if client_gst_normalized else pd.Series(False, index=df.index)
        # Combined condition: (HBD GST OR Client GST) + Guest Name + (Check-In OR Check-Out Date ±1 day)
        combined_condition = (use_condition1 | use_condition2) & condition3 & (condition4 | condition5)
        matching_row_indices = df[combined_condition].index.tolist()
        
        # Debug: Show why matching failed if no matches found
        if not matching_row_indices:
            print(f"   ❌ No matching row found. Debugging match conditions...")
            
            # Check each condition separately
            hbd_gst_matches = df[condition1].index.tolist()
            client_gst_matches = df[condition2].index.tolist()
            guest_name_matches = df[condition3].index.tolist()
            checkin_matches = df[condition4].index.tolist()
            checkout_matches = df[condition5].index.tolist()
            date_matches = df[condition4 | condition5].index.tolist()
            
            print(f"      📊 Condition breakdown:")
            print(f"         HBD GST matches: {len(hbd_gst_matches)} row(s)")
            print(f"         Client GST matches: {len(client_gst_matches)} row(s)")
            print(f"         Guest Name matches: {len(guest_name_matches)} row(s)")
            print(f"         Check-In Date matches (±1 day): {len(checkin_matches)} row(s)")
            print(f"         Check-Out Date matches (±1 day): {len(checkout_matches)} row(s)")
            print(f"         Either date matches (±1 day): {len(date_matches)} row(s)")
        else:
            print(f"   ✅ Found {len(matching_row_indices)} row(s) matching: GST Numbers + Guest Name + (Check-In OR Check-Out Date ±1 day)")
        
        # Check if GST numbers matched in any of the matching rows
        hbd_gst_matched = False
        client_gst_matched = False
        
        if matching_row_indices:
            # Check if HBD GST matched in any matching row
            for row_idx in matching_row_indices:
                excel_hbd_gst = normalize_gst_for_match(df.at[row_idx, hbd_gst_col])
                if excel_hbd_gst == hbd_gst_normalized:
                    hbd_gst_matched = True
                    break
            
            # Check if Client GST matched in any matching row
            for row_idx in matching_row_indices:
                excel_client_gst = normalize_gst_for_match(df.at[row_idx, client_gst_col])
                if excel_client_gst == client_gst_normalized:
                    client_gst_matched = True
                    break
        else:
            # No matches found - check if GST numbers exist in Excel at all
            hbd_gst_exists = condition1.any()  # condition1 is HBD GST match condition
            client_gst_exists = condition2.any()  # condition2 is Client GST match condition
            
            # If GST exists in Excel but didn't match (due to other conditions), mark as not matched
            hbd_gst_matched = False if hbd_gst_exists else None  # None means GST not found in Excel
            client_gst_matched = False if client_gst_exists else None
    
    except Exception as e:
        print(f"   ❌ Error matching by GST numbers: {e}")
        import traceback
        traceback.print_exc()
        # Return None for GST match status on error
        hbd_gst_matched = None
        client_gst_matched = None
    
    return matching_row_indices, guest_scores, checkin_deltas, checkout_deltas, hbd_gst_matched, client_gst_matched

