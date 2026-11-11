"""In-memory process/match task store used by the API."""

from __future__ import annotations

import threading
from typing import Dict

tasks: Dict[str, Dict] = {}
task_lock = threading.Lock()
process_start_lock = threading.Lock()
