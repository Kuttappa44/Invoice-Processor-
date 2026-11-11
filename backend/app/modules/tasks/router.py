"""HTTP for process/match start, poll, and cancel."""

from __future__ import annotations

import threading
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException

from backend.app.modules.tasks.registry import process_start_lock, task_lock, tasks
from backend.app.modules.tasks.schemas import ProcessRequest, TaskResponse
from backend.app.modules.tasks.service import (
    find_running_process_task_id,
    wait_until_task_not_running,
)
from backend.app.modules.tasks.workers.tasks import run_task_in_thread

router = APIRouter()


@router.post("/api/process")
async def start_process_task(request: ProcessRequest = ProcessRequest()):
    """
    Start the invoice processing task
    
    Workflow:
    1. Auto-generates Excel file from API (via test.py)
    2. Fetches unread emails from Gmail
    3. Processes PDFs with OpenAI native PDF support (GPT extraction)
    4. Uploads PDFs to Google Drive folders
    5. Matches invoices with Excel rows
    6. Updates matched rows with all invoice data
    7. Pushes all 'Received' rows to API endpoint
    8. Forwards unmatched invoices to vendors
    
    Args:
        request: ProcessRequest with required from_date and to_date (YYYY-MM-DD format);
            set force_replace to cancel the current run (cooperatively) and start a new one.
    
    Returns:
        Task ID for tracking progress
    """
    # A date range is mandatory; reject before touching any running task.
    if not request.from_date or not request.to_date:
        raise HTTPException(
            status_code=400,
            detail="from_date and to_date are required (YYYY-MM-DD).",
        )
    try:
        _fd = datetime.strptime(request.from_date, "%Y-%m-%d")
        _td = datetime.strptime(request.to_date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="from_date and to_date must be in YYYY-MM-DD format.",
        )
    if _fd > _td:
        raise HTTPException(
            status_code=400, detail="from_date must not be after to_date."
        )

    running_id: Optional[str] = None
    with process_start_lock:
        running_id = find_running_process_task_id()
        if running_id and not request.force_replace:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "A full flow is already running. Keep it, or retry with force_replace.",
                    "running_task_id": running_id,
                },
            )
        if running_id and request.force_replace:
            ev = tasks.get(running_id, {}).get("cancel_event")
            if ev:
                ev.set()

    if running_id and request.force_replace:
        stopped = await wait_until_task_not_running(running_id)
        if not stopped:
            raise HTTPException(
                status_code=503,
                detail="Previous run did not stop within the time limit; try again.",
            )

    with process_start_lock:
        still = find_running_process_task_id()
        if still and not request.force_replace:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "A full flow is already running.",
                    "running_task_id": still,
                },
            )
        if still and request.force_replace and still != running_id:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Another process run is active; try again.",
                    "running_task_id": still,
                },
            )

        task_id = f"process_{uuid.uuid4().hex[:8]}"
        cancel_ev = threading.Event()
        tasks[task_id] = {
            "task_id": task_id,
            "status": "running",
            "started_at": datetime.now().isoformat(),
            "finished_at": None,
            "logs": "",
            "error": None,
            "excel_link": None,
            "cancel_event": cancel_ev,
        }

        thread = threading.Thread(
            target=run_task_in_thread,
            args=(task_id, "process", request.from_date, request.to_date),
            daemon=True,
        )
        thread.start()

    return {"task_id": task_id}


@router.post("/api/match-master")
async def start_match_master_task():
    """
    Start the master sheet matching task
    
    Matches invoice data from Google Drive master sheet with local Excel file.
    Updates 'Invoice Received' status and all invoice data fields for matched rows.
    Automatically pushes all 'Received' rows to API endpoint after matching.
    """
    task_id = f"match-master_{uuid.uuid4().hex[:8]}"
    
    # Initialize task
    with task_lock:
        tasks[task_id] = {
            "task_id": task_id,
            "status": "running",
            "started_at": datetime.now().isoformat(),
            "finished_at": None,
            "logs": "",
            "error": None
        }
    
    # Run task in background thread
    thread = threading.Thread(target=run_task_in_thread, args=(task_id, "match-master"), daemon=True)
    thread.start()
    
    return {"task_id": task_id}


@router.get("/api/tasks/running-process")
async def get_running_process_task():
    """True if a process_* task is currently running (server-side)."""
    with task_lock:
        for tid, rec in tasks.items():
            if tid.startswith("process_") and rec.get("status") == "running":
                return {
                    "running": True,
                    "task": {
                        "task_id": tid,
                        "started_at": rec.get("started_at"),
                        "status": rec.get("status"),
                    },
                }
    return {"running": False, "task": None}


@router.post("/api/tasks/{task_id}/cancel")
async def cancel_process_task(task_id: str):
    """Request cooperative cancellation of a running process task."""
    with task_lock:
        if task_id not in tasks:
            raise HTTPException(status_code=404, detail="Task not found")
        if not task_id.startswith("process_"):
            raise HTTPException(status_code=400, detail="Only process tasks can be cancelled")
        if tasks[task_id].get("status") != "running":
            raise HTTPException(status_code=400, detail="Task is not running")
        ev = tasks[task_id].get("cancel_event")
        if ev:
            ev.set()
    return {"ok": True}


@router.get("/api/tasks/{task_id}")
async def get_task_status(task_id: str):
    """Get task status and logs"""
    with task_lock:
        if task_id not in tasks:
            raise HTTPException(status_code=404, detail="Task not found")

        task = tasks[task_id].copy()
        task.pop("cancel_event", None)

    return TaskResponse(**task)
