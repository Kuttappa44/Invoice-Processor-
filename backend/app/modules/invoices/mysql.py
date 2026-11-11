"""
MySQL persistence layer.

Phase 2 modularization: these implementations were extracted from `legacy_impl.py` without
changing behavior (same SQL, same prints, same parsing).
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

import pandas as pd
from sqlalchemy import text

_UNSET = object()

# We intentionally reuse the existing date formatting helper to avoid behavior changes.
from backend.app.shared.dates import format_date_to_dd_mm_yy
from backend.app.shared.gstin import gst_field_value_for_mysql_with_invalid
from backend.app.integrations.azure_blob import sanitize_blob_url


def _push_cancel_reasons(
    *,
    gst_invalid_row: bool,
    gst_missing_row: bool,
    guest_name_missing: bool,
    dates_missing: bool,
    bill_number_missing: bool,
    bill_date_missing: bool,
    amount_missing: bool,
    amount_zero: bool,
) -> list[str]:
    """Human-readable reasons for pre-push cancellation (stable order, no duplicates)."""
    reasons: list[str] = []
    if gst_invalid_row:
        reasons.append("invalid gst number")
    if gst_missing_row:
        reasons.append("missing gst number")
    if guest_name_missing:
        reasons.append("missing guest name")
    if dates_missing:
        reasons.append("missing dates")
    if bill_number_missing:
        reasons.append("missing invoice number")
    if bill_date_missing:
        reasons.append("missing bill date")
    if amount_missing or amount_zero:
        reasons.append("missing amount")
    return reasons


def get_invoice_table_name():
    """Return invoice table name from env INVOICE_TABLE_NAME, default 'invoice_data'. Safe for SQL (alphanumeric + underscore only)."""
    from backend.app.config import get_settings

    return get_settings().invoice_table_name


def get_db_engine():
    """Get MySQL database engine connection (same config as load_data.py)"""
    from backend.app.database import get_engine

    return get_engine()


MAILBOX_DAILY_COUNTS_TABLE = "mailbox_daily_counts"
MAILBOX_INSTANCE_TO_SOURCE = {
    "a": "bills",
    "b": "xtract",
    "c": "btcbills",
}
MAILBOX_SOURCES = frozenset(MAILBOX_INSTANCE_TO_SOURCE.values())


def normalize_mailbox_source(source: Optional[str] = None, instance: Optional[str] = None) -> str:
    """Map UI source or scheduled instance (a/b/c) to bills|xtract|btcbills."""
    raw_source = (source or "").strip().lower()
    if raw_source in MAILBOX_SOURCES:
        return raw_source
    if raw_source in MAILBOX_INSTANCE_TO_SOURCE:
        return MAILBOX_INSTANCE_TO_SOURCE[raw_source]
    raw_instance = (instance or os.getenv("SCHEDULED_INSTANCE") or "a").strip().lower()
    return MAILBOX_INSTANCE_TO_SOURCE.get(raw_instance, "bills")


def ensure_mailbox_daily_counts_table_exists(engine=None) -> None:
    """Create mailbox_daily_counts if missing (email + PDF volume per source/day)."""
    own_engine = engine is None
    if engine is None:
        engine = get_db_engine()
    create_sql = f"""
    CREATE TABLE IF NOT EXISTS {MAILBOX_DAILY_COUNTS_TABLE} (
        id INT AUTO_INCREMENT PRIMARY KEY,
        source VARCHAR(32) NOT NULL,
        instance VARCHAR(16) NOT NULL,
        imap_user VARCHAR(255) NULL,
        count_date DATE NOT NULL,
        emails INT NOT NULL DEFAULT 0,
        pdfs INT NOT NULL DEFAULT 0,
        emails_found INT NOT NULL DEFAULT 0,
        pdfs_found INT NOT NULL DEFAULT 0,
        counted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uq_source_date (source, count_date),
        INDEX idx_count_date (count_date)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """
    try:
        with engine.connect() as conn:
            conn.execute(text(create_sql))
            conn.commit()
            for column_name in ("emails_found", "pdfs_found"):
                check_sql = text(
                    """
                    SELECT COUNT(*) AS col_count
                    FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND TABLE_NAME = :table_name
                      AND COLUMN_NAME = :column_name
                    """
                )
                exists = conn.execute(
                    check_sql,
                    {"table_name": MAILBOX_DAILY_COUNTS_TABLE, "column_name": column_name},
                ).fetchone()[0] > 0
                if not exists:
                    conn.execute(
                        text(
                            f"ALTER TABLE {MAILBOX_DAILY_COUNTS_TABLE} "
                            f"ADD COLUMN {column_name} INT NOT NULL DEFAULT 0"
                        )
                    )
                    conn.commit()
    finally:
        if own_engine:
            engine.dispose()


def upsert_mailbox_daily_counts(
    rows: list,
    *,
    source: Optional[str] = None,
    instance: Optional[str] = None,
    imap_user: Optional[str] = None,
    engine=None,
) -> int:
    """Insert or update one row per (source, count_date). Returns rows written."""
    if not rows:
        return 0
    source_id = normalize_mailbox_source(source, instance)
    instance_id = (instance or os.getenv("SCHEDULED_INSTANCE") or "a").strip() or "a"
    imap_user = (imap_user or os.getenv("GMAIL_EMAIL_1") or "").strip() or None
    own_engine = engine is None
    if engine is None:
        engine = get_db_engine()
    ensure_mailbox_daily_counts_table_exists(engine)
    sql = text(
        f"""
        INSERT INTO {MAILBOX_DAILY_COUNTS_TABLE}
            (source, instance, imap_user, count_date, emails, pdfs)
        VALUES
            (:source, :instance, :imap_user, :count_date, :emails, :pdfs)
        ON DUPLICATE KEY UPDATE
            instance = VALUES(instance),
            imap_user = VALUES(imap_user),
            emails = VALUES(emails),
            pdfs = VALUES(pdfs),
            counted_at = CURRENT_TIMESTAMP
        """
    )
    written = 0
    try:
        with engine.begin() as conn:
            for row in rows:
                count_date = row.get("count_date") or row.get("date")
                if not count_date:
                    continue
                conn.execute(
                    sql,
                    {
                        "source": source_id,
                        "instance": instance_id[:16],
                        "imap_user": imap_user,
                        "count_date": str(count_date)[:10],
                        "emails": int(row.get("emails") or 0),
                        "pdfs": int(row.get("pdfs") or 0),
                    },
                )
                written += 1
    finally:
        if own_engine:
            engine.dispose()
    return written


def upsert_mailbox_found_counts(
    rows: list,
    *,
    source: Optional[str] = None,
    instance: Optional[str] = None,
    imap_user: Optional[str] = None,
    engine=None,
) -> int:
    """Insert or update emails_found / pdfs_found without overwriting mailbox volume."""
    if not rows:
        return 0
    source_id = normalize_mailbox_source(source, instance)
    instance_id = (instance or os.getenv("SCHEDULED_INSTANCE") or "a").strip() or "a"
    imap_user = (imap_user or os.getenv("GMAIL_EMAIL_1") or "").strip() or None
    own_engine = engine is None
    if engine is None:
        engine = get_db_engine()
    ensure_mailbox_daily_counts_table_exists(engine)
    sql = text(
        f"""
        INSERT INTO {MAILBOX_DAILY_COUNTS_TABLE}
            (source, instance, imap_user, count_date, emails, pdfs, emails_found, pdfs_found)
        VALUES
            (:source, :instance, :imap_user, :count_date, 0, 0, :emails_found, :pdfs_found)
        ON DUPLICATE KEY UPDATE
            emails_found = VALUES(emails_found),
            pdfs_found = VALUES(pdfs_found),
            counted_at = CURRENT_TIMESTAMP
        """
    )
    written = 0
    try:
        with engine.begin() as conn:
            for row in rows:
                count_date = row.get("count_date") or row.get("date")
                if not count_date:
                    continue
                conn.execute(
                    sql,
                    {
                        "source": source_id,
                        "instance": instance_id[:16],
                        "imap_user": imap_user,
                        "count_date": str(count_date)[:10],
                        "emails_found": int(row.get("emails_found") or 0),
                        "pdfs_found": int(row.get("pdfs_found") or 0),
                    },
                )
                written += 1
    finally:
        if own_engine:
            engine.dispose()
    return written


def ensure_invoice_report_table_exists():
    """Ensure invoice table exists, create if missing, and migrate legacy columns/indexes."""
    table_name = get_invoice_table_name()
    engine = get_db_engine()
    create_table_sql = f"""
    CREATE TABLE IF NOT EXISTS {table_name} (
        id INT AUTO_INCREMENT PRIMARY KEY,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        s_no INT,
        email_from VARCHAR(255),
        assigned_to VARCHAR(100),
        hotel_name TEXT,
        guest_name TEXT,
        booking_code VARCHAR(100),
        bill_number VARCHAR(100),
        bill_date DATE,
        check_in_date DATE,
        check_out_date DATE,
        amount_paid DECIMAL(15, 2),
        hbd_gst_number VARCHAR(50),
        client_gst_number VARCHAR(50),
        validation_flag TINYINT NOT NULL DEFAULT 0,
        total_cgst DECIMAL(15, 2),
        total_sgst DECIMAL(15, 2),
        pdf_filename VARCHAR(255),
        blob_url TEXT,
        matched_status VARCHAR(20) DEFAULT NULL,
        INDEX idx_booking_code (booking_code),
        INDEX idx_email_from (email_from),
        INDEX idx_created_at (created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
    """
    try:
        with engine.connect() as conn:
            # Create table if it doesn't exist
            conn.execute(text(create_table_sql))
            conn.commit()

            # Check if matched_status column exists, add it if missing (for existing tables)
            check_matched_status_sql = f"""
            SELECT COUNT(*) as col_count
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
            AND TABLE_NAME = '{table_name}'
            AND COLUMN_NAME = 'matched_status'
            """
            result = conn.execute(text(check_matched_status_sql))
            matched_status_exists = result.fetchone()[0] > 0

            if not matched_status_exists:
                print(f"   Adding 'matched_status' column to existing {table_name} table...")
                alter_table_sql = f"ALTER TABLE {table_name} ADD COLUMN matched_status VARCHAR(20) DEFAULT NULL AFTER blob_url"
                conn.execute(text(alter_table_sql))
                conn.commit()
                print(f"   ✓ Added 'matched_status' column to {table_name} table")

            # NEW: Match diagnostics columns (for understanding how PDF↔email entry was matched)
            def _ensure_column(column_name: str, alter_sql: str) -> None:
                check_sql = f"""
                SELECT COUNT(*) as col_count
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                AND TABLE_NAME = '{table_name}'
                AND COLUMN_NAME = '{column_name}'
                """
                result = conn.execute(text(check_sql))
                exists = result.fetchone()[0] > 0
                if not exists:
                    print(f"   Adding '{column_name}' column to existing {table_name} table...")
                    conn.execute(text(alter_sql))
                    conn.commit()
                    print(f"   ✓ Added '{column_name}' column to {table_name} table")

            def _drop_column_if_exists(column_name: str) -> None:
                check_sql = f"""
                SELECT COUNT(*) as col_count
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                AND TABLE_NAME = '{table_name}'
                AND COLUMN_NAME = '{column_name}'
                """
                result = conn.execute(text(check_sql))
                exists = result.fetchone()[0] > 0
                if exists:
                    print(f"   Dropping legacy column '{column_name}' from {table_name}...")
                    conn.execute(text(f"ALTER TABLE {table_name} DROP COLUMN `{column_name}`"))
                    conn.commit()
                    print(f"   ✓ Dropped '{column_name}'")

            def _drop_index_if_exists(index_name: str) -> None:
                check_sql = f"""
                SELECT COUNT(*) AS n
                FROM INFORMATION_SCHEMA.STATISTICS
                WHERE TABLE_SCHEMA = DATABASE()
                AND TABLE_NAME = '{table_name}'
                AND INDEX_NAME = '{index_name}'
                """
                result = conn.execute(text(check_sql))
                if result.fetchone()[0] > 0:
                    print(f"   Dropping index '{index_name}' from {table_name}...")
                    conn.execute(text(f"ALTER TABLE {table_name} DROP INDEX `{index_name}`"))
                    conn.commit()
                    print(f"   ✓ Dropped index '{index_name}'")

            def _ensure_index(index_name: str, create_sql: str) -> None:
                check_sql = f"""
                SELECT COUNT(*) AS idx_count
                FROM INFORMATION_SCHEMA.STATISTICS
                WHERE TABLE_SCHEMA = DATABASE()
                AND TABLE_NAME = '{table_name}'
                AND INDEX_NAME = '{index_name}'
                """
                result = conn.execute(text(check_sql))
                if result.fetchone()[0] == 0:
                    print(f"   Adding index '{index_name}' to {table_name} table...")
                    try:
                        conn.execute(text(create_sql))
                        conn.commit()
                        print(f"   ✓ Added index '{index_name}' to {table_name} table")
                    except Exception as index_error:
                        print(f"   ⚠️  Could not add index '{index_name}': {index_error}")

            _ensure_column(
                "message_id",
                f"ALTER TABLE {table_name} ADD COLUMN message_id VARCHAR(255) NULL AFTER email_from",
            )
            # Date the email arrived in the mailbox (IMAP INTERNALDATE, IST).
            _ensure_column(
                "email_date",
                f"ALTER TABLE {table_name} ADD COLUMN email_date DATE NULL AFTER message_id",
            )

            # Remove legacy processing_datetime. The unique dedupe index is intentionally
            # dropped (and never recreated): duplicate invoices are now stored and marked
            # matched_status='duplicate' (see business_key_duplicate_exists).
            _drop_index_if_exists("uq_invoice_dedupe")
            _drop_index_if_exists("idx_processing_datetime")
            _drop_column_if_exists("processing_datetime")

            # Non-unique index to keep the duplicate lookup fast.
            _ensure_index(
                "idx_bill_number",
                f"CREATE INDEX idx_bill_number ON {table_name}(bill_number)",
            )
            _ensure_index(
                "idx_created_at",
                f"CREATE INDEX idx_created_at ON {table_name}(created_at)",
            )

            # NEW: Excel matching diagnostic columns
            _ensure_column(
                "excel_match_guest_score",
                f"ALTER TABLE {table_name} ADD COLUMN excel_match_guest_score INT NULL AFTER matched_status",
            )
            _ensure_column(
                "excel_match_checkin_delta",
                f"ALTER TABLE {table_name} ADD COLUMN excel_match_checkin_delta TINYINT NULL AFTER excel_match_guest_score",
            )
            _ensure_column(
                "excel_match_checkout_delta",
                f"ALTER TABLE {table_name} ADD COLUMN excel_match_checkout_delta TINYINT NULL AFTER excel_match_checkin_delta",
            )
            
            # NEW: Booking code match indicator (0 = matches, 1 = doesn't match)
            _ensure_column(
                "match_booking_code",
                f"ALTER TABLE {table_name} ADD COLUMN match_booking_code TINYINT NULL AFTER excel_match_checkout_delta",
            )
            
            # NEW: GST match indicators (0 = matches, 1 = doesn't match, NULL = not checked)
            _ensure_column(
                "hbd_gst_match",
                f"ALTER TABLE {table_name} ADD COLUMN hbd_gst_match TINYINT NULL AFTER match_booking_code",
            )
            _ensure_column(
                "client_gst_match",
                f"ALTER TABLE {table_name} ADD COLUMN client_gst_match TINYINT NULL AFTER hbd_gst_match",
            )
            _ensure_column(
                "amount_match",
                f"ALTER TABLE {table_name} ADD COLUMN amount_match TINYINT NULL AFTER remarks",
            )
            _ensure_column(
                "amount_difference",
                f"ALTER TABLE {table_name} ADD COLUMN amount_difference DECIMAL(15, 2) NULL AFTER amount_match",
            )
            _ensure_column(
                "push_failed",
                f"ALTER TABLE {table_name} ADD COLUMN push_failed VARCHAR(512) NULL",
            )
            # push_cancelled: set at insert time when required fields (invoice number / date / amount)
            # are missing or amount is zero — row is immediately disqualified before any matching
            _ensure_column(
                "push_cancelled",
                f"ALTER TABLE {table_name} ADD COLUMN push_cancelled VARCHAR(512) NULL AFTER push_failed",
            )
            _ensure_column(
                "pushed_at",
                f"ALTER TABLE {table_name} ADD COLUMN pushed_at TINYINT DEFAULT 0",
            )
            _ensure_column(
                "validation_flag",
                f"ALTER TABLE {table_name} ADD COLUMN validation_flag TINYINT NOT NULL DEFAULT 0 AFTER client_gst_number",
            )
            _drop_column_if_exists("pdf_page_count")
            _drop_column_if_exists("is_multi_page_pdf")
            _ensure_column(
                "multi_page_pdf",
                f"ALTER TABLE {table_name} ADD COLUMN multi_page_pdf VARCHAR(8) NULL",
            )
            _ensure_column(
                "room_charge",
                f"ALTER TABLE {table_name} ADD COLUMN room_charge DECIMAL(15, 2) NULL",
            )
            _ensure_column(
                "total_tariff",
                f"ALTER TABLE {table_name} ADD COLUMN total_tariff DECIMAL(15, 2) NULL",
            )
            _ensure_column(
                "other_charges",
                f"ALTER TABLE {table_name} ADD COLUMN other_charges DECIMAL(15, 2) NULL",
            )
            _ensure_column(
                "total_igst",
                f"ALTER TABLE {table_name} ADD COLUMN total_igst DECIMAL(15, 2) NULL",
            )
            _ensure_column(
                "total_taxes",
                f"ALTER TABLE {table_name} ADD COLUMN total_taxes DECIMAL(15, 2) NULL",
            )
            _ensure_column(
                "discount",
                f"ALTER TABLE {table_name} ADD COLUMN discount DECIMAL(15, 2) NULL",
            )
            _ensure_column(
                "advance_received",
                f"ALTER TABLE {table_name} ADD COLUMN advance_received DECIMAL(15, 2) NULL",
            )
            _ensure_column(
                "email_amount",
                f"ALTER TABLE {table_name} ADD COLUMN email_amount DECIMAL(15, 2) NULL AFTER amount_paid",
            )
            _ensure_column(
                "flag",
                f"ALTER TABLE {table_name} ADD COLUMN flag VARCHAR(20) NULL",
            )
            _ensure_column(
                "room_captured",
                f"ALTER TABLE {table_name} ADD COLUMN room_captured VARCHAR(255) NULL AFTER advance_received",
            )
            # Invoice-type classification: set to 1 for MakeMyTrip (MMT) invoices.
            _ensure_column(
                "mmt_flag",
                f"ALTER TABLE {table_name} ADD COLUMN mmt_flag TINYINT NULL DEFAULT 0",
            )

        print("✓ Invoice data table verified/created")
        return True
    except Exception as e:
        print(f"❌ Error ensuring invoice table exists: {e}")
        import traceback

        traceback.print_exc()
        return False


def convert_row_for_mysql(row_dict):
    """Convert Excel row dictionary to MySQL-compatible format"""
    mysql_row = {}

    # Helper function to convert 'Not Found' to None for MySQL
    def convert_value(value):
        if value == "Not Found" or value == "":
            return None
        return value

    # Helper function to parse dates
    def parse_date(date_val):
        if date_val is None:
            return None

        s = str(date_val).strip()
        if not s or s == "Not Found" or s.lower() in ("nan", "none"):
            return None

        # Normalize timestamp-like values to date-only (MySQL column is DATE).
        # Examples handled:
        # - "2026-04-01 14:01:34" -> "2026-04-01"
        # - "2026-04-01T14:01:34" -> "2026-04-01"
        original_s = s
        s = s.replace("T", " ")
        if " " in s:
            date_only = s.split(" ")[0].strip()
            print(f"   🕒 Stripping time from date: raw={original_s!r} -> date_only={date_only!r}")
            s = date_only

        # Try common date formats
        for fmt in ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d", "%d/%m/%y", "%d-%m-%y"]:
            try:
                return datetime.strptime(s, fmt).date()
            except Exception:
                continue

        return None

    # Helper function to parse decimal values
    def parse_decimal(value):
        if value == "Not Found":
            return None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        if value is None or value == "":
            return None
        try:
            # Remove commas and currency symbols
            clean_value = str(value).replace(",", "").replace("₹", "").replace("$", "").strip()
            return float(clean_value) if clean_value else None
        except Exception:
            return None

    mysql_row["s_no"] = row_dict.get("S.No")
    mysql_row["email_from"] = convert_value(row_dict.get("Email From"))
    mysql_row["message_id"] = convert_value(row_dict.get("Message ID"))
    mysql_row["email_date"] = parse_date(row_dict.get("Email Date"))
    mysql_row["assigned_to"] = convert_value(row_dict.get("Assigned To"))
    mysql_row["hotel_name"] = convert_value(row_dict.get("Hotel Name"))
    mysql_row["guest_name"] = convert_value(row_dict.get("Guest Name"))
    mysql_row["booking_code"] = convert_value(row_dict.get("Booking Code"))
    mysql_row["bill_number"] = convert_value(row_dict.get("Bill Number"))
    mysql_row["bill_date"] = parse_date(row_dict.get("Bill Date"))
    mysql_row["check_in_date"] = parse_date(row_dict.get("Check-in Date"))
    mysql_row["check_out_date"] = parse_date(row_dict.get("Check-out Date"))
    mysql_row["amount_paid"] = parse_decimal(row_dict.get("Amount Paid"))
    mysql_row["email_amount"] = parse_decimal(row_dict.get("Email Amount"))
    hbd_gst_val, hbd_gst_invalid = gst_field_value_for_mysql_with_invalid(
        row_dict.get("HBD GST Number")
    )
    client_gst_val, client_gst_invalid = gst_field_value_for_mysql_with_invalid(
        row_dict.get("Client GST Number")
    )
    mysql_row["hbd_gst_number"] = hbd_gst_val
    mysql_row["client_gst_number"] = client_gst_val
    gst_invalid_row = hbd_gst_invalid or client_gst_invalid
    # 1 = HBD and/or Client GST failed GSTIN validation; 0 = both sides pass (or no invalid segment)
    mysql_row["validation_flag"] = 1 if gst_invalid_row else 0
    mysql_row["total_cgst"] = parse_decimal(row_dict.get("Total CGST"))
    mysql_row["total_sgst"] = parse_decimal(row_dict.get("Total SGST"))
    mysql_row["room_charge"] = parse_decimal(row_dict.get("Room Charge"))
    mysql_row["total_tariff"] = parse_decimal(row_dict.get("Total Tariff"))
    mysql_row["other_charges"] = parse_decimal(row_dict.get("Other Charges"))
    mysql_row["total_igst"] = parse_decimal(row_dict.get("Total IGST"))
    mysql_row["total_taxes"] = parse_decimal(row_dict.get("Total Taxes"))
    mysql_row["discount"] = parse_decimal(row_dict.get("Discount"))
    mysql_row["advance_received"] = parse_decimal(row_dict.get("Advance Received"))
    mysql_row["pdf_filename"] = convert_value(row_dict.get("PDF Filename"))
    raw_blob = convert_value(row_dict.get("Blob URL"))
    mysql_row["blob_url"] = sanitize_blob_url(raw_blob) if raw_blob else None

    def parse_multi_page_pdf(val):
        if val is None or val == "" or val == "Not Found":
            return None
        s = str(val).strip().lower()
        if s in ("yes", "y", "true", "1"):
            return "yes"
        if s in ("no", "n", "false", "0"):
            return "no"
        return None

    mysql_row["multi_page_pdf"] = parse_multi_page_pdf(row_dict.get("Multi Page PDF"))

    def _gst_stored_value_present(val) -> bool:
        return bool(val and str(val).strip())

    gst_missing_row = (not _gst_stored_value_present(hbd_gst_val)) or (
        not _gst_stored_value_present(client_gst_val)
    )

    # Check for missing/zero required invoice fields
    guest_name_missing = not bool(
        mysql_row.get("guest_name") and str(mysql_row["guest_name"]).strip()
    )
    dates_missing = (
        mysql_row.get("check_in_date") is None
        and mysql_row.get("check_out_date") is None
    )
    bill_number_missing = not bool(mysql_row.get("bill_number") and str(mysql_row["bill_number"]).strip())
    bill_date_missing = mysql_row.get("bill_date") is None
    amount_val = mysql_row.get("amount_paid")
    amount_missing = amount_val is None
    amount_zero = amount_val is not None and amount_val == 0.0

    cancel_reasons = _push_cancel_reasons(
        gst_invalid_row=gst_invalid_row,
        gst_missing_row=gst_missing_row,
        guest_name_missing=guest_name_missing,
        dates_missing=dates_missing,
        bill_number_missing=bill_number_missing,
        bill_date_missing=bill_date_missing,
        amount_missing=amount_missing,
        amount_zero=amount_zero,
    )

    # Matched Status: 'matched' if selected for API push, 'unmatched' if not, NULL if not yet determined
    if cancel_reasons:
        cancel_msg = ", ".join(cancel_reasons)
        mysql_row["push_cancelled"] = cancel_msg

        # Left open for retry (pushed_at=0) when GST (missing OR invalid) is the
        # ONLY problem with this row -- Step-7 can still backfill/re-validate GST
        # later. Any other cancel reason present locks the row (pushed_at=1).
        gst_only_issue = (
            (gst_missing_row or gst_invalid_row)
            and not guest_name_missing
            and not dates_missing
            and not bill_number_missing
            and not bill_date_missing
            and not amount_missing
            and not amount_zero
        )

        if gst_only_issue:
            mysql_row["matched_status"] = None
            mysql_row["pushed_at"] = 0
        else:
            mysql_row["matched_status"] = "unmatched"
            mysql_row["pushed_at"] = 1

        print(
            f"   ⚠️  Row cancelled before push → matched_status={mysql_row['matched_status']!r}, "
            f"pushed_at={mysql_row['pushed_at']}, "
            f"push_cancelled='{cancel_msg}' "
            f"(guest_name={mysql_row.get('guest_name')!r}, "
            f"check_in_date={mysql_row.get('check_in_date')!r}, "
            f"check_out_date={mysql_row.get('check_out_date')!r}, "
            f"hbd_gst_number={hbd_gst_val!r}, client_gst_number={client_gst_val!r}, "
            f"bill_number={mysql_row.get('bill_number')!r}, "
            f"bill_date={mysql_row.get('bill_date')!r}, "
            f"amount_paid={mysql_row.get('amount_paid')!r})"
        )
    else:
        matched_status = row_dict.get("Matched Status")
        if matched_status in ["matched", "unmatched"]:
            mysql_row["matched_status"] = matched_status
        else:
            mysql_row["matched_status"] = None
        mysql_row["pushed_at"] = 0
        mysql_row["push_cancelled"] = None

    return mysql_row


_EXCEL_TO_MYSQL_DEDUPE = {
    "message_id": "Message ID",
    "booking_code": "Booking Code",
    "bill_number": "Bill Number",
    "bill_date": "Bill Date",
    "check_in_date": "Check-in Date",
    "check_out_date": "Check-out Date",
    "amount_paid": "Amount Paid",
    "guest_name": "Guest Name",
}

# Fallback when uq_invoice_dedupe is missing (legacy_impl ensure_invoice_report_table_exists).
_DEFAULT_DEDUPE_COLUMNS = (
    "booking_code",
    "bill_number",
    "check_in_date",
    "check_out_date",
    "amount_paid",
    "guest_name",
)


def _parse_dedupe_scalar(column: str, raw) -> object | None:
    """Normalize one dedupe-key field the same way insert would (no logging side effects)."""
    if raw is None:
        return None
    if raw == "Not Found" or raw == "":
        return None
    try:
        if pd.isna(raw):
            return None
    except (TypeError, ValueError):
        pass

    if column in ("check_in_date", "check_out_date", "bill_date"):
        s = str(raw).strip()
        if not s or s.lower() in ("nan", "none"):
            return None
        s = s.replace("T", " ")
        if " " in s:
            s = s.split(" ")[0].strip()
        for fmt in ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d", "%d/%m/%y", "%d-%m-%y"]:
            try:
                return datetime.strptime(s, fmt).date()
            except ValueError:
                continue
        return None

    if column == "amount_paid":
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return float(raw)
        try:
            clean = str(raw).replace(",", "").replace("₹", "").replace("$", "").strip()
            return float(clean) if clean else None
        except (ValueError, TypeError):
            return None

    s = str(raw).strip()
    return s if s else None


def _dedupe_values_from_row_dict(row_dict: dict) -> dict[str, object | None]:
    out: dict[str, object | None] = {}
    for col, excel_key in _EXCEL_TO_MYSQL_DEDUPE.items():
        out[col] = _parse_dedupe_scalar(col, row_dict.get(excel_key))
    return out


def _get_dedupe_index_columns(engine, table_name: str) -> list[str]:
    """Columns on uq_invoice_dedupe in index order, or legacy default."""
    sql = text(
        """
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.STATISTICS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = :table_name
          AND INDEX_NAME = 'uq_invoice_dedupe'
        ORDER BY SEQ_IN_INDEX
        """
    )
    try:
        with engine.connect() as conn:
            rows = conn.execute(sql, {"table_name": table_name}).fetchall()
        cols = [str(r[0]) for r in rows if r and r[0]]
        if cols:
            return cols
    except Exception:
        pass
    return list(_DEFAULT_DEDUPE_COLUMNS)


def mysql_duplicate_exists(row_dict, engine=None) -> bool:
    """Return True if a row with the same uq_invoice_dedupe business key already exists."""
    if not row_dict:
        return False
    try:
        if engine is None:
            engine = get_db_engine()
        table_name = get_invoice_table_name()
        index_cols = _get_dedupe_index_columns(engine, table_name)
        dedupe = _dedupe_values_from_row_dict(row_dict)

        conditions: list[str] = []
        params: dict[str, object] = {}
        for col in index_cols:
            val = dedupe.get(col)
            if col == "guest_name":
                if val is None:
                    conditions.append("(guest_name IS NULL)")
                else:
                    conditions.append("LEFT(guest_name, 100) <=> LEFT(:guest_name, 100)")
                    params["guest_name"] = str(val)[:100]
            elif val is None:
                conditions.append(f"({col} IS NULL)")
            else:
                conditions.append(f"({col} <=> :{col})")
                params[col] = val

        if not conditions:
            return False

        where_sql = " AND ".join(conditions)
        query = text(f"SELECT 1 FROM {table_name} WHERE {where_sql} LIMIT 1")
        with engine.connect() as conn:
            hit = conn.execute(query, params).fetchone()
        if hit:
            bill = dedupe.get("bill_number") or "(no bill number)"
            guest = dedupe.get("guest_name") or "(no guest)"
            print(
                f"   ⏭️  Skipping duplicate MySQL row (uq_invoice_dedupe): "
                f"bill_number={bill!r}, guest_name={guest!r}"
            )
            return True
        return False
    except Exception as e:
        print(f"   ⚠️  Duplicate check failed (proceeding with insert): {e}")
        return False


# Fields that define a duplicate invoice. message_id is intentionally NOT part of this key,
# so the same invoice arriving in a different email (or attached twice) is detected.
_DUPLICATE_BUSINESS_COLUMNS = (
    "bill_number",
    "bill_date",
    "guest_name",
    "check_in_date",
    "check_out_date",
    "amount_paid",
    "booking_code",
)


def business_key_duplicate_exists(row_dict, engine=None) -> bool:
    """Return True if a row with the same business key is already in MySQL.

    Key: bill_number, bill_date, guest_name (first 100 chars), check_in_date,
    check_out_date, amount_paid, booking_code. message_id is ignored. Missing values
    ("Not Found", empty, NaN) are treated as NULL and match other NULLs (NULL-safe compare).

    Unlike mysql_duplicate_exists, this does NOT mean the row should be skipped: the caller
    still inserts it and then marks it via mark_row_duplicate_in_mysql.
    On any error returns False (row is treated as not a duplicate).
    """
    if not row_dict:
        return False
    try:
        if engine is None:
            engine = get_db_engine()
        table_name = get_invoice_table_name()
        dedupe = _dedupe_values_from_row_dict(row_dict)

        conditions: list[str] = []
        params: dict[str, object] = {}
        for col in _DUPLICATE_BUSINESS_COLUMNS:
            val = dedupe.get(col)
            if col == "guest_name":
                if val is None:
                    conditions.append("(guest_name IS NULL)")
                else:
                    conditions.append("LEFT(guest_name, 100) <=> LEFT(:guest_name, 100)")
                    params["guest_name"] = str(val)[:100]
            elif val is None:
                conditions.append(f"({col} IS NULL)")
            else:
                conditions.append(f"({col} <=> :{col})")
                params[col] = val

        where_sql = " AND ".join(conditions)
        query = text(f"SELECT 1 FROM {table_name} WHERE {where_sql} LIMIT 1")
        with engine.connect() as conn:
            hit = conn.execute(query, params).fetchone()
        if hit:
            bill = dedupe.get("bill_number") or "(no bill number)"
            guest = dedupe.get("guest_name") or "(no guest)"
            code = dedupe.get("booking_code") or "(no booking code)"
            print(
                f"   🔁 Duplicate invoice detected (business key): "
                f"bill_number={bill!r}, guest_name={guest!r}, booking_code={code!r} "
                f"- row will be saved with matched_status='duplicate'"
            )
            return True
        return False
    except Exception as e:
        print(f"   ⚠️  Business-key duplicate check failed (treating as not duplicate): {e}")
        return False


def mark_row_duplicate_in_mysql(row_id, engine=None) -> bool:
    """Mark an inserted row as a duplicate: matched_status='duplicate', pushed_at=1.

    pushed_at=1 keeps the row out of Step-7 (which only reads pushed_at = 0 or NULL).
    Takes priority over any other status already set on the row.
    """
    if row_id is None:
        return False
    try:
        if engine is None:
            engine = get_db_engine()
        table_name = get_invoice_table_name()
        with engine.connect() as conn:
            result = conn.execute(
                text(
                    f"UPDATE {table_name} SET matched_status = 'duplicate', pushed_at = 1 "
                    f"WHERE id = :row_id"
                ),
                {"row_id": int(row_id)},
            )
            conn.commit()
            ok = result.rowcount > 0
        if ok:
            print(f"   🔁 Row ID {row_id} marked matched_status='duplicate', pushed_at=1")
        else:
            print(f"   ⚠️  No row found with ID {row_id} to mark as duplicate")
        return ok
    except Exception as e:
        print(f"   ⚠️  Error marking row as duplicate in MySQL: {e}")
        import traceback

        traceback.print_exc()
        return False


def insert_row_to_mysql(row_dict, engine):
    """Insert a single row into MySQL invoice table (name from INVOICE_TABLE_NAME env).
    
    Returns:
        int: The inserted row's ID (primary key), or None if insertion failed
    """
    try:
        table_name = get_invoice_table_name()
        mysql_row = convert_row_for_mysql(row_dict)

        # Build INSERT statement with proper parameter binding
        columns = ", ".join(mysql_row.keys())
        placeholders = ", ".join([f":{key}" for key in mysql_row.keys()])

        insert_sql = f"""
        INSERT INTO {table_name} ({columns})
        VALUES ({placeholders})
        """

        with engine.connect() as conn:
            result = conn.execute(text(insert_sql), mysql_row)
            conn.commit()
            # Get the inserted row's ID
            inserted_id = result.lastrowid
        print(f"   ✅ Inserted row to MySQL database (ID: {inserted_id}, S.No: {row_dict.get('S.No', 'N/A')})")
        return inserted_id
    except Exception as e:
        print(f"   ⚠️  Error inserting row to MySQL: {e}")
        import traceback

        traceback.print_exc()
        return None


def update_matched_status_in_mysql(row_id, matched_status, match_booking_code=None, hbd_gst_match=None, client_gst_match=None, remarks=None, amount_match=None, amount_difference=_UNSET, engine=None):
    """Update matched_status in MySQL for a specific invoice by row ID (primary key)
    
    Args:
        row_id: The primary key (id) of the row to update
        matched_status: 'matched' or 'unmatched'
        match_booking_code: 0 if booking code matched, 1 if it didn't (optional)
        hbd_gst_match: 0 if HBD GST matched, 1 if it didn't, None if not checked (optional)
        client_gst_match: 0 if Client GST matched, 1 if it didn't, None if not checked (optional)
        remarks: JSON string with matching details (optional)
        amount_match: 0 if PDF total amount and API balanceAmount match, 1 if not, None if not compared (optional, reference only)
        amount_difference: Absolute diff between invoice amount and API balance sum when compared; pass None to clear; omit to leave unchanged
        engine: Optional database engine. If None, creates a new one.
    
    Returns:
        bool: True if successful, False otherwise
    """
    if matched_status not in ["matched", "unmatched"]:
        print(f"   ⚠️  Invalid matched_status: {matched_status}. Must be 'matched' or 'unmatched'")
        return False

    if row_id is None:
        print(f"   ⚠️  Invalid row_id: None. Cannot update matched_status")
        return False

    try:
        if engine is None:
            engine = get_db_engine()
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
        _ensure_column(
            "amount_difference",
            f"ALTER TABLE {table_name} ADD COLUMN amount_difference DECIMAL(15, 2) NULL AFTER amount_match",
        )

        # Build UPDATE statement
        update_fields = ["matched_status = :matched_status"]
        update_values = {
            "matched_status": matched_status,
            "row_id": int(row_id),
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
        
        # Add amount_match if provided (reference only: 0 = match, 1 = no match)
        if amount_match is not None:
            amount_match_val = int(amount_match) if amount_match in (0, 1) else None
            if amount_match_val is not None:
                update_fields.append("amount_match = :amount_match")
                update_values["amount_match"] = amount_match_val

        if amount_difference is not _UNSET:
            if amount_difference is None:
                update_fields.append("amount_difference = NULL")
            else:
                update_fields.append("amount_difference = :amount_difference")
                update_values["amount_difference"] = round(float(amount_difference), 2)
        
        # Add remarks based on matched_status
        if matched_status == "unmatched":
            # For unmatched invoices, always set remarks to NULL
            update_fields.append("remarks = NULL")
        elif remarks is not None:
            # For matched invoices, set remarks if provided
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


def update_push_failed_in_mysql(row_id, message, engine=None):
    """Set push_failed reason text for an invoice row, or NULL to clear (successful push / no failure).

    Args:
        row_id: Primary key (id) in the invoice table
        message: Human-readable reason, or None to clear push_failed
        engine: Optional SQLAlchemy engine

    Returns:
        bool: True if update ran without error
    """
    if row_id is None:
        print(f"   ⚠️  Invalid row_id: None. Cannot update push_failed")
        return False
    try:
        if engine is None:
            engine = get_db_engine()
        table_name = get_invoice_table_name()

        def _ensure_push_failed_column() -> None:
            check_sql = f"""
            SELECT COUNT(*) as col_count
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
            AND TABLE_NAME = '{table_name}'
            AND COLUMN_NAME = 'push_failed'
            """
            with engine.connect() as conn:
                result = conn.execute(text(check_sql))
                exists = result.fetchone()[0] > 0
                if not exists:
                    print(f"   Adding 'push_failed' column to existing {table_name} table...")
                    conn.execute(
                        text(
                            f"ALTER TABLE {table_name} ADD COLUMN push_failed VARCHAR(512) NULL"
                        )
                    )
                    conn.commit()
                    print(f"   ✓ Added 'push_failed' column to {table_name} table")

        _ensure_push_failed_column()

        rid = int(row_id)
        if message is None:
            update_sql = text(f"UPDATE {table_name} SET push_failed = NULL WHERE id = :row_id")
            params = {"row_id": rid}
        else:
            update_sql = text(f"UPDATE {table_name} SET push_failed = :msg WHERE id = :row_id")
            params = {"row_id": rid, "msg": str(message)[:512]}

        with engine.connect() as conn:
            result = conn.execute(update_sql, params)
            conn.commit()
            rows_affected = result.rowcount
        if rows_affected > 0:
            return True
        print(f"   ⚠️  No row found with ID {row_id} to update push_failed")
        return False
    except Exception as e:
        print(f"   ⚠️  Error updating push_failed in MySQL: {e}")
        import traceback

        traceback.print_exc()
        return False


def update_push_cancelled_in_mysql(row_id, message, engine=None):
    """Set push_cancelled reason text for an invoice row, or NULL to clear it.

    push_cancelled is populated at INSERT time when any pre-push checks fail
    (invalid/missing GST, missing guest name, missing dates, missing invoice number,
    missing bill date, missing/zero amount),
    as a comma-separated list, e.g. "missing gst number, missing guest name".
    Unlike push_failed (which records API-call failures), push_cancelled means
    the row was disqualified before matching/pushing even started.

    Args:
        row_id: Primary key (id) in the invoice table
        message: Human-readable reason string, e.g. "missing invoice number, amount is 0"
                 Pass None to clear (set NULL).

    Returns:
        bool: True if update ran without error
    """
    if row_id is None:
        print(f"   ⚠️  Invalid row_id: None. Cannot update push_cancelled")
        return False
    try:
        if engine is None:
            engine = get_db_engine()
        table_name = get_invoice_table_name()

        def _ensure_push_cancelled_column() -> None:
            check_sql = f"""
            SELECT COUNT(*) as col_count
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
            AND TABLE_NAME = '{table_name}'
            AND COLUMN_NAME = 'push_cancelled'
            """
            with engine.connect() as conn:
                result = conn.execute(text(check_sql))
                exists = result.fetchone()[0] > 0
                if not exists:
                    print(f"   Adding 'push_cancelled' column to existing {table_name} table...")
                    conn.execute(
                        text(
                            f"ALTER TABLE {table_name} ADD COLUMN push_cancelled VARCHAR(512) NULL AFTER push_failed"
                        )
                    )
                    conn.commit()
                    print(f"   ✓ Added 'push_cancelled' column to {table_name} table")

        _ensure_push_cancelled_column()

        rid = int(row_id)
        if message is None:
            update_sql = text(f"UPDATE {table_name} SET push_cancelled = NULL WHERE id = :row_id")
            params = {"row_id": rid}
        else:
            update_sql = text(f"UPDATE {table_name} SET push_cancelled = :msg WHERE id = :row_id")
            params = {"row_id": rid, "msg": str(message)[:512]}

        with engine.connect() as conn:
            result = conn.execute(update_sql, params)
            conn.commit()
            rows_affected = result.rowcount
        if rows_affected > 0:
            return True
        print(f"   ⚠️  No row found with ID {row_id} to update push_cancelled")
        return False
    except Exception as e:
        print(f"   ⚠️  Error updating push_cancelled in MySQL: {e}")
        import traceback
        traceback.print_exc()
        return False


def get_already_pushed_ids(engine=None):
    """Return a set of row IDs where pushed_at = 1 (already processed through the push loop)."""
    try:
        if engine is None:
            engine = get_db_engine()
        table_name = get_invoice_table_name()
        with engine.connect() as conn:
            result = conn.execute(text(f"SELECT id FROM {table_name} WHERE pushed_at = 1"))
            return {row[0] for row in result.fetchall()}
    except Exception as e:
        print(f"   ⚠️  Error reading already-pushed IDs: {e}")
        return set()


def update_pushed_at_in_mysql(row_id, engine=None):
    """Set pushed_at = 1 for a row, marking it as processed through the push loop.

    Args:
        row_id: Primary key (id) in the invoice table
        engine: Optional SQLAlchemy engine

    Returns:
        bool: True if update ran without error
    """
    if row_id is None:
        return False
    try:
        if engine is None:
            engine = get_db_engine()
        table_name = get_invoice_table_name()
        with engine.connect() as conn:
            result = conn.execute(
                text(f"UPDATE {table_name} SET pushed_at = 1 WHERE id = :row_id"),
                {"row_id": int(row_id)},
            )
            conn.commit()
            return result.rowcount > 0
    except Exception as e:
        print(f"   ⚠️  Error updating pushed_at in MySQL: {e}")
        return False


def update_classification_flags_in_mysql(row_id, classification, engine=None):
    """Apply invoice-type classification flags to a MySQL row by id.

    - hummingbird -> matched_status='hummingbird_mail', pushed_at=1 (excluded from Step-7 push)
    - mmt         -> mmt_flag=1 (row still flows through Step-7)
    - b2b         -> matched_status='b2b_invoice', pushed_at=1 (excluded from Step-7 push)
    - food        -> matched_status='food_invoice', pushed_at=1 (excluded from Step-7 push)
    - not_invoice -> matched_status='not_invoice', pushed_at=1 (excluded from Step-7 push)
    - anything else -> no change (normal flow).

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


def _parse_date_to_mysql(date_str):
    """Convert a date string (various formats) to YYYY-MM-DD for MySQL DATE comparison."""
    if not date_str:
        return None
    s = str(date_str).strip()
    if not s or s.lower() in ("not found", "nan", "none"):
        return None
    from datetime import datetime as _dt
    fmts = [
        "%Y-%m-%d",
        "%d/%m/%y",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d-%m-%y",
        "%Y-%m-%d %H:%M:%S",
        "%d/%m/%Y %H:%M:%S",
    ]
    for fmt in fmts:
        try:
            return _dt.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def lookup_row_id_by_fields(guest_name, check_in_date, check_out_date, invoice_number, engine=None):
    """Fallback lookup: find a MySQL row id using guest_name + dates + invoice_number.

    Used when the primary row-id resolution (from the merged dict) returns None.
    Returns the first matching id, or None if no match is found.
    """
    if not any([guest_name, check_in_date, check_out_date, invoice_number]):
        return None
    try:
        if engine is None:
            engine = get_db_engine()
        table_name = get_invoice_table_name()

        conditions = []
        params = {}

        if guest_name:
            conditions.append("LOWER(TRIM(guest_name)) = LOWER(TRIM(:guest_name))")
            params["guest_name"] = str(guest_name).strip()
        if check_in_date:
            parsed = _parse_date_to_mysql(check_in_date)
            if parsed:
                conditions.append("check_in_date = :check_in_date")
                params["check_in_date"] = parsed
        if check_out_date:
            parsed = _parse_date_to_mysql(check_out_date)
            if parsed:
                conditions.append("check_out_date = :check_out_date")
                params["check_out_date"] = parsed
        if invoice_number:
            conditions.append("LOWER(TRIM(bill_number)) = LOWER(TRIM(:invoice_number))")
            params["invoice_number"] = str(invoice_number).strip()

        if len(conditions) < 2:
            return None

        where_clause = " AND ".join(conditions)
        sql = f"SELECT id FROM {table_name} WHERE {where_clause} ORDER BY id DESC LIMIT 1"

        with engine.connect() as conn:
            result = conn.execute(text(sql), params)
            row = result.fetchone()
            if row:
                return int(row[0])
        return None
    except Exception as e:
        print(f"   ⚠️  Error in fallback row-id lookup: {e}")
        return None


def update_excel_match_diagnostics_in_mysql(row_id, excel_guest_score, excel_checkin_delta, excel_checkout_delta, engine=None):
    """Update Excel matching diagnostics in MySQL for a specific invoice by row ID (primary key)
    
    Args:
        row_id: The primary key (id) of the row to update
        excel_guest_score: RapidFuzz score (0-100) for guest name matching
        excel_checkin_delta: Check-in date delta (-1, 0, or 1)
        excel_checkout_delta: Check-out date delta (-1, 0, or 1)
        engine: Optional database engine. If None, creates a new one.
    
    Returns:
        bool: True if successful, False otherwise
    """
    if engine is None:
        engine = get_db_engine()
    
    if row_id is None:
        print(f"   ⚠️  Invalid row_id: None. Cannot update Excel match diagnostics")
        return False
    
    try:
        table_name = get_invoice_table_name()
        # Ensure columns exist before attempting to update
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
            "excel_match_guest_score",
            f"ALTER TABLE {table_name} ADD COLUMN excel_match_guest_score INT NULL AFTER match_checkout_delta",
        )
        _ensure_column(
            "excel_match_checkin_delta",
            f"ALTER TABLE {table_name} ADD COLUMN excel_match_checkin_delta TINYINT NULL AFTER excel_match_guest_score",
        )
        _ensure_column(
            "excel_match_checkout_delta",
            f"ALTER TABLE {table_name} ADD COLUMN excel_match_checkout_delta TINYINT NULL AFTER excel_match_checkin_delta",
        )
        def parse_score(value):
            if value is None or value == "" or pd.isna(value):
                return None
            try:
                v = int(float(value))
                return v if 0 <= v <= 100 else None
            except Exception:
                return None

        def parse_delta(value):
            if value is None or value == "" or pd.isna(value):
                return None
            try:
                v = int(value)
                return v if v in (-1, 0, 1) else None
            except Exception:
                return None
        
        excel_guest_score_val = parse_score(excel_guest_score)
        excel_checkin_delta_val = parse_delta(excel_checkin_delta)
        excel_checkout_delta_val = parse_delta(excel_checkout_delta)
        
        # Build UPDATE statement
        update_fields = []
        update_values = {"row_id": int(row_id)}
        
        if excel_guest_score_val is not None:
            update_fields.append("excel_match_guest_score = :excel_match_guest_score")
            update_values["excel_match_guest_score"] = excel_guest_score_val
        
        if excel_checkin_delta_val is not None:
            update_fields.append("excel_match_checkin_delta = :excel_match_checkin_delta")
            update_values["excel_match_checkin_delta"] = excel_checkin_delta_val
        
        if excel_checkout_delta_val is not None:
            update_fields.append("excel_match_checkout_delta = :excel_match_checkout_delta")
            update_values["excel_match_checkout_delta"] = excel_checkout_delta_val
        
        if not update_fields:
            return True  # Nothing to update
        
        # Use primary key (id) for precise matching - ensures only the specific row is updated
        update_sql = f"""
        UPDATE {table_name}
        SET {', '.join(update_fields)}
        WHERE id = :row_id
        """
        
        with engine.connect() as conn:
            result = conn.execute(text(update_sql), update_values)
            conn.commit()
            rows_updated = result.rowcount
            if rows_updated > 0:
                print(f"   ✅ Updated Excel match diagnostics in MySQL for row ID {row_id} ({rows_updated} row(s))")
                return True
            else:
                print(f"   ⚠️  No row found with ID {row_id} to update Excel match diagnostics")
                return False
    except Exception as e:
        print(f"   ⚠️  Error updating Excel match diagnostics in MySQL: {e}")
        import traceback
        traceback.print_exc()
        return False


def update_room_captured_in_mysql(row_id, value, engine=None):
    """Store roomCaptured from the matched booking/API Excel row for MySQL-only API payloads."""
    if row_id is None:
        print(f"   ⚠️  Invalid row_id: None. Cannot update room_captured")
        return False
    try:
        if engine is None:
            engine = get_db_engine()
        table_name = get_invoice_table_name()

        check_sql = f"""
        SELECT COUNT(*) as col_count
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
        AND TABLE_NAME = '{table_name}'
        AND COLUMN_NAME = 'room_captured'
        """
        with engine.connect() as conn:
            result = conn.execute(text(check_sql))
            exists = result.fetchone()[0] > 0
            if not exists:
                print(f"   Adding 'room_captured' column to existing {table_name} table...")
                conn.execute(
                    text(
                        f"ALTER TABLE {table_name} ADD COLUMN room_captured VARCHAR(255) NULL AFTER advance_received"
                    )
                )
                conn.commit()
                print(f"   ✓ Added 'room_captured' column to {table_name} table")

        val = None
        if value is not None:
            try:
                if pd.isna(value):
                    val = None
                else:
                    s = str(value).strip()
                    if s and s.lower() not in ("nan", "none", "nat"):
                        val = s[:255]
            except TypeError:
                s = str(value).strip()
                val = s[:255] if s else None

        with engine.connect() as conn:
            result = conn.execute(
                text(f"UPDATE {table_name} SET room_captured = :v WHERE id = :row_id"),
                {"v": val, "row_id": int(row_id)},
            )
            conn.commit()
            ok = result.rowcount > 0
        if ok:
            print(f"   ✅ Updated room_captured for row ID {row_id}")
        else:
            print(f"   ⚠️  No row found with ID {row_id} to update room_captured")
        return ok
    except Exception as e:
        print(f"   ⚠️  Error updating room_captured in MySQL: {e}")
        import traceback

        traceback.print_exc()
        return False


def update_gst_numbers_in_mysql(
    row_id,
    hbd_gst=None,
    client_gst=None,
    gst_flag=None,
    engine=None,
):
    """Update GST numbers and gst_flag when values were backfilled from matched API rows."""
    if row_id is None:
        print("   ⚠️  Invalid row_id: None. Cannot update GST numbers")
        return False
    if hbd_gst is None and client_gst is None and gst_flag is None:
        return False
    try:
        if engine is None:
            engine = get_db_engine()
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
                result = conn.execute(text(check_sql))
                exists = result.fetchone()[0] > 0
                if not exists:
                    print(f"   Adding '{column_name}' column to existing {table_name} table...")
                    conn.execute(text(alter_sql))
                    conn.commit()
                    print(f"   ✓ Added '{column_name}' column to {table_name} table")

        _ensure_column(
            "gst_flag",
            f"ALTER TABLE {table_name} ADD COLUMN gst_flag VARCHAR(64) NULL AFTER client_gst_number",
        )

        def _gst_val(value) -> str | None:
            if value is None:
                return None
            try:
                if pd.isna(value):
                    return None
            except (TypeError, ValueError):
                pass
            s = str(value).strip()
            return s[:50] if s and s.lower() not in ("nan", "none", "nat") else None

        update_fields: list[str] = []
        update_values: dict = {"row_id": int(row_id)}

        hbd_val = _gst_val(hbd_gst)
        if hbd_val is not None:
            update_fields.append("hbd_gst_number = :hbd_gst")
            update_values["hbd_gst"] = hbd_val

        client_val = _gst_val(client_gst)
        if client_val is not None:
            update_fields.append("client_gst_number = :client_gst")
            update_values["client_gst"] = client_val

        if gst_flag is not None:
            flag_s = str(gst_flag).strip()[:64]
            if flag_s:
                update_fields.append("gst_flag = :gst_flag")
                update_values["gst_flag"] = flag_s

        if not update_fields:
            return False

        update_sql = f"UPDATE {table_name} SET {', '.join(update_fields)} WHERE id = :row_id"
        with engine.connect() as conn:
            result = conn.execute(text(update_sql), update_values)
            conn.commit()
            ok = result.rowcount > 0
        if ok:
            print(f"   ✅ Updated GST fields for row ID {row_id} (backfilled from API)")
        else:
            print(f"   ⚠️  No row found with ID {row_id} to update GST fields")
        return ok
    except Exception as e:
        print(f"   ⚠️  Error updating GST numbers in MySQL: {e}")
        import traceback

        traceback.print_exc()
        return False


def update_flag_in_mysql(row_id, flag, engine=None):
    """Set duplicate-bill amount flag (1, 0, or 'not found') on a MySQL row."""
    if row_id is None or flag is None:
        return False
    try:
        from backend.app.modules.matching.bill_duplicate import flag_value_for_mysql

        if engine is None:
            engine = get_db_engine()
        table_name = get_invoice_table_name()
        val = flag_value_for_mysql(flag)
        with engine.connect() as conn:
            result = conn.execute(
                text(f"UPDATE {table_name} SET flag = :v WHERE id = :row_id"),
                {"v": val if val else None, "row_id": int(row_id)},
            )
            conn.commit()
            ok = result.rowcount > 0
        if ok:
            print(f"   ✅ Updated flag={val!r} for row ID {row_id}")
        return ok
    except Exception as e:
        print(f"   ⚠️  Error updating flag in MySQL: {e}")
        import traceback

        traceback.print_exc()
        return False


def read_mysql_data_as_dataframe(only_unpushed=False):
    """
    Read invoice table from MySQL as a full DataFrame (table name from INVOICE_TABLE_NAME env).
    This contains all extracted invoice data from PDFs.

    Args:
        only_unpushed: If True, only return rows where pushed_at = 0 or pushed_at IS NULL
                       (i.e. rows not yet processed through the push loop).

    Returns:
        pd.DataFrame: Full DataFrame with all columns from MySQL, mapped to Excel column names, or None if failed
    """
    try:
        table_name = get_invoice_table_name()
        print(f"\n   📊 Reading MySQL {table_name} table for data merging...")

        engine = get_db_engine()

        if only_unpushed:
            query = f"SELECT * FROM {table_name} WHERE pushed_at = 0 OR pushed_at IS NULL ORDER BY id ASC"
            print(f"      🔍 Filtering: only rows with pushed_at=0 or NULL (unpushed)")
        else:
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
            "hotel_name": "Hotel Name",
            "guest_name": "Guest Name",
            "booking_code": "Booking Code",
            "bill_number": "Bill Number",
            "bill_date": "Bill Date",
            "check_in_date": "Check-in Date",
            "check_out_date": "Check-out Date",
            "amount_paid": "Amount Paid",
            "email_amount": "Email Amount",
            "flag": "flag",
            "hbd_gst_number": "HBD GST Number",
            "client_gst_number": "Client GST Number",
            "cgst_number": "CGST Number",
            "sgst_number": "SGST Number",
            "total_cgst": "Total CGST",
            "total_sgst": "Total SGST",
            "room_charge": "Room Charge",
            "total_tariff": "Total Tariff",
            "other_charges": "Other Charges",
            "total_igst": "Total IGST",
            "total_taxes": "Total Taxes",
            "discount": "Discount",
            "advance_received": "Advance Received",
            "room_captured": "Room Captured",
            "gst_flag": "gst flag",
            "pdf_filename": "PDF Filename",
            "blob_url": "Blob URL",
            "multi_page_pdf": "Multi Page PDF",
            "push_failed": "Push Failed",
            "push_cancelled": "Push Cancelled",
            "validation_flag": "Validation Flag",
            "email_from": "Email From",
            "email_date": "Email Date",
            "assigned_to": "Assigned To",
            "created_at": "Created At",
            "processing_date": "Processing Date",
        }

        # Rename columns to Excel format
        master_df = master_df.rename(columns=column_mapping)

        # Format date columns to match Excel format (dd/mm/yy)
        date_columns = ["Bill Date", "Check-in Date", "Check-out Date"]
        for col in date_columns:
            if col in master_df.columns:
                master_df[col] = master_df[col].apply(
                    lambda x: format_date_to_dd_mm_yy(str(x))
                    if pd.notna(x) and str(x) != "None" and str(x) != "Not Found"
                    else ""
                )

        # Convert numeric columns to strings for consistency
        numeric_columns = ["Amount Paid", "Total CGST", "Total SGST"]
        for col in numeric_columns:
            if col in master_df.columns:
                master_df[col] = master_df[col].apply(lambda x: str(x) if pd.notna(x) else "")

        # Replace None/NaN with empty strings for consistency
        master_df = master_df.fillna("")

        print(f"      ✅ Mapped MySQL columns to Excel format")
        print(f"      📋 Excel columns: {[col for col in master_df.columns if col in column_mapping.values()]}")

        engine.dispose()
        return master_df

    except Exception as e:
        print(f"      ❌ Error reading MySQL data: {e}")
        import traceback

        traceback.print_exc()
        return None
