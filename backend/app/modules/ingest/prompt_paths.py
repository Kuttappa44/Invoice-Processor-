"""Locate prompt .txt files in prompts/ (repo or Linux deploy)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

LINUX_PROMPTS_DIR = Path("/home/ubuntu/Invoice-processor/invoice/prompts")
REPO_PROMPTS_DIR = Path(__file__).resolve().parents[4] / "prompts"


def prompt_file_candidates(filename: str, env_var: str = "") -> List[Path]:
    paths: List[Path] = []
    if env_var:
        env_path = (os.getenv(env_var) or "").strip()
        if env_path:
            paths.append(Path(env_path))
    paths.append(LINUX_PROMPTS_DIR / filename)
    paths.append(REPO_PROMPTS_DIR / filename)
    return paths


def first_existing_prompt(filename: str, env_var: str = "") -> Optional[Path]:
    for path in prompt_file_candidates(filename, env_var):
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None
