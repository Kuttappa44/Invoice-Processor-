"""Ingest conductor: Gmail -> extract/classify -> Blob -> MySQL -> Step 7."""

from __future__ import annotations

import email
import imaplib
import os
import threading
from datetime import datetime, timedelta
from typing import Optional

from dotenv import load_dotenv
from sqlalchemy import text

from backend.app.exceptions import ProcessCancelled
from backend.app.modules.ingest.email_parse import (
    _part_text,
    decode_email_subject,
    extract_attachment_info,
    extract_comprehensive_email_data,
    extract_vendor_name_improved,
    load_vendor_reference,
)
from backend.app.modules.ingest.persist import update_excel_sheet
from backend.app.modules.ingest.vendor import find_vendor_column, upload_pdf_to_blob_storage
from backend.app.shared.run_excels import clear_run_excels
from backend.app.shared.text import normalize_field_text
from backend.app.integrations.azure_blob import sanitize_blob_url
from backend.app.integrations.gmail_imap import (
    fetch_internal_date,
    fetch_rfc822,
    get_imap_mailbox,
    only_unread_mode,
    search_unread_uids,
    select_imap_mailbox,
)
from backend.app.modules.invoices.mysql import get_db_engine

load_dotenv()

def _load_known_message_ids() -> set:
    """
    Message-IDs of emails that already produced at least one row in the invoice table.

    Used in "process all emails" mode to skip already-ingested emails BEFORE the expensive
    PDF rendering / Blob upload / GPT extraction. Returns an empty set (no skipping) on any
    error or when SKIP_ALREADY_PROCESSED_EMAILS=false.
    """
    if os.getenv("SKIP_ALREADY_PROCESSED_EMAILS", "true").strip().lower() not in ("1", "true", "yes", "on"):
        return set()
    try:
        from backend.app.modules.invoices.mysql import get_invoice_table_name

        table_name = get_invoice_table_name()
        engine = get_db_engine()
        with engine.connect() as conn:
            rows = conn.execute(
                text(f"SELECT DISTINCT message_id FROM {table_name} WHERE message_id IS NOT NULL AND message_id <> ''")
            ).fetchall()
        known = {str(r[0]) for r in rows if r and r[0]}
        print(f"🗂️  Loaded {len(known)} already-processed Message-IDs from {table_name} (will skip these emails)")
        return known
    except Exception as e:
        print(f"⚠️  Could not preload processed Message-IDs ({e}); no early skipping this run")
        return set()


def process_invoices(
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    cancel_event: Optional[threading.Event] = None,
):
    """Main function to process invoices from Gmail
    
    Args:
        from_date: Start date for email search (YYYY-MM-DD format). REQUIRED.
        to_date: End date for email search (YYYY-MM-DD format). REQUIRED.
        cancel_event: When set, the run exits cooperatively at safe points (between emails).

    Raises:
        ValueError: if from_date / to_date is missing, malformed, or from_date > to_date.
    """
    # A date range is mandatory: never run over an open-ended / default window.
    if not from_date or not to_date:
        raise ValueError(
            "from_date and to_date are required (YYYY-MM-DD). "
            "Running without a date range is not allowed."
        )
    try:
        _from_dt = datetime.strptime(from_date, "%Y-%m-%d")
        _to_dt = datetime.strptime(to_date, "%Y-%m-%d")
    except ValueError:
        raise ValueError("from_date and to_date must be in YYYY-MM-DD format.")
    if _from_dt > _to_dt:
        raise ValueError("from_date must not be after to_date.")

    def _abort_if_cancelled() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise ProcessCancelled("Invoice processing was cancelled.")

    # Mailbox volume is counted per calendar day immediately before that day's emails
    # are processed, so the dashboard can pick up completed days before the full range ends.

    # Booking/API Excel for matching is generated inside Step 7 (run_step7_from_test1_no_mysql).
    # MATCHING_EXCEL_FILE_PATH remains from EXCEL_FILE_PATH / main shim for other entrypoints.
    
    print("📧 FINAL INVOICE PROCESSOR - COMPLETE FLOW")
    print("=" * 50)
    print("1. 📧 Check unread emails")
    print("2. 📝 Extract invoice data from email content")
    print("3. ☁️  Upload PDFs to Azure Blob Storage (organized by vendor)")
    print("4. 📊 Save extracted data to MySQL")
    print("=" * 50)
    
    # OCR model loading removed - now using OpenAI native PDF support
    # No need to load OCR models since PDFs are sent directly to OpenAI
    #print("\n✅ Using OpenAI native PDF support (no OCR model needed)")
    
    # Check for Blob Storage configuration
    blob_sas_url = os.getenv('BLOB_STORAGE_SAS_URL')
    if not blob_sas_url:
        print("⚠️  WARNING: BLOB_STORAGE_SAS_URL not set in environment variables")
        print("⚠️  PDFs will not be uploaded to Blob Storage")
    else:
        print("✅ Blob Storage SAS URL configured")
    
    print("\n📊 Master Excel disabled - using MySQL storage only")
    
    # Load vendor reference data
    vendor_reference = load_vendor_reference()
    print(f"✅ Loaded {len(vendor_reference)} vendor references")
    
    # IMAP connection helper functions
    def connect_imap():
        """Connect to IMAP server with retry logic"""
        max_retries = 10
        for attempt in range(max_retries):
            try:
                mail = imaplib.IMAP4_SSL(os.getenv('IMAP_SERVER'), int(os.getenv('IMAP_PORT')))
                mail.login(os.getenv('GMAIL_EMAIL_1'), os.getenv('GMAIL_PASSWORD_1'))
                select_imap_mailbox(mail)
                return mail
            except Exception as e:
                if attempt < max_retries - 1:
                    print(f"⚠️  Connection attempt {attempt + 1} failed: {e}. Retrying...")
                    import time
                    time.sleep(2)  # Wait before retry
                else:
                    raise
    
    def reconnect_imap(mail):
        """Reconnect to IMAP if connection is lost"""
        try:
            mail.close()
        except:
            pass
        try:
            mail.logout()
        except:
            pass
        print("🔄 Reconnecting to IMAP server...")
        return connect_imap()
    
    # Connect to Gmail
    try:
        mail = connect_imap()
        print(f"✅ Connected to Gmail successfully! (mailbox: {get_imap_mailbox()})")
    except Exception as e:
        print(f"❌ Gmail connection failed after retries: {e}")
        return

    _abort_if_cancelled()
    _mail_kind = "unread emails" if only_unread_mode() else "emails (read + unread)"
    
    # Process emails day by day (count → process → Step-7 → save found counts)
    try:
        processed_invoice_ids = []
        processed_email_ids_set = set()  # Track processed email IDs to prevent duplicates after reconnection
        invoice_count = 0
        folder_counts = {'UNASSIGNED': 0}
        all_invoice_data = []  # Store all invoice data for Excel
        all_api_push_selection_messages = []  # Collect all API push selection messages for Invoice Summary
        found_by_day: dict = {}

        def _bump_found(day_key: str, *, emails: int = 0, pdfs: int = 0) -> None:
            bucket = found_by_day.setdefault(
                day_key, {"emails_found": 0, "pdfs_found": 0}
            )
            bucket["emails_found"] += emails
            bucket["pdfs_found"] += pdfs

        def _save_found_counts(day_key: Optional[str] = None) -> None:
            try:
                from backend.app.modules.invoices.mysql import upsert_mailbox_found_counts

                if day_key:
                    counts = found_by_day.get(
                        day_key, {"emails_found": 0, "pdfs_found": 0}
                    )
                    rows = [{"count_date": day_key, **counts}]
                else:
                    if not found_by_day:
                        return
                    rows = [
                        {"count_date": day, **counts}
                        for day, counts in sorted(found_by_day.items())
                    ]
                written = upsert_mailbox_found_counts(rows)
                emails_found_total = sum(int(r.get("emails_found") or 0) for r in rows)
                pdfs_found_total = sum(int(r.get("pdfs_found") or 0) for r in rows)
                print(
                    f"   📬 Found counts saved ({written} day(s)): "
                    f"emails_found={emails_found_total}  pdfs_found={pdfs_found_total}"
                )
            except Exception as _found_err:
                print(f"   ⚠️  Failed to save emails/PDFs found counts: {_found_err}")

        step7_every_n = int(os.getenv("STEP7_EVERY_N_EMAILS", "100"))
        if step7_every_n < 1:
            step7_every_n = 100
        emails_since_step7 = 0
        day_str = from_date

        # In "all emails" mode, read emails come back on every run; skip ones already in MySQL.
        known_message_ids = set() if only_unread_mode() else _load_known_message_ids()

        def _run_step7_batch(reason: str):
            """Run Step 7 (match + API push) using unpushed MySQL rows directly."""
            print(f"\n{'='*70}")
            print(f"🔄 STEP-7: {reason}")
            print(f"{'='*70}")
            try:
                from backend.app.modules.matching.service import run as _run_step7
                step7_result = _run_step7(
                    push=True,
                    immediate_push=True,
                )
                if step7_result == "skipped":
                    print("   ⏭️  Step-7 skipped: no unpushed MySQL rows")
                else:
                    print(f"   ✅ Step-7 matching + API push completed")
            except Exception as _step7_err:
                print(f"   ⚠️  Step-7 auto-trigger failed (non-fatal): {_step7_err}")
                import traceback
                traceback.print_exc()
    
        from backend.app.modules.mailbox.counts import count_mailbox_range

        day_cursor = _from_dt.date()
        day_end = _to_dt.date()
        while day_cursor <= day_end:
            day_str = day_cursor.strftime("%Y-%m-%d")
            _abort_if_cancelled()
            print(f"\n{'='*70}")
            print(f"📅 DAY {day_str}")
            print(f"{'='*70}")

            print(f"\n📬 Counting mailbox emails and PDFs for {day_str}...")
            try:
                count_mailbox_range(day_str, day_str)
                print(f"   ✅ Mailbox counts saved for {day_str} (Load Stats to refresh dashboard)")
            except Exception as _count_err:
                print(f"   ⚠️  Mailbox count failed for {day_str} — continuing: {_count_err}")

            try:
                try:
                    mail.noop()
                except Exception:
                    mail = reconnect_imap(mail)
                print(
                    f"🔍 Searching for {_mail_kind} on {day_str} in {get_imap_mailbox()}"
                )
                recent_emails = search_unread_uids(
                    mail, from_date=day_str, to_date=day_str
                )
                print(f"📧 Found {len(recent_emails)} {_mail_kind} on {day_str}")
            except Exception as e:
                print(f"❌ Error searching emails for {day_str}: {e}")
                try:
                    mail = reconnect_imap(mail)
                    recent_emails = search_unread_uids(
                        mail, from_date=day_str, to_date=day_str
                    )
                    print(f"📧 Found {len(recent_emails)} {_mail_kind} on {day_str} after reconnect")
                except Exception as e2:
                    print(f"❌ Search still failing for {day_str}: {e2}")
                    recent_emails = []

            emails_since_step7 = 0

            for i, email_id in enumerate(recent_emails):
                _abort_if_cancelled()
                # Convert email_id to string for consistent tracking (handles both bytes and string)
                email_id_str = email_id.decode('utf-8') if isinstance(email_id, bytes) else str(email_id)

                # Skip if this email was already processed (prevents duplicates after reconnection)
                if email_id_str in processed_email_ids_set:
                    print(f"\n⏭️  Skipping email {i+1}/{len(recent_emails)} - already processed (ID: {email_id_str[:20]}...)")
                    continue
        
                counted_found = False
                try:
                    print(f"\n📨 Processing email {i+1}/{len(recent_emails)} ({day_str})")
            
                    # Fetch email with reconnection on failure
                    max_fetch_retries = 10
                    email_message = None
                    for fetch_attempt in range(max_fetch_retries):
                        try:
                            payload = fetch_rfc822(mail, email_id)
                            email_message = email.message_from_bytes(payload)
                            break
                        except Exception as fetch_error:
                            if fetch_attempt < max_fetch_retries - 1:
                                print(f"⚠️  Fetch attempt {fetch_attempt + 1} failed: {fetch_error}. Reconnecting...")
                                # Reconnect and retry
                                mail = reconnect_imap(mail)
                                import time
                                time.sleep(1)  # Brief pause before retry
                            else:
                                print(f"❌ Failed to fetch email after {max_fetch_retries} attempts: {fetch_error}")
                                raise
            
                    if email_message is None:
                        print(f"❌ Could not fetch email {i+1}, skipping...")
                        processed_email_ids_set.add(email_id_str)  # Mark as processed to skip on retry
                        _bump_found(day_str, emails=1)
                        counted_found = True
                        continue

                    # Date the email arrived in the mailbox (IMAP INTERNALDATE, IST). None if unavailable.
                    email_arrival_date = fetch_internal_date(mail, email_id)
                    if email_arrival_date is None:
                        print("   ⚠️  Could not read INTERNALDATE; email_date will be NULL for this email")
                    elif not (_from_dt.date() <= email_arrival_date <= _to_dt.date()):
                        print(
                            f"   ⚠️  Email arrival date {email_arrival_date} (IST) is outside the requested "
                            f"range {from_date} → {to_date}"
                        )

                    _bump_found(day_str, emails=1)
                    pdf_attachments = extract_attachment_info(email_message)[0]
                    _bump_found(day_str, pdfs=len(pdf_attachments))
                    counted_found = True

                    if known_message_ids:
                        _mid = normalize_field_text(email_message.get('Message-ID', ''))
                        if _mid and _mid in known_message_ids:
                            print(f"   ⏭️  Already processed (Message-ID in database) - skipping")
                            processed_email_ids_set.add(email_id_str)
                            continue
            
                    # Extract basic email info
                    subject = decode_email_subject(email_message.get('Subject', ''))
                    from_email = email_message.get('From', '')
                    body = ""
            
                    if email_message.is_multipart():
                        for part in email_message.walk():
                            if part.get_content_type() == "text/plain":
                                body = _part_text(part)
                                if body:
                                    break
                    else:
                        body = _part_text(email_message)
            
                    print(f"   From: {from_email}")
                    print(f"   Subject: {subject}")
            
                    # Process only emails that carry at least one PDF; skip the rest.
                    _has_pdf = bool(pdf_attachments)
                    if _has_pdf:
                        print("   🎯 INVOICE DETECTED! (PDF attachment found)")
                        invoice_count += 1
                
                        # Extract comprehensive email data
                        comprehensive_data = extract_comprehensive_email_data(email_message, subject, from_email, body)
                        email_details = comprehensive_data.get('email_booking_details', {}) or {}
                        raw_pdf_analysis = comprehensive_data.get('pdf_analysis', {})
                        if isinstance(raw_pdf_analysis, dict):
                            pdf_analysis_data = {k: v for k, v in raw_pdf_analysis.items() if isinstance(v, dict)}
                        else:
                            pdf_analysis_data = {}

                        # Only create synthetic entry if we have email details but NO PDFs
                        # If we have PDFs, they should be processed regardless of email extraction
                        if (not pdf_analysis_data or len(pdf_analysis_data) == 0) and email_details:
                            synthetic_entry = {
                                'hotel_name': email_details.get('property_name') or 'Not Found',
                                'guest_name': email_details.get('guest_name') or 'Not Found',
                                'arrival_date': email_details.get('check_in_date') or 'Not Found',
                                'departure_date': email_details.get('check_out_date') or 'Not Found',
                                'bill_number': 'Not Found',
                                'bill_date': 'Not Found',
                                'room_number': 'Not Found',
                                'total_amount': 'Not Found',
                                'gst_number': 'Not Found',
                                'pan_number': 'Not Found',
                                'filename': 'email_content',
                                'payload': None,
                                'generated_from_email': True
                            }
                            pdf_analysis_data = {'email_table_1': synthetic_entry}
                        # If we have PDFs, process them (email data may or may not be present)
                        elif pdf_analysis_data and len(pdf_analysis_data) > 0:
                            if email_details:
                                print(f"   📄 Processing {len(pdf_analysis_data)} PDF(s) with email data")
                            else:
                                print(f"   📄 Processing {len(pdf_analysis_data)} PDF(s) - email extraction returned no data, but PDFs will be processed")

                        comprehensive_data['pdf_analysis'] = pdf_analysis_data

                        # Extract vendor name using email table data or improved method
                        email_property_name = comprehensive_data.get('email_property_name')
                        pdf_data_for_vision = None
                        attachments_list = comprehensive_data.get('attachments', [])
                        if attachments_list and len(attachments_list) > 0:
                            # Filter to only dictionary attachments
                            valid_attachments = [a for a in attachments_list if isinstance(a, dict)]
                            for attachment in valid_attachments:
                                if attachment.get('content_type') == 'application/pdf' and attachment.get('payload'):
                                    pdf_data_for_vision = attachment['payload']
                                    break
                
                        vendor_name = None
                        if email_property_name and isinstance(email_property_name, str):
                            vendor_name = email_property_name.strip()
                            if vendor_name:
                                print(f"   🏨 Property name from email table: {vendor_name}")
                
                        if not vendor_name or vendor_name.lower() in ['unknown', 'unknown vendor', 'not found']:
                            vendor_name = extract_vendor_name_improved(
                                subject, from_email, body, 
                                pdf_analysis_data, 
                                pdf_data_for_vision,
                                email_property_name=email_property_name  # Pass email property name for priority
                            )
                
                        # Find the correct column for this vendor
                        assigned_column = find_vendor_column(vendor_name, vendor_reference)
                
                        print(f"   🏨 Extracted vendor: {vendor_name}")
                        print(f"   📁 Assigned to: {assigned_column}")
                        print(f"   🔧 DEBUG: assigned_column type: {type(assigned_column)}, value: '{assigned_column}'")
                
                        # Extract amounts for display
                        amounts = comprehensive_data.get('amounts', [])
                        if amounts:
                            print(f"   💰 Amounts found: {amounts}")
                
                        # Extract urgency level
                        urgency = comprehensive_data.get('urgency_level', 'NORMAL')
                        print(f"   ⚡ Urgency: {urgency}")
                
                        # Process each PDF attachment for upload
                        if pdf_analysis_data:
                            pdf_analysis = pdf_analysis_data
                            pdf_count = 0
                            for pdf_key, pdf_data in pdf_analysis.items():
                                # Ensure pdf_data is a dictionary
                                if not isinstance(pdf_data, dict):
                                    print(f"   ⚠️  PDF data item {pdf_key} is not a dictionary, skipping")
                                    continue
                        
                                # Skip upload for synthetic entries generated from email content
                                if pdf_data.get('generated_from_email'):
                                    print(f"   📝 Using email-derived booking data (no PDF attachment)")
                                    continue
                        
                                # Skip if no payload (can't upload)
                                if not pdf_data.get('payload'):
                                    print(f"   ⚠️  Skipping PDF {pdf_count + 1} - no payload")
                                    continue
                            
                                pdf_count += 1
                        
                                # Upload PDF to Azure Blob Storage
                                blob_url = upload_pdf_to_blob_storage(pdf_data, assigned_column)
                                if blob_url:
                                    pdf_data['blob_url'] = sanitize_blob_url(blob_url)
                                    folder_key = assigned_column if assigned_column != 'Not Found' else 'UNASSIGNED'
                                    folder_counts[folder_key] = folder_counts.get(folder_key, 0) + 1
                                    print(f"   💾 Uploaded PDF {pdf_count}: {pdf_data.get('filename', 'unknown.pdf')}")
                                else:
                                    print(f"   ❌ Failed to upload PDF {pdf_count} to Blob Storage")
                    
                            if pdf_count > 0:
                                print(f"   ✅ Uploaded {pdf_count} PDF(s) to Blob Storage: {assigned_column} folder")
                        else:
                            print("   ⚠️  No PDF attachments found")
                
                        # Store invoice data for Excel
                        invoice_data = {
                            'from_email': from_email,
                            'subject': subject,
                            'message_id': comprehensive_data.get('message_id'),
                            'email_date': email_arrival_date,
                            'processing_datetime': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                            'assigned_column': assigned_column,
                            'urgency_level': urgency,
                            'amounts': amounts,
                            'pdf_analysis': pdf_analysis_data,
                            'booking_code': comprehensive_data.get('booking_code'),  # Store booking code from email
                            'email_booking_details': comprehensive_data.get('email_booking_details', {}),
                            'email_guest_name': comprehensive_data.get('email_guest_name'),
                            'email_property_name': comprehensive_data.get('email_property_name'),
                            'email_check_in_date': comprehensive_data.get('email_check_in_date'),
                            'email_check_out_date': comprehensive_data.get('email_check_out_date'),
                            'email_table_best_entry': comprehensive_data.get('email_table_best_entry'),
                            'email_table_entries': comprehensive_data.get('email_table_entries', [])
                        }
                        all_invoice_data.append(invoice_data)  # Keep for final statistics
                
                        # Save invoice data to MySQL (Master Excel disabled)
                        print(f"\n   📊 Saving invoice data to MySQL...")
                        new_rows, inserted_ids_map = update_excel_sheet([invoice_data])
                        if new_rows is None:
                            print(f"   ❌ Failed to save invoice data to MySQL")
                        elif new_rows:
                            print(f"   ✅ Invoice data saved to MySQL ({len(new_rows)} row(s))")
                        else:
                            print(f"   ⚠️  No invoice rows saved to MySQL (duplicate or skipped)")
                
                        # NOTE: Per-invoice matching + push + forwarding disabled.
                        # Matching and API push is now handled by run_step7_from_test1_no_mysql
                        # which runs every STEP7_EVERY_N_EMAILS (default 100) and once at end for remainder.
                
                        # Track processed invoice emails for stats / duplicate prevention.
                        # IMAP read/unread flags are left unchanged (peek fetch, no \\Seen).
                        processed_invoice_ids.append(email_id)
                        processed_email_ids_set.add(email_id_str)  # Track in set to prevent duplicates
                    else:
                        print("   📄 Regular email (no PDF) - leaving IMAP flags unchanged")
                        # Still track non-invoice emails to prevent re-processing after reconnection
                        processed_email_ids_set.add(email_id_str)

                except ProcessCancelled:
                    raise
                except Exception as e:
                    print(f"   ❌ Error processing email {i+1}: {e}")
                    # Track failed emails too to prevent infinite retry loops
                    processed_email_ids_set.add(email_id_str)
                    if not counted_found:
                        _bump_found(day_str, emails=1)
                    continue
                finally:
                    # Count each non-duplicate email iteration; run Step 7 every STEP7_EVERY_N_EMAILS (default 100)
                    emails_since_step7 += 1
                    if emails_since_step7 >= step7_every_n:
                        _run_step7_batch(
                            f"{day_str}: every {step7_every_n} emails processed"
                        )
                        emails_since_step7 = 0


            if emails_since_step7 > 0:
                _run_step7_batch(
                    f"{day_str}: final batch ({emails_since_step7} email(s) since last Step-7)"
                )

            _save_found_counts(day_str)
            print(f"\n✅ Finished {day_str} — counts saved. Load Stats to see this day on the dashboard.")
            day_cursor += timedelta(days=1)

        _abort_if_cancelled()
        # IMAP read/unread flags are left as they were (BODY.PEEK fetch; no mark-seen).

        # Note: Matching and API push are handled by run_step7_from_test1_no_mysql (triggered above)
    
        # Final statistics
        try:
            # Always a true unread count (independent of PROCESS_ONLY_UNREAD).
            unread_after = len(search_unread_uids(mail, unread_only=True))
            print(f"\n📧 Unread emails after processing: {unread_after}")
            print(f"📊 Invoice emails processed: {len(processed_invoice_ids)}")
            print(f"🎯 Invoices found: {invoice_count}")
            print(
                f"📬 Emails found this run: {sum(v['emails_found'] for v in found_by_day.values())}  "
                f"PDFs found this run: {sum(v['pdfs_found'] for v in found_by_day.values())}"
            )
        
            # Print summary
            print(f"\n📊 INVOICE SUMMARY:")
            print("-" * 30)
            for folder, count in folder_counts.items():
                if count > 0:
                    print(f"📁 {folder}: {count} PDFs uploaded")
        
            # Print API push selection status for all invoices
            if all_api_push_selection_messages:
                print(f"\n📋 API Push Selection Status:")
                print("-" * 30)
                for pdf_filename, is_selected, already_marked in all_api_push_selection_messages:
                    if is_selected:
                        if already_marked:
                            print(f"   ✅ Invoice selected for API push: {pdf_filename} (already marked)")
                        else:
                            print(f"   ✅ Invoice selected for API push: {pdf_filename}")
                    else:
                        print(f"   ❌ Invoice not selected for API push: {pdf_filename}")
        
        except Exception as e:
            print(f"⚠️  Error getting final statistics: {e}")
        _save_found_counts()
        # Master Excel disabled - no Excel link to return
        return None
    except ProcessCancelled as pc:
        print(f"\n⏹️  {pc}")
        _save_found_counts()
        return None
    finally:
        try:
            mail.close()
            mail.logout()
        except Exception:
            pass
        clear_run_excels()

