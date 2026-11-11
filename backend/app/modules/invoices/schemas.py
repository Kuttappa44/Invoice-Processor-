"""HTTP models for Excel matching files."""

from __future__ import annotations

from typing import Dict, List

from pydantic import BaseModel


class ExcelInfo(BaseModel):
    path: str
    exists: bool
    uploads: List[Dict]
