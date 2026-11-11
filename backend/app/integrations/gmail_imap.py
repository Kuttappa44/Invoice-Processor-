"""Gmail/IMAP client: mailbox selection, search, peek-fetch. No invoice rules."""

from __future__ import annotations

import imaplib
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

DEFAULT_IMAP_MAILBOX = "[Gmail]/All Mail"

# Business timezone: date-range searches and the stored email_date both use IST days.
_IST = timezone(timedelta(hours=5, minutes=30))


def get_imap_mailbox() -> str:
    """Return configured IMAP folder (default: Gmail All Mail)."""
    return os.getenv("IMAP_MAILBOX", DEFAULT_IMAP_MAILBOX).strip() or DEFAULT_IMAP_MAILBOX


def select_imap_mailbox(mail: imaplib.IMAP4_SSL) -> str:
    """Select the configured mailbox; raises if selection fails."""
    mailbox = get_imap_mailbox()
    select_arg = f'"{mailbox}"' if any(ch in mailbox for ch in ' []/') else mailbox
    status, _ = mail.select(select_arg)
    if status != "OK":
        raise RuntimeError(f"Failed to select IMAP mailbox {mailbox!r}: {status}")
    return mailbox


def _use_gmail_xgm_raw() -> bool:
    server = (os.getenv("IMAP_SERVER") or "").lower()
    if "gmail" in server:
        return True
    return os.getenv("IMAP_USE_XGM_RAW", "false").lower() == "true"


def _parse_uid_list(data) -> list[bytes]:
    raw = data[0] if data and data[0] else b""
    return raw.split() if raw else []


def only_unread_mode() -> bool:
    """
    True when only UNSEEN mail should be processed.

    Controlled by env PROCESS_ONLY_UNREAD (default: false -> process ALL mail in the
    selected date range, read or unread). Set PROCESS_ONLY_UNREAD=true to restore the
    old unread-only behaviour.
    """
    return os.getenv("PROCESS_ONLY_UNREAD", "false").strip().lower() in ("1", "true", "yes", "on")


def _gmail_unread_raw_query(
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    unread_only: bool = True,
) -> str:
    # In "all mail" mode exclude Sent/Drafts: [Gmail]/All Mail also contains our own outgoing
    # messages (e.g. forwards), which are normally read and so were never picked up before.
    if unread_only:
        base = "is:unread -in:spam -in:trash"
    else:
        base = "-in:spam -in:trash -in:sent -in:drafts"
    if from_date and to_date:
        from_dt = datetime.strptime(from_date, "%Y-%m-%d")
        to_dt = datetime.strptime(to_date, "%Y-%m-%d")
        # Use exact epoch seconds at IST midnight so the search window uses the same
        # day boundaries as the stored email_date (INTERNALDATE converted to IST).
        # Gmail's date-string form (after:YYYY/MM/DD) picks its own timezone.
        after_ts = int(datetime.combine(from_dt.date(), datetime.min.time(), tzinfo=_IST).timestamp())
        before_ts = int(
            datetime.combine(to_dt.date() + timedelta(days=1), datetime.min.time(), tzinfo=_IST).timestamp()
        )
        return f"{base} after:{after_ts} before:{before_ts}"
    return base


def _uid_search(mail: imaplib.IMAP4_SSL, *parts: str) -> list[bytes]:
    status, data = mail.uid("search", None, *parts)
    if status != "OK":
        raise RuntimeError(f"IMAP UID SEARCH failed: {status} ({' '.join(parts)})")
    return _parse_uid_list(data)


def search_unread_uids(
    mail: imaplib.IMAP4_SSL,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    since_date: Optional[str] = None,
    unread_only: Optional[bool] = None,
) -> list[bytes]:
    """
    Return message UIDs for mail in the configured mailbox.

    By default this returns ALL mail (read or unread) in the requested range. Set env
    PROCESS_ONLY_UNREAD=true (or pass ``unread_only=True``) to restrict to unread mail.
    ``unread_only`` overrides the env setting when not None.

    Prefers Gmail X-GM-RAW (matches web UI). Falls back to plain IMAP criteria.
    """
    if unread_only is None:
        unread_only = only_unread_mode()
    unseen = "UNSEEN " if unread_only else ""

    if _use_gmail_xgm_raw():
        try:
            if from_date and to_date:
                query = _gmail_unread_raw_query(from_date, to_date, unread_only)
            elif since_date:
                # Gmail raw "after:" accepts YYYY/MM/DD
                try:
                    dt = datetime.strptime(since_date, "%d-%b-%Y")
                    after = dt.strftime("%Y/%m/%d")
                    query = f"{_gmail_unread_raw_query(unread_only=unread_only)} after:{after}"
                except ValueError:
                    query = _gmail_unread_raw_query(unread_only=unread_only)
            else:
                query = _gmail_unread_raw_query(unread_only=unread_only)
            return _uid_search(mail, "X-GM-RAW", query)
        except Exception:
            pass

    if from_date and to_date:
        from_dt = datetime.strptime(from_date, "%Y-%m-%d")
        to_dt = datetime.strptime(to_date, "%Y-%m-%d")
        since = from_dt.strftime("%d-%b-%Y")
        before = (to_dt + timedelta(days=1)).strftime("%d-%b-%Y")
        return _uid_search(mail, f"{unseen}SINCE {since} BEFORE {before}")

    if since_date:
        return _uid_search(mail, f"{unseen}SINCE {since_date}")

    return _uid_search(mail, "UNSEEN" if unread_only else "ALL")


def count_unread_uids(
    mail: imaplib.IMAP4_SSL,
    from_date: str,
    to_date: str,
) -> int:
    # Always a true unread count, regardless of PROCESS_ONLY_UNREAD.
    return len(
        search_unread_uids(mail, from_date=from_date, to_date=to_date, unread_only=True)
    )


def fetch_rfc822(mail: imaplib.IMAP4_SSL, uid: bytes) -> bytes:
    # BODY.PEEK[] returns the full message without setting \\Seen, so unread
    # mail stays unread after processing. RFC822 would mark it read on fetch.
    status, msg_data = mail.uid("fetch", uid, "(BODY.PEEK[])")
    if status != "OK" or not msg_data or not msg_data[0]:
        raise RuntimeError(f"Failed to fetch message UID {uid!r}: {status}")
    item = msg_data[0]
    if isinstance(item, tuple):
        payload = item[1]
        if isinstance(payload, tuple):
            payload = payload[1]
        return payload
    if isinstance(item, bytes):
        return item
    raise RuntimeError(f"Unexpected FETCH response for UID {uid!r}")


_INTERNALDATE_RE = re.compile(rb'INTERNALDATE\s+"([^"]+)"', re.IGNORECASE)


def fetch_internal_date(mail: imaplib.IMAP4_SSL, uid: bytes) -> Optional[date]:
    """
    Return the date (IST) the message arrived in the mailbox (IMAP INTERNALDATE),
    or None if it cannot be fetched/parsed. Never raises.
    """
    try:
        status, msg_data = mail.uid("fetch", uid, "(INTERNALDATE)")
        if status != "OK" or not msg_data:
            return None
        for item in msg_data:
            raw = item[0] if isinstance(item, tuple) else item
            if not isinstance(raw, bytes):
                continue
            m = _INTERNALDATE_RE.search(raw)
            if not m:
                continue
            # e.g. "29-Oct-2026 10:20:30 +0000" (day may be space-padded)
            dt = datetime.strptime(m.group(1).decode("ascii").strip(), "%d-%b-%Y %H:%M:%S %z")
            return dt.astimezone(_IST).date()
    except Exception:
        return None
    return None


def mark_uid_seen(mail: imaplib.IMAP4_SSL, uid: bytes) -> None:
    status, _ = mail.uid("store", uid, "+FLAGS", "\\Seen")
    if status != "OK":
        raise RuntimeError(f"Failed to mark UID {uid!r} as read: {status}")
