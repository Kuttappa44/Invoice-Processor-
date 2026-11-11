"""
Count emails and PDF attachments per day for a Gmail date range.

Credentials from .env (or mailbox env loaded by run_mailbox_{a,b,c}.sh):
    GMAIL_EMAIL_1, GMAIL_PASSWORD_1, IMAP_SERVER, IMAP_PORT (optional), IMAP_MAILBOX (optional)

Counts are upserted into MySQL table mailbox_daily_counts for the dashboard API.

Run:
    python count_mailbox_by_day.py
    python count_mailbox_by_day.py --from 2026-10-01 --to 2026-10-23
    python count_mailbox_by_day.py --from 2026-10-01 --to 2026-10-23 --source bills
    python count_mailbox_by_day.py --from 2026-10-01 --to 2026-10-23 --output mailbox_counts.xlsx
"""

from __future__ import annotations

import argparse
import email
import imaplib
import os
import ssl
import time
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Optional

from dotenv import load_dotenv

from backend.app.modules.invoices.mysql import (
    MAILBOX_SOURCES,
    normalize_mailbox_source,
    upsert_mailbox_daily_counts,
)
from backend.app.integrations.gmail_imap import select_imap_mailbox

load_dotenv()

PDF_EXTS = {".pdf"}

SKIP_CONTENT_TYPES = {
    "text/plain",
    "text/html",
    "multipart/mixed",
    "multipart/alternative",
    "multipart/related",
    "multipart/signed",
    "message/rfc822",
}

COUNT_KEYS = (
    "emails",
    "pdfs",
)


def _env_required(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"Missing {name} in .env")
    return value


def parse_date(value: str) -> datetime:
    return datetime.strptime(value.strip(), "%Y-%m-%d")


def prompt_dates(from_date: Optional[str], to_date: Optional[str]) -> tuple[str, str]:
    if not from_date:
        from_date = input("From date (YYYY-MM-DD): ").strip()
    if not to_date:
        to_date = input("To date (YYYY-MM-DD): ").strip()
    parse_date(from_date)
    parse_date(to_date)
    if parse_date(from_date) > parse_date(to_date):
        raise ValueError("From date must be on or before to date")
    return from_date, to_date


def is_imap_dead(exc: BaseException) -> bool:
    if isinstance(exc, (imaplib.IMAP4.abort, ssl.SSLEOFError, TimeoutError, BrokenPipeError, ConnectionError)):
        return True
    msg = str(exc).lower()
    return any(
        token in msg
        for token in (
            "eof",
            "socket error",
            "bye",
            "connection reset",
            "broken pipe",
            "timed out",
        )
    )


def connect_imap() -> imaplib.IMAP4_SSL:
    server = os.getenv("IMAP_SERVER", "imap.gmail.com")
    port = int(os.getenv("IMAP_PORT", "993"))
    user = _env_required("GMAIL_EMAIL_1")
    password = _env_required("GMAIL_PASSWORD_1")
    last_err: Optional[BaseException] = None
    for attempt in range(1, 11):
        try:
            mail = imaplib.IMAP4_SSL(server, port)
            mail.login(user, password)
            select_imap_mailbox(mail)
            return mail
        except Exception as exc:
            last_err = exc
            wait = min(2 * attempt, 20)
            print(f"  IMAP connect attempt {attempt}/10 failed: {exc}. Retrying in {wait}s...", flush=True)
            time.sleep(wait)
    raise RuntimeError(f"Could not connect to IMAP: {last_err}") from last_err


def reconnect_imap(mail: Optional[imaplib.IMAP4_SSL]) -> imaplib.IMAP4_SSL:
    if mail is not None:
        try:
            mail.close()
        except Exception:
            pass
        try:
            mail.logout()
        except Exception:
            pass
    print("  Reconnecting IMAP...", flush=True)
    return connect_imap()


def ensure_imap(mail: Optional[imaplib.IMAP4_SSL]) -> imaplib.IMAP4_SSL:
    if mail is None:
        return connect_imap()
    try:
        status, _ = mail.noop()
        if status == "OK":
            return mail
    except Exception:
        pass
    return reconnect_imap(mail)


def search_uids(mail: imaplib.IMAP4_SSL, from_date: str, to_date: str) -> list[bytes]:
    from_dt = parse_date(from_date)
    to_dt = parse_date(to_date)
    after = from_dt.strftime("%Y/%m/%d")
    before = (to_dt + timedelta(days=1)).strftime("%Y/%m/%d")
    since = from_dt.strftime("%d-%b-%Y")
    before_imap = (to_dt + timedelta(days=1)).strftime("%d-%b-%Y")

    query = f"after:{after} before:{before} -in:spam -in:trash"
    try:
        status, data = mail.uid("search", None, "X-GM-RAW", f'"{query}"')
        if status == "OK":
            raw = data[0] if data and data[0] else b""
            return raw.split() if raw else []
    except imaplib.IMAP4.error as exc:
        if is_imap_dead(exc) or isinstance(exc, imaplib.IMAP4.abort):
            raise
        # Gmail rejected X-GM-RAW syntax; fall back to standard IMAP search.

    status, data = mail.uid("search", None, f"(SINCE {since} BEFORE {before_imap})")
    if status != "OK":
        raise RuntimeError(f"IMAP search failed: {status}")
    raw = data[0] if data and data[0] else b""
    return raw.split() if raw else []


def fetch_message(mail: imaplib.IMAP4_SSL, uid: bytes) -> email.message.Message:
    status, msg_data = mail.uid("fetch", uid, "(BODY.PEEK[])")
    if status != "OK" or not msg_data or not msg_data[0]:
        raise RuntimeError(f"Failed to fetch UID {uid!r}")
    item = msg_data[0]
    payload = item[1] if isinstance(item, tuple) else item
    if isinstance(payload, tuple):
        payload = payload[1]
    return email.message_from_bytes(payload)


def email_day(msg: email.message.Message, fallback: str) -> str:
    raw = msg.get("Date")
    if not raw:
        return fallback
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return fallback


def is_pdf_attachment(filename: str, content_type: str) -> bool:
    name = (filename or "").lower().strip()
    ctype = (content_type or "").lower().strip()
    ext = os.path.splitext(name)[1] if name else ""
    return ext in PDF_EXTS or ctype == "application/pdf"


def iter_pdf_attachments(msg: email.message.Message):
    if not msg.is_multipart():
        filename = msg.get_filename()
        ctype = msg.get_content_type()
        if is_pdf_attachment(filename or "", ctype):
            yield filename or "", ctype
        return

    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype in SKIP_CONTENT_TYPES:
            continue
        filename = part.get_filename() or ""
        if not is_pdf_attachment(filename, ctype):
            continue
        if not part.get_payload(decode=True):
            continue
        yield filename, ctype


def empty_bucket() -> dict:
    return {key: 0 for key in COUNT_KEYS}


def print_report(from_date: str, to_date: str, by_day: dict[str, dict]) -> None:
    headers = ["date", *COUNT_KEYS]
    days = []
    cur = parse_date(from_date)
    end = parse_date(to_date)
    while cur <= end:
        days.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)

    widths = {h: len(h) for h in headers}
    totals = empty_bucket()
    rows = []
    for day in days:
        row = by_day.get(day, empty_bucket())
        rows.append((day, row))
        for k in COUNT_KEYS:
            totals[k] += row[k]
            widths[k] = max(widths[k], len(str(row[k])))
        widths["date"] = max(widths["date"], len(day), len("TOTAL"))
    for k in COUNT_KEYS:
        widths[k] = max(widths[k], len(str(totals[k])))

    rule = "-" * (sum(widths.values()) + len(headers) * 2)
    print(flush=True)
    print(f"FULL REPORT  {from_date}  to  {to_date}  (inclusive)", flush=True)
    print(rule, flush=True)
    print("  ".join(h.ljust(widths[h]) for h in headers), flush=True)
    for day, row in rows:
        cells = [day.ljust(widths["date"])] + [str(row[k]).ljust(widths[k]) for k in COUNT_KEYS]
        print("  ".join(cells), flush=True)
    print(rule, flush=True)
    print(
        "  ".join(["TOTAL".ljust(widths["date"])] + [str(totals[k]).ljust(widths[k]) for k in COUNT_KEYS]),
        flush=True,
    )
    print(flush=True)


def write_excel(from_date: str, to_date: str, by_day: dict[str, dict], path: str) -> None:
    import pandas as pd
    from openpyxl import load_workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    headers = ["date", *COUNT_KEYS]
    cur = parse_date(from_date)
    end = parse_date(to_date)
    rows = []
    totals = empty_bucket()
    while cur <= end:
        day = cur.strftime("%Y-%m-%d")
        row = by_day.get(day, empty_bucket())
        rows.append({"date": day, **{k: row[k] for k in COUNT_KEYS}})
        for key in COUNT_KEYS:
            totals[key] += row[key]
        cur += timedelta(days=1)
    rows.append({"date": "TOTAL", **totals})

    df = pd.DataFrame(rows, columns=headers)
    df.to_excel(path, index=False, sheet_name="Mailbox counts", engine="openpyxl")

    wb = load_workbook(path)
    ws = wb["Mailbox counts"]
    header_fill = PatternFill(start_color="366092", end_color="366092", fill_type="solid")
    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_alignment = Alignment(horizontal="center", vertical="center")
    thin = Border(
        left=Side(style="thin"),
        right=Side(style="thin"),
        top=Side(style="thin"),
        bottom=Side(style="thin"),
    )
    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = header_alignment
        cell.border = thin
    total_font = Font(bold=True)
    for cell in ws[ws.max_row]:
        cell.font = total_font
        cell.border = thin
    ws.column_dimensions["A"].width = 14
    for col in ws.iter_cols(min_col=2, max_col=ws.max_column, max_row=1):
        letter = col[0].column_letter
        ws.column_dimensions[letter].width = max(len(str(col[0].value or "")) + 2, 12)
    ws.freeze_panes = "A2"
    wb.save(path)
    print(f"Excel written: {path}", flush=True)


def save_counts_to_mysql(
    by_day: dict[str, dict],
    *,
    source: str,
    instance: str,
    imap_user: str,
) -> None:
    rows = [
        {
            "count_date": day,
            "emails": bucket.get("emails", 0),
            "pdfs": bucket.get("pdfs", 0),
        }
        for day, bucket in sorted(by_day.items())
    ]
    written = upsert_mailbox_daily_counts(
        rows,
        source=source,
        instance=instance,
        imap_user=imap_user,
    )
    print(
        f"MySQL upserted {written} day(s) into mailbox_daily_counts (source={source}, instance={instance})",
        flush=True,
    )


def count_one_day(mail: imaplib.IMAP4_SSL, day: str) -> tuple[dict, imaplib.IMAP4_SSL]:
    bucket = empty_bucket()
    uids: Optional[list[bytes]] = None
    for attempt in range(1, 6):
        try:
            mail = ensure_imap(mail)
            uids = search_uids(mail, day, day)
            break
        except Exception as exc:
            if not is_imap_dead(exc) or attempt == 5:
                raise
            print(f"  Search failed ({exc}); reconnecting ({attempt}/5)...", flush=True)
            mail = reconnect_imap(mail)
    if uids is None:
        raise RuntimeError(f"Could not search mail for {day}")

    print(f"  Found {len(uids)} email(s) on {day}", flush=True)
    i = 0
    while i < len(uids):
        uid = uids[i]
        uid_ok = False
        for attempt in range(1, 6):
            try:
                msg = fetch_message(mail, uid)
                if email_day(msg, day) == day:
                    bucket["emails"] += 1
                    for filename, ctype in iter_pdf_attachments(msg):
                        if is_pdf_attachment(filename, ctype):
                            bucket["pdfs"] += 1
                uid_ok = True
                break
            except Exception as exc:
                if is_imap_dead(exc):
                    print(
                        f"  IMAP dropped on UID {uid.decode(errors='replace')} "
                        f"({i + 1}/{len(uids)}): {exc}. Reconnecting and retrying...",
                        flush=True,
                    )
                    mail = reconnect_imap(mail)
                    time.sleep(1)
                    continue
                print(f"  skip UID {uid!r}: {exc}", flush=True)
                uid_ok = True
                break
        if not uid_ok:
            print(f"  skip UID {uid!r}: still failing after reconnect retries", flush=True)
        i += 1
        if i % 50 == 0:
            print(f"  Progress {day}: {i}/{len(uids)}", flush=True)
            try:
                mail.noop()
            except Exception:
                mail = reconnect_imap(mail)
    return bucket, mail


def count_mailbox_range(
    from_date: str,
    to_date: str,
    *,
    source: Optional[str] = None,
    output: Optional[str] = None,
    write_mysql: bool = True,
) -> dict[str, dict]:
    """Count emails and PDFs for an inclusive date range and optionally upsert MySQL.

    Used by process_invoices and the CLI. Does not prompt for dates.
    """
    parse_date(from_date)
    parse_date(to_date)
    if parse_date(from_date) > parse_date(to_date):
        raise ValueError("From date must be on or before to date")

    instance = (os.getenv("SCHEDULED_INSTANCE") or "a").strip() or "a"
    if source:
        raw_source = source.strip().lower()
        if raw_source not in MAILBOX_SOURCES and raw_source not in ("a", "b", "c"):
            raise ValueError("source must be one of: bills, xtract, btcbills")
    source_id = normalize_mailbox_source(source, instance)
    imap_user = _env_required("GMAIL_EMAIL_1")
    mail = connect_imap()
    try:
        headers = ["date", *COUNT_KEYS]
        print(f"Mailbox counts  {from_date}  to  {to_date}  (inclusive)", flush=True)
        print(f"Source: {source_id} | instance: {instance} | IMAP: {imap_user}", flush=True)
        print("  ".join(headers), flush=True)

        by_day: dict[str, dict] = {}
        cur = parse_date(from_date)
        end = parse_date(to_date)
        while cur <= end:
            day = cur.strftime("%Y-%m-%d")
            print(f"Scanning {day}...", flush=True)
            mail = ensure_imap(mail)
            bucket, mail = count_one_day(mail, day)
            by_day[day] = bucket
            print("  ".join([day] + [str(bucket[k]) for k in COUNT_KEYS]), flush=True)
            cur += timedelta(days=1)

        print_report(from_date, to_date, by_day)
        if output:
            write_excel(from_date, to_date, by_day, output)
        if write_mysql:
            try:
                save_counts_to_mysql(
                    by_day,
                    source=source_id,
                    instance=instance,
                    imap_user=imap_user,
                )
            except Exception as exc:
                print(f"WARNING: Failed to save mailbox counts to MySQL: {exc}", flush=True)
        return by_day
    finally:
        try:
            mail.close()
        except Exception:
            pass
        try:
            mail.logout()
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Count Gmail emails and PDF attachments per day.")
    parser.add_argument("--from", dest="from_date", default=None, help="Start date YYYY-MM-DD")
    parser.add_argument("--to", dest="to_date", default=None, help="End date YYYY-MM-DD")
    parser.add_argument(
        "--source",
        default=None,
        help="Dashboard source: bills, xtract, or btcbills (default: from SCHEDULED_INSTANCE)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Optional Excel path (not written unless this flag is set)",
    )
    parser.add_argument(
        "--no-mysql",
        action="store_true",
        help="Skip writing mailbox_daily_counts",
    )
    args = parser.parse_args()

    from_date, to_date = prompt_dates(args.from_date, args.to_date)
    count_mailbox_range(
        from_date,
        to_date,
        source=args.source,
        output=args.output,
        write_mysql=not args.no_mysql,
    )


if __name__ == "__main__":
    main()
