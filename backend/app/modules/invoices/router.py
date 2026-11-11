"""HTTP for invoice rows, MySQL export, and matching Excel files."""

from __future__ import annotations

import os
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

import main
from backend.app.modules.invoices.queries import (
    export_invoice_data_by_date_range,
    fetch_invoice_rows_for_api,
    list_invoice_sources,
)
from backend.app.modules.invoices.export import export_invoice_data_to_excel

from backend.app.modules.invoices.service import (
    DEFAULT_EXCEL_PATH,
    UPLOADS_DIR,
    get_current_excel_info,
    get_excel_uploads,
    refresh_excel_path_from_main,
    resolve_mysql_source,
    sync_excel_path_with_main,
)

router = APIRouter()


@router.get("/api/excel")
async def get_excel_info():
    """Get current Excel file information"""
    info = get_current_excel_info()
    return info


@router.get("/api/excel/download")
async def download_excel():
    """Download the current Excel file (after matching with API data) or latest generated file"""
    CURRENT_EXCEL_PATH = refresh_excel_path_from_main()

    excel_path = None
    all_candidates = []

    # First, try to use the configured path if it exists
    if CURRENT_EXCEL_PATH:
        potential_path = Path(CURRENT_EXCEL_PATH)
        if potential_path.exists():
            all_candidates.append(potential_path)

    # Also look for files matching the pattern: booking_data_*.xlsx
    current_dir = Path.cwd()
    booking_files = list(current_dir.glob("booking_data_*.xlsx"))
    all_candidates.extend(booking_files)

    # Remove duplicates and filter to only existing files
    unique_candidates = list(set(all_candidates))
    existing_candidates = [p for p in unique_candidates if p.exists()]

    if existing_candidates:
        # Sort by modification time (newest first) - this ensures we always get the latest file
        existing_candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        excel_path = existing_candidates[0]
        print(f"📥 Downloading Excel file: {excel_path.name} (modified: {datetime.fromtimestamp(excel_path.stat().st_mtime)})")
    else:
        raise HTTPException(
            status_code=404,
            detail="No Excel file found. Please run 'Run Full Flow' first to generate an Excel file."
        )

    # Generate a safe filename for download with timestamp to prevent caching
    base_filename = excel_path.name
    if not base_filename.endswith(('.xlsx', '.xlsm', '.xls')):
        base_filename = f"invoice_data_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"

    # Add modification timestamp to filename to ensure uniqueness and prevent caching
    file_mtime = excel_path.stat().st_mtime
    timestamp_str = datetime.fromtimestamp(file_mtime).strftime('%Y%m%d_%H%M%S')
    name_without_ext = base_filename.rsplit('.', 1)[0]
    ext = base_filename.rsplit('.', 1)[1] if '.' in base_filename else 'xlsx'
    filename = f"{name_without_ext}_{timestamp_str}.{ext}"

    return FileResponse(
        path=str(excel_path),
        filename=filename,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f"attachment; filename={filename}",
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )


@router.get("/api/mysql/export")
async def download_mysql_excel():
    """Download MySQL invoice_data table as Excel file"""
    try:
        print("📊 API: Exporting MySQL invoice_data to Excel...")
        # Call the export function
        excel_file_path = export_invoice_data_to_excel()

        if not excel_file_path or not os.path.exists(excel_file_path):
            raise HTTPException(
                status_code=500,
                detail="Failed to generate Excel file from MySQL. No data found or export failed."
            )

        # Get filename from path
        filename = os.path.basename(excel_file_path)

        return FileResponse(
            path=excel_file_path,
            filename=filename,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": f"attachment; filename={filename}"
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error exporting MySQL to Excel: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=500,
            detail=f"Error exporting MySQL data to Excel: {str(e)}"
        )


@router.get("/api/mysql/sources")
async def mysql_invoice_sources():
    """List Bills/Xtract sources and the MySQL tables they resolve to."""
    return {"sources": list_invoice_sources()}


@router.get("/api/mysql/rows")
async def mysql_invoice_rows(
    from_date: str,
    to_date: str,
    match: str = "all",
    limit: int = 500,
    offset: int = 0,
    source: str = "bills",
):
    """JSON rows from invoice table: dashboard date range, optional matched_status filter, paginated.

    Through 31 Aug 2026 the range is created_at; from 1 Sep 2026 it is email_date.
    """
    m = (match or "all").strip().lower()
    if m not in ("all", "matched", "unmatched", "others"):
        raise HTTPException(
            status_code=400,
            detail="match must be one of: all, matched, unmatched, others",
        )
    source_id, table_name = resolve_mysql_source(source)
    limit = min(max(limit, 1), 2000)
    offset = max(offset, 0)
    try:
        datetime.strptime(from_date, "%Y-%m-%d")
        datetime.strptime(to_date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid date format. Expected YYYY-MM-DD format.",
        )
    data = fetch_invoice_rows_for_api(
        from_date, to_date, m, limit, offset, table_name=table_name
    )
    if data is None:
        raise HTTPException(
            status_code=400,
            detail="Invalid date range (from > to) or database error.",
        )
    data["source"] = source_id
    return data


@router.get("/api/mysql/export-by-date")
async def download_mysql_excel_by_date_range(
    from_date: str,
    to_date: str,
    match: str = "all",
    source: str = "bills",
):
    """Download MySQL invoice table as Excel filtered by dashboard date range and optional match filter.

    Through 31 Aug 2026 the range is created_at; from 1 Sep 2026 it is email_date.
    """
    try:
        m = (match or "all").strip().lower()
        if m not in ("all", "matched", "unmatched", "others"):
            raise HTTPException(
                status_code=400,
                detail="match must be one of: all, matched, unmatched, others",
            )
        source_id, table_name = resolve_mysql_source(source)
        print(
            f"📊 API: Exporting MySQL {table_name} (source={source_id}) to Excel by date range "
            f"({from_date} to {to_date}, match={m})..."
        )

        # Validate date format
        try:
            datetime.strptime(from_date, '%Y-%m-%d')
            datetime.strptime(to_date, '%Y-%m-%d')
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="Invalid date format. Expected YYYY-MM-DD format."
            )

        # Call the export function
        excel_file_path = export_invoice_data_by_date_range(
            from_date, to_date, match_filter=m, table_name=table_name
        )

        if not excel_file_path or not os.path.exists(excel_file_path):
            raise HTTPException(
                status_code=500,
                detail=f"Failed to generate Excel file from MySQL for date range {from_date} to {to_date}. No data found or export failed."
            )

        # Get filename from path
        filename = os.path.basename(excel_file_path)

        return FileResponse(
            path=excel_file_path,
            filename=filename,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": f"attachment; filename={filename}"
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error exporting MySQL to Excel by date range: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=500,
            detail=f"Error exporting MySQL to Excel by date range: {str(e)}"
        )


@router.post("/api/excel")
async def upload_excel(file: UploadFile = File(...)):
    """Upload an Excel file for matching"""
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    ext = Path(file.filename).suffix.lower()
    if ext not in ['.xlsx', '.xlsm', '.xls']:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file type. Expected .xlsx, .xlsm, or .xls, got {ext}"
        )

    # Generate unique filename
    stored_name = f"{uuid.uuid4().hex[:8]}_{file.filename}"
    stored_path = UPLOADS_DIR / stored_name

    try:
        # Save uploaded file
        with open(stored_path, "wb") as f:
            content = await file.read()
            f.write(content)

        # Set as current Excel file
        sync_excel_path_with_main(str(stored_path))

        # Return updated info
        info = get_current_excel_info()
        return info

    except Exception as e:
        if stored_path.exists():
            stored_path.unlink()
        raise HTTPException(status_code=500, detail=f"Failed to save file: {str(e)}")


@router.delete("/api/excel/uploads/{stored_name}")
async def delete_excel_upload(stored_name: str):
    """Delete an uploaded Excel file"""
    # Security: prevent path traversal
    if '..' in stored_name or '/' in stored_name or '\\' in stored_name:
        raise HTTPException(status_code=400, detail="Invalid filename")

    file_path = UPLOADS_DIR / stored_name

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    # Check if this is the currently active file
    current_path = main.MATCHING_EXCEL_FILE_PATH

    if str(file_path) == current_path:
        # Switch to another file if available
        uploads = get_excel_uploads()
        other_uploads = [u for u in uploads if u["stored_name"] != stored_name]

        if other_uploads:
            # Switch to the most recent other upload
            sync_excel_path_with_main(other_uploads[0]["path"])
        else:
            # Switch back to default
            sync_excel_path_with_main(DEFAULT_EXCEL_PATH)

    try:
        file_path.unlink()
        info = get_current_excel_info()
        return {
            "message": "File deleted successfully",
            "current": main.MATCHING_EXCEL_FILE_PATH,
            "uploads": info["uploads"]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete file: {str(e)}")
