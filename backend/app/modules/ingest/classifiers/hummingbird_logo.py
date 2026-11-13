"""
Detect Hummingbird platform logo on invoice PDF page images by comparing to a reference logo file.

One vision call: reference logo.png + invoice page image(s) → logo_found true/false.
"""

from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from typing import Any, List, Optional

from backend.app.modules.ingest.prompt_paths import (
    REPO_PROMPTS_DIR,
    prompt_file_candidates,
)

_LINUX_HUMMINGBIRD_LOGO = "/home/ubuntu/Invoice-processor/invoice/logo.png"

_REFERENCE_IMAGE_INPUT: Optional[dict] = None
_REFERENCE_LOAD_LOGGED = False
_PROMPT_CACHE: Optional[str] = None


def resolve_hummingbird_logo_path() -> Optional[Path]:
    env_path = os.getenv("HUMMINGBIRD_REFERENCE_LOGO_PATH", "").strip()
    for p in (
        [Path(env_path)] if env_path else []
    ) + [Path(_LINUX_HUMMINGBIRD_LOGO), REPO_PROMPTS_DIR.parent / "logo.png"]:
        try:
            if p.is_file():
                return p.resolve()
        except OSError:
            continue
    return None


def load_hummingbird_logo_detect_prompt() -> str:
    global _PROMPT_CACHE
    if _PROMPT_CACHE is not None:
        return _PROMPT_CACHE
    candidates = prompt_file_candidates(
        "hummingbird_logo_detect_prompt.txt", "HUMMINGBIRD_LOGO_DETECT_PROMPT_PATH"
    )
    for p in candidates:
        try:
            if p.is_file():
                _PROMPT_CACHE = p.read_text(encoding="utf-8").strip()
                return _PROMPT_CACHE
        except OSError:
            continue
    _PROMPT_CACHE = (
        "Image 1 is the reference Hummingbird logo. Image 2+ are invoice pages. "
        'Return JSON only: {"logo_found": true|false, "confidence": "high|medium|low", '
        '"brief_reason": "..."}'
    )
    return _PROMPT_CACHE


def get_hummingbird_reference_image_input() -> Optional[dict]:
    global _REFERENCE_IMAGE_INPUT, _REFERENCE_LOAD_LOGGED
    if _REFERENCE_IMAGE_INPUT is not None:
        return _REFERENCE_IMAGE_INPUT
    logo_path = resolve_hummingbird_logo_path()
    if logo_path is None:
        if not _REFERENCE_LOAD_LOGGED:
            print(
                f"   ⚠️  Hummingbird reference logo not found (expected {_LINUX_HUMMINGBIRD_LOGO})"
            )
            _REFERENCE_LOAD_LOGGED = True
        return None
    try:
        raw = logo_path.read_bytes()
        mime = "image/png" if logo_path.suffix.lower() == ".png" else "image/jpeg"
        b64 = base64.standard_b64encode(raw).decode("ascii")
        _REFERENCE_IMAGE_INPUT = {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"},
        }
        if not _REFERENCE_LOAD_LOGGED:
            print(f"   ✅ Loaded Hummingbird reference logo from {logo_path}")
            _REFERENCE_LOAD_LOGGED = True
        return _REFERENCE_IMAGE_INPUT
    except OSError as e:
        if not _REFERENCE_LOAD_LOGGED:
            print(f"   ⚠️  Could not read Hummingbird reference logo: {e}")
            _REFERENCE_LOAD_LOGGED = True
        return None


def slice_image_inputs_for_pages(
    image_inputs: list,
    start_page: int,
    end_page: int,
) -> list:
    if not image_inputs or start_page < 1 or end_page < start_page:
        return list(image_inputs)
    return list(image_inputs[start_page - 1 : end_page])


def _strip_json_fence(raw: str) -> str:
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\s*```\s*$", "", s)
    return s.strip()


def invoice_contains_hummingbird_logo(
    invoice_image_inputs: list,
    *,
    context: str = "",
    max_retries: int = 3,
) -> bool:
    """
    One vision call: reference logo + invoice page(s).
    Returns True when the model reports logo_found on the invoice.
    """
    if not invoice_image_inputs:
        return False
    ref = get_hummingbird_reference_image_input()
    if ref is None:
        return False

    from backend.app.integrations.azure_openai import client

    if not client:
        return False

    import time

    instructions = load_hummingbird_logo_detect_prompt()
    azure = bool(os.getenv("AZURE_OPENAI_API_KEY") and os.getenv("AZURE_OPENAI_BASE_URL"))
    model = os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-5-mini")
    label = f" ({context})" if context else ""
    print(f"   🐦 Hummingbird logo detect{label}...")

    last_raw = ""
    for attempt in range(1, max_retries + 1):
        try:
            if attempt > 1:
                time.sleep(min(2.0 * attempt, 8.0))
            if azure:
                content: List[Any] = [
                    {"type": "text", "text": instructions},
                    {"type": "text", "text": "REFERENCE LOGO (Image 1):"},
                    ref,
                    {"type": "text", "text": "INVOICE PAGE(S) (Image 2+):"},
                    *invoice_image_inputs,
                ]
                resp = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": content}],
                    max_completion_tokens=1024,
                )
                last_raw = resp.choices[0].message.content or ""
            else:
                resp = client.responses.create(
                    model=model,
                    input=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": instructions},
                                {"type": "input_text", "text": "REFERENCE LOGO (Image 1):"},
                                ref,
                                {"type": "input_text", "text": "INVOICE PAGE(S) (Image 2+):"},
                                *invoice_image_inputs,
                            ],
                        }
                    ],
                )
                last_raw = (
                    resp.output_text if hasattr(resp, "output_text") else str(resp)
                ) or ""

            if not last_raw.strip():
                continue
            data = json.loads(_strip_json_fence(last_raw))
            if not isinstance(data, dict):
                continue
            found = bool(data.get("logo_found"))
            conf = str(data.get("confidence") or "").strip().lower()
            reason = str(data.get("brief_reason") or "")[:120]
            print(
                f"   🐦 Logo detect{label} -> logo_found={found} confidence={conf} reason={reason!r}"
            )
            return found
        except Exception as e:
            print(f"   ⚠️  Logo detect attempt {attempt} error: {e}")

    print(f"   ⚠️  Logo detect failed{label}. Last: {last_raw[:200]!r}")
    return False
