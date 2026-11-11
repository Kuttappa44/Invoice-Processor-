"""Health and config endpoints (no product rules)."""

from __future__ import annotations

import os
from datetime import datetime

from fastapi import APIRouter
from fastapi.responses import JSONResponse

import main
from backend.app.modules.tasks.registry import task_lock, tasks

router = APIRouter()


@router.get("/api/health")
async def health_check():
    """Health check endpoint"""
    with task_lock:
        active_tasks = len([t for t in tasks.values() if t["status"] == "running"])

    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "active_tasks": active_tasks,
        "total_tasks": len(tasks)
    }


@router.get("/api/api-push/config")
async def get_api_push_config():
    """Get API push configuration and status"""
    try:
        # Import API push constants from main.py
        api_endpoint = getattr(main, 'API_PUSH_ENDPOINT', None)
        api_token = getattr(main, 'API_BEARER_TOKEN', None)

        # Check if token is set (don't expose full token, just indicate if it exists)
        token_configured = bool(api_token)
        token_preview = None
        if api_token:
            # Show first 10 and last 4 characters for verification
            token_preview = f"{api_token[:10]}...{api_token[-4:]}" if len(api_token) > 14 else "***"

        return {
            "endpoint": api_endpoint or "Not configured",
            "token_configured": token_configured,
            "token_preview": token_preview,
            "status": "configured" if (api_endpoint and token_configured) else "not_configured",
            "note": "API push happens automatically after Excel matching. All rows marked as 'Received' are pushed to the API endpoint."
        }
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "endpoint": None,
            "token_configured": False
        }


@router.get("/api/config")
async def get_config():
    """Get all system configurations"""
    def mask_sensitive(value: str, show_last: int = 4) -> str:
        """Mask sensitive values, showing only last N characters"""
        if not value or len(value) <= show_last:
            return "***" if value else "Not set"
        return "***" + value[-show_last:]

    config = {
        "email": {
            "imap_server": os.getenv("IMAP_SERVER", "Not set"),
            "imap_port": os.getenv("IMAP_PORT", "Not set"),
            "smtp_server": os.getenv("SMTP_SERVER", "smtp.gmail.com"),
            "smtp_port": os.getenv("SMTP_PORT", "587"),
            "gmail_email": mask_sensitive(os.getenv("GMAIL_EMAIL", ""), 10),
            "gmail_password": "***" if os.getenv("GMAIL_PASSWORD") else "Not set",
        },
        "azure_openai": {
            "api_key": mask_sensitive(os.getenv("AZURE_OPENAI_API_KEY", ""), 4),
            "base_url": os.getenv("AZURE_OPENAI_BASE_URL", "Not set"),
            "deployment_name": os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-5-mini"),
        },
        "openai": {
            "api_key": mask_sensitive(os.getenv("OPENAI_API_KEY", ""), 4),
        },
        "blob_storage": {
            "sas_url": "Set" if os.getenv("BLOB_STORAGE_SAS_URL") else "Not set",
        },
        "google_drive": {
            "enabled": os.getenv("ENABLE_GOOGLE_DRIVE_UPLOAD", "false").lower() == "true",
            "master_excel_file_id": os.getenv("MASTER_EXCEL_FILE_ID", "Not set"),
            "invoice_master_sheet_id": os.getenv("INVOICE_MASTER_SHEET_ID", "10LdcnknrQqtrXM-UqnmXQ3ZOCvTD06nd"),
        },
        "api": {
            "push_endpoint": main.API_PUSH_ENDPOINT,
            "bearer_token": mask_sensitive(main.API_BEARER_TOKEN, 4) if main.API_BEARER_TOKEN else "Not set",
        },
        "processing": {
            "days_to_search": os.getenv("DAYS_TO_SEARCH", "7"),
            "excel_file_path": main.MATCHING_EXCEL_FILE_PATH or "Auto-generated from API",
        },
        "ocr_server": {
            "url": os.getenv("OCR_SERVER_URL", "http://localhost:5000"),
        },
        "vendor_emails": {
            "KUMAR": os.getenv("VENDOR_KUMAR_EMAIL", "Not set"),
            "SANDHYA": os.getenv("VENDOR_SANDHYA_EMAIL", "Not set"),
            "MOKSHITHA": os.getenv("VENDOR_MOKSHITHA_EMAIL", "Not set"),
            "LAKSHMI": os.getenv("VENDOR_LAKSHMI_EMAIL", "Not set"),
            "UNASSIGNED": os.getenv("VENDOR_UNASSIGNED_EMAIL", "Not set"),
        },
        "report_email": {
            "email": os.getenv("GMAIL_EMAIL", "Not set"),  # Use Gmail email for reports
        }
    }

    return JSONResponse(content=config)
