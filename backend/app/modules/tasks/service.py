"""Task registry helpers. Start/cancel HTTP stays on the router (same 409 rules)."""

from __future__ import annotations

import asyncio
import time
from typing import Optional

from backend.app.modules.tasks.registry import task_lock, tasks


def update_task_logs(task_id: str, new_logs: str):
    """Thread-safe update of task logs"""
    with task_lock:
        if task_id in tasks:
            # Append new logs to existing logs
            existing_logs = tasks[task_id].get("logs", "")
            tasks[task_id]["logs"] = existing_logs + new_logs


def find_running_process_task_id() -> Optional[str]:
    """Return task_id of an active process_* task, if any."""
    with task_lock:
        for tid, rec in tasks.items():
            if tid.startswith("process_") and rec.get("status") == "running":
                return tid
    return None


async def wait_until_task_not_running(task_id: str, max_seconds: float = 600.0) -> bool:
    """Wait until task status is no longer 'running' (or timeout)."""
    deadline = time.time() + max_seconds
    while time.time() < deadline:
        with task_lock:
            st = tasks.get(task_id, {}).get("status")
        if st != "running":
            return True
        await asyncio.sleep(0.25)
    return False
