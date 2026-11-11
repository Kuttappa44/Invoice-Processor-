#!/usr/bin/env python3
"""Compatibility re-export. The FastAPI app lives in backend.app.main."""

from __future__ import annotations

import os

import uvicorn

from backend.app.main import app
from backend.app.modules.invoices.service import DEFAULT_EXCEL_PATH, UPLOADS_DIR
from backend.app.modules.tasks.workers.tasks import run_task_in_thread  # noqa: F401
from backend.app.spa import FRONTEND_DIR, FRONTEND_INDEX, assets_dir

__all__ = ["app", "run_task_in_thread"]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Invoice Processor API Server")
    parser.add_argument(
        "--host",
        type=str,
        default=os.getenv("API_HOST", "0.0.0.0"),
        help="Host to bind to"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("API_PORT", "9005")),
        help="Port to bind to"
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        default=os.getenv("API_RELOAD", "false").lower() == "true",
        help="Enable auto-reload for development"
    )

    args = parser.parse_args()

    print("=" * 70)
    print("🚀 Invoice Processor API Server")
    print("=" * 70)
    print(f"📍 Host: {args.host}")
    print(f"🔌 Port: {args.port}")
    print(f"🔄 Reload: {args.reload}")
    print(f"📁 Excel uploads directory: {UPLOADS_DIR.absolute()}")
    print(f"📊 Default Excel path: {DEFAULT_EXCEL_PATH}")
    print(f"🌐 Frontend directory: {FRONTEND_DIR.absolute()}")
    if FRONTEND_DIR.exists() and FRONTEND_INDEX.exists():
        print(f"✅ Frontend found - serving React app")
        if assets_dir and assets_dir.exists():
            asset_count = len(list(assets_dir.glob("*")))
            print(f"   📦 Assets directory found ({asset_count} files)")
        else:
            print(f"   ⚠️  Assets directory not found")
    else:
        print(f"⚠️  Frontend not found at {FRONTEND_DIR}")
        print(f"   Build with: cd frontend && npm install && npm run build")
    print("=" * 70)

    uvicorn.run(
        "api_server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info"
    )
