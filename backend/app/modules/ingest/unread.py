"""Count unread emails for a date range (IMAP / Gmail All Mail)."""

from __future__ import annotations

import imaplib
import os

from dotenv import load_dotenv

from backend.app.integrations.gmail_imap import (
    count_unread_uids,
    only_unread_mode,
    select_imap_mailbox,
)

load_dotenv()


def count_unread_emails_for_date_range(from_date: str, to_date: str) -> int:
    """
    Return count of UNSEEN messages for [from_date, to_date] inclusive.

    Uses the same mailbox and search logic as process_invoices in legacy_impl.py.

    When processing ALL mail (PROCESS_ONLY_UNREAD is not true), a single pass already covers
    every email in the range, so there is nothing "remaining" to re-queue: returns 0 so the
    scheduled follow-up-pass loop in run_scheduled.sh does not repeat needlessly.
    """
    if not only_unread_mode():
        return 0

    imap_server = os.getenv("IMAP_SERVER")
    imap_port = int(os.getenv("IMAP_PORT", "993"))
    email_user = os.getenv("GMAIL_EMAIL_1")
    email_pass = os.getenv("GMAIL_PASSWORD_1")

    if not imap_server or not email_user or not email_pass:
        raise RuntimeError(
            "Missing IMAP credentials (IMAP_SERVER, GMAIL_EMAIL_1, GMAIL_PASSWORD_1)"
        )

    mail = imaplib.IMAP4_SSL(imap_server, imap_port)
    try:
        mail.login(email_user, email_pass)
        select_imap_mailbox(mail)
        return count_unread_uids(mail, from_date, to_date)
    finally:
        try:
            mail.close()
        except Exception:
            pass
        try:
            mail.logout()
        except Exception:
            pass
