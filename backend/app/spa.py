"""Serve the Vite SPA, assets, and logo. Catch-all must be mounted last."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

FRONTEND_DIR = Path("frontend/dist")
FRONTEND_INDEX = FRONTEND_DIR / "index.html"
LOGO_PATH = Path("/home/ubuntu/Invoice-processor/invoice/logo.png")
assets_dir = FRONTEND_DIR / "assets" if FRONTEND_DIR.exists() else None

router = APIRouter()


def find_logo_path():
    """Return logo path if present on disk."""
    return LOGO_PATH if LOGO_PATH.exists() else None


@router.get("/logo.png")
async def get_logo():
    """Serve the logo image from LOGO_PATH."""
    logo_path = find_logo_path()
    if logo_path and logo_path.exists():
        return FileResponse(logo_path, media_type="image/png")
    else:
        raise HTTPException(status_code=404, detail="Logo not found")


@router.get("/api/logo")
async def get_logo_api():
    """Serve the logo image at /api/logo from LOGO_PATH."""
    logo_path = find_logo_path()
    if logo_path and logo_path.exists():
        return FileResponse(logo_path, media_type="image/png")
    else:
        raise HTTPException(status_code=404, detail="Logo not found")


@router.get("/assets/{file_path:path}")
async def serve_asset(file_path: str):
    """Serve frontend assets"""
    if not assets_dir or not assets_dir.exists():
        raise HTTPException(status_code=404, detail="Assets directory not found")

    # Remove query parameters
    if '?' in file_path:
        file_path = file_path.split('?')[0]

    asset_file = assets_dir / file_path

    # Security: prevent path traversal
    try:
        resolved_asset = asset_file.resolve()
        resolved_dir = assets_dir.resolve()
        resolved_asset.relative_to(resolved_dir)
    except (ValueError, OSError):
        raise HTTPException(status_code=403, detail="Invalid path")

    if not asset_file.exists() or not asset_file.is_file():
        raise HTTPException(status_code=404, detail=f"Asset not found: {file_path}")

    # Determine content type
    content_type = "application/octet-stream"
    if file_path.endswith(".js"):
        content_type = "application/javascript; charset=utf-8"
    elif file_path.endswith(".css"):
        content_type = "text/css; charset=utf-8"
    elif file_path.endswith(".png"):
        content_type = "image/png"
    elif file_path.endswith((".jpg", ".jpeg")):
        content_type = "image/jpeg"
    elif file_path.endswith(".svg"):
        content_type = "image/svg+xml"
    elif file_path.endswith(".woff") or file_path.endswith(".woff2"):
        content_type = "font/woff" if file_path.endswith(".woff") else "font/woff2"

    return FileResponse(
        asset_file,
        media_type=content_type,
        headers={
            "Cache-Control": "public, max-age=31536000"  # Cache for 1 year
        }
    )


@router.get("/")
async def root():
    """Root endpoint - serve frontend if available"""
    if FRONTEND_INDEX.exists():
        try:
            with open(FRONTEND_INDEX, 'r', encoding='utf-8') as f:
                html_content = f.read()

            # Return HTML with proper headers
            return HTMLResponse(
                content=html_content,
                status_code=200,
                headers={
                    "Cache-Control": "no-cache, no-store, must-revalidate",
                    "Pragma": "no-cache",
                    "Expires": "0"
                }
            )
        except Exception as e:
            return JSONResponse(
                status_code=500,
                content={
                    "error": "Failed to read frontend index.html",
                    "details": str(e),
                    "frontend_path": str(FRONTEND_INDEX)
                }
            )

    return JSONResponse(
        content={
            "message": "Invoice Processor API",
            "version": "1.0.0",
            "note": "Frontend not found. Build the frontend with 'cd frontend && npm run build'",
            "frontend_path": str(FRONTEND_DIR),
            "frontend_exists": FRONTEND_DIR.exists(),
            "index_exists": FRONTEND_INDEX.exists() if FRONTEND_DIR.exists() else False,
            "endpoints": {
                "process": "POST /api/process",
                "match-master": "POST /api/match-master",
                "task": "GET /api/tasks/{task_id}",
                "excel": "GET /api/excel",
                "upload_excel": "POST /api/excel",
                "delete_excel": "DELETE /api/excel/uploads/{stored_name}",
                "api_push_config": "GET /api/api-push/config",
                "health": "GET /api/health"
            },
            "features": {
                "auto_excel_generation": "Excel files are auto-generated from API during processing",
                "api_push": "All 'Received' rows are automatically pushed to API endpoint",
                "enhanced_gst_extraction": "Advanced GST number extraction with regex + GPT validation",
                "full_field_updates": "Matched rows are updated with all invoice data fields"
            }
        }
    )


@router.get("/{full_path:path}")
async def serve_frontend(full_path: str):
    """Catch-all route to serve frontend for client-side routing"""
    # Don't interfere with API routes
    if full_path.startswith("api/"):
        raise HTTPException(status_code=404, detail="API endpoint not found")

    # Don't interfere with assets
    if full_path.startswith("assets/"):
        raise HTTPException(status_code=404, detail="Asset not found")

    # Serve index.html for all other routes
    if FRONTEND_INDEX.exists():
        try:
            with open(FRONTEND_INDEX, 'r', encoding='utf-8') as f:
                html_content = f.read()

            return HTMLResponse(
                content=html_content,
                status_code=200,
                headers={
                    "Cache-Control": "no-cache, no-store, must-revalidate",
                    "Pragma": "no-cache",
                    "Expires": "0"
                }
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error serving frontend: {str(e)}")

    raise HTTPException(status_code=404, detail="Frontend not found. Please build the frontend first.")
