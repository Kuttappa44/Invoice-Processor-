"""Excel/MySQL row -> API payload mapping and push orchestration. HTTP stays in invoice_push_api."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from backend.app.integrations.invoice_push_api import (
    API_PUSH_ENDPOINT,
    get_api_bearer_token,
    post_invoice_payload,
)
from backend.app.integrations.azure_blob import sanitize_blob_url

def _stringify_api_scalar(val):
    """
    Convert a cell value to string for API JSON. Excel/pandas often use float for
    integers (e.g. 1234.0) — avoid sending '1234.0' when the value is whole.
    Also normalizes string forms like '1234.0' for numeric-like text.
    """
    if val is None:
        return None
    if isinstance(val, bool):
        return str(val)
    if isinstance(val, str):
        s = val.strip()
        if re.fullmatch(r"-?\d+\.0+", s):
            return s.split(".")[0]
        return s
    try:
        if pd.isna(val):
            return ""
    except (TypeError, ValueError):
        pass
    try:
        fv = float(val)
    except (TypeError, ValueError):
        return str(val)
    try:
        if fv != fv:  # NaN
            return ""
    except Exception:
        pass
    try:
        iv = int(fv)
        if fv == iv:
            return str(iv)
    except (ValueError, OverflowError):
        pass
    return str(fv)


def push_row_to_api(row_data: dict, row_index: int):
    """
    Push a single row's data to the API endpoint.
    Only sends fields defined in api.json, mapping Excel columns to API fields.

    Args:
        row_data: Dictionary containing all row data (column names as keys)
        row_index: Row index (0-based) for logging purposes

    Returns:
        (True, None) on success.
        (False, error_message: str) on failure — error_message is the "message"
        field from the API response body when available, otherwise a short
        description of the failure (e.g. "Request timeout", "HTTP 500").
    """
    try:
        # Load API field definitions from api.json (absolute path)
        api_json_path = "/home/ubuntu/Invoice-processor/invoice/api.json"
        try:
            with open(api_json_path, 'r', encoding='utf-8') as f:
                api_schema = json.load(f)
            api_fields = list(api_schema.keys())
        except (FileNotFoundError, json.JSONDecodeError) as e:
            print(f"      ⚠️  Warning: Could not load api.json: {e}")
            print(f"      💡 Using default field mapping")
            # Fallback to default API fields if api.json not found
            api_fields = [
                'hotel_name', 'invoice_date', 'checkin_date', 'checkout_date', 'guest_name',
                'invoice_number', 'hotel_gst_number', 'hb_gst_number', 'bookingcode',
                'roomcaptured', 'room_charge', 'cgst', 'sgst', 'igst', 'total_tariff',
                'other_charges', 'total_cgst', 'total_sgst', 'total_igst', 'total_charges',
                'total_taxes', 'discount', 'advance_amount', 'total_amount', 'remarks', 'file_name', 'file_location'
            ]
        
        # Mapping from Excel column names to API field names
        # Supports both Title Case (from invoice processing) and camelCase (from generated Excel/test.py)
        excel_to_api_mapping = {
            # Title Case mappings (from invoice processing)
            'Hotel Name': 'hotel_name',
            'Bill Date': 'invoice_date',
            'Check-in Date': 'checkin_date',
            'Check-out Date': 'checkout_date',
            'Checkout Date': 'checkout_date',  # Alternative column name
            'Guest Name': 'guest_name',
            'Bill Number': 'invoice_number',
            'Client GST Number': 'hotel_gst_number',
            'HBD GST Number': 'hb_gst_number',
            'Booking Code': 'bookingcode',
            'CGST Number': 'cgst',
            'SGST Number': 'sgst',
            'Total CGST': 'total_cgst',
            'Total SGST': 'total_sgst',
            'Room Charge': 'room_charge',
            'Total Tariff': 'total_tariff',
            'Other Charges': 'other_charges',
            'Total IGST': 'total_igst',
            'Total Taxes': 'total_taxes',
            'Discount': 'discount',
            'Advance Received': 'advance_amount',
            'Amount Paid': 'total_amount',
            'PDF Filename': 'file_name',
            'Blob URL': 'file_location',
            'Remarks': 'remarks',
            'remarks': 'remarks',
            'Room Captured': 'roomcaptured',
            'room_captured': 'roomcaptured',
            'room captured': 'roomcaptured',
            # camelCase mappings (from generated Excel/test.py)
            'propertyName': 'hotel_name',
            'hotelName': 'hotel_name',
            'checkInDate': 'checkin_date',
            'checkOutDate': 'checkout_date',
            'guestName': 'guest_name',
            'bookingCode': 'bookingcode',
            'totalAmount': 'total_amount',
            'advanceAmount': 'advance_amount',
            'balanceAmount': 'balance_amount',
            'serviceAmount': 'service_amount',
            'poNo': 'invoice_number',
            'propertyReference': 'bookingcode',
            'roomCaptured': 'roomcaptured',  # Map roomCaptured from Excel to roomcaptured API field
            # Additional camelCase variations
            'billDate': 'invoice_date',
            'billNumber': 'invoice_number',
            'invoiceDate': 'invoice_date',
            'invoiceNumber': 'invoice_number',
            # Fields not in Excel will remain None
        }
        
        # DEBUG: Log available columns in row_data
        print(f"      🔍 DEBUG: Available columns in row_data:")
        print(f"         Total columns: {len(row_data)}")
        print(f"         Column names: {list(row_data.keys())}")
        print(f"      ")
        print(f"      🔍 DEBUG: Column mapping process:")
        
        # Get fresh token (check expiry before each API call)
        current_token = get_api_bearer_token()
        
        # Prepare headers
        headers = {
            'accept': 'text/plain',
            'Authorization': f'Bearer {current_token}',
            'Content-Type': 'application/json'
        }
        
        # Build JSON data with only API fields
        json_data = {}
        mapping_debug = []  # Store mapping debug info
        
        for api_field in api_fields:
            # Find corresponding Excel column
            excel_column = None
            for excel_col, api_field_name in excel_to_api_mapping.items():
                if api_field_name == api_field:
                    excel_column = excel_col
                    break
            
            # Get value from Excel data if column exists
            if excel_column:
                if excel_column in row_data:
                    value = row_data[excel_column]
                    mapping_debug.append(f"         ✅ {api_field} <- '{excel_column}': {repr(value)}")
                else:
                    # Column name not found in row_data - try multiple search strategies
                    value = None
                    found_col = None
                    
                    # Strategy 1: Case-insensitive exact match
                    for col in row_data.keys():
                        if str(col).strip().lower() == excel_column.lower():
                            found_col = col
                            value = row_data[col]
                            break
                    
                    # Strategy 2: If still not found, try camelCase to Title Case conversion
                    if not found_col:
                        # Convert camelCase to Title Case for matching
                        # e.g., "checkInDate" -> "Check In Date" or "Check-in Date"
                        camel_to_title_patterns = {
                            'checkInDate': ['checkInDate', 'checkIn', 'check-in date', 'check in date'],
                            'checkOutDate': ['checkOutDate', 'checkOut', 'check-out date', 'check out date'],
                            'guestName': ['guestName', 'guest name'],
                            'propertyName': ['propertyName', 'hotel name', 'property name'],
                            'bookingCode': ['bookingCode', 'booking code'],
                            'totalAmount': ['totalAmount', 'amount paid', 'total amount'],
                            'advanceAmount': ['advanceAmount', 'advance amount'],
                            'poNo': ['poNo', 'po no', 'bill number', 'invoice number'],
                        }
                        
                        # Check if excel_column matches any camelCase pattern
                        for camel_key, patterns in camel_to_title_patterns.items():
                            if excel_column.lower() in [p.lower() for p in patterns]:
                                # Try to find matching column in row_data
                                for col in row_data.keys():
                                    col_lower = str(col).strip().lower()
                                    if any(pattern.lower() in col_lower for pattern in patterns):
                                        found_col = col
                                        value = row_data[col]
                                        break
                                if found_col:
                                    break
                    
                    # Strategy 3: Fuzzy match by checking if key parts of the column name exist
                    if not found_col:
                        # Extract key words from excel_column (e.g., "Check-in Date" -> ["check", "in", "date"])
                        excel_keywords = [w.lower() for w in re.findall(r'\w+', str(excel_column))]
                        for col in row_data.keys():
                            col_keywords = [w.lower() for w in re.findall(r'\w+', str(col))]
                            # If most keywords match, use this column
                            matching_keywords = set(excel_keywords) & set(col_keywords)
                            if len(matching_keywords) >= 2 and len(matching_keywords) >= len(excel_keywords) * 0.6:
                                found_col = col
                                value = row_data[col]
                                break
                    
                    if found_col:
                        mapping_debug.append(f"         ⚠️  {api_field} <- '{excel_column}' (not found, used '{found_col}'): {repr(value)}")
                    else:
                        mapping_debug.append(f"         ❌ {api_field} <- '{excel_column}': NOT FOUND in row_data")
            else:
                # No mapping defined for this API field
                value = None
                mapping_debug.append(f"         ⚠️  {api_field}: No Excel column mapping defined")
            
            # Fields that should be "-1" instead of null when missing (numerical fields)
            numerical_fields_use_negative_one = [
                'bookingcode', 'roomcaptured', 'room_charge', 'cgst', 'sgst', 'igst',
                'total_tariff', 'other_charges', 'total_cgst', 'total_sgst', 'total_igst',
                'total_charges', 'total_taxes', 'discount', 'advance_amount', 'total_amount'
            ]
            
            # Handle NaN/None/empty values
            try:
                is_na = pd.isna(value)
            except (TypeError, ValueError):
                is_na = value is None or value == ''
            
            if is_na or value == '' or (isinstance(value, str) and value.strip() == ''):
                # For date fields, use "9999-12-12" when not found
                if api_field in ['invoice_date', 'checkin_date', 'checkout_date']:
                    json_data[api_field] = "9999-12-12"
                # For numerical fields, use "-1" instead of None
                elif api_field in numerical_fields_use_negative_one:
                    json_data[api_field] = "-1"
                else:
                    json_data[api_field] = "string"
                    if api_field == 'remarks':
                        print(f"         ⚠️  remarks not in row_data → using default 'string'")
            else:
                # Handle date fields - convert to YYYY-MM-DD format
                if api_field in ['invoice_date', 'checkin_date', 'checkout_date']:
                    try:
                        # Try to parse various date formats
                        date_str = str(value).strip()
                        if date_str and date_str.lower() != 'not found':
                            # Try common date formats
                            date_formats = [
                                '%Y-%m-%d',           # 2025-01-01
                                '%d/%m/%Y',           # 01/01/2025
                                '%d-%m-%Y',           # 01-01-2025
                                '%d/%m/%y',           # 01/01/25
                                '%d-%m-%y',           # 01-01-25
                                '%Y-%m-%d %H:%M:%S',  # 2025-01-01 12:00:00
                                '%d/%m/%Y %H:%M',     # 01/01/2025 12:00
                            ]
                            parsed_date = None
                            for fmt in date_formats:
                                try:
                                    parsed_date = datetime.strptime(date_str, fmt)
                                    break
                                except ValueError:
                                    continue
                            
                            # If pandas Timestamp, convert directly
                            if parsed_date is None and hasattr(value, 'strftime'):
                                parsed_date = value
                            
                            if parsed_date:
                                json_data[api_field] = parsed_date.strftime('%Y-%m-%d')
                            else:
                                json_data[api_field] = "9999-12-12"
                        else:
                            json_data[api_field] = "9999-12-12"
                    except Exception as e:
                        print(f"      ⚠️  Warning: Could not parse date for {api_field}: {value} - {e}")
                        json_data[api_field] = "9999-12-12"
                else:
                    # Convert to appropriate type for non-date fields
                    if isinstance(value, (int, float)):
                        # For numerical fields that should be strings per API schema, convert to string
                        if api_field in numerical_fields_use_negative_one:
                            json_data[api_field] = _stringify_api_scalar(value)
                        else:
                            json_data[api_field] = value
                    else:
                        # For numerical fields, check if value is "Not Found" or similar and use "-1"
                        value_str = str(value).strip()
                        if api_field in numerical_fields_use_negative_one:
                            if value_str.lower() in ['not found', 'nan', 'none', '']:
                                json_data[api_field] = "-1"
                            else:
                                json_data[api_field] = _stringify_api_scalar(value_str)
                        else:
                            json_data[api_field] = value_str
        
        # Print mapping debug info
        for debug_line in mapping_debug:
            print(debug_line)
        print(f"      ")

        # Ensure every value is a string (API expects all string fields per api.json)
        for k in json_data:
            if json_data[k] is not None and not isinstance(json_data[k], str):
                json_data[k] = _stringify_api_scalar(json_data[k])

        # Force these fields to "-1" — they are not extracted or tracked.
        # Fields now extracted from invoices (room_charge, total_tariff, etc.)
        # have been removed from this list so their real values pass through.
        _always_negative_one = [
            'cgst', 'sgst', 'igst', 'total_charges',
        ]
        for field in _always_negative_one:
            if field in json_data:
                json_data[field] = "-1"

        # Security: never send SAS tokens in file_location (Blob URL).
        try:
            if 'file_location' in json_data and json_data.get('file_location'):
                json_data['file_location'] = sanitize_blob_url(str(json_data['file_location']))
        except Exception:
            pass
        
        # Log request details with prominent data display
        print(f"      📋 Request Details:")
        print(f"         URL: {API_PUSH_ENDPOINT}")
        print(f"         Method: POST")
        print(f"         Headers:")
        print(f"            - accept: {headers.get('accept')}")
        print(f"            - Authorization: Bearer {'*' * 20}...{current_token[-10:] if current_token and len(current_token) > 10 else '***'}")
        print(f"            - Content-Type: {headers.get('Content-Type')}")
        print(f"      ")
        # Debug: log remarks value being sent to API
        remarks_val = json_data.get('remarks', '<not set>')
        print(f"      📝 API payload remarks: {repr(remarks_val)[:80]}{'...' if len(repr(remarks_val)) > 80 else ''}")
        print(f"      📦 DATA BEING PUSHED TO API:")
        print(f"      {'=' * 70}")
        # Show data in a table-like format for better readability
        print(f"         Field Name                          | Value")
        print(f"         {'-' * 70}")
        for api_field, field_value in sorted(json_data.items()):
            # Format the value for display
            if field_value is None:
                display_value = "None (null)"
            elif isinstance(field_value, str):
                # Truncate very long strings
                if len(field_value) > 50:
                    display_value = f"{field_value[:47]}..."
                else:
                    display_value = field_value
            elif isinstance(field_value, (int, float)):
                display_value = str(field_value)
            else:
                display_value = str(field_value)
            
            # Format field name to fit in column
            field_name_display = api_field.ljust(37)
            print(f"         {field_name_display} | {display_value}")
        print(f"      {'=' * 70}")
        print(f"      ")
        print(f"      📄 Complete JSON Payload:")
        # Print JSON payload in a readable format
        try:
            payload_str = json.dumps(json_data, indent=12, ensure_ascii=False)
            # Indent each line
            for line in payload_str.split('\n'):
                print(f"            {line}")
        except Exception as e:
            print(f"            {json_data}")
        print(f"      ")
        
        return post_invoice_payload(
            json_data,
            row_index=row_index,
            token=current_token,
            endpoint=API_PUSH_ENDPOINT,
        )

    except requests.exceptions.Timeout:
        print(f"      ❌ Row {row_index + 1} API push failed: Request timeout")
        return False, "Request timeout"
    except requests.exceptions.RequestException as e:
        print(f"      ❌ Row {row_index + 1} API push failed: {str(e)}")
        return False, f"Network error: {str(e)}"
    except Exception as e:
        print(f"      ❌ Row {row_index + 1} API push failed: Unexpected error: {str(e)}")
        import traceback
        traceback.print_exc()
        return False, f"Unexpected error: {str(e)}"

def _build_api_json_preview(row_data: dict[str, Any]) -> dict[str, Any]:
    """Build API payload JSON using the same mapping/default rules as push_row_to_api, without sending."""
    api_json_path = Path(__file__).resolve().parents[4] / "api.json"
    try:
        with open(api_json_path, "r", encoding="utf-8") as f:
            api_schema = json.load(f)
        api_fields = list(api_schema.keys())
    except Exception:
        api_fields = [
            "hotel_name",
            "invoice_date",
            "checkin_date",
            "checkout_date",
            "guest_name",
            "invoice_number",
            "hotel_gst_number",
            "hb_gst_number",
            "bookingcode",
            "roomcaptured",
            "room_charge",
            "cgst",
            "sgst",
            "igst",
            "total_tariff",
            "other_charges",
            "total_cgst",
            "total_sgst",
            "total_igst",
            "total_charges",
            "total_taxes",
            "discount",
            "advance_amount",
            "total_amount",
            "remarks",
            "file_name",
            "file_location",
        ]

    excel_to_api_mapping = {
        "Hotel Name": "hotel_name",
        "Bill Date": "invoice_date",
        "Check-in Date": "checkin_date",
        "Check-out Date": "checkout_date",
        "Checkout Date": "checkout_date",
        "Guest Name": "guest_name",
        "Bill Number": "invoice_number",
        "Client GST Number": "hotel_gst_number",
        "HBD GST Number": "hb_gst_number",
        "Booking Code": "bookingcode",
        "CGST Number": "cgst",
        "SGST Number": "sgst",
        "Total CGST": "total_cgst",
        "Total SGST": "total_sgst",
        "Room Charge": "room_charge",
        "Total Tariff": "total_tariff",
        "Other Charges": "other_charges",
        "Total IGST": "total_igst",
        "Total Taxes": "total_taxes",
        "Discount": "discount",
        "Advance Received": "advance_amount",
        "Amount Paid": "total_amount",
        "PDF Filename": "file_name",
        "Blob URL": "file_location",
        "Remarks": "remarks",
        "remarks": "remarks",
        "Room Captured": "roomcaptured",
        "propertyName": "hotel_name",
        "hotelName": "hotel_name",
        "checkInDate": "checkin_date",
        "checkOutDate": "checkout_date",
        "guestName": "guest_name",
        "bookingCode": "bookingcode",
        "totalAmount": "total_amount",
        "advanceAmount": "advance_amount",
        "balanceAmount": "balance_amount",
        "serviceAmount": "service_amount",
        "poNo": "invoice_number",
        "propertyReference": "bookingcode",
        "roomCaptured": "roomcaptured",
        "billDate": "invoice_date",
        "billNumber": "invoice_number",
        "invoiceDate": "invoice_date",
        "invoiceNumber": "invoice_number",
    }

    numerical_fields_use_negative_one = [
        "bookingcode",
        "roomcaptured",
        "room_charge",
        "cgst",
        "sgst",
        "igst",
        "total_tariff",
        "other_charges",
        "total_cgst",
        "total_sgst",
        "total_igst",
        "total_charges",
        "total_taxes",
        "discount",
        "advance_amount",
        "total_amount",
    ]

    json_data: dict[str, Any] = {}
    for api_field in api_fields:
        excel_column = None
        for excel_col, api_field_name in excel_to_api_mapping.items():
            if api_field_name == api_field:
                excel_column = excel_col
                break

        value = None
        if excel_column:
            if excel_column in row_data:
                value = row_data[excel_column]
            else:
                for col in row_data.keys():
                    if str(col).strip().lower() == excel_column.lower():
                        value = row_data[col]
                        break

        try:
            is_na = pd.isna(value)
        except Exception:
            is_na = value is None or value == ""

        if is_na or value == "" or (isinstance(value, str) and value.strip() == ""):
            if api_field in ["invoice_date", "checkin_date", "checkout_date"]:
                json_data[api_field] = "9999-12-12"
            elif api_field in numerical_fields_use_negative_one:
                json_data[api_field] = "-1"
            else:
                json_data[api_field] = "string"
            continue

        if api_field in ["invoice_date", "checkin_date", "checkout_date"]:
            date_str = str(value).strip()
            parsed_date = None
            for fmt in [
                "%Y-%m-%d",
                "%d/%m/%Y",
                "%d-%m-%Y",
                "%d/%m/%y",
                "%d-%m-%y",
                "%Y-%m-%d %H:%M:%S",
                "%d/%m/%Y %H:%M",
            ]:
                try:
                    parsed_date = datetime.strptime(date_str, fmt)
                    break
                except ValueError:
                    continue
            if parsed_date is None and hasattr(value, "strftime"):
                parsed_date = value
            json_data[api_field] = parsed_date.strftime("%Y-%m-%d") if parsed_date else "9999-12-12"
        else:
            if isinstance(value, (int, float)):
                json_data[api_field] = str(value) if api_field in numerical_fields_use_negative_one else value
            else:
                value_str = str(value).strip()
                if api_field in numerical_fields_use_negative_one:
                    if value_str.lower() in ["not found", "nan", "none", ""]:
                        json_data[api_field] = "-1"
                    else:
                        json_data[api_field] = value_str
                else:
                    json_data[api_field] = value_str

    _always_negative_one = ['cgst', 'sgst', 'igst', 'total_charges']
    for field in _always_negative_one:
        if field in json_data:
            json_data[field] = "-1"

    try:
        if 'file_location' in json_data and json_data.get('file_location'):
            json_data['file_location'] = sanitize_blob_url(str(json_data['file_location']))
    except Exception:
        pass

    return json_data

