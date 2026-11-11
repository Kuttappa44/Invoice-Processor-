"""
The only new reader of environment variables.

Existing modules may still call os.getenv until later phases. Defaults match today's
hardcoded values so connection and table names do not change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _as_bool(raw: str | None, default: bool) -> bool:
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _safe_table_name(raw: str | None, default: str = "invoice_data") -> str:
    name = (raw or default).strip()
    if not name or not all(c.isalnum() or c == "_" for c in name):
        return default
    return name


@dataclass(frozen=True)
class Settings:
    mysql_host: str
    mysql_user: str
    mysql_password: str
    mysql_database: str
    invoice_table_name: str
    imap_server: str | None
    imap_port: int
    imap_mailbox: str
    gmail_email: str | None
    gmail_password: str | None
    process_only_unread: bool
    skip_already_processed_emails: bool
    step7_every_n_emails: int
    blob_storage_sas_url: str | None
    azure_openai_api_key: str | None
    azure_openai_base_url: str | None
    azure_openai_deployment_name: str
    openai_api_key: str | None
    api_push_endpoint: str
    enable_google_drive_upload: bool
    excel_file_path: str | None
    scheduled_instance: str
    cors_allow_origins: str


def get_settings() -> Settings:
    """Read current process environment. Not cached — matches existing os.getenv call sites."""
    try:
        step7_every_n = int(os.getenv("STEP7_EVERY_N_EMAILS", "100"))
    except ValueError:
        step7_every_n = 100
    if step7_every_n < 1:
        step7_every_n = 100

    try:
        imap_port = int(os.getenv("IMAP_PORT", "993"))
    except ValueError:
        imap_port = 993

    return Settings(
        mysql_host=os.getenv("MYSQL_HOST") or "localhost",
        mysql_user=os.getenv("MYSQL_USER") or "root",
        mysql_password=os.getenv("MYSQL_PASSWORD") or "Irahirs11@ittak!",
        mysql_database=os.getenv("MYSQL_DATABASE") or "bsr",
        invoice_table_name=_safe_table_name(os.getenv("INVOICE_TABLE_NAME")),
        imap_server=os.getenv("IMAP_SERVER"),
        imap_port=imap_port,
        imap_mailbox=(os.getenv("IMAP_MAILBOX") or "[Gmail]/All Mail").strip()
        or "[Gmail]/All Mail",
        gmail_email=os.getenv("GMAIL_EMAIL_1"),
        gmail_password=os.getenv("GMAIL_PASSWORD_1"),
        process_only_unread=_as_bool(os.getenv("PROCESS_ONLY_UNREAD"), False),
        skip_already_processed_emails=_as_bool(
            os.getenv("SKIP_ALREADY_PROCESSED_EMAILS"), True
        ),
        step7_every_n_emails=step7_every_n,
        blob_storage_sas_url=os.getenv("BLOB_STORAGE_SAS_URL"),
        azure_openai_api_key=os.getenv("AZURE_OPENAI_API_KEY"),
        azure_openai_base_url=os.getenv("AZURE_OPENAI_BASE_URL"),
        azure_openai_deployment_name=os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME")
        or "gpt-5-mini",
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        api_push_endpoint=os.getenv(
            "API_PUSH_ENDPOINT",
            "https://hbinvoiceapi.staysimplyfied.com/api/HotelInvoice/Push",
        ),
        enable_google_drive_upload=_as_bool(
            os.getenv("ENABLE_GOOGLE_DRIVE_UPLOAD"), False
        ),
        excel_file_path=os.getenv("EXCEL_FILE_PATH"),
        scheduled_instance=(os.getenv("SCHEDULED_INSTANCE") or "a").strip() or "a",
        cors_allow_origins=os.getenv("CORS_ALLOW_ORIGINS", "*"),
    )
