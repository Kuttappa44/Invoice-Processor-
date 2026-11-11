"""Drive master matching and the old Excel matcher (pre-Step 7)."""

from __future__ import annotations

import os
from typing import Optional

import pandas as pd
from sqlalchemy import text

from backend.app.modules.matching.remarks import generate_matching_remarks
from backend.app.shared.dates import format_date_to_dd_mm_yy
from backend.app.shared.guest_names import guest_names_match_for_merge
from backend.app.shared.text import (
    normalize_booking_code_value,
    normalize_column_name,
    normalize_field_text,
)
from backend.app.modules.matching import constants as _config
from backend.app.modules.invoices.mysql import get_db_engine
from backend.app.modules.matching.excel import find_matching_column, read_matching_excel_file

MATCHING_EXCEL_FILE_PATH = os.getenv("EXCEL_FILE_PATH") or _config.MATCHING_EXCEL_FILE_PATH
MATCHING_FIELDS = _config.MATCHING_FIELDS
INVOICE_RECEIVED_COLUMN = _config.INVOICE_RECEIVED_COLUMN
INVOICE_RECEIVED_VALUE = _config.INVOICE_RECEIVED_VALUE
MYSQL_ROW_ID_COLUMN = _config.MYSQL_ROW_ID_COLUMN
EXCEL_MATCH_GUEST_SCORE_COLUMN = _config.EXCEL_MATCH_GUEST_SCORE_COLUMN
EXCEL_MATCH_CHECKIN_DELTA_COLUMN = _config.EXCEL_MATCH_CHECKIN_DELTA_COLUMN
EXCEL_MATCH_CHECKOUT_DELTA_COLUMN = _config.EXCEL_MATCH_CHECKOUT_DELTA_COLUMN

def create_or_get_excel_file(drive_uploader, cached_file_id=None):
    """DISABLED: Master Excel functionality removed - using MySQL only
    
    Args:
        drive_uploader: GoogleDriveUploader instance (not used)
        cached_file_id: Optional cached file ID (not used)
    
    Returns:
        None: Master Excel is disabled
    """
    # Master Excel disabled - return None
    print("📊 Master Excel disabled - using MySQL storage only")
    return None

def convert_row_for_mysql(row_dict):
    """Convert Excel row dictionary to MySQL-compatible format (delegates to invoice_processor.db)."""
    from backend.app.modules.invoices.mysql import convert_row_for_mysql as _db_convert_row_for_mysql

    mysql_row = _db_convert_row_for_mysql(row_dict)
    print(f"   📋 MySQL row — new extracted fields:")
    print(f"      room_charge: {mysql_row.get('room_charge')}")
    print(f"      total_tariff: {mysql_row.get('total_tariff')}")
    print(f"      other_charges: {mysql_row.get('other_charges')}")
    print(f"      total_igst: {mysql_row.get('total_igst')}")
    print(f"      total_taxes: {mysql_row.get('total_taxes')}")
    print(f"      discount: {mysql_row.get('discount')}")
    print(f"      advance_received: {mysql_row.get('advance_received')}")
    return mysql_row


def insert_row_to_mysql(row_dict, engine):
    """Insert a single row into MySQL invoice table (name from INVOICE_TABLE_NAME env).
    
    Returns:
        int: The inserted row's ID (primary key), or None if insertion failed
    """
    try:
        from backend.app.modules.invoices.mysql import get_invoice_table_name

        table_name = get_invoice_table_name()
        mysql_row = convert_row_for_mysql(row_dict)
        
        # Build INSERT statement with proper parameter binding
        columns = ', '.join(mysql_row.keys())
        placeholders = ', '.join([f':{key}' for key in mysql_row.keys()])
        
        insert_sql = f"""
        INSERT INTO {table_name} ({columns})
        VALUES ({placeholders})
        """

        with engine.connect() as conn:
            result = conn.execute(text(insert_sql), mysql_row)
            conn.commit()
            inserted_id = result.lastrowid
        print(f"   ✅ Inserted row to MySQL database (ID: {inserted_id}, S.No: {row_dict.get('S.No', 'N/A')})")
        return inserted_id
    except Exception as e:
        print(f"   ⚠️  Error inserting row to MySQL: {e}")
        import traceback
        traceback.print_exc()
        return None


def update_classification_flags_in_mysql(row_id, classification, engine=None):
    """Apply invoice-type classification flags to a MySQL row by id.

    - hummingbird -> matched_status='hummingbird_mail', pushed_at=1 (excluded from Step-7 push)
    - mmt         -> mmt_flag=1 (row still flows through Step-7)
    - b2b         -> matched_status='b2b_invoice', pushed_at=1 (excluded from Step-7 push)
    - food        -> matched_status='food_invoice', pushed_at=1 (excluded from Step-7 push)
    - not_invoice -> matched_status='not_invoice', pushed_at=1 (excluded from Step-7 push)
    - anything else -> no change (normal flow; matched_status/pushed_at untouched).

    Returns True only when a row was actually updated.
    """
    if row_id is None or not classification:
        return False
    c = str(classification).strip().lower()
    if c not in ("hummingbird", "mmt", "b2b", "food", "not_invoice"):
        return False
    try:
        if engine is None:
            engine = get_db_engine()
        from backend.app.modules.invoices.mysql import get_invoice_table_name

        table_name = get_invoice_table_name()

        def _ensure_column(column_name: str, alter_sql: str) -> None:
            check_sql = f"""
            SELECT COUNT(*) as col_count
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
            AND TABLE_NAME = '{table_name}'
            AND COLUMN_NAME = '{column_name}'
            """
            with engine.connect() as conn:
                exists = conn.execute(text(check_sql)).fetchone()[0] > 0
                if not exists:
                    print(f"   Adding '{column_name}' column to existing {table_name} table...")
                    conn.execute(text(alter_sql))
                    conn.commit()
                    print(f"   ✓ Added '{column_name}' column to {table_name} table")

        _ensure_column(
            "mmt_flag",
            f"ALTER TABLE {table_name} ADD COLUMN mmt_flag TINYINT NULL DEFAULT 0",
        )
        _ensure_column(
            "pushed_at",
            f"ALTER TABLE {table_name} ADD COLUMN pushed_at TINYINT DEFAULT 0",
        )

        if c == "hummingbird":
            update_sql = f"UPDATE {table_name} SET matched_status='hummingbird_mail', pushed_at=1 WHERE id=:row_id"
        elif c == "food":
            update_sql = f"UPDATE {table_name} SET matched_status='food_invoice', pushed_at=1 WHERE id=:row_id"
        elif c == "b2b":
            update_sql = f"UPDATE {table_name} SET matched_status='b2b_invoice', pushed_at=1 WHERE id=:row_id"
        elif c == "not_invoice":
            update_sql = f"UPDATE {table_name} SET matched_status='not_invoice', pushed_at=1 WHERE id=:row_id"
        else:  # mmt
            update_sql = f"UPDATE {table_name} SET mmt_flag=1 WHERE id=:row_id"

        with engine.connect() as conn:
            result = conn.execute(text(update_sql), {"row_id": int(row_id)})
            conn.commit()
            ok = result.rowcount > 0
        if ok:
            print(f"   🏷️  Classification flags applied to row ID {row_id}: {c}")
        else:
            print(f"   ⚠️  No row found with ID {row_id} to apply classification flags")
        return ok
    except Exception as e:
        print(f"   ⚠️  Error updating classification flags in MySQL: {e}")
        import traceback
        traceback.print_exc()
        return False


def update_matched_status_in_mysql(row_id, matched_status, match_booking_code=None, hbd_gst_match=None, client_gst_match=None, amount_match=None, remarks=None, engine=None):
    """Update matched_status in MySQL for a specific invoice by row ID (primary key)
    
    Args:
        row_id: The primary key (id) of the row to update
        matched_status: 'matched' or 'unmatched'
        match_booking_code: 0 if booking code matched, 1 if it didn't (optional)
        hbd_gst_match: 0 if HBD GST matched, 1 if it didn't, None if not checked (optional)
        client_gst_match: 0 if Client GST matched, 1 if it didn't, None if not checked (optional)
        amount_match: 0 if PDF total and API balanceAmount match, 1 if not, None if not compared (optional, reference only)
        remarks: JSON string with matching details (optional)
        engine: Optional database engine. If None, creates a new one.
    
    Returns:
        bool: True if successful, False otherwise
    """
    if matched_status not in ['matched', 'unmatched']:
        print(f"   ⚠️  Invalid matched_status: {matched_status}. Must be 'matched' or 'unmatched'")
        return False
    
    if row_id is None:
        print(f"   ⚠️  Invalid row_id: None. Cannot update matched_status")
        return False
    
    try:
        if engine is None:
            engine = get_db_engine()
        from backend.app.modules.invoices.mysql import get_invoice_table_name
        table_name = get_invoice_table_name()
        
        # Ensure match_booking_code column exists
        def _ensure_column(column_name: str, alter_sql: str) -> None:
            check_sql = f"""
            SELECT COUNT(*) as col_count
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
            AND TABLE_NAME = '{table_name}'
            AND COLUMN_NAME = '{column_name}'
            """
            with engine.connect() as conn:
                result = conn.execute(text(check_sql))
                exists = result.fetchone()[0] > 0
                if not exists:
                    print(f"   Adding '{column_name}' column to existing {table_name} table...")
                    conn.execute(text(alter_sql))
                    conn.commit()
                    print(f"   ✓ Added '{column_name}' column to {table_name} table")
        
        _ensure_column(
            "match_booking_code",
            f"ALTER TABLE {table_name} ADD COLUMN match_booking_code TINYINT NULL AFTER excel_match_checkout_delta",
        )
        _ensure_column(
            "hbd_gst_match",
            f"ALTER TABLE {table_name} ADD COLUMN hbd_gst_match TINYINT NULL AFTER match_booking_code",
        )
        _ensure_column(
            "client_gst_match",
            f"ALTER TABLE {table_name} ADD COLUMN client_gst_match TINYINT NULL AFTER hbd_gst_match",
        )
        _ensure_column(
            "remarks",
            f"ALTER TABLE {table_name} ADD COLUMN remarks TEXT NULL AFTER client_gst_match",
        )
        _ensure_column(
            "amount_match",
            f"ALTER TABLE {table_name} ADD COLUMN amount_match TINYINT NULL AFTER remarks",
        )

        # Build UPDATE statement
        update_fields = ["matched_status = :matched_status"]
        update_values = {
            'matched_status': matched_status,
            'row_id': int(row_id),
        }
        
        # Add match_booking_code if provided
        if match_booking_code is not None:
            match_booking_code_val = int(match_booking_code) if match_booking_code in (0, 1) else None
            if match_booking_code_val is not None:
                update_fields.append("match_booking_code = :match_booking_code")
                update_values["match_booking_code"] = match_booking_code_val
        
        # Add hbd_gst_match if provided
        if hbd_gst_match is not None:
            hbd_gst_match_val = int(hbd_gst_match) if hbd_gst_match in (0, 1) else None
            if hbd_gst_match_val is not None:
                update_fields.append("hbd_gst_match = :hbd_gst_match")
                update_values["hbd_gst_match"] = hbd_gst_match_val
        
        # Add client_gst_match if provided
        if client_gst_match is not None:
            client_gst_match_val = int(client_gst_match) if client_gst_match in (0, 1) else None
            if client_gst_match_val is not None:
                update_fields.append("client_gst_match = :client_gst_match")
                update_values["client_gst_match"] = client_gst_match_val
        
        # Add amount_match if provided (reference only: 0=match, 1=no match, None=not compared)
        if amount_match is not None and amount_match in (0, 1):
            update_fields.append("amount_match = :amount_match")
            update_values["amount_match"] = int(amount_match)
        
        # Add remarks if provided
        if remarks is not None:
            update_fields.append("remarks = :remarks")
            update_values["remarks"] = str(remarks)

        # Use primary key (id) for precise matching - ensures only the specific row is updated
        update_sql = f"""
        UPDATE {table_name}
        SET {', '.join(update_fields)}
        WHERE id = :row_id
        """
        
        with engine.connect() as conn:
            result = conn.execute(text(update_sql), update_values)
            conn.commit()
            rows_affected = result.rowcount
            
        if rows_affected > 0:
            print(f"   ✅ Updated matched_status to '{matched_status}' for row ID {row_id} ({rows_affected} row(s))")
            return True
        else:
            print(f"   ⚠️  No row found with ID {row_id} to update matched_status")
            return False
            
    except Exception as e:
        print(f"   ⚠️  Error updating matched_status in MySQL: {e}")
        import traceback
        traceback.print_exc()
        return False


def read_invoice_master_sheet_from_drive(drive_uploader):
    """DISABLED: Master Excel functionality removed - using MySQL only"""
    print("📊 Master Excel disabled - this function is no longer used")
    return None
    
    # Original code below (disabled)
    if not drive_uploader or not drive_uploader.service:
        print("❌ Google Drive service not available")
        return None
    
    try:
        print(f"\n📊 Reading Invoice Master Sheet from Google Drive...")
        print(f"   File ID: {INVOICE_MASTER_SHEET_ID}")
        
        # First, check the file type
        file_metadata = drive_uploader.service.files().get(
            fileId=INVOICE_MASTER_SHEET_ID,
            fields='id,name,mimeType'
        ).execute()
        
        file_mime_type = file_metadata.get('mimeType', '')
        file_name = file_metadata.get('name', 'Unknown')
        print(f"   📄 File: {file_name}")
        print(f"   📋 MIME Type: {file_mime_type}")
        
        # Download to temporary file
        temp_sheet_file = 'temp_invoice_master.xlsx'
        
        # Try export_media first (for Google Sheets)
        if file_mime_type == 'application/vnd.google-apps.spreadsheet':
            print(f"   🔄 Detected Google Sheets - exporting as Excel...")
            try:
                request = drive_uploader.service.files().export_media(
                    fileId=INVOICE_MASTER_SHEET_ID,
                    mimeType='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
                )
                with open(temp_sheet_file, 'wb') as f:
                    f.write(request.execute())
                print(f"   ✅ Exported Google Sheets as Excel")
            except Exception as export_error:
                print(f"   ⚠️  Export failed: {export_error}")
                # Try alternative: export as CSV then convert
                print(f"   🔄 Trying CSV export as fallback...")
                try:
                    request = drive_uploader.service.files().export_media(
                        fileId=INVOICE_MASTER_SHEET_ID,
                        mimeType='text/csv'
                    )
                    temp_csv_file = 'temp_invoice_master.csv'
                    with open(temp_csv_file, 'wb') as f:
                        f.write(request.execute())
                    # Read CSV and convert to Excel
                    df_csv = pd.read_csv(temp_csv_file)
                    df_csv.to_excel(temp_sheet_file, index=False, engine='openpyxl')
                    os.remove(temp_csv_file)
                    print(f"   ✅ Exported as CSV and converted to Excel")
                except Exception as csv_error:
                    print(f"   ❌ CSV export also failed: {csv_error}")
                    raise
        else:
            # For Excel files already in Drive, use get_media
            print(f"   🔄 Detected Excel file - downloading directly...")
            request = drive_uploader.service.files().get_media(
                fileId=INVOICE_MASTER_SHEET_ID
            )
            with open(temp_sheet_file, 'wb') as f:
                f.write(request.execute())
            print(f"   ✅ Downloaded Excel file from Drive")
        
        print(f"   ✅ Downloaded Invoice Master Sheet")
        
        # Read Excel file with pandas
        df = pd.read_excel(temp_sheet_file, engine='openpyxl', header=0)
        print(f"   ✅ Read {len(df)} rows from Invoice Master Sheet")
        print(f"   📋 Columns: {list(df.columns)}")
        
        # Extract matching fields from the sheet
        extracted_data_list = []
        
        # Find column mappings
        booking_code_col = None
        guest_name_col = None
        hotel_name_col = None
        checkin_col = None
        checkout_col = None
        
        # Map columns (case-insensitive)
        for col in df.columns:
            col_lower = str(col).strip().lower()
            if 'booking code' in col_lower or 'booking' in col_lower:
                booking_code_col = col
            elif 'guest name' in col_lower or ('guest' in col_lower and 'name' in col_lower):
                guest_name_col = col
            elif ('hotel' in col_lower or 'property' in col_lower) and 'name' in col_lower:
                hotel_name_col = col
            elif 'check-in' in col_lower or 'check in' in col_lower or 'arrival' in col_lower:
                checkin_col = col
            elif 'check-out' in col_lower or 'check out' in col_lower or 'departure' in col_lower:
                checkout_col = col
        
        print(f"\n   🔍 Column mappings found:")
        print(f"      Booking Code: {booking_code_col}")
        print(f"      Guest Name: {guest_name_col}")
        print(f"      Hotel Name: {hotel_name_col}")
        print(f"      Check-In Date: {checkin_col}")
        print(f"      Check-Out Date: {checkout_col}")
        
        # Extract data from each row
        for idx, row in df.iterrows():
            # Skip empty rows
            if pd.isna(row.get(booking_code_col if booking_code_col else '')) and \
               pd.isna(row.get(guest_name_col if guest_name_col else '')):
                continue
            
            entry = {
                'Booking Code': str(row[booking_code_col]).strip() if booking_code_col and not pd.isna(row.get(booking_code_col)) else '',
                'Guest Name': str(row[guest_name_col]).strip() if guest_name_col and not pd.isna(row.get(guest_name_col)) else '',
                'Hotel Name': str(row[hotel_name_col]).strip() if hotel_name_col and not pd.isna(row.get(hotel_name_col)) else '',
                'Check-In Date': str(row[checkin_col]).strip() if checkin_col and not pd.isna(row.get(checkin_col)) else '',
                'Check-Out Date': str(row[checkout_col]).strip() if checkout_col and not pd.isna(row.get(checkout_col)) else ''
            }
            
            # Clean up "Not Found" and "nan" values
            for key, value in entry.items():
                if value.lower() in ['not found', 'nan', 'none', '']:
                    entry[key] = ''
            
            # Only add if we have at least one field
            if any(entry.values()):
                extracted_data_list.append(entry)
        
        print(f"   ✅ Extracted {len(extracted_data_list)} entries from Invoice Master Sheet")
        
        # Clean up temp file
        try:
            import time
            time.sleep(0.3)
            if os.path.exists(temp_sheet_file):
                os.remove(temp_sheet_file)
        except:
            pass
        
        return extracted_data_list
        
    except Exception as e:
        print(f"❌ Error reading Invoice Master Sheet from Google Drive: {e}")
        import traceback
        traceback.print_exc()
        return None

def match_invoice_master_sheet_with_excel(drive_uploader):
    """
    DISABLED: Master Excel functionality removed - using MySQL only
    """
    print("📊 Master Excel disabled - this function is no longer used")
    return 0
    
    # Original code below (disabled)
    # Read Invoice Master Sheet from Google Drive
    extracted_data_list = read_invoice_master_sheet_from_drive(drive_uploader)
    if not extracted_data_list:
        print("⚠️  No data extracted from Invoice Master Sheet - skipping matching")
        return 0
    
    # Read local Excel file
    df = read_matching_excel_file()
    if df is None or df.empty:
        print("⚠️  Excel file is empty or invalid - skipping matching")
        return 0
    
    print(f"\n🔍 Matching Invoice Master Sheet data with local Excel file...")
    print(f"🔍 Excel file has {len(df)} rows and {len(df.columns)} columns")
    print(f"🔍 Excel columns: {list(df.columns)}")
    print(f"🔍 Extracted {len(extracted_data_list)} entries from Invoice Master Sheet")
    
    # Create a mapping of field names to Excel columns
    field_to_column = {}
    for field in MATCHING_FIELDS:
        column = find_matching_column(df, field)
        if column:
            field_to_column[field] = column
            print(f"✅ Mapped '{field}' → '{column}'")
        else:
            print(f"⚠️  No matching column found for '{field}'")
    
    if not field_to_column:
        print("❌ No matching columns found. Cannot match Excel rows.")
        return 0
    
    # Check if 'Invoice Received' column exists, if not add it at the end
    column_was_added = False
    if INVOICE_RECEIVED_COLUMN not in df.columns:
        print(f"➕ Adding new column: '{INVOICE_RECEIVED_COLUMN}'")
        df[INVOICE_RECEIVED_COLUMN] = ''
        column_was_added = True
    else:
        print(f"✅ Column '{INVOICE_RECEIVED_COLUMN}' already exists")
        # FIX: Convert column to object dtype if it's numeric to allow string values
        if df[INVOICE_RECEIVED_COLUMN].dtype != 'object':
            print(f"   🔄 Converting '{INVOICE_RECEIVED_COLUMN}' column from {df[INVOICE_RECEIVED_COLUMN].dtype} to object dtype")
            # Convert to string, replacing NaN/None with empty string
            df[INVOICE_RECEIVED_COLUMN] = df[INVOICE_RECEIVED_COLUMN].astype(str).replace('nan', '').replace('None', '').replace('NaT', '')

    # Ensure MySQL Row ID column exists (Option A: write row_id into Excel so API push can join exactly)
    if MYSQL_ROW_ID_COLUMN not in df.columns:
        print(f"➕ Adding new column: '{MYSQL_ROW_ID_COLUMN}'")
        df[MYSQL_ROW_ID_COLUMN] = pd.Series(dtype='Int64', index=df.index)
    else:
        # Coerce to numeric if possible (keep NaN for empty)
        try:
            df[MYSQL_ROW_ID_COLUMN] = pd.to_numeric(df[MYSQL_ROW_ID_COLUMN], errors='coerce').astype('Int64')
        except Exception:
            pass
    
    rows_updated = 0
    rows_skipped = 0
    
    # Collect API push selection messages for Invoice Summary
    api_push_selection_messages = []  # List of (pdf_filename, is_selected, already_marked) tuples
    
    # Process each extracted data entry
    for idx, entry in enumerate(extracted_data_list):
        if not entry or not any(entry.values()):
            continue
        
        print(f"\n📋 Processing entry {idx+1}:")
        for field, value in entry.items():
            if value:
                print(f"   {field}: {value}")
        
        matching_row_indices = []
        
        # Single step: Match by Booking Code + Guest Name + Check-In Date + Check-Out Date (all must match)
        required_fields = ['Booking Code', 'Guest Name', 'Check-In Date', 'Check-Out Date']
        missing_fields = [f for f in required_fields if f not in field_to_column]
        
        if missing_fields:
            print(f"   ⚠️  Cannot match: Missing columns for {missing_fields}")
            continue
        
        # Get values for all four fields
        booking_code_value_raw = entry.get('Booking Code', '')
        booking_code_value = booking_code_value_raw.strip()
        guest_name_value = entry.get('Guest Name', '').strip()
        checkin_value = entry.get('Check-In Date', '').strip()
        checkout_value = entry.get('Check-Out Date', '').strip()
        
        # All four must be present
        if not booking_code_value or not guest_name_value or not checkin_value or not checkout_value:
            print(f"   ⚠️  Cannot match: Missing required fields")
            print(f"      Booking Code: {'✓' if booking_code_value else '✗'}")
            print(f"      Guest Name: {'✓' if guest_name_value else '✗'}")
            print(f"      Check-In Date: {'✓' if checkin_value else '✗'}")
            print(f"      Check-Out Date: {'✓' if checkout_value else '✗'}")
            continue
        
        print(f"   🔍 Matching by all four fields:")
        print(f"      Booking Code: '{booking_code_value}'")
        print(f"      Guest Name: '{guest_name_value}'")
        print(f"      Check-In Date: '{checkin_value}'")
        print(f"      Check-Out Date: '{checkout_value}'")
        
        try:
            booking_code_col = field_to_column['Booking Code']
            guest_col = field_to_column['Guest Name']
            checkin_col = field_to_column['Check-In Date']
            checkout_col = field_to_column['Check-Out Date']
            
            # Normalize date formats for matching
            def normalize_date_for_match(date_str):
                """Normalize date string for comparison"""
                if pd.isna(date_str) or not date_str:
                    return '', ''
                
                # Handle pandas Timestamp
                if hasattr(date_str, 'strftime'):
                    try:
                        format1 = date_str.strftime('%d-%b-%Y').lower()
                        format2 = date_str.strftime('%d/%m/%Y').lower()
                        return format1, format2
                    except:
                        date_str_lower = str(date_str).lower()
                        return date_str_lower, date_str_lower
                
                date_str = str(date_str).strip().lower()
                format1 = date_str.replace('/', '-').replace(' ', '-').replace('.', '-')
                format2 = date_str
                return format1, format2
            
            # Normalize booking code
            normalized_entry_code = normalize_booking_code_value(booking_code_value_raw)
            
            # Normalize extracted dates
            checkin_norm, checkin_norm2 = normalize_date_for_match(checkin_value)
            checkout_norm, checkout_norm2 = normalize_date_for_match(checkout_value)
            
            # Condition 1: Booking Code matching (normalized)
            normalized_series = df[booking_code_col].apply(normalize_booking_code_value)
            condition1 = normalized_series == normalized_entry_code
            
            # Condition 2: Guest Name matching (case-insensitive, exact or substring)
            condition2 = df[guest_col].astype(str).str.strip().str.lower() == guest_name_value.lower()
            if not condition2.any():
                condition2 = df[guest_col].astype(str).str.strip().str.lower().str.contains(
                    guest_name_value.lower(), na=False, regex=False
                )
            
            # Condition 3: Check-In Date matching
            condition3_list = []
            for row_idx in df.index:
                excel_checkin = df.at[row_idx, checkin_col]
                excel_checkin_norm, excel_checkin_norm2 = normalize_date_for_match(excel_checkin)
                
                if (checkin_norm == excel_checkin_norm or checkin_norm == excel_checkin_norm2 or
                    checkin_norm2 == excel_checkin_norm or checkin_norm2 == excel_checkin_norm2):
                    condition3_list.append(True)
                elif checkin_norm and excel_checkin_norm:
                    checkin_clean = checkin_norm.replace('-', '').replace('/', '').replace(' ', '')
                    excel_clean = excel_checkin_norm.replace('-', '').replace('/', '').replace(' ', '')
                    if checkin_clean in excel_clean or excel_clean in checkin_clean:
                        condition3_list.append(True)
                    else:
                        condition3_list.append(False)
                else:
                    condition3_list.append(False)
            
            condition3 = pd.Series(condition3_list, index=df.index)
            
            # Condition 4: Check-Out Date matching
            condition4_list = []
            for row_idx in df.index:
                excel_checkout = df.at[row_idx, checkout_col]
                excel_checkout_norm, excel_checkout_norm2 = normalize_date_for_match(excel_checkout)
                
                if (checkout_norm == excel_checkout_norm or checkout_norm == excel_checkout_norm2 or
                    checkout_norm2 == excel_checkout_norm or checkout_norm2 == excel_checkout_norm2):
                    condition4_list.append(True)
                elif checkout_norm and excel_checkout_norm:
                    checkout_clean = checkout_norm.replace('-', '').replace('/', '').replace(' ', '')
                    excel_clean = excel_checkout_norm.replace('-', '').replace('/', '').replace(' ', '')
                    if checkout_clean in excel_clean or excel_clean in checkout_clean:
                        condition4_list.append(True)
                    else:
                        condition4_list.append(False)
                else:
                    condition4_list.append(False)
            
            condition4 = pd.Series(condition4_list, index=df.index)
            
            # All four conditions must match
            combined_condition = condition1 & condition2 & condition3 & condition4
            matching_row_indices = df[combined_condition].index.tolist()
            
            if matching_row_indices:
                print(f"   ✅ Found {len(matching_row_indices)} row(s) matching all four fields")
            else:
                print(f"   ⚠️  No row found matching Booking Code + Guest Name + Check-In Date + Check-Out Date")
        except Exception as e:
            print(f"   ❌ Error matching by four fields: {e}")
            import traceback
            traceback.print_exc()
            continue
        
        # Update matching rows
        if matching_row_indices:
            for row_idx in matching_row_indices:
                try:
                    # Check if already marked as "Received"
                    current_value = str(df.at[row_idx, INVOICE_RECEIVED_COLUMN]).strip()
                    if current_value.lower() == INVOICE_RECEIVED_VALUE.lower():
                        print(f"   ⏭️  Row {row_idx + 1} already marked as '{INVOICE_RECEIVED_VALUE}', skipping")
                        rows_skipped += 1
                        continue
                    
                    # Update the Invoice Received column
                    df.at[row_idx, INVOICE_RECEIVED_COLUMN] = INVOICE_RECEIVED_VALUE
                    rows_updated += 1
                    
                    # Build log info with all matching fields
                    log_parts = []
                    if 'Booking Code' in field_to_column:
                        bc_val = str(df.at[row_idx, field_to_column['Booking Code']])
                        log_parts.append(f"Booking Code: '{bc_val}'")
                    if 'Guest Name' in field_to_column:
                        gn_val = str(df.at[row_idx, field_to_column['Guest Name']])
                        log_parts.append(f"Guest Name: '{gn_val}'")
                    if 'Check-In Date' in field_to_column:
                        ci_val = str(df.at[row_idx, field_to_column['Check-In Date']])
                        log_parts.append(f"Check-In: '{ci_val}'")
                    if 'Check-Out Date' in field_to_column:
                        co_val = str(df.at[row_idx, field_to_column['Check-Out Date']])
                        log_parts.append(f"Check-Out: '{co_val}'")
                    
                    log_info = " | ".join(log_parts) if log_parts else "All 4 fields matched"
                    print(f"   ✅ Updated row {row_idx + 1} - {log_info} → '{INVOICE_RECEIVED_VALUE}'")
                    
                except Exception as e:
                    print(f"   ❌ Error updating row {row_idx + 1}: {e}")
                    continue
        else:
            print(f"   ❌ No matching row found for this entry")
    
    print(f"\n📊 Matching Summary:")
    print(f"   ✅ Rows updated: {rows_updated}")
    print(f"   ⏭️  Rows skipped (already marked): {rows_skipped}")
    print(f"   📋 Total rows in Excel: {len(df)}")
    
    # Save updated Excel file (save if rows were updated OR if column was added)
    if rows_updated > 0 or column_was_added:
        try:
            print(f"\n💾 Saving updated Excel file...")
            print(f"   📁 File path: {MATCHING_EXCEL_FILE_PATH}")
            if column_was_added and rows_updated == 0:
                print(f"   💡 Saving file because new column '{INVOICE_RECEIVED_COLUMN}' was added")
            
            # Check if file is open (try to open it in append mode first)
            try:
                # Try to open the file to check if it's locked
                test_file = open(MATCHING_EXCEL_FILE_PATH, 'r+b')
                test_file.close()
            except PermissionError:
                print(f"   ⚠️  File might be open in Excel. Please close it and try again.")
                print(f"   💡 The file will be saved when you close Excel and run the function again.")
                return rows_updated  # Return the count but don't save yet
            
            from openpyxl import load_workbook
            from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
            
            # Save DataFrame to Excel
            print(f"   💾 Writing DataFrame to Excel...")
            df.to_excel(MATCHING_EXCEL_FILE_PATH, index=False, engine='openpyxl')
            print(f"   ✅ DataFrame saved successfully")
            
            # Load with openpyxl to apply formatting
            wb = load_workbook(MATCHING_EXCEL_FILE_PATH)
            ws = wb.active
            
            # Apply header formatting
            header_font = Font(bold=True, color="FFFFFF", size=11)
            header_fill = PatternFill(start_color="2F5597", end_color="2F5597", fill_type="solid")
            header_alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            
            for cell in ws[1]:
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = header_alignment
            
            # Add borders
            thin_border = Border(
                left=Side(style='thin'),
                right=Side(style='thin'),
                top=Side(style='thin'),
                bottom=Side(style='thin')
            )
            
            for row in ws.iter_rows():
                for cell in row:
                    cell.border = thin_border
                    if cell.row > 1:
                        cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
            
            # Freeze header row
            ws.freeze_panes = "A2"
            
            wb.save(MATCHING_EXCEL_FILE_PATH)
            wb.close()
            
            print(f"✅ Saved updated Excel file: {MATCHING_EXCEL_FILE_PATH}")
            print(f"   📊 Total rows in saved file: {len(df)}")
            print(f"   ✅ Rows marked as 'Received': {len(df[df[INVOICE_RECEIVED_COLUMN].astype(str).str.strip().str.lower() == INVOICE_RECEIVED_VALUE.lower()])}")
            if column_was_added and rows_updated == 0:
                print(f"   💡 File saved because new column '{INVOICE_RECEIVED_COLUMN}' was added")
        except PermissionError as e:
            print(f"❌ Permission denied: Cannot save Excel file")
            print(f"   The file might be open in Excel. Please close it and try again.")
            print(f"   File path: {MATCHING_EXCEL_FILE_PATH}")
        except Exception as e:
            print(f"⚠️  Error saving Excel file: {e}")
            import traceback
            traceback.print_exc()
    else:
        print(f"\n⚠️  No rows were updated (rows_updated = {rows_updated})")
        print(f"   This could mean:")
        print(f"   1. No matching rows were found in the Excel file")
        print(f"   2. All matching rows were already marked as 'Received'")
        print(f"   3. Column matching failed (check logs above for column mappings)")
    
    return rows_updated



def read_mysql_data_as_dataframe():
    """
    Read invoice table from MySQL as a full DataFrame (table name from INVOICE_TABLE_NAME env).
    This contains all extracted invoice data from PDFs.
    
    Returns:
        pd.DataFrame: Full DataFrame with all columns from MySQL, mapped to Excel column names, or None if failed
    """
    try:
        from backend.app.modules.invoices.mysql import get_invoice_table_name
        table_name = get_invoice_table_name()
        print(f"\n   📊 Reading MySQL {table_name} table for data merging...")
        
        engine = get_db_engine()
        
        # Query all data from invoice table
        query = f"SELECT * FROM {table_name} ORDER BY id ASC"
        master_df = pd.read_sql(query, engine)
        
        if master_df.empty:
            print(f"      ⚠️  MySQL {table_name} table is empty")
            engine.dispose()
            return None
        
        print(f"      ✅ Read {len(master_df)} rows from MySQL {table_name} table")
        print(f"      📋 MySQL columns: {list(master_df.columns)}")
        
        # Map MySQL column names to Excel column names for compatibility with merge logic
        column_mapping = {
            'hotel_name': 'Hotel Name',
            'guest_name': 'Guest Name',
            'booking_code': 'Booking Code',
            'bill_number': 'Bill Number',
            'bill_date': 'Bill Date',
            'check_in_date': 'Check-in Date',
            'check_out_date': 'Check-out Date',
            'amount_paid': 'Amount Paid',
            'hbd_gst_number': 'HBD GST Number',
            'client_gst_number': 'Client GST Number',
            'cgst_number': 'CGST Number',
            'sgst_number': 'SGST Number',
            'total_cgst': 'Total CGST',
            'total_sgst': 'Total SGST',
            'pdf_filename': 'PDF Filename',
            'blob_url': 'Blob URL',
            'multi_page_pdf': 'Multi Page PDF',
            'push_failed': 'Push Failed',
            'push_cancelled': 'Push Cancelled',
            'validation_flag': 'Validation Flag',
            'email_from': 'Email From',
            'assigned_to': 'Assigned To',
            'created_at': 'Created At',
            'processing_date': 'Processing Date'
        }
        
        # Rename columns to Excel format
        master_df = master_df.rename(columns=column_mapping)
        
        # Format date columns to match Excel format (dd/mm/yy)
        date_columns = ['Bill Date', 'Check-in Date', 'Check-out Date']
        for col in date_columns:
            if col in master_df.columns:
                master_df[col] = master_df[col].apply(lambda x: format_date_to_dd_mm_yy(str(x)) if pd.notna(x) and str(x) != 'None' and str(x) != 'Not Found' else '')
        
        # Convert numeric columns to strings for consistency
        numeric_columns = ['Amount Paid', 'Total CGST', 'Total SGST']
        for col in numeric_columns:
            if col in master_df.columns:
                master_df[col] = master_df[col].apply(lambda x: str(x) if pd.notna(x) else '')
        
        # Replace None/NaN with empty strings for consistency
        master_df = master_df.fillna('')
        
        print(f"      ✅ Mapped MySQL columns to Excel format")
        print(f"      📋 Excel columns: {[col for col in master_df.columns if col in column_mapping.values()]}")
        
        engine.dispose()
        return master_df
        
    except Exception as e:
        print(f"      ❌ Error reading MySQL data: {e}")
        import traceback
        traceback.print_exc()
        return None

def read_master_excel_as_dataframe(drive_uploader):
    """
    DISABLED: Master Excel functionality removed - use read_mysql_data_as_dataframe() instead.
    
    DEPRECATED: Use read_mysql_data_as_dataframe() instead.
    
    Args:
        drive_uploader: GoogleDriveUploader instance (not used)
    
    Returns:
        None: Master Excel is disabled
    """
    print("📊 Master Excel disabled - use read_mysql_data_as_dataframe() instead")
    return None
    
    # Original code below (disabled)
    """
    Read Invoice Master Sheet from Google Drive as a full DataFrame (not just matching fields).
    This contains all extracted invoice data from PDFs.
    
    DEPRECATED: Use read_mysql_data_as_dataframe() instead.
    
    Args:
        drive_uploader: GoogleDriveUploader instance
    
    Returns:
        pd.DataFrame: Full DataFrame with all columns from Master Excel, or None if failed
    """
    if not drive_uploader or not drive_uploader.service:
        print("   ⚠️  Google Drive service not available - cannot read Master Excel")
        return None
    
    try:
        print(f"\n   📊 Reading Master Excel (Invoice Master Sheet) for data merging...")
        # INVOICE_MASTER_SHEET_ID is disabled
        # print(f"      File ID: {INVOICE_MASTER_SHEET_ID}")
        
        # First, check the file type
        file_metadata = drive_uploader.service.files().get(
            fileId=INVOICE_MASTER_SHEET_ID,
            fields='id,name,mimeType'
        ).execute()
        
        file_mime_type = file_metadata.get('mimeType', '')
        file_name = file_metadata.get('name', 'Unknown')
        
        # Download to temporary file
        temp_sheet_file = 'temp_master_excel_for_merge.xlsx'
        
        # Try export_media first (for Google Sheets)
        if file_mime_type == 'application/vnd.google-apps.spreadsheet':
            try:
                request = drive_uploader.service.files().export_media(
                    fileId=INVOICE_MASTER_SHEET_ID,
                    mimeType='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
                )
                with open(temp_sheet_file, 'wb') as f:
                    f.write(request.execute())
            except Exception as export_error:
                # Try CSV export as fallback
                try:
                    request = drive_uploader.service.files().export_media(
                        fileId=INVOICE_MASTER_SHEET_ID,
                        mimeType='text/csv'
                    )
                    temp_csv_file = 'temp_master_excel_for_merge.csv'
                    with open(temp_csv_file, 'wb') as f:
                        f.write(request.execute())
                    df_csv = pd.read_csv(temp_csv_file)
                    df_csv.to_excel(temp_sheet_file, index=False, engine='openpyxl')
                    os.remove(temp_csv_file)
                except Exception as csv_error:
                    print(f"      ❌ Error exporting Master Excel: {csv_error}")
                    return None
        else:
            # For Excel files already in Drive, use get_media
            request = drive_uploader.service.files().get_media(
                fileId=INVOICE_MASTER_SHEET_ID
            )
            with open(temp_sheet_file, 'wb') as f:
                f.write(request.execute())
        
        # Read Excel file with pandas
        master_df = pd.read_excel(temp_sheet_file, engine='openpyxl', header=0)
        print(f"      ✅ Read {len(master_df)} rows from Master Excel")
        print(f"      📋 Columns: {list(master_df.columns)}")
        
        # Clean up temp file
        try:
            import time
            time.sleep(0.3)
            if os.path.exists(temp_sheet_file):
                os.remove(temp_sheet_file)
        except:
            pass
        
        return master_df
        
    except Exception as e:
        print(f"      ❌ Error reading Master Excel: {e}")
        import traceback
        traceback.print_exc()
        return None


def extract_all_invoice_fields(invoice_data_list):
    """
    Extract all invoice data fields from invoice_data_list for updating matched rows.
    Returns a list of dictionaries with all invoice fields.
    """
    all_invoice_fields_list = []
    
    for invoice in invoice_data_list:
        pdf_analysis = invoice.get('pdf_analysis', {})
        if not isinstance(pdf_analysis, dict):
            continue
        
        table_entries = invoice.get('email_table_entries') or []
        email_details = invoice.get('email_booking_details', {}) or {}
        email_from = invoice.get('from_email', '').replace('<', '').replace('>', '')
        assigned_folder = invoice.get('assigned_column', 'UNASSIGNED')
        
        def format_date_if_needed(value: Optional[str]) -> str:
            """Format date to dd/mm/yy format"""
            result = format_date_to_dd_mm_yy(value)
            return result if result != 'Not Found' else ''
        
        # Extract invoice data from PDFs (priority: merged PDFs > standalone PDFs)
        for pdf_key, pdf_data in pdf_analysis.items():
            if not isinstance(pdf_data, dict):
                continue
            if pdf_data.get('generated_from_email'):
                continue
            if pdf_data.get('hotel_name') == 'PDF couldn\'t be processed':
                continue
            
            # Extract all fields from PDF data
            hotel_name = pdf_data.get('hotel_name', 'Not Found')
            guest_name = pdf_data.get('guest_name', 'Not Found')
            booking_code = pdf_data.get('booking_code', 'Not Found')
            bill_number = pdf_data.get('bill_number', 'Not Found')
            bill_date = pdf_data.get('bill_date', 'Not Found')
            check_in_date = format_date_if_needed(pdf_data.get('arrival_date') or pdf_data.get('check_in_date'))
            check_out_date = format_date_if_needed(pdf_data.get('departure_date') or pdf_data.get('check_out_date'))
            total_amount = pdf_data.get('total_amount', 'Not Found')
            if total_amount != 'Not Found':
                total_match = re.search(r'\d[\d,\.]*', str(total_amount))
                total_amount = total_match.group(0).replace(',', '') if total_match else 'Not Found'
            
            hbd_gst_number = pdf_data.get('hbd_gst_number') or pdf_data.get('hbd_gst', 'Not Found')
            client_gst_number = pdf_data.get('client_gst_number') or pdf_data.get('client_gst', 'Not Found')
            
            # Get CGST and SGST numbers and amounts
            cgst_number = pdf_data.get('cgst_number', 'Not Found')
            sgst_number = pdf_data.get('sgst_number', 'Not Found')
            total_cgst = pdf_data.get('total_cgst', 'Not Found')
            total_sgst = pdf_data.get('total_sgst', 'Not Found')
            
            # Clean CGST and SGST amounts if they contain currency symbols or commas
            if total_cgst != 'Not Found':
                cgst_match = re.search(r'\d[\d,\.]*', str(total_cgst))
                total_cgst = cgst_match.group(0).replace(',', '') if cgst_match else 'Not Found'
            
            if total_sgst != 'Not Found':
                sgst_match = re.search(r'\d[\d,\.]*', str(total_sgst))
                total_sgst = sgst_match.group(0).replace(',', '') if sgst_match else 'Not Found'
            
            _new_numeric_fields = {
                'room_charge': pdf_data.get('room_charge', 'Not Found'),
                'total_tariff': pdf_data.get('total_tariff', 'Not Found'),
                'other_charges': pdf_data.get('other_charges', 'Not Found'),
                'total_igst': pdf_data.get('total_igst', 'Not Found'),
                'total_taxes': pdf_data.get('total_taxes', 'Not Found'),
                'discount': pdf_data.get('discount', 'Not Found'),
                'advance_received': pdf_data.get('advance_received', 'Not Found'),
            }
            for _k, _v in _new_numeric_fields.items():
                if _v != 'Not Found':
                    _m = re.search(r'\d[\d,\.]*', str(_v))
                    _new_numeric_fields[_k] = _m.group(0).replace(',', '') if _m else 'Not Found'

            original_filename = pdf_data.get('filename', '')
            processing_date_for_filename = datetime.now().strftime('%Y%m%d')
            safe_filename = re.sub(r'[^\w\s\-\.]', '', original_filename).strip().replace(' ', '_')
            pdf_filename = f"{processing_date_for_filename}_{safe_filename}"
            blob_url = sanitize_blob_url(pdf_data.get('blob_url', '') or '')
            processing_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            invoice_fields = {
                'Processing Date': processing_date,
                'Email From': email_from,
                'Assigned To': assigned_folder,
                'Hotel Name': normalize_field_text(hotel_name) or 'Not Found',
                'Guest Name': normalize_field_text(guest_name) or 'Not Found',
                'Booking Code': normalize_field_text(booking_code) or 'Not Found',
                'Bill Number': normalize_field_text(bill_number) or 'Not Found',
                'Bill Date': bill_date if bill_date else 'Not Found',
                'Check-in Date': check_in_date,
                'Check-out Date': check_out_date,
                'Amount Paid': total_amount,
                'HBD GST Number': normalize_field_text(hbd_gst_number) if hbd_gst_number != 'Not Found' else 'Not Found',
                'Client GST Number': normalize_field_text(client_gst_number) if client_gst_number != 'Not Found' else 'Not Found',
                'CGST Number': normalize_field_text(cgst_number) if cgst_number != 'Not Found' else 'Not Found',
                'SGST Number': normalize_field_text(sgst_number) if sgst_number != 'Not Found' else 'Not Found',
                'Total CGST': total_cgst if total_cgst != 'Not Found' else 'Not Found',
                'Total SGST': total_sgst if total_sgst != 'Not Found' else 'Not Found',
                'Room Charge': _new_numeric_fields['room_charge'],
                'Total Tariff': _new_numeric_fields['total_tariff'],
                'Other Charges': _new_numeric_fields['other_charges'],
                'Total IGST': _new_numeric_fields['total_igst'],
                'Total Taxes': _new_numeric_fields['total_taxes'],
                'Discount': _new_numeric_fields['discount'],
                'Advance Received': _new_numeric_fields['advance_received'],
                'PDF Filename': pdf_filename,
                'Blob URL': blob_url,
                # Matching fields for linking
                'Match Booking Code': normalize_field_text(booking_code) or '',
                'Match Guest Name': normalize_field_text(guest_name) or '',
                'Match Check-In Date': format_date_if_needed(pdf_data.get('arrival_date') or pdf_data.get('check_in_date')),
                'Match Check-Out Date': format_date_if_needed(pdf_data.get('departure_date') or pdf_data.get('check_out_date'))
            }
            all_invoice_fields_list.append(invoice_fields)
        
        # If no PDFs, extract from email table entries
        if not all_invoice_fields_list and table_entries:
            for entry in table_entries:
                invoice_fields = {
                    'Processing Date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'Email From': email_from,
                    'Assigned To': assigned_folder,
                    'Hotel Name': normalize_field_text(entry.get('property_name')) or 'Not Found',
                    'Guest Name': normalize_field_text(entry.get('guest_name')) or 'Not Found',
                    'Booking Code': normalize_field_text(entry.get('booking_code')) or 'Not Found',
                    'Bill Number': 'Not Found',
                    'Bill Date': 'Not Found',
                    'Check-in Date': format_date_if_needed(entry.get('check_in_date')),
                    'Check-out Date': format_date_if_needed(entry.get('check_out_date')),
                    'Amount Paid': normalize_field_text(entry.get('total_amount')) or 'Not Found',
                    'HBD GST Number': 'Not Found',
                    'Client GST Number': 'Not Found',
                    'CGST Number': 'Not Found',
                    'SGST Number': 'Not Found',
                    'Total CGST': 'Not Found',
                    'Total SGST': 'Not Found',
                    'Room Charge': 'Not Found',
                    'Total Tariff': 'Not Found',
                    'Other Charges': 'Not Found',
                    'Total IGST': 'Not Found',
                    'Total Taxes': 'Not Found',
                    'Discount': 'Not Found',
                    'Advance Received': 'Not Found',
                    'PDF Filename': 'email_table_entry',
                    'Blob URL': '',
                    # Matching fields
                    'Match Booking Code': normalize_field_text(entry.get('booking_code')) or '',
                    'Match Guest Name': normalize_field_text(entry.get('guest_name')) or '',
                    'Match Check-In Date': format_date_if_needed(entry.get('check_in_date')),
                    'Match Check-Out Date': format_date_if_needed(entry.get('check_out_date'))
                }
                all_invoice_fields_list.append(invoice_fields)
    
    return all_invoice_fields_list

def _extract_gst_from_invoice_fields(entry, all_invoice_fields_list):
    """
    Extract HBD GST and Client GST numbers from invoice fields by matching guest name and dates.
    
    Args:
        entry: Dictionary containing invoice entry data
        all_invoice_fields_list: List of invoice field dictionaries
    
    Returns:
        Tuple of (hbd_gst, client_gst) or (None, None) if not found
    """
    if not all_invoice_fields_list:
        return None, None
    
    entry_guest_name = normalize_field_text(entry.get('Guest Name', ''))
    entry_checkin = entry.get('Check-In Date', '').strip()
    entry_checkout = entry.get('Check-Out Date', '').strip()
    
    for inv_fields in all_invoice_fields_list:
        inv_guest = normalize_field_text(inv_fields.get('Match Guest Name', ''))
        inv_checkin = inv_fields.get('Match Check-In Date', '').strip()
        inv_checkout = inv_fields.get('Match Check-Out Date', '').strip()
        
        # Match by guest name and dates (booking code not needed for GST extraction)
        if (entry_guest_name and inv_guest and entry_guest_name.lower() == inv_guest.lower() and
            entry_checkin and inv_checkin and entry_checkin == inv_checkin and
            entry_checkout and inv_checkout and entry_checkout == inv_checkout):
            hbd_gst = inv_fields.get('HBD GST Number', 'Not Found')
            client_gst = inv_fields.get('Client GST Number', 'Not Found')
            
            # Clean up "Not Found" values
            hbd_gst = hbd_gst if hbd_gst and hbd_gst != 'Not Found' else None
            client_gst = client_gst if client_gst and client_gst != 'Not Found' else None
            
            if hbd_gst or client_gst:
                return hbd_gst, client_gst
    
    return None, None


def match_invoice_data_with_excel(invoice_data_list, drive_uploader=None, inserted_ids_map=None):
    """
    Match invoice data with Excel sheet and update Invoice Received status.
    Uses the same matching logic as email_to_excel_mapper.py
    
    Matching Priority:
    1. Match by Booking Code (Primary Key)
    2. If no Booking Code, match by GST Numbers (HBD + Client) + Guest Name + Dates
    3. If no Booking Code match, match by Guest Name + Hotel Name + Check-In Date + Check-Out Date (all four required)
    
    Update Logic:
    - Updates the existing row's "Invoice Received" column with "Received"
    - Updates all invoice data fields in matched rows (NEW)
    - Skips if already marked as "Received"
    
    Args:
        invoice_data_list: List of invoice data dictionaries
        drive_uploader: Optional GoogleDriveUploader instance for reading Master Excel
        inserted_ids_map: Dictionary mapping pdf_filename to inserted row ID (NEW)
    """
    if inserted_ids_map is None:
        inserted_ids_map = {}
    # Read Excel file
    df = read_matching_excel_file()
    if df is None or df.empty:
        print("⚠️  Excel file is empty or invalid - skipping matching")
        return 0, []
    
    print(f"\n🔍 Matching invoice data with Excel sheet...")
    print(f"🔍 Excel file has {len(df)} rows and {len(df.columns)} columns")
    print(f"🔍 Excel columns: {list(df.columns)}")
    
    # Extract all invoice fields for updating matched rows
    all_invoice_fields_list = extract_all_invoice_fields(invoice_data_list)
    print(f"📋 Extracted {len(all_invoice_fields_list)} invoice data entries with all fields")
    
    # Helper function to lookup row_id from MySQL using booking_code + dates + guest_name
    def lookup_row_id_from_mysql(booking_code, guest_name, check_in_date, check_out_date, inserted_ids_map, pdf_filename):
        """Helper function to lookup row_id from MySQL using booking_code + dates + guest_name, or guest_name + dates + pdf_filename if booking_code is missing"""
        # Try inserted_ids_map first (most reliable - uses pdf_filename which should be unique)
        row_id = inserted_ids_map.get(pdf_filename) if inserted_ids_map else None
        if row_id:
            return row_id
        
        # FALLBACK: Look up from MySQL
        try:
            from sqlalchemy import text
            from datetime import datetime
            
            def parse_date_for_lookup(date_str):
                if not date_str or str(date_str).lower() in ['not found', 'nan', 'none', '']:
                    return None
                try:
                    date_str = str(date_str).strip()
                    for fmt in ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d", "%d/%m/%y", "%d-%m-%y"]:
                        try:
                            return datetime.strptime(date_str, fmt).date()
                        except:
                            continue
                    return None
                except:
                    return None
            
            check_in_parsed = parse_date_for_lookup(check_in_date)
            check_out_parsed = parse_date_for_lookup(check_out_date)
            
            # Primary lookup: If booking_code exists, use it
            from backend.app.modules.invoices.mysql import get_db_engine, get_invoice_table_name
            _inv_table = get_invoice_table_name()
            if booking_code and guest_name and check_in_parsed and check_out_parsed:
                # Include pdf_filename if available to distinguish between multiple rows with same booking code/guest/dates
                if pdf_filename:
                    lookup_sql = f"""
                    SELECT id FROM {_inv_table}
                    WHERE booking_code = :booking_code
                      AND check_in_date = :check_in_date
                      AND check_out_date = :check_out_date
                      AND guest_name = :guest_name
                      AND pdf_filename = :pdf_filename
                    ORDER BY id DESC
                    LIMIT 1
                    """
                    from backend.app.modules.invoices.mysql import get_db_engine
                    mysql_engine = get_db_engine()
                    with mysql_engine.connect() as conn:
                        result = conn.execute(text(lookup_sql), {
                            'booking_code': booking_code,
                            'check_in_date': check_in_parsed,
                            'check_out_date': check_out_parsed,
                            'guest_name': guest_name,
                            'pdf_filename': pdf_filename
                        })
                        row = result.fetchone()
                        if row:
                            return row[0]
                else:
                    # Fallback: if pdf_filename not available, use original lookup
                    lookup_sql = f"""
                    SELECT id FROM {_inv_table}
                    WHERE booking_code = :booking_code
                      AND check_in_date = :check_in_date
                      AND check_out_date = :check_out_date
                      AND guest_name = :guest_name
                    ORDER BY id DESC
                    LIMIT 1
                    """
                    from backend.app.modules.invoices.mysql import get_db_engine
                    mysql_engine = get_db_engine()
                    with mysql_engine.connect() as conn:
                        result = conn.execute(text(lookup_sql), {
                            'booking_code': booking_code,
                            'check_in_date': check_in_parsed,
                            'check_out_date': check_out_parsed,
                            'guest_name': guest_name
                        })
                        row = result.fetchone()
                        if row:
                            return row[0]
            
            # Fallback lookup: If booking_code is missing (GST matching), use guest_name + dates + pdf_filename
            # This handles cases where pdf_filename might not be in inserted_ids_map (e.g., if same pdf_filename exists)
            if not booking_code and guest_name and check_in_parsed and check_out_parsed and pdf_filename:
                lookup_sql = f"""
                SELECT id FROM {_inv_table}
                WHERE guest_name = :guest_name
                  AND check_in_date = :check_in_date
                  AND check_out_date = :check_out_date
                  AND pdf_filename = :pdf_filename
                ORDER BY id DESC
                LIMIT 1
                """
                from backend.app.modules.invoices.mysql import get_db_engine
                mysql_engine = get_db_engine()
                with mysql_engine.connect() as conn:
                    result = conn.execute(text(lookup_sql), {
                        'guest_name': guest_name,
                        'check_in_date': check_in_parsed,
                        'check_out_date': check_out_parsed,
                        'pdf_filename': pdf_filename
                    })
                    row = result.fetchone()
                    if row:
                        return row[0]
        except Exception as e:
            print(f"   ⚠️  Error in MySQL lookup for row_id: {e}")
        
        return None
    
    # Convert invoice data to matching format
    # USE ONLY MERGED PDF DATA - do not use email table entries
    extracted_data_list = []
    pdf_filename_map = {}  # Map entry index to PDF filename for logging
    for invoice in invoice_data_list:
        pdf_analysis = invoice.get('pdf_analysis', {})
        if not isinstance(pdf_analysis, dict):
            continue

        def format_date_for_match(value: Optional[str]) -> str:
            """Format date to dd/mm/yy format for matching"""
            result = format_date_to_dd_mm_yy(value)
            return result if result != 'Not Found' else ''

        # Extract matching data from merged PDF data only (not from email table entries)
        seen_pdfs = set()
        for pdf_key, pdf_data in pdf_analysis.items():
            if not isinstance(pdf_data, dict):
                continue
            # Skip PDFs that were generated from email (not actual PDF attachments)
            if pdf_data.get('generated_from_email'):
                continue
            # Skip PDFs that couldn't be processed
            if pdf_data.get('hotel_name') == 'PDF couldn\'t be processed':
                continue
            
            # Get PDF filename for logging
            original_filename = pdf_data.get('filename', '')
            processing_date_for_filename = datetime.now().strftime('%Y%m%d')
            safe_filename = re.sub(r'[^\w\s\-\.]', '', original_filename).strip().replace(' ', '_')
            base_pdf_filename = f"{processing_date_for_filename}_{safe_filename}" if original_filename else 'unknown.pdf'
            
            # Get base PDF data (same for all matches)
            guest_name = normalize_field_text(pdf_data.get('guest_name', ''))
            hotel_name = normalize_field_text(pdf_data.get('hotel_name', ''))
            check_in_date = format_date_for_match(pdf_data.get('arrival_date') or pdf_data.get('check_in_date'))
            check_out_date = format_date_for_match(pdf_data.get('departure_date') or pdf_data.get('check_out_date'))
            
            # CHANGED: Handle multiple matches - create one entry per match
            all_matched_entries = pdf_data.get('matched_table_entries', [])
            
            if all_matched_entries:
                # Create one extracted entry for each match (each with different booking code)
                for match_idx, (matched_entry, match_diagnostics) in enumerate(all_matched_entries, 1):
                    entry_booking_code = matched_entry.get('booking_code') or ''
                    booking_code = normalize_field_text(entry_booking_code)
                    
                    # Create unique signature to avoid duplicates
                    # Include match_idx AND base_pdf_filename to handle cases where multiple PDFs match same email entries
                    normalized_signature = (
                        booking_code,
                        hotel_name,
                        guest_name,
                        check_in_date,
                        check_out_date,
                        match_idx,  # Match index within this PDF
                        base_pdf_filename  # Add PDF filename to distinguish between different PDFs with same match_idx
                    )
                    if normalized_signature in seen_pdfs:
                        continue
                    seen_pdfs.add(normalized_signature)
                    
                    # Create extracted entry with this match's booking code
                    if booking_code or (guest_name and check_in_date and check_out_date):
                        extracted_entry = {
                            'Booking Code': booking_code or '',
                            'Guest Name': guest_name or '',
                            'Hotel Name': hotel_name or '',
                            'Check-In Date': check_in_date or '',
                            'Check-Out Date': check_out_date or ''
                        }
                        if any(extracted_entry.values()):
                            entry_index = len(extracted_data_list)
                            extracted_data_list.append(extracted_entry)
                            # For multiple matches, insert match number before extension so .pdf stays last
                            if len(all_matched_entries) > 1:
                                pdf_filename = pdf_filename_with_match_suffix(base_pdf_filename, match_idx)
                            else:
                                pdf_filename = base_pdf_filename
                            pdf_filename_map[entry_index] = pdf_filename
            else:
                # No matches - use PDF data only (booking_code might be empty)
                booking_code = normalize_field_text(pdf_data.get('booking_code', ''))
                
                # Create unique signature to avoid duplicates
                normalized_signature = (
                    booking_code,
                    hotel_name,
                    guest_name,
                    check_in_date,
                    check_out_date
                )
                if normalized_signature in seen_pdfs:
                    continue
                seen_pdfs.add(normalized_signature)
                
                # Only add if we have essential fields
                if booking_code or (guest_name and check_in_date and check_out_date):
                    extracted_entry = {
                        'Booking Code': booking_code or '',
                        'Guest Name': guest_name or '',
                        'Hotel Name': hotel_name or '',
                        'Check-In Date': check_in_date or '',
                        'Check-Out Date': check_out_date or ''
                    }
                    if any(extracted_entry.values()):
                        entry_index = len(extracted_data_list)
                        extracted_data_list.append(extracted_entry)
                        pdf_filename_map[entry_index] = base_pdf_filename
    
    if not extracted_data_list:
        print("⚠️  No invoice data to match")
        return 0, []
    
    print(f"🔍 Extracted {len(extracted_data_list)} entries from invoices for matching")
    
    # Create a mapping of field names to Excel columns
    field_to_column = {}
    for field in MATCHING_FIELDS:
        column = find_matching_column(df, field)
        if column:
            field_to_column[field] = column
            print(f"✅ Mapped '{field}' → '{column}'")
        else:
            print(f"⚠️  No matching column found for '{field}'")
    
    if not field_to_column:
        print("❌ No matching columns found. Cannot match Excel rows.")
        return 0
    
    # Resolve Excel column for balance amount from API (for amount_match: balanceAmount vs PDF total).
    if 'balanceAmount' in df.columns:
        excel_balance_amount_col = 'balanceAmount'
    else:
        excel_balance_amount_col = None
        for col in df.columns:
            c = str(col).strip().lower()
            if c in ('balance amount', 'balanceamount'):
                excel_balance_amount_col = col
                break
    print(f"   [amount_match] Using Excel column for API balance (balanceAmount vs PDF total): '{excel_balance_amount_col}'")
    
    def _normalize_amount_for_match(val):
        """Return float or None for amount comparison."""
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return None
        if isinstance(val, (int, float)) and not (isinstance(val, float) and pd.isna(val)):
            return float(val)
        s = str(val).strip().lower()
        if not s or s in ('not found', 'nan', 'none'):
            return None
        s = str(val).strip().replace(',', '')
        m = re.search(r'[\d.]+', s)
        if m:
            try:
                return float(m.group(0))
            except ValueError:
                pass
        return None
    
    def _compute_amount_match(pdf_total_amount, api_balance_amount):
        """Compare PDF total amount vs API balanceAmount. Return 1 if match, 0 if not, None if either missing."""
        a = _normalize_amount_for_match(pdf_total_amount)
        b = _normalize_amount_for_match(api_balance_amount)
        if a is None or b is None:
            print(f"   [amount_match] PDF total={pdf_total_amount!r} (normalized={a}), API balance={api_balance_amount!r} (normalized={b}) → not compared (None)")
            return None
        result = 1 if abs(a - b) < 1e-6 else 0
        status = "matched" if result == 1 else "mismatch"
        print(f"   [amount_match] PDF total={pdf_total_amount!r} (normalized={a}), API balance={api_balance_amount!r} (normalized={b}) → {status} ({result})")
        return result
    
    # Check if 'Invoice Received' column exists, if not add it at the end
    column_was_added = False
    if INVOICE_RECEIVED_COLUMN not in df.columns:
        print(f"➕ Adding new column: '{INVOICE_RECEIVED_COLUMN}'")
        df[INVOICE_RECEIVED_COLUMN] = ''
        column_was_added = True
    else:
        print(f"✅ Column '{INVOICE_RECEIVED_COLUMN}' already exists")
        # FIX: Convert column to object dtype if it's numeric to allow string values
        if df[INVOICE_RECEIVED_COLUMN].dtype != 'object':
            print(f"   🔄 Converting '{INVOICE_RECEIVED_COLUMN}' column from {df[INVOICE_RECEIVED_COLUMN].dtype} to object dtype")
            # Convert to string, replacing NaN/None with empty string
            df[INVOICE_RECEIVED_COLUMN] = df[INVOICE_RECEIVED_COLUMN].astype(str).replace('nan', '').replace('None', '').replace('NaT', '')
    
    # Add Excel matching diagnostic columns (numeric columns for scores and deltas)
    for col_name in [EXCEL_MATCH_GUEST_SCORE_COLUMN, EXCEL_MATCH_CHECKIN_DELTA_COLUMN, EXCEL_MATCH_CHECKOUT_DELTA_COLUMN]:
        if col_name not in df.columns:
            print(f"➕ Adding new column: '{col_name}'")
            # Initialize as numeric column (float64 with NaN values)
            df[col_name] = pd.Series(dtype='float64', index=df.index)
        else:
            print(f"✅ Column '{col_name}' already exists")
            # Convert to numeric dtype if it's currently string/object
            if df[col_name].dtype == 'object' or pd.api.types.is_string_dtype(df[col_name]):
                print(f"   🔄 Converting '{col_name}' column from {df[col_name].dtype} to numeric dtype")
                df[col_name] = pd.to_numeric(df[col_name], errors='coerce')
    
    rows_updated = 0
    rows_skipped = 0
    
    # Collect API push selection messages for Invoice Summary
    api_push_selection_messages = []  # List of (pdf_filename, is_selected, already_marked) tuples
    match_booking_code_map = {}  # NEW: Map entry index to match_booking_code value (1 = match, 0 = no match)
    gst_match_map = {}  # NEW: Map entry index to GST match status {'hbd_gst_match': 1/0/None, 'client_gst_match': 1/0/None}
    
    # Process each extracted data entry
    for idx, entry in enumerate(extracted_data_list):
        if not entry or not any(entry.values()):
            print(f"⚠️  Entry {idx+1} is empty, skipping")
            continue
        
        print(f"\n📋 Processing entry {idx+1}:")
        for field, value in entry.items():
            if value:
                print(f"   {field}: {value}")
        
        matching_row_indices = []
        
        # Matching: Booking Code + Guest Name + (Check-In OR Check-Out Date with ±1 day tolerance)
        required_fields = ['Booking Code', 'Guest Name', 'Check-In Date', 'Check-Out Date']
        missing_fields = [f for f in required_fields if f not in field_to_column]
        
        if missing_fields:
            print(f"   ⚠️  Cannot match: Missing columns for {missing_fields}")
            continue
        
        # Get values for all four fields
        booking_code_value_raw = entry.get('Booking Code', '').strip()
        guest_name_value = entry.get('Guest Name', '').strip()
        checkin_value = entry.get('Check-In Date', '').strip()
        checkout_value = entry.get('Check-Out Date', '').strip()
        
        # Initialize diagnostic variables (will be populated in try block or GST matching)
        matching_row_indices = []
        guest_scores = pd.Series(dtype=object)
        checkin_deltas = pd.Series(dtype=object)
        checkout_deltas = pd.Series(dtype=object)
        use_gst_matching = False
        
        # Check if booking code is missing - try GST-based matching
        if not booking_code_value_raw:
            print(f"   🔍 Booking Code not found - attempting GST-based matching...")
            
            # Extract GST numbers from invoice fields
            hbd_gst, client_gst = _extract_gst_from_invoice_fields(entry, all_invoice_fields_list)
            
            if hbd_gst or client_gst:
                # Use GST matching
                from backend.app.modules.matching.gst import match_by_gst_numbers
                matching_row_indices, guest_scores, checkin_deltas, checkout_deltas, hbd_gst_matched, client_gst_matched = match_by_gst_numbers(
                    entry, df, field_to_column, hbd_gst, client_gst
                )
                
                # Set match_booking_code to 0 (doesn't match) since we used GST instead
                match_booking_code_map[idx] = 0
                use_gst_matching = True
                
                # Store GST match status for later MySQL update
                # Convert to 1/0/None: 1=matched, 0=not matched, None=not checked
                gst_match_map[idx] = {
                    'hbd_gst_match': 1 if hbd_gst_matched is True else (0 if hbd_gst_matched is False else None),
                    'client_gst_match': 1 if client_gst_matched is True else (0 if client_gst_matched is False else None)
                }
                
                # Continue to invoice fields matching and row processing (skip booking code matching)
            else:
                print(f"   ⚠️  Cannot match: Missing booking code AND GST numbers")
                print(f"      Booking Code: ❌")
                print(f"      HBD GST: {'✅' if hbd_gst else '❌'}")
                print(f"      Client GST: {'✅' if client_gst else '❌'}")
                print(f"      Guest Name: {'✅' if guest_name_value else '❌'}")
                print(f"      Check-In Date: {'✅' if checkin_value else '❌'}")
                print(f"      Check-Out Date: {'✅' if checkout_value else '❌'}")
                continue
        else:
            # Booking code exists - use existing booking code matching logic
            if not guest_name_value or (not checkin_value and not checkout_value):
                print(f"   ⚠️  Cannot match: Missing required fields")
                print(f"      Booking Code: {'✅' if booking_code_value_raw else '❌'}")
                print(f"      Guest Name: {'✅' if guest_name_value else '❌'}")
                print(f"      Check-In Date: {'✅' if checkin_value else '❌'}")
                print(f"      Check-Out Date: {'✅' if checkout_value else '❌'}")
                continue
        
        # Only do booking code matching if we didn't use GST matching
        if not use_gst_matching:
            print(f"   🔍 Matching by: Booking Code + Guest Name + (Check-In OR Check-Out Date ±1 day):")
            print(f"      Booking Code: '{booking_code_value_raw}'")
            print(f"      Guest Name: '{guest_name_value}'")
            print(f"      Check-In Date: '{checkin_value}'")
            print(f"      Check-Out Date: '{checkout_value}'")
        
        # Booking code matching logic (only executed if use_gst_matching is False)
        if not use_gst_matching:
            try:
                booking_code_col = field_to_column['Booking Code']
                guest_col = field_to_column['Guest Name']
                checkin_col = field_to_column['Check-In Date']
                checkout_col = field_to_column['Check-Out Date']
            
                # Normalize booking code
                normalized_entry_code = normalize_booking_code_value(booking_code_value_raw)
                
                # Date matching helper functions (same as email↔PDF matching)
                def _to_date_for_match(value):
                    """Parse a date value into a date() for matching (supports many formats via existing formatter)."""
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
                
                # Condition 1: Booking Code match (exact)
                normalized_series = df[booking_code_col].apply(normalize_booking_code_value)
                condition1 = normalized_series == normalized_entry_code
                
                # Condition 2: Guest Name match (using the same logic as guest_names_match_for_merge - RapidFuzz > 60)
                condition2_list = []
                guest_score_list = []  # NEW: Store RapidFuzz scores for each row
                for row_idx in df.index:
                    excel_guest_name = str(df.at[row_idx, guest_col]).strip()
                    # Use the same advanced guest name matching logic as used in merge
                    guest_match = guest_names_match_for_merge(excel_guest_name, guest_name_value)
                    condition2_list.append(guest_match)
                    
                    # NEW: Compute and store RapidFuzz score
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
                
                condition2 = pd.Series(condition2_list, index=df.index)
                guest_scores = pd.Series(guest_score_list, index=df.index)  # NEW: Series of scores
                if condition2.any():
                    print(f"      💡 Using advanced guest name matching logic (same as merge)")
                
                # Condition 3: Check-In Date match (with ±1 day tolerance)
                condition3_list = []
                checkin_delta_list = []  # NEW: Store check-in date deltas
                for row_idx in df.index:
                    excel_checkin = df.at[row_idx, checkin_col]
                    matches = _date_matches_or_pm1(excel_checkin, checkin_value) if checkin_value else False
                    condition3_list.append(matches)
                    
                    # NEW: Compute and store check-in date delta
                    if checkin_value:
                        d_excel = _to_date_for_match(excel_checkin)
                        d_invoice = _to_date_for_match(checkin_value)
                        if d_excel and d_invoice:
                            delta = (d_invoice - d_excel).days  # 1=invoice next day, 0=same day, -1=invoice previous day
                            checkin_delta_list.append(delta if delta in (-1, 0, 1) else None)
                        else:
                            checkin_delta_list.append(None)
                    else:
                        checkin_delta_list.append(None)
                
                condition3 = pd.Series(condition3_list, index=df.index)
                checkin_deltas = pd.Series(checkin_delta_list, index=df.index)  # NEW: Series of deltas
                
                # Condition 4: Check-Out Date match (with ±1 day tolerance)
                condition4_list = []
                checkout_delta_list = []  # NEW: Store check-out date deltas
                for row_idx in df.index:
                    excel_checkout = df.at[row_idx, checkout_col]
                    matches = _date_matches_or_pm1(excel_checkout, checkout_value) if checkout_value else False
                    condition4_list.append(matches)
                    
                    # NEW: Compute and store check-out date delta
                    if checkout_value:
                        d_excel = _to_date_for_match(excel_checkout)
                        d_invoice = _to_date_for_match(checkout_value)
                        if d_excel and d_invoice:
                            delta = (d_invoice - d_excel).days  # 1=invoice next day, 0=same day, -1=invoice previous day
                            checkout_delta_list.append(delta if delta in (-1, 0, 1) else None)
                        else:
                            checkout_delta_list.append(None)
                    else:
                        checkout_delta_list.append(None)
                
                condition4 = pd.Series(condition4_list, index=df.index)
                checkout_deltas = pd.Series(checkout_delta_list, index=df.index)  # NEW: Series of deltas
                
                # Matching: Booking Code + Guest Name + (Check-In OR Check-Out Date with ±1 day)
                combined_condition = condition1 & condition2 & (condition3 | condition4)
                matching_row_indices = df[combined_condition].index.tolist()
                # #region agent log
                try:
                    with open(r'c:\Users\ADMIN\Desktop\invoice final\.cursor\debug.log', 'a', encoding='utf-8') as f:
                        import json
                        log_entry = {
                            "id": f"log_{int(__import__('time').time() * 1000)}_combined",
                            "timestamp": int(__import__('time').time() * 1000),
                            "location": "legacy_impl.py:8473",
                            "message": "Combined condition result",
                            "data": {
                                "entry_idx": idx,
                                "booking_code": booking_code_value_raw,
                                "guest_name": guest_name_value,
                                "condition1_count": int(condition1.sum()),
                                "condition2_count": int(condition2.sum()),
                                "condition3_count": int(condition3.sum()),
                                "condition4_count": int(condition4.sum()),
                                "date_match_count": int((condition3 | condition4).sum()),
                                "combined_match_count": len(matching_row_indices),
                                "matching_row_indices": [int(x) for x in matching_row_indices[:5]]
                            },
                            "runId": "debug1",
                            "hypothesisId": "B"
                        }
                        f.write(json.dumps(log_entry) + '\n')
                except Exception:
                    pass
                # #endregion
                
                # Debug: Show why matching failed if no matches found
                if not matching_row_indices:
                    print(f"   ❌ No matching row found. Debugging match conditions...")
                    
                    # Check each condition separately
                    booking_code_matches = df[condition1].index.tolist()
                    guest_name_matches = df[condition2].index.tolist()
                    checkin_matches = df[condition3].index.tolist()
                    checkout_matches = df[condition4].index.tolist()
                    date_matches = df[condition3 | condition4].index.tolist()  # Either check-in or check-out
                    
                    print(f"      📊 Condition breakdown:")
                    print(f"         Booking Code matches: {len(booking_code_matches)} row(s)")
                    print(f"         Guest Name matches: {len(guest_name_matches)} row(s)")
                    print(f"         Check-In Date matches (±1 day): {len(checkin_matches)} row(s)")
                    print(f"         Check-Out Date matches (±1 day): {len(checkout_matches)} row(s)")
                    print(f"         Either date matches (±1 day): {len(date_matches)} row(s)")
                    
                    # Show sample Excel values for debugging
                    if len(df) > 0:
                        sample_row = df.iloc[0]
                        print(f"\n      📋 Sample Excel row values (Row 1):")
                        print(f"         {booking_code_col}: '{sample_row.get(booking_code_col, 'N/A')}'")
                        print(f"         {guest_col}: '{sample_row.get(guest_col, 'N/A')}'")
                        print(f"         {checkin_col}: '{sample_row.get(checkin_col, 'N/A')}'")
                        print(f"         {checkout_col}: '{sample_row.get(checkout_col, 'N/A')}'")
                        
                        print(f"\n      🔍 Searching for:")
                        print(f"         Booking Code: '{booking_code_value_raw}' (normalized: '{normalized_entry_code}')")
                        print(f"         Guest Name: '{guest_name_value}' (using RapidFuzz > 60)")
                        print(f"         Check-In Date: '{checkin_value}' (matching with ±1 day tolerance)")
                        print(f"         Check-Out Date: '{checkout_value}' (matching with ±1 day tolerance)")
                        print(f"         Note: Either Check-In OR Check-Out date must match (±1 day)")
                    
                    # If booking code matches but other fields don't, show those rows
                    if booking_code_matches:
                        print(f"\n      🔍 Rows with matching Booking Code (showing first 3):")
                        for i, row_idx in enumerate(booking_code_matches[:3]):
                            row = df.loc[row_idx]
                            # #region agent log
                            try:
                                with open(r'c:\Users\ADMIN\Desktop\invoice final\.cursor\debug.log', 'a', encoding='utf-8') as f:
                                    import json
                                    excel_guest = str(row.get(guest_col, 'N/A')).strip()
                                    guest_score_val = guest_scores.at[row_idx] if row_idx in guest_scores.index else None
                                    condition2_val = condition2.at[row_idx] if row_idx in condition2.index else False
                                    condition3_val = condition3.at[row_idx] if row_idx in condition3.index else False
                                    condition4_val = condition4.at[row_idx] if row_idx in condition4.index else False
                                    combined_result = condition1.at[row_idx] and condition2_val and (condition3_val or condition4_val) if row_idx in condition1.index else False
                                    log_entry = {
                                        "id": f"log_{int(__import__('time').time() * 1000)}_{i}",
                                        "timestamp": int(__import__('time').time() * 1000),
                                        "location": "legacy_impl.py:8513",
                                        "message": "Booking code match row analysis",
                                        "data": {
                                            "row_idx": int(row_idx),
                                            "booking_code": str(row.get(booking_code_col, 'N/A')),
                                            "excel_guest_name": excel_guest,
                                            "invoice_guest_name": guest_name_value,
                                            "guest_score": guest_score_val,
                                            "condition2_match": bool(condition2_val),
                                            "condition3_match": bool(condition3_val),
                                            "condition4_match": bool(condition4_val),
                                            "combined_condition_result": bool(combined_result)
                                        },
                                        "runId": "debug1",
                                        "hypothesisId": "A"
                                    }
                                    f.write(json.dumps(log_entry) + '\n')
                            except Exception:
                                pass
                            # #endregion
                            print(f"         Row {row_idx + 1}:")
                            print(f"            Booking Code: '{row.get(booking_code_col, 'N/A')}'")
                            guest_score_val = guest_scores.at[row_idx] if row_idx in guest_scores.index else None
                            condition2_val = condition2.at[row_idx] if row_idx in condition2.index else False
                            print(f"            Guest Name: '{row.get(guest_col, 'N/A')}' (searching: '{guest_name_value}') (score: {guest_score_val}, match: {condition2_val})")
                            excel_checkin = row.get(checkin_col, 'N/A')
                            checkin_match = _date_matches_or_pm1(excel_checkin, checkin_value) if checkin_value else False
                            print(f"            Check-In: '{excel_checkin}' (searching: '{checkin_value}') (match ±1 day: {checkin_match})")
                            excel_checkout = row.get(checkout_col, 'N/A')
                            checkout_match = _date_matches_or_pm1(excel_checkout, checkout_value) if checkout_value else False
                            print(f"            Check-Out: '{excel_checkout}' (searching: '{checkout_value}') (match ±1 day: {checkout_match})")
                
                if matching_row_indices:
                    print(f"   ✅ Found {len(matching_row_indices)} row(s) matching: Booking Code + Guest Name + (Check-In OR Check-Out Date ±1 day)")
                else:
                    print(f"   ⚠️  No row found matching: Booking Code + Guest Name + (Check-In OR Check-Out Date ±1 day)")
                    # FALLBACK: Try GST matching if booking code didn't match
                    print(f"   🔍 Booking Code didn't match - attempting GST-based matching as fallback...")
                    
                    # Extract GST numbers from invoice fields
                    hbd_gst, client_gst = _extract_gst_from_invoice_fields(entry, all_invoice_fields_list)
                    
                    if hbd_gst or client_gst:
                        # Use GST matching as fallback
                        from backend.app.modules.matching.gst import match_by_gst_numbers
                        matching_row_indices, guest_scores, checkin_deltas, checkout_deltas, hbd_gst_matched, client_gst_matched = match_by_gst_numbers(
                            entry, df, field_to_column, hbd_gst, client_gst
                        )
                        
                        # Set match_booking_code to 0 (doesn't match) since booking code didn't match
                        match_booking_code_map[idx] = 0
                        use_gst_matching = True
                        
                        # Store GST match status for later MySQL update (1=matched, 0=not matched, None=not checked)
                        gst_match_map[idx] = {
                            'hbd_gst_match': 1 if hbd_gst_matched is True else (0 if hbd_gst_matched is False else None),
                            'client_gst_match': 1 if client_gst_matched is True else (0 if client_gst_matched is False else None)
                        }
                        
                        if matching_row_indices:
                            print(f"   ✅ GST fallback matching found {len(matching_row_indices)} row(s)")
                        else:
                            print(f"   ⚠️  GST fallback matching also found no matches")
                    else:
                        print(f"   ⚠️  Cannot use GST fallback: Missing GST numbers")
                        print(f"      HBD GST: {'✅' if hbd_gst else '❌'}")
                        print(f"      Client GST: {'✅' if client_gst else '❌'}")
            except Exception as e:
                print(f"   ❌ Error matching by booking code: {e}")
                import traceback
                traceback.print_exc()
                continue
        
        # Find corresponding invoice fields for this entry
        invoice_fields_for_entry = None
        if all_invoice_fields_list:
            # Match invoice fields by Booking Code, Guest Name, and dates
            entry_booking_code = normalize_field_text(entry.get('Booking Code', ''))
            entry_guest_name = normalize_field_text(entry.get('Guest Name', ''))
            entry_checkin = entry.get('Check-In Date', '').strip()
            entry_checkout = entry.get('Check-Out Date', '').strip()
            
            for inv_fields in all_invoice_fields_list:
                inv_booking = normalize_field_text(inv_fields.get('Match Booking Code', ''))
                inv_guest = normalize_field_text(inv_fields.get('Match Guest Name', ''))
                inv_checkin = inv_fields.get('Match Check-In Date', '').strip()
                inv_checkout = inv_fields.get('Match Check-Out Date', '').strip()
                
                # Match by all 4 fields (booking code + guest name + dates)
                if (entry_booking_code and inv_booking and entry_booking_code == inv_booking and
                    entry_guest_name and inv_guest and entry_guest_name.lower() == inv_guest.lower() and
                    entry_checkin and inv_checkin and entry_checkin == inv_checkin and
                    entry_checkout and inv_checkout and entry_checkout == inv_checkout):
                    invoice_fields_for_entry = inv_fields
                    break
                
                # Fallback: match by guest name and dates only (used when booking code is missing)
                if (entry_guest_name and inv_guest and entry_guest_name.lower() == inv_guest.lower() and
                    entry_checkin and inv_checkin and entry_checkin == inv_checkin and
                    entry_checkout and inv_checkout and entry_checkout == inv_checkout):
                    invoice_fields_for_entry = inv_fields
                    break
        
        # Update matching rows
        if matching_row_indices:
            for row_idx in matching_row_indices:
                try:
                    # Check if already marked as "Received"
                    current_value = str(df.at[row_idx, INVOICE_RECEIVED_COLUMN]).strip()
                    if current_value.lower() == INVOICE_RECEIVED_VALUE.lower():
                        print(f"   ⏭️  Row {row_idx + 1} already marked as '{INVOICE_RECEIVED_VALUE}', skipping")
                        rows_skipped += 1
                        # Collect message for Invoice Summary (already marked as Received)
                        pdf_filename = pdf_filename_map.get(idx, 'unknown.pdf')
                        api_push_selection_messages.append((pdf_filename, True, True))  # (filename, is_selected, already_marked)
                        
                        # ============================================================
                        # IMMEDIATE MYSQL UPDATE: Mark as "matched" (already selected)
                        # Also update Excel match diagnostics even for already-marked rows
                        # ============================================================
                        try:
                            from backend.app.modules.invoices.mysql import update_matched_status_in_mysql, update_excel_match_diagnostics_in_mysql, get_db_engine
                            
                            if idx < len(extracted_data_list):
                                extracted_entry = extracted_data_list[idx]
                                entry_booking_code = extracted_entry.get('Booking Code', '').strip()
                                entry_guest_name = extracted_entry.get('Guest Name', '').strip()
                                entry_check_in = extracted_entry.get('Check-In Date', '').strip()
                                entry_check_out = extracted_entry.get('Check-Out Date', '').strip()
                                
                                # Get Excel match diagnostics for this matched row
                                excel_guest_score = guest_scores.at[row_idx] if row_idx in guest_scores.index else None
                                excel_checkin_delta = checkin_deltas.at[row_idx] if row_idx in checkin_deltas.index else None
                                excel_checkout_delta = checkout_deltas.at[row_idx] if row_idx in checkout_deltas.index else None
                                
                                row_id = lookup_row_id_from_mysql(
                                    entry_booking_code, entry_guest_name, entry_check_in, entry_check_out,
                                    inserted_ids_map, pdf_filename
                                )
                                
                                if row_id:
                                    # Write row_id into Excel so API push can join by ID later
                                    try:
                                        df.at[row_idx, MYSQL_ROW_ID_COLUMN] = int(row_id)
                                    except Exception:
                                        df.at[row_idx, MYSQL_ROW_ID_COLUMN] = row_id
                                    mysql_engine = get_db_engine()
                                    match_booking_code = match_booking_code_map.get(idx, None)
                                    gst_matches = gst_match_map.get(idx, {})
                                    
                                    # Generate remarks for already-matched rows
                                    # Determine match_type: GST matching if booking_code didn't match (1) AND GST values exist
                                    # Otherwise, it's booking code matching
                                    has_gst_values = (gst_matches.get('hbd_gst_match') is not None or gst_matches.get('client_gst_match') is not None)
                                    is_gst_matching = (match_booking_code == 1 and has_gst_values)
                                    match_type = "gst" if is_gst_matching else "booking_code"
                                    remarks = generate_matching_remarks(
                                        match_type=match_type,
                                        booking_code_matched=match_booking_code if match_type == "booking_code" else None,
                                        hbd_gst_matched=gst_matches.get('hbd_gst_match') if match_type == "gst" else None,
                                        client_gst_matched=gst_matches.get('client_gst_match') if match_type == "gst" else None,
                                        guest_score=excel_guest_score,
                                        checkin_delta=excel_checkin_delta,
                                        checkout_delta=excel_checkout_delta
                                    )
                                    invoice_amt = invoice_fields_for_entry.get('Amount Paid') if invoice_fields_for_entry else None
                                    api_balance_amt = df.at[row_idx, excel_balance_amount_col] if excel_balance_amount_col and excel_balance_amount_col in df.columns else None
                                    amount_match_val = _compute_amount_match(invoice_amt, api_balance_amt)
                                    if update_matched_status_in_mysql(
                                        row_id=row_id,
                                        matched_status='matched',
                                        match_booking_code=match_booking_code if match_booking_code in (0, 1) else None,
                                        hbd_gst_match=gst_matches.get('hbd_gst_match'),
                                        client_gst_match=gst_matches.get('client_gst_match'),
                                        amount_match=amount_match_val,
                                        remarks=remarks,
                                        engine=mysql_engine
                                    ):
                                        print(f"   ✅ Marked as 'matched' in MySQL (row_id: {row_id})")
                                    else:
                                        print(f"   ⚠️  Failed to mark as 'matched' in MySQL (row_id: {row_id})")
                                    
                                    # NEW: Also update Excel match diagnostics for already-marked rows
                                    if update_excel_match_diagnostics_in_mysql(
                                        row_id,
                                        excel_guest_score,
                                        excel_checkin_delta,
                                        excel_checkout_delta,
                                        mysql_engine
                                    ):
                                        print(f"   ✅ Updated Excel match diagnostics in MySQL for already-marked row (row_id: {row_id})")
                                    else:
                                        print(f"   ⚠️  Could not update Excel match diagnostics in MySQL (row may not exist)")
                                else:
                                    print(f"   ⚠️  Could not find row_id for pdf_filename '{pdf_filename}' - skipping MySQL update")
                        except Exception as e:
                            print(f"   ⚠️  Error updating MySQL for already-marked row (non-fatal): {e}")
                            import traceback
                            traceback.print_exc()
                            # Don't crash - continue processing
                        
                        continue
                    
                    # Update the Invoice Received column
                    df.at[row_idx, INVOICE_RECEIVED_COLUMN] = INVOICE_RECEIVED_VALUE
                    rows_updated += 1
                    
                    # NEW: Store Excel matching diagnostics
                    # Get the computed values for this matched row
                    excel_guest_score = guest_scores.at[row_idx] if row_idx in guest_scores.index else None
                    excel_checkin_delta = checkin_deltas.at[row_idx] if row_idx in checkin_deltas.index else None
                    excel_checkout_delta = checkout_deltas.at[row_idx] if row_idx in checkout_deltas.index else None
                    
                    # NEW: Check if booking code matched (1 = matches, 0 = doesn't match)
                    # Calculate this BEFORE the MySQL update so we have the value
                    # Note: condition1 only exists when booking code matching was used
                    if not use_gst_matching and 'condition1' in locals():
                        booking_code_matched = condition1.at[row_idx] if row_idx in condition1.index else False
                        match_booking_code = 1 if booking_code_matched else 0
                        # #region agent log
                        try:
                            with open(r'c:\Users\ADMIN\Desktop\invoice final\.cursor\debug.log', 'a', encoding='utf-8') as f:
                                import json
                                log_entry = {
                                    "id": f"log_{int(__import__('time').time() * 1000)}_matchcode",
                                    "timestamp": int(__import__('time').time() * 1000),
                                    "location": "legacy_impl.py:8698",
                                    "message": "match_booking_code calculation",
                                    "data": {
                                        "entry_idx": idx,
                                        "row_idx": int(row_idx),
                                        "booking_code": booking_code_value_raw,
                                        "booking_code_matched": bool(booking_code_matched),
                                        "match_booking_code": int(match_booking_code),
                                        "use_gst_matching": use_gst_matching,
                                        "condition1_exists": 'condition1' in locals()
                                    },
                                    "runId": "debug1",
                                    "hypothesisId": "C"
                                }
                                f.write(json.dumps(log_entry) + '\n')
                        except Exception:
                            pass
                        # #endregion
                    else:
                        # GST matching was used, so booking code didn't match
                        booking_code_matched = False  # Initialize to avoid UnboundLocalError
                        match_booking_code = 0
                    match_booking_code_map[idx] = match_booking_code  # Store for this invoice entry
                    
                    # ============================================================
                    # IMMEDIATE MYSQL UPDATE: Mark as "matched" when selected for API push
                    # ============================================================
                    try:
                        from backend.app.modules.invoices.mysql import update_matched_status_in_mysql, get_db_engine
                        
                        pdf_filename = pdf_filename_map.get(idx, 'unknown.pdf')
                        entry_booking_code = extracted_entry.get('Booking Code', '').strip()
                        entry_guest_name = extracted_entry.get('Guest Name', '').strip()
                        entry_check_in = extracted_entry.get('Check-In Date', '').strip()
                        entry_check_out = extracted_entry.get('Check-Out Date', '').strip()
                        
                        row_id = lookup_row_id_from_mysql(
                            entry_booking_code, entry_guest_name, entry_check_in, entry_check_out,
                            inserted_ids_map, pdf_filename
                        )
                        
                        if row_id:
                            # Write row_id into Excel so API push can join by ID later
                            try:
                                df.at[row_idx, MYSQL_ROW_ID_COLUMN] = int(row_id)
                            except Exception:
                                df.at[row_idx, MYSQL_ROW_ID_COLUMN] = row_id
                            mysql_engine = get_db_engine()
                            # Now match_booking_code is already calculated above
                            match_booking_code = match_booking_code_map.get(idx, None)
                            gst_matches = gst_match_map.get(idx, {})  # Get GST match values if they exist
                            
                            # Generate remarks with full diagnostics now that we have all the data
                            # Determine match_type: GST matching if booking_code didn't match (0) AND GST values exist
                            # Otherwise, it's booking code matching
                            has_gst_values = (gst_matches.get('hbd_gst_match') is not None or gst_matches.get('client_gst_match') is not None)
                            is_gst_matching = (match_booking_code == 0 and has_gst_values)
                            match_type = "gst" if is_gst_matching else "booking_code"
                            remarks = generate_matching_remarks(
                                match_type=match_type,
                                booking_code_matched=match_booking_code if match_type == "booking_code" else None,
                                hbd_gst_matched=gst_matches.get('hbd_gst_match') if match_type == "gst" else None,
                                client_gst_matched=gst_matches.get('client_gst_match') if match_type == "gst" else None,
                                guest_score=excel_guest_score,
                                checkin_delta=excel_checkin_delta,
                                checkout_delta=excel_checkout_delta
                            )
                            invoice_amt = invoice_fields_for_entry.get('Amount Paid') if invoice_fields_for_entry else None
                            api_balance_amt = df.at[row_idx, excel_balance_amount_col] if excel_balance_amount_col and excel_balance_amount_col in df.columns else None
                            amount_match_val = _compute_amount_match(invoice_amt, api_balance_amt)
                            if update_matched_status_in_mysql(
                                row_id=row_id,
                                matched_status='matched',
                                match_booking_code=match_booking_code if match_booking_code in (0, 1) else None,
                                hbd_gst_match=gst_matches.get('hbd_gst_match'),  # Will be None if booking code matching was used
                                client_gst_match=gst_matches.get('client_gst_match'),  # Will be None if booking code matching was used
                                amount_match=amount_match_val,
                                remarks=remarks,
                                engine=mysql_engine
                            ):
                                print(f"   ✅ Marked as 'matched' in MySQL (row_id: {row_id})")
                            else:
                                print(f"   ⚠️  Failed to mark as 'matched' in MySQL (row_id: {row_id})")
                        else:
                            print(f"   ⚠️  Could not find row_id for pdf_filename '{pdf_filename}' - skipping MySQL update")
                    except Exception as e:
                        print(f"   ⚠️  Error updating MySQL matched_status (non-fatal): {e}")
                        import traceback
                        traceback.print_exc()
                        # Don't crash - continue processing
                    
                    # Store in Excel columns (ensure numeric dtype)
                    if excel_guest_score is not None and pd.notna(excel_guest_score):
                        try:
                            df.at[row_idx, EXCEL_MATCH_GUEST_SCORE_COLUMN] = int(excel_guest_score)
                        except (TypeError, ValueError):
                            # Fallback: convert column to numeric if needed
                            df[EXCEL_MATCH_GUEST_SCORE_COLUMN] = pd.to_numeric(df[EXCEL_MATCH_GUEST_SCORE_COLUMN], errors='coerce')
                            df.at[row_idx, EXCEL_MATCH_GUEST_SCORE_COLUMN] = int(excel_guest_score)
                    if excel_checkin_delta is not None and pd.notna(excel_checkin_delta):
                        try:
                            df.at[row_idx, EXCEL_MATCH_CHECKIN_DELTA_COLUMN] = int(excel_checkin_delta)
                        except (TypeError, ValueError):
                            # Fallback: convert column to numeric if needed
                            df[EXCEL_MATCH_CHECKIN_DELTA_COLUMN] = pd.to_numeric(df[EXCEL_MATCH_CHECKIN_DELTA_COLUMN], errors='coerce')
                            df.at[row_idx, EXCEL_MATCH_CHECKIN_DELTA_COLUMN] = int(excel_checkin_delta)
                    if excel_checkout_delta is not None and pd.notna(excel_checkout_delta):
                        try:
                            df.at[row_idx, EXCEL_MATCH_CHECKOUT_DELTA_COLUMN] = int(excel_checkout_delta)
                        except (TypeError, ValueError):
                            # Fallback: convert column to numeric if needed
                            df[EXCEL_MATCH_CHECKOUT_DELTA_COLUMN] = pd.to_numeric(df[EXCEL_MATCH_CHECKOUT_DELTA_COLUMN], errors='coerce')
                            df.at[row_idx, EXCEL_MATCH_CHECKOUT_DELTA_COLUMN] = int(excel_checkout_delta)
                    
                    print(f"   📊 Excel Match Diagnostics:")
                    if excel_guest_score is not None and pd.notna(excel_guest_score):
                        print(f"      Guest Name Score: {excel_guest_score}/100")
                    if excel_checkin_delta is not None and pd.notna(excel_checkin_delta):
                        delta_desc = '+1 day' if excel_checkin_delta == 1 else 'same day' if excel_checkin_delta == 0 else '-1 day'
                        print(f"      Check-In Delta: {excel_checkin_delta} ({delta_desc})")
                    if excel_checkout_delta is not None and pd.notna(excel_checkout_delta):
                        delta_desc = '+1 day' if excel_checkout_delta == 1 else 'same day' if excel_checkout_delta == 0 else '-1 day'
                        print(f"      Check-Out Delta: {excel_checkout_delta} ({delta_desc})")
                    print(f"      Booking Code Match: {'✅ Matched' if booking_code_matched else '❌ Not Matched'} (value: {match_booking_code})")
                    
                    # NEW: Update MySQL with Excel matching diagnostics
                    try:
                        from backend.app.modules.invoices.mysql import update_excel_match_diagnostics_in_mysql
                        # Get row_id using lookup function (handles both booking code and GST matching)
                        pdf_filename = pdf_filename_map.get(idx, 'unknown.pdf')
                        
                        # Get entry data for lookup
                        extracted_entry = extracted_data_list[idx] if idx < len(extracted_data_list) else {}
                        entry_booking_code = extracted_entry.get('Booking Code', '').strip()
                        entry_guest_name = extracted_entry.get('Guest Name', '').strip()
                        entry_check_in = extracted_entry.get('Check-In Date', '').strip()
                        entry_check_out = extracted_entry.get('Check-Out Date', '').strip()
                        
                        # Use lookup function which handles:
                        # 1. inserted_ids_map (pdf_filename key) - most reliable
                        # 2. MySQL query with booking_code + guest + dates (if booking_code exists)
                        # 3. MySQL query with guest + dates + pdf_filename (if booking_code missing - GST matching)
                        row_id = lookup_row_id_from_mysql(
                            entry_booking_code, entry_guest_name, entry_check_in, entry_check_out,
                            inserted_ids_map, pdf_filename
                        )
                        
                        if row_id:
                            mysql_engine = get_db_engine()
                            
                            # Update Excel match diagnostics
                            if update_excel_match_diagnostics_in_mysql(
                                row_id,
                                excel_guest_score,
                                excel_checkin_delta,
                                excel_checkout_delta,
                                mysql_engine
                            ):
                                print(f"   ✅ Updated Excel match diagnostics in MySQL")
                            else:
                                print(f"   ⚠️  Could not update Excel match diagnostics in MySQL (row may not exist)")
                            
                            # Update remarks with full diagnostics now that we have all the data
                            match_booking_code = match_booking_code_map.get(idx, None)
                            gst_matches = gst_match_map.get(idx, {})
                            match_type = "gst" if (gst_matches.get('hbd_gst_match') is not None or gst_matches.get('client_gst_match') is not None) else "booking_code"
                            remarks = generate_matching_remarks(
                                match_type=match_type,
                                booking_code_matched=match_booking_code if match_type == "booking_code" else None,
                                hbd_gst_matched=gst_matches.get('hbd_gst_match') if match_type == "gst" else None,
                                client_gst_matched=gst_matches.get('client_gst_match') if match_type == "gst" else None,
                                guest_score=excel_guest_score,
                                checkin_delta=excel_checkin_delta,
                                checkout_delta=excel_checkout_delta
                            )
                            
                            # Update remarks in MySQL
                            if remarks:
                                from backend.app.modules.invoices.mysql import update_matched_status_in_mysql
                                if update_matched_status_in_mysql(
                                    row_id=row_id,
                                    matched_status='matched',  # Keep existing status
                                    remarks=remarks,
                                    engine=mysql_engine
                                ):
                                    print(f"   ✅ Updated remarks in MySQL")
                        else:
                            print(f"   ⚠️  Could not find row ID for pdf_filename '{pdf_filename}' - skipping Excel match diagnostics update")
                    except Exception as e:
                        print(f"   ⚠️  Error updating Excel match diagnostics in MySQL: {e}")
                        import traceback
                        traceback.print_exc()
                    
                    # NEW: Update all invoice data fields in matched row
                    if invoice_fields_for_entry:
                        print(f"   📝 Updating all invoice data fields in row {row_idx + 1}...")
                        
                        # Map master Excel fields to generated Excel columns
                        field_mappings = {
                            'Hotel Name': ['hotelName', 'hotel_name', 'hotel name', 'propertyName', 'property_name', 'property name'],
                            'Guest Name': ['guestName', 'guest_name', 'guest name', 'customer name'],
                            'Booking Code': ['bookingCode', 'booking_code', 'booking code', 'booking id'],
                            'Bill Number': ['billNumber', 'bill_number', 'bill number', 'invoice number', 'invoiceNumber'],
                            'Bill Date': ['billDate', 'bill_date', 'bill date', 'invoice date', 'invoiceDate'],
                            'Check-in Date': ['checkInDate', 'check_in_date', 'check-in date', 'checkin date', 'arrival date'],
                            'Check-out Date': ['checkOutDate', 'check_out_date', 'check-out date', 'checkout date', 'departure date'],
                            'Amount Paid': ['amountPaid', 'amount_paid', 'amount paid', 'total amount', 'totalAmount', 'amount'],
                            'HBD GST Number': ['hbdGst', 'hbd_gst', 'hbd gst', 'hbd gst number', 'hbdGstNumber'],
                            'Client GST Number': ['clientGst', 'client_gst', 'client gst', 'client gst number', 'clientGstNumber'],
                            'CGST Number': ['cgstNumber', 'cgst_number', 'cgst number', 'cgst', 'central gst number'],
                            'SGST Number': ['sgstNumber', 'sgst_number', 'sgst number', 'sgst', 'state gst number'],
                            'Total CGST': ['totalCgst', 'total_cgst', 'total cgst', 'cgst amount', 'cgstAmount', 'cgst total'],
                            'Total SGST': ['totalSgst', 'total_sgst', 'total sgst', 'sgst amount', 'sgstAmount', 'sgst total'],
                            'PDF Filename': ['pdfFilename', 'pdf_filename', 'pdf filename', 'filename'],
                            'Blob URL': ['blobUrl', 'blob_url', 'blob url', 'blob storage url', 'storage url'],
                            'Email From': ['emailFrom', 'email_from', 'email from', 'from email'],
                            'Assigned To': ['assignedTo', 'assigned_to', 'assigned to', 'vendor'],
                            'Processing Date': ['processingDate', 'processing_date', 'processing date']
                        }
                        
                        fields_updated = []
                        for master_field, possible_columns in field_mappings.items():
                            if master_field not in invoice_fields_for_entry:
                                continue
                            
                            value = invoice_fields_for_entry[master_field]
                            if value and value != 'Not Found':
                                # Find matching column in Excel
                                excel_column = None
                                for col_name in possible_columns:
                                    excel_column = find_matching_column(df, col_name)
                                    if excel_column:
                                        break
                                
                                if excel_column:
                                    # Add column if it doesn't exist
                                    if excel_column not in df.columns:
                                        df[excel_column] = ''
                                    
                                    # Update the value (dtype-safe to avoid pandas LossySetitemError)
                                    def _coerce_for_excel_cell(series, v):
                                        if v is None:
                                            return None
                                        try:
                                            if pd.api.types.is_integer_dtype(series.dtype):
                                                return int(float(str(v).strip()))
                                            if pd.api.types.is_float_dtype(series.dtype):
                                                return float(str(v).replace(',', '').strip())
                                        except Exception:
                                            # Fall back to string if conversion fails
                                            return str(v)
                                        return v

                                    df.at[row_idx, excel_column] = _coerce_for_excel_cell(df[excel_column], value)
                                    fields_updated.append(f"{master_field} → {excel_column}")
                                else:
                                    # Column doesn't exist - add it
                                    # Use normalized column name
                                    normalized_col = normalize_column_name(master_field)
                                    if normalized_col not in df.columns:
                                        df[normalized_col] = ''
                                    df.at[row_idx, normalized_col] = _coerce_for_excel_cell(df[normalized_col], value)
                                    fields_updated.append(f"{master_field} → {normalized_col} (new column)")
                        
                        if fields_updated:
                            print(f"      ✅ Updated {len(fields_updated)} field(s): {', '.join(fields_updated[:5])}{'...' if len(fields_updated) > 5 else ''}")
                        else:
                            print(f"      ⚠️  No invoice fields to update (invoice_fields_for_entry is None or empty)")
                    else:
                        print(f"      ⚠️  No invoice fields found for this entry - only updating 'Invoice Received' status")
                    
                    # Build log info with all matching fields
                    log_parts = []
                    if 'Booking Code' in field_to_column:
                        bc_val = str(df.at[row_idx, field_to_column['Booking Code']])
                        log_parts.append(f"Booking Code: '{bc_val}'")
                    if 'Guest Name' in field_to_column:
                        gn_val = str(df.at[row_idx, field_to_column['Guest Name']])
                        log_parts.append(f"Guest Name: '{gn_val}'")
                    if 'Check-In Date' in field_to_column:
                        ci_val = str(df.at[row_idx, field_to_column['Check-In Date']])
                        log_parts.append(f"Check-In: '{ci_val}'")
                    if 'Check-Out Date' in field_to_column:
                        co_val = str(df.at[row_idx, field_to_column['Check-Out Date']])
                        log_parts.append(f"Check-Out: '{co_val}'")
                    
                    log_info = " | ".join(log_parts) if log_parts else "All 4 fields matched"
                    print(f"   ✅ Updated row {row_idx + 1} - {log_info} → '{INVOICE_RECEIVED_VALUE}'")
                    
                    # Collect message for Invoice Summary
                    pdf_filename = pdf_filename_map.get(idx, 'unknown.pdf')
                    api_push_selection_messages.append((pdf_filename, True, False))  # (filename, is_selected, already_marked)
                    
                except Exception as e:
                    print(f"   ❌ Error updating row {row_idx + 1}: {e}")
                    import traceback
                    traceback.print_exc()
                    continue
        else:
            print(f"   ❌ No matching row found for this entry")
            # Collect message for Invoice Summary
            pdf_filename = pdf_filename_map.get(idx, 'unknown.pdf')
            api_push_selection_messages.append((pdf_filename, False, False))  # (filename, is_selected, already_marked)
            # NEW: Determine match_booking_code based on whether booking code actually matched
            # Check if booking code matching was attempted and if booking code matched
            if not use_gst_matching and 'condition1' in locals() and condition1.any():
                # Booking code matched, but overall match failed (likely due to guest name or dates)
                match_booking_code_map[idx] = 1  # 1 = booking code matched
            else:
                # Booking code didn't match (or GST matching was used)
                match_booking_code_map[idx] = 0  # 0 = doesn't match
            
            # ============================================================
            # IMMEDIATE MYSQL UPDATE: Mark as "unmatched" when NOT selected for API push
            # ============================================================
            try:
                from backend.app.modules.invoices.mysql import update_matched_status_in_mysql, get_db_engine
                
                if idx < len(extracted_data_list):
                    extracted_entry = extracted_data_list[idx]
                    entry_booking_code = extracted_entry.get('Booking Code', '').strip()
                    entry_guest_name = extracted_entry.get('Guest Name', '').strip()
                    entry_check_in = extracted_entry.get('Check-In Date', '').strip()
                    entry_check_out = extracted_entry.get('Check-Out Date', '').strip()
                    
                    row_id = lookup_row_id_from_mysql(
                        entry_booking_code, entry_guest_name, entry_check_in, entry_check_out,
                        inserted_ids_map, pdf_filename
                    )
                    
                    if row_id:
                        mysql_engine = get_db_engine()
                        match_booking_code = match_booking_code_map.get(idx, None)
                        
                        if update_matched_status_in_mysql(
                            row_id=row_id,
                            matched_status='unmatched',
                            match_booking_code=match_booking_code if match_booking_code in (0, 1) else None,
                            engine=mysql_engine
                        ):
                            print(f"   ✅ Marked as 'unmatched' in MySQL (row_id: {row_id})")
                        else:
                            print(f"   ⚠️  Failed to mark as 'unmatched' in MySQL (row_id: {row_id})")
                    else:
                        print(f"   ⚠️  Could not find row_id for pdf_filename '{pdf_filename}' - skipping MySQL update")
            except Exception as e:
                print(f"   ⚠️  Error updating MySQL matched_status (non-fatal): {e}")
                import traceback
                traceback.print_exc()
                # Don't crash - continue processing
    
    print(f"\n📊 Matching Summary:")
    print(f"   ✅ Rows updated: {rows_updated}")
    print(f"   ⏭️  Rows skipped (already marked): {rows_skipped}")
    print(f"   📋 Total rows in Excel: {len(df)}")
    
    # Save updated Excel file (save if rows were updated OR if column was added)
    if rows_updated > 0 or column_was_added:
        try:
            print(f"\n💾 Saving updated Excel file...")
            print(f"   📁 File path: {MATCHING_EXCEL_FILE_PATH}")
            if column_was_added and rows_updated == 0:
                print(f"   💡 Saving file because new column '{INVOICE_RECEIVED_COLUMN}' was added")
            
            # Check if file is open (try to open it in append mode first)
            try:
                # Try to open the file to check if it's locked
                test_file = open(MATCHING_EXCEL_FILE_PATH, 'r+b')
                test_file.close()
            except PermissionError:
                print(f"   ⚠️  File might be open in Excel. Please close it and try again.")
                print(f"   💡 The file will be saved when you close Excel and run the function again.")
                return rows_updated  # Return the count but don't save yet
            
            from openpyxl import load_workbook
            from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
            
            # Save DataFrame to Excel
            print(f"   💾 Writing DataFrame to Excel...")
            df.to_excel(MATCHING_EXCEL_FILE_PATH, index=False, engine='openpyxl')
            print(f"   ✅ DataFrame saved successfully")
            
            # Load with openpyxl to apply formatting
            wb = load_workbook(MATCHING_EXCEL_FILE_PATH)
            ws = wb.active
            
            # Apply header formatting
            header_font = Font(bold=True, color="FFFFFF", size=11)
            header_fill = PatternFill(start_color="2F5597", end_color="2F5597", fill_type="solid")
            header_alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            
            for cell in ws[1]:
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = header_alignment
            
            # Add borders
            thin_border = Border(
                left=Side(style='thin'),
                right=Side(style='thin'),
                top=Side(style='thin'),
                bottom=Side(style='thin')
            )
            
            for row in ws.iter_rows():
                for cell in row:
                    cell.border = thin_border
                    if cell.row > 1:
                        cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
            
            # Freeze header row
            ws.freeze_panes = "A2"
            
            wb.save(MATCHING_EXCEL_FILE_PATH)
            wb.close()
            
            print(f"✅ Saved updated Excel file: {MATCHING_EXCEL_FILE_PATH}")
            print(f"   📊 Total rows in saved file: {len(df)}")
            print(f"   ✅ Rows marked as 'Received': {len(df[df[INVOICE_RECEIVED_COLUMN].astype(str).str.strip().str.lower() == INVOICE_RECEIVED_VALUE.lower()])}")
            if column_was_added and rows_updated == 0:
                print(f"   💡 File saved because new column '{INVOICE_RECEIVED_COLUMN}' was added")
            
            # Push all "Received" rows to API (merge with Master Excel data)
            # Validate drive_uploader is available
            if drive_uploader is None:
                print(f"   ❌ ERROR: drive_uploader is None - cannot merge with Master Excel")
                print(f"   ❌ CANCELLING API push operation")
                print(f"   💡 Please ensure ENABLE_GOOGLE_DRIVE_UPLOAD=true in .env and Google Drive authentication succeeded")
            else:
                push_received_rows_to_api(df, drive_uploader)
            
        except PermissionError as e:
            print(f"❌ Permission denied: Cannot save Excel file")
            print(f"   The file might be open in Excel. Please close it and try again.")
            print(f"   File path: {MATCHING_EXCEL_FILE_PATH}")
        except Exception as e:
            print(f"⚠️  Error saving Excel file: {e}")
            import traceback
            traceback.print_exc()
    else:
        print(f"\n⚠️  No rows were updated (rows_updated = {rows_updated})")
        print(f"   This could mean:")
        print(f"   1. No matching rows were found in the Excel file")
        print(f"   2. All matching rows were already marked as 'Received'")
        print(f"   3. Column matching failed (check logs above for column mappings)")
    
    # Update matched_status in MySQL for all processed invoices
    print(f"\n💾 Updating matched_status in MySQL...")
    mysql_engine = get_db_engine()
    # REMOVED: MySQL updates now happen immediately when selection decision is made
    # No need for this end-of-process update loop anymore
    # This ensures that if an error occurs mid-process, invoices already marked as "matched" 
    # will remain marked correctly in MySQL
    
    # Return rows updated and API push selection messages for Invoice Summary
    return rows_updated, api_push_selection_messages

def match_master_sheet_with_excel():
    """Entrypoint for POST /api/match-master. Master Excel / Drive is disabled."""
    print("=" * 70)
    print("🔗 MATCHING INVOICE MASTER SHEET WITH EXCEL")
    print("=" * 70)
    print("📊 Master Excel disabled - using MySQL storage only")
    rows_matched = match_invoice_master_sheet_with_excel(None)
    if rows_matched > 0:
        print(f"\n✅ Successfully matched and updated {rows_matched} row(s) in Excel file")
    else:
        print(f"\n⚠️  No rows matched or updated")

