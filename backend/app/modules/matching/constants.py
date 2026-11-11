"""Constants and env helpers. Does not import leftover_impl1."""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


def get_env_value(name, *, default=None, required=False):
    """Fetch environment variable values with optional defaults."""
    value = os.getenv(name, default)
    if required and not value:
        raise ValueError(f"Environment variable '{name}' is required but missing.")
    return value


MATCHING_EXCEL_FILE_PATH = os.getenv("EXCEL_FILE_PATH", None)

MATCHING_FIELDS = [
    "Booking Code",
    "Guest Name",
    "Hotel Name",
    "Check-In Date",
    "Check-Out Date",
]

INVOICE_RECEIVED_COLUMN = "Invoice Received"
INVOICE_RECEIVED_VALUE = "Received"
MYSQL_ROW_ID_COLUMN = "MySQL Row ID"
EXCEL_MATCH_GUEST_SCORE_COLUMN = "Excel Match Guest Score"
EXCEL_MATCH_CHECKIN_DELTA_COLUMN = "Excel Match Checkin Delta"
EXCEL_MATCH_CHECKOUT_DELTA_COLUMN = "Excel Match Checkout Delta"

VENDOR_EMAILS = {
    "KUMAR": os.getenv("VENDOR_KUMAR_EMAIL", ""),
    "SANDHYA": os.getenv("VENDOR_SANDHYA_EMAIL", ""),
    "MOKSHITHA": os.getenv("VENDOR_MOKSHITHA_EMAIL", ""),
    "LAKSHMI": os.getenv("VENDOR_LAKSHMI_EMAIL", ""),
    "UNASSIGNED": os.getenv("VENDOR_UNASSIGNED_EMAIL", ""),
}
