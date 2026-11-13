"""Write extracted invoice rows to MySQL (Master Excel disabled)."""

from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Optional

from sqlalchemy import text

from backend.app.shared.dates import format_date_to_dd_mm_yy
from backend.app.shared.text import normalize_field_text
from backend.app.integrations.azure_blob import sanitize_blob_url
from backend.app.modules.invoices.mysql import (
    convert_row_for_mysql as _db_convert_row_for_mysql,
    ensure_invoice_report_table_exists,
    get_db_engine,
    update_classification_flags_in_mysql,
)

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

def pdf_filename_with_match_suffix(base_name: str, match_idx: int) -> str:
    """Insert _match{idx} before the file extension (e.g. a.pdf -> a_match1.pdf)."""
    if not base_name:
        return f"_match{match_idx}"
    if "." in base_name:
        stem, ext = base_name.rsplit(".", 1)
        return f"{stem}_match{match_idx}.{ext}"
    return f"{base_name}_match{match_idx}"


def update_excel_sheet(invoice_data_list):
    """Save invoice data to MySQL only (Master Excel disabled)
    
    Args:
        invoice_data_list: List of invoice data dictionaries
    """
    if not invoice_data_list:
        print("⚠️  No invoice data to save")
        return None, None
    
    # Ensure MySQL table exists
    print("📊 Ensuring MySQL invoice table exists...")
    ensure_invoice_report_table_exists()
    
    # Get MySQL engine
    mysql_engine = get_db_engine()
    
    # Master Excel disabled - only using MySQL storage
    print("📊 Master Excel disabled - saving to MySQL only")
    
    try:
        # Prepare new data for MySQL insertion
        new_rows = []
        inserted_ids_map = {}  # Map pdf_filename to inserted row ID

        print(f"📊 Processing {len(invoice_data_list)} invoice(s) for MySQL insertion...")
        for invoice_idx, invoice in enumerate(invoice_data_list, 1):
            print(f"   📄 Processing invoice {invoice_idx}/{len(invoice_data_list)}...")
            pdf_analysis = invoice.get('pdf_analysis', {})
            if not isinstance(pdf_analysis, dict):
                print(f"   ⚠️  Skipping invoice - pdf_analysis is not a dictionary (type: {type(pdf_analysis)})")
                continue

            email_details = invoice.get('email_booking_details', {}) or {}
            table_entries = invoice.get('email_table_entries') or []
            email_from = invoice.get('from_email', '').replace('<', '').replace('>', '')
            email_subject_raw = invoice.get('subject', '')
            email_subject = email_subject_raw[:50] + '...' if len(email_subject_raw) > 50 else email_subject_raw
            assigned_folder = invoice.get('assigned_column', 'UNASSIGNED')
            urgency_level = invoice.get('urgency_level', 'NORMAL')
            message_id = normalize_field_text(invoice.get('message_id')) or None
            email_date = invoice.get('email_date')
            processing_datetime = invoice.get('processing_datetime') or datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            def format_date_if_needed(value: Optional[str]) -> str:
                """Format date to dd/mm/yy format and strip time"""
                return format_date_to_dd_mm_yy(value)

            def resolve_email_amount(source: dict) -> str:
                """Amount from matched email table row: total_amount, else advance_paid_amount."""
                if not source or not isinstance(source, dict):
                    return 'Not Found'
                for key in ('total_amount', 'advance_paid_amount'):
                    raw = source.get(key)
                    if raw:
                        m = re.search(r'\d[\d,\.]*', str(raw))
                        if m:
                            return m.group(0).replace(',', '')
                return 'Not Found'

            # Check if we have any PDF attachments (for filename tracking)
            has_pdf_attachments = False
            for pdf_key, pdf_data in pdf_analysis.items():
                if isinstance(pdf_data, dict) and pdf_data.get('payload') and not pdf_data.get('generated_from_email'):
                    has_pdf_attachments = True
                    break
            
            # Priority: 1) Merged PDFs (have both email and PDF data), 2) Standalone PDFs, 3) Table entries only (no PDFs)
            # Check if we have any valid PDFs to process
            has_any_valid_pdf = False
            has_merged_pdf_data = False
            for pdf_key, pdf_data in pdf_analysis.items():
                if isinstance(pdf_data, dict) and pdf_data.get('hotel_name') != 'PDF couldn\'t be processed':
                    has_any_valid_pdf = True
                    # Check if this PDF was merged (has booking_code from email merge)
                    if pdf_data.get('booking_code') and pdf_data.get('booking_code') != 'Not Found':
                        has_merged_pdf_data = True
                        break
            
            # Process PDFs if they exist (merged PDFs take priority over table entries)
            if has_any_valid_pdf:
                # Always process PDFs if they exist, regardless of email extraction results
                # This ensures PDFs are processed even when email extraction returns nothing
                pass  # Will fall through to PDF processing below
            elif table_entries:
                # No PDFs at all - skip unmatched email table entries (not writing to MySQL)
                print(f"   ⏭️  Skipping {len(table_entries)} unmatched email table entry(ies) - no PDF attachments found")
                continue

            # Process PDF attachments - read from merged PDF data
            matched_table_entries = set()  # Track which table entries were matched to PDFs
            if has_any_valid_pdf or (not table_entries and has_pdf_attachments):
                # Process each PDF attachment - use merged PDF data which contains both email and PDF fields
                for pdf_key, pdf_data in pdf_analysis.items():
                    if not isinstance(pdf_data, dict):
                        continue
                    if pdf_data.get('generated_from_email'):
                        continue
                    if pdf_data.get('hotel_name') == 'PDF couldn\'t be processed':
                        continue

                    from backend.app.shared.pdf_pages import count_pdf_pages as _count_pdf_pages

                    _pc = pdf_data.get('page_count')
                    if _pc is None or (isinstance(_pc, int) and _pc < 1):
                        _pc = _count_pdf_pages(pdf_data.get('payload'))
                    _multi_page_pdf = None if _pc is None else ('yes' if _pc > 1 else 'no')
                    _pdf_label = pdf_data.get('filename') or pdf_key
                    print(f"   Multi Page PDF: {_pdf_label} -> {_multi_page_pdf}")

                    # CHANGED: Get all matched entries for this PDF
                    all_matched_entries = pdf_data.get('matched_table_entries', [])
                    
                    if not all_matched_entries:
                        # No matches - process PDF once with PDF data only
                        print(f"   📋 PDF {pdf_key} has no email match - will be processed with PDF data only")
                        
                        # Extract PDF data (same as before)
                        hotel_name = pdf_data.get('hotel_name', 'Not Found')
                        guest_name = pdf_data.get('guest_name', 'Not Found')
                        booking_code = pdf_data.get('booking_code') or 'Not Found'
                        bill_number = pdf_data.get('bill_number', 'Not Found')
                        bill_date = pdf_data.get('bill_date', 'Not Found')
                        check_in_date = format_date_if_needed(pdf_data.get('arrival_date') or pdf_data.get('check_in_date')) or 'Not Found'
                        check_out_date = format_date_if_needed(pdf_data.get('departure_date') or pdf_data.get('check_out_date')) or 'Not Found'
                        total_amount = pdf_data.get('total_amount', 'Not Found')
                        if total_amount != 'Not Found':
                            total_match = re.search(r'\d[\d,\.]*', str(total_amount))
                            total_amount = total_match.group(0).replace(',', '') if total_match else 'Not Found'
                        
                        hbd_gst_number = pdf_data.get('hbd_gst_number') or pdf_data.get('hbd_gst', 'Not Found')
                        client_gst_number = pdf_data.get('client_gst_number') or pdf_data.get('client_gst', 'Not Found')
                        total_cgst = pdf_data.get('total_cgst', 'Not Found')
                        total_sgst = pdf_data.get('total_sgst', 'Not Found')
                        
                        if total_cgst != 'Not Found':
                            cgst_match = re.search(r'\d[\d,\.]*', str(total_cgst))
                            total_cgst = cgst_match.group(0).replace(',', '') if cgst_match else 'Not Found'
                        
                        if total_sgst != 'Not Found':
                            sgst_match = re.search(r'\d[\d,\.]*', str(total_sgst))
                            total_sgst = sgst_match.group(0).replace(',', '') if sgst_match else 'Not Found'
                        
                        _nf_inline = {
                            'room_charge': pdf_data.get('room_charge', 'Not Found'),
                            'total_tariff': pdf_data.get('total_tariff', 'Not Found'),
                            'other_charges': pdf_data.get('other_charges', 'Not Found'),
                            'total_igst': pdf_data.get('total_igst', 'Not Found'),
                            'total_taxes': pdf_data.get('total_taxes', 'Not Found'),
                            'discount': pdf_data.get('discount', 'Not Found'),
                            'advance_received': pdf_data.get('advance_received', 'Not Found'),
                        }
                        for _k, _v in _nf_inline.items():
                            if _v != 'Not Found':
                                _m = re.search(r'\d[\d,\.]*', str(_v))
                                _nf_inline[_k] = _m.group(0).replace(',', '') if _m else 'Not Found'

                        original_filename = pdf_data.get('filename', '')
                        processing_date_for_filename = datetime.now().strftime('%Y%m%d')
                        safe_filename = re.sub(r'[^\w\s\-\.]', '', original_filename).strip().replace(' ', '_')
                        pdf_filename = f"{processing_date_for_filename}_{safe_filename}"
                        blob_url = sanitize_blob_url(pdf_data.get('blob_url', '') or '')

                        new_row = {
                            'S.No': None,
                            'Processing Date': processing_datetime,
                            'Email From': email_from,
                            'Message ID': message_id,
                            'Email Date': email_date,
                            'Assigned To': assigned_folder,
                            'Hotel Name': normalize_field_text(hotel_name) or 'Not Found',
                            'Guest Name': normalize_field_text(guest_name) or 'Not Found',
                            'Booking Code': normalize_field_text(booking_code) or 'Not Found',
                            'Bill Number': normalize_field_text(bill_number) or 'Not Found',
                            'Bill Date': bill_date if bill_date else 'Not Found',
                            'Check-in Date': check_in_date,
                            'Check-out Date': check_out_date,
                            'Amount Paid': total_amount,
                            'Email Amount': 'Not Found',
                            'HBD GST Number': normalize_field_text(hbd_gst_number) if hbd_gst_number != 'Not Found' else 'Not Found',
                            'Client GST Number': normalize_field_text(client_gst_number) if client_gst_number != 'Not Found' else 'Not Found',
                            'Total CGST': total_cgst if total_cgst != 'Not Found' else 'Not Found',
                            'Total SGST': total_sgst if total_sgst != 'Not Found' else 'Not Found',
                            'Room Charge': _nf_inline['room_charge'],
                            'Total Tariff': _nf_inline['total_tariff'],
                            'Other Charges': _nf_inline['other_charges'],
                            'Total IGST': _nf_inline['total_igst'],
                            'Total Taxes': _nf_inline['total_taxes'],
                            'Discount': _nf_inline['discount'],
                            'Advance Received': _nf_inline['advance_received'],
                            'PDF Filename': pdf_filename,
                            'Blob URL': blob_url,
                            'Multi Page PDF': _multi_page_pdf,
                            'Matched Status': None
                        }
                        
                        print(f"   📝 SAVING TO MYSQL (PDF has no email match - using PDF data only) for {pdf_filename}:")
                        print(f"      Hotel Name: {new_row['Hotel Name']}")
                        print(f"      Guest Name: {new_row['Guest Name']}")
                        print(f"      Booking Code: {new_row['Booking Code']}")

                        # Duplicates are no longer skipped: detect by business key (ignores
                        # message_id), insert anyway, then mark as 'duplicate' (highest priority).
                        from backend.app.modules.invoices.mysql import (
                            business_key_duplicate_exists,
                            mark_row_duplicate_in_mysql,
                        )
                        is_duplicate_row = business_key_duplicate_exists(new_row, mysql_engine)
                        inserted_id = insert_row_to_mysql(new_row, mysql_engine)
                        if inserted_id:
                            inserted_ids_map[pdf_filename] = inserted_id
                            update_classification_flags_in_mysql(
                                inserted_id, pdf_data.get('_classification'), mysql_engine
                            )
                            if is_duplicate_row:
                                mark_row_duplicate_in_mysql(inserted_id, mysql_engine)
                            print(f"   ✅ Successfully saved to MySQL")
                        else:
                            print(f"   ⚠️  Failed to insert row to MySQL")
                        
                        new_rows.append(new_row)
                    else:
                        # CHANGED: Process PDF multiple times - once for each matched email entry
                        print(f"   📋 PDF {pdf_key} matched to {len(all_matched_entries)} email table entry/entries - creating {len(all_matched_entries)} MySQL row(s)")
                        
                        # Extract base PDF data (same for all matches)
                        base_hotel_name = pdf_data.get('hotel_name', 'Not Found')
                        base_guest_name = pdf_data.get('guest_name', 'Not Found')
                        base_bill_number = pdf_data.get('bill_number', 'Not Found')
                        base_bill_date = pdf_data.get('bill_date', 'Not Found')
                        base_check_in_date = format_date_if_needed(pdf_data.get('arrival_date') or pdf_data.get('check_in_date')) or 'Not Found'
                        base_check_out_date = format_date_if_needed(pdf_data.get('departure_date') or pdf_data.get('check_out_date')) or 'Not Found'
                        base_total_amount = pdf_data.get('total_amount', 'Not Found')
                        if base_total_amount != 'Not Found':
                            total_match = re.search(r'\d[\d,\.]*', str(base_total_amount))
                            base_total_amount = total_match.group(0).replace(',', '') if total_match else 'Not Found'
                        
                        base_hbd_gst_number = pdf_data.get('hbd_gst_number') or pdf_data.get('hbd_gst', 'Not Found')
                        base_client_gst_number = pdf_data.get('client_gst_number') or pdf_data.get('client_gst', 'Not Found')
                        base_total_cgst = pdf_data.get('total_cgst', 'Not Found')
                        base_total_sgst = pdf_data.get('total_sgst', 'Not Found')
                        
                        if base_total_cgst != 'Not Found':
                            cgst_match = re.search(r'\d[\d,\.]*', str(base_total_cgst))
                            base_total_cgst = cgst_match.group(0).replace(',', '') if cgst_match else 'Not Found'
                        
                        if base_total_sgst != 'Not Found':
                            sgst_match = re.search(r'\d[\d,\.]*', str(base_total_sgst))
                            base_total_sgst = sgst_match.group(0).replace(',', '') if sgst_match else 'Not Found'
                        
                        _nf_matched = {
                            'room_charge': pdf_data.get('room_charge', 'Not Found'),
                            'total_tariff': pdf_data.get('total_tariff', 'Not Found'),
                            'other_charges': pdf_data.get('other_charges', 'Not Found'),
                            'total_igst': pdf_data.get('total_igst', 'Not Found'),
                            'total_taxes': pdf_data.get('total_taxes', 'Not Found'),
                            'discount': pdf_data.get('discount', 'Not Found'),
                            'advance_received': pdf_data.get('advance_received', 'Not Found'),
                        }
                        for _k, _v in _nf_matched.items():
                            if _v != 'Not Found':
                                _m = re.search(r'\d[\d,\.]*', str(_v))
                                _nf_matched[_k] = _m.group(0).replace(',', '') if _m else 'Not Found'

                        original_filename = pdf_data.get('filename', '')
                        processing_date_for_filename = datetime.now().strftime('%Y%m%d')
                        safe_filename = re.sub(r'[^\w\s\-\.]', '', original_filename).strip().replace(' ', '_')
                        base_pdf_filename = f"{processing_date_for_filename}_{safe_filename}"
                        blob_url = pdf_data.get('blob_url', '')
                        
                        # Create one MySQL row for each matched email entry
                        for match_idx, (matched_entry, match_diagnostics) in enumerate(all_matched_entries, 1):
                            entry_booking_code = matched_entry.get('booking_code') or 'Not Found'
                            matched_entry_index = match_diagnostics['matched_entry_index']
                            matched_table_entries.add(matched_entry_index)
                            
                            # Use PDF data for all fields, but booking_code from the matched email entry
                            # For multiple matches, insert match number before extension so .pdf stays last
                            if len(all_matched_entries) > 1:
                                pdf_filename = pdf_filename_with_match_suffix(base_pdf_filename, match_idx)
                            else:
                                pdf_filename = base_pdf_filename
                            
                            new_row = {
                                'S.No': None,
                                'Processing Date': processing_datetime,
                                'Email From': email_from,
                                'Message ID': message_id,
                                'Email Date': email_date,
                                'Assigned To': assigned_folder,
                                'Hotel Name': normalize_field_text(base_hotel_name) or 'Not Found',
                                'Guest Name': normalize_field_text(base_guest_name) or 'Not Found',
                                'Booking Code': normalize_field_text(entry_booking_code) or 'Not Found',  # Different booking code per match
                                'Bill Number': normalize_field_text(base_bill_number) or 'Not Found',
                                'Bill Date': base_bill_date if base_bill_date else 'Not Found',
                                'Check-in Date': base_check_in_date,
                                'Check-out Date': base_check_out_date,
                                'Amount Paid': base_total_amount,
                                'Email Amount': resolve_email_amount(matched_entry),
                                'HBD GST Number': normalize_field_text(base_hbd_gst_number) if base_hbd_gst_number != 'Not Found' else 'Not Found',
                                'Client GST Number': normalize_field_text(base_client_gst_number) if base_client_gst_number != 'Not Found' else 'Not Found',
                                'Total CGST': base_total_cgst if base_total_cgst != 'Not Found' else 'Not Found',
                                'Total SGST': base_total_sgst if base_total_sgst != 'Not Found' else 'Not Found',
                                'Room Charge': _nf_matched['room_charge'],
                                'Total Tariff': _nf_matched['total_tariff'],
                                'Other Charges': _nf_matched['other_charges'],
                                'Total IGST': _nf_matched['total_igst'],
                                'Total Taxes': _nf_matched['total_taxes'],
                                'Discount': _nf_matched['discount'],
                                'Advance Received': _nf_matched['advance_received'],
                                'PDF Filename': pdf_filename,
                                'Blob URL': blob_url,
                                'Multi Page PDF': _multi_page_pdf,
                                'Matched Status': None
                            }
                            
                            print(f"   📝 SAVING TO MYSQL (Match {match_idx}/{len(all_matched_entries)} - PDF matched to email table entry {matched_entry_index+1}) for {pdf_filename}:")
                            print(f"      Hotel Name: {new_row['Hotel Name']}")
                            print(f"      Guest Name: {new_row['Guest Name']}")
                            print(f"      Booking Code: {new_row['Booking Code']} (from email entry {matched_entry_index+1})")
                            print(f"      Email Amount: {new_row['Email Amount']}")

                            # Duplicates are no longer skipped: detect by business key (ignores
                            # message_id), insert anyway, then mark as 'duplicate' (highest priority).
                            from backend.app.modules.invoices.mysql import (
                                business_key_duplicate_exists,
                                mark_row_duplicate_in_mysql,
                            )
                            is_duplicate_row = business_key_duplicate_exists(new_row, mysql_engine)
                            inserted_id = insert_row_to_mysql(new_row, mysql_engine)
                            if inserted_id:
                                inserted_ids_map[pdf_filename] = inserted_id
                                update_classification_flags_in_mysql(
                                    inserted_id, pdf_data.get('_classification'), mysql_engine
                                )
                                if is_duplicate_row:
                                    mark_row_duplicate_in_mysql(inserted_id, mysql_engine)
                                print(f"   ✅ Successfully saved to MySQL")
                            else:
                                print(f"   ⚠️  Failed to insert row to MySQL")
                            
                            new_rows.append(new_row)
                
                # After processing PDFs, skip unmatched table entries
                if table_entries and has_any_valid_pdf:
                    unmatched_table_entries = [entry for idx, entry in enumerate(table_entries) if idx not in matched_table_entries]
                    if unmatched_table_entries:
                        print(f"   ⏭️  Skipping {len(unmatched_table_entries)} unmatched email table entry(ies) - no PDF match found")
                
                # If no PDFs but we have email data, create one row (fallback case)
                if not has_pdf_attachments:
                    # Use email details or invoice-level data with fallback chain
                    hotel_name = invoice.get('email_property_name') or email_details.get('property_name') or 'Not Found'
                    guest_name = invoice.get('email_guest_name') or email_details.get('guest_name') or 'Not Found'
                    booking_code = invoice.get('booking_code') or email_details.get('booking_code') or 'Not Found'
                    check_in_date = format_date_if_needed(
                        invoice.get('email_check_in_date') or 
                        email_details.get('check_in_date')
                    ) or 'Not Found'
                    check_out_date = format_date_if_needed(
                        invoice.get('email_check_out_date') or 
                        email_details.get('check_out_date')
                    ) or 'Not Found'
                    total_amount = 'Not Found'
                    total_amount_raw = email_details.get('total_amount')
                    if total_amount_raw:
                        amt_match = re.search(r'\d[\d,\.]*', str(total_amount_raw))
                        total_amount = amt_match.group(0).replace(',', '') if amt_match else 'Not Found'
                    
                    if hotel_name != 'Not Found' or guest_name != 'Not Found':
                        new_row = {
                            'S.No': None,  # Will be auto-incremented by MySQL
                            'Processing Date': processing_datetime,
                            'Email From': email_from,
                            'Message ID': message_id,
                            'Email Date': email_date,
                            'Assigned To': assigned_folder,
                            'Hotel Name': normalize_field_text(hotel_name) or 'Not Found',
                            'Guest Name': normalize_field_text(guest_name) or 'Not Found',
                            'Booking Code': normalize_field_text(booking_code) or 'Not Found',
                            'Bill Number': 'Not Found',
                            'Bill Date': 'Not Found',
                            'Check-in Date': check_in_date,
                            'Check-out Date': check_out_date,
                            'Amount Paid': total_amount,
                            'HBD GST Number': 'Not Found',
                            'Client GST Number': 'Not Found',
                            'Total CGST': 'Not Found',
                            'Total SGST': 'Not Found',
                            'Room Charge': 'Not Found',
                            'Total Tariff': 'Not Found',
                            'Other Charges': 'Not Found',
                            'Total IGST': 'Not Found',
                            'Total Taxes': 'Not Found',
                            'Discount': 'Not Found',
                            'Advance Received': 'Not Found',
                            'PDF Filename': 'email_content',
                            'Blob URL': '',
                            'Matched Status': None
                        }
                        
                        # Skip MySQL insertion for email-only entries (no PDFs)
                        print(f"   ⏭️  Skipping MySQL insertion for email-only entry (no PDF attachment)")
                        
                        new_rows.append(new_row)
        
        if not new_rows:
            print("⚠️  No valid invoice data to save (all rows were duplicates or failed)")
            try:
                mysql_engine.dispose()
            except:
                pass
            return [], {}
        
        print(f"📊 Summary: {len(new_rows)} row(s) were processed - MySQL insertion was attempted for each row above")
        
        # Clean up MySQL engine
        try:
            mysql_engine.dispose()
        except:
            pass
        
        print(f"✅ All invoice data saved to MySQL (Master Excel disabled)")
        return new_rows, inserted_ids_map  # Return new_rows and inserted_ids_map
        
    except Exception as e:
        print(f"❌ Error saving invoice data to MySQL: {e}")
        import traceback
        traceback.print_exc()
        # Clean up MySQL engine on error
        try:
            mysql_engine.dispose()
        except:
            pass
        return None, None
