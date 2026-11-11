"""HTTP models for process/match tasks."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class TaskResponse(BaseModel):
    task_id: str
    status: str  # "running" | "success" | "error" | "cancelled"
    started_at: str
    finished_at: Optional[str] = None
    logs: str
    error: Optional[str] = None
    excel_link: Optional[str] = None  # Excel file shareable link (Google Sheets URL)


class ProcessRequest(BaseModel):
    from_date: Optional[str] = None
    to_date: Optional[str] = None
    force_replace: bool = False
