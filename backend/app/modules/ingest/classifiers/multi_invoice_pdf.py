"""
Stage 1: detect distinct invoices and page ranges in multi-page PDFs (vision JSON only).
Used only when page count > 1; Stage 2 extraction stays in legacy_impl with prompt.txt.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, List, Optional, Tuple

from backend.app.modules.ingest.prompt_paths import (
    REPO_PROMPTS_DIR,
    prompt_file_candidates,
)

# Allowed canonical classification labels (anything else => normalized to None / "other").
_CLASSIFICATION_LABELS = ("hummingbird", "mmt", "b2b", "food", "not_invoice")


def _package_detection_prompt_path() -> Path:
    return REPO_PROMPTS_DIR / "invoice_detection_prompt.txt"


def _load_prompt_from_candidates(candidates: List[Path], label: str) -> Optional[str]:
    for p in candidates:
        try:
            if p.is_file():
                text = p.read_text(encoding="utf-8").strip()
                if text:
                    print(f"   ✅ Loaded {label} from {p} ({len(text)} chars)")
                    return text
        except OSError:
            continue
    return None


def load_classify_detect_prompt() -> str:
    """Prompt A: Stage-1 enabled (multi-page) detection + per-invoice classification."""
    loaded = _load_prompt_from_candidates(
        prompt_file_candidates("classify_detect_prompt.txt", "INVOICE_CLASSIFY_DETECT_PROMPT_PATH"),
        "classify+detect prompt (A)",
    )
    if loaded:
        return loaded
    print("   ⚠️  classify_detect_prompt.txt not found; falling back to detection-only prompt")
    return load_invoice_detection_prompt()


def load_classify_single_prompt() -> str:
    """Prompt B: Stage-1 skipped (single page) classification only."""
    loaded = _load_prompt_from_candidates(
        prompt_file_candidates("classify_single_prompt.txt", "INVOICE_CLASSIFY_SINGLE_PROMPT_PATH"),
        "classify single prompt (B)",
    )
    if loaded:
        return loaded
    print("   ⚠️  classify_single_prompt.txt not found; using embedded fallback")
    return (
        'Classify the single-invoice PDF as exactly one of "mmt" | "b2b" | "food" | "other". '
        'Output ONLY JSON: {"classification":"other","confidence":"low","brief_reason":""}'
    )


def normalize_classification(raw: Any) -> Optional[str]:
    """Map a raw model classification label to a canonical one, or None for other/unknown."""
    t = str(raw or "").strip().lower()
    if t in ("hummingbird", "hummingbird_mail", "hummingbird mail", "humming bird", "hbd"):
        return "hummingbird"
    if t in ("mmt", "makemytrip", "make my trip", "make-my-trip"):
        return "mmt"
    if t in ("b2b", "b2b_invoice", "b2b invoice", "corporate", "corporate_invoice"):
        return "b2b"
    if t in ("food", "food_invoice", "fnb", "f&b", "restaurant"):
        return "food"
    if t in ("not_invoice", "not_an_invoice", "not an invoice", "non_invoice", "no_invoice", "not invoice"):
        return "not_invoice"
    return None


def load_invoice_detection_prompt() -> str:
    """Load Stage 1 prompt: env override, Linux prompts dir, then repo prompts/."""
    candidates = prompt_file_candidates(
        "invoice_detection_prompt.txt", "INVOICE_DETECTION_PROMPT_PATH"
    )

    for p in candidates:
        try:
            if p.is_file():
                text = p.read_text(encoding="utf-8").strip()
                if text:
                    print(f"   ✅ Loaded invoice detection prompt from {p} ({len(text)} chars)")
                    return text
        except OSError:
            continue

    print("   ⚠️  invoice_detection_prompt.txt not found; using embedded fallback")
    return _embedded_detection_prompt_fallback()


def _embedded_detection_prompt_fallback() -> str:
    return (
        'Output only JSON: {"total_pages":N,"invoice_count":1,'
        '"invoices":[{"index":1,"start_page":1,"end_page":N,"primary_signals":{'
        '"bill_or_invoice_number_observed":"unknown","guest_name_observed":"unknown",'
        '"bill_date_observed":"unknown","check_in_observed":"unknown","check_out_observed":"unknown"}}]}'
    )


def _strip_json_fence(raw: str) -> str:
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\s*```\s*$", "", s)
    return s.strip()


def _segment_from_invoice_item(item: dict, page_count: int) -> Optional[Tuple[int, int]]:
    """Resolve (start_page, end_page) from start_page/end_page ints or a pages string like '1', '2', '1-3'."""
    sp, ep = item.get("start_page"), item.get("end_page")
    if sp is not None and ep is not None:
        try:
            a, b = int(sp), int(ep)
            return a, b
        except (TypeError, ValueError):
            pass

    pages = item.get("pages")
    if pages is None:
        return None
    if isinstance(pages, bool):
        return None
    if isinstance(pages, int):
        try:
            p = int(pages)
            return p, p
        except (TypeError, ValueError):
            return None
    if isinstance(pages, str):
        s = pages.strip().replace("–", "-").replace("—", "-").replace("−", "-")
        s = re.sub(r"\s*-\s*", "-", s)
        if "-" in s:
            parts = s.split("-", 1)
            try:
                a, b = int(parts[0].strip()), int(parts[1].strip())
                return a, b
            except ValueError:
                return None
        try:
            p = int(s)
            return p, p
        except ValueError:
            return None
    return None


# Field names only (no values) for differences_from_previous — aligns with prompt.txt concepts.
_ALLOWED_DIFF_FIELDS = frozenset(
    {"invoice number", "invoice date", "guest name", "check-in date", "check-out date"}
)


def _canonical_diff_field(s: str) -> Optional[str]:
    t = (s or "").strip().lower()
    aliases = {
        "bill number": "invoice number",
        "invoice/bill number": "invoice number",
        "bill date": "invoice date",
        "invoice/bill date": "invoice date",
    }
    t = aliases.get(t, t)
    return t if t in _ALLOWED_DIFF_FIELDS else None


def _sanitize_differences_from_previous(raw: Any) -> List[str]:
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    seen = set()
    for x in raw:
        if not isinstance(x, str):
            continue
        c = _canonical_diff_field(x)
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def parse_stage1_response(
    raw: str, page_count: int
) -> Tuple[List[Tuple[int, int]], List[dict]]:
    """
    Parse Stage 1 JSON into normalized segments and the invoice dicts that produced them
    (same order, equal length) for logging differences_from_previous.

    If normalization changes segment count or parse fails, returns ([(1,page_count)], []).
    """
    try:
        data = json.loads(_strip_json_fence(raw))
    except json.JSONDecodeError:
        print("   ⚠️  Stage 1 detection: invalid JSON, defaulting to single invoice")
        return [(1, page_count)], []

    if not isinstance(data, dict):
        return [(1, page_count)], []

    invs = data.get("invoices")
    if not isinstance(invs, list) or not invs:
        return [(1, page_count)], []

    paired: List[Tuple[Tuple[int, int], dict]] = []
    for item in invs:
        if not isinstance(item, dict):
            continue
        seg = _segment_from_invoice_item(item, page_count)
        if seg is not None:
            paired.append((seg, item))

    if not paired:
        print(
            "   ⚠️  Stage 1 detection: no valid page ranges in JSON "
            "(need start_page/end_page or pages per invoice); defaulting to single invoice"
        )
        return [(1, page_count)], []

    raw_segments = [p[0] for p in paired]
    items_ordered = [p[1] for p in paired]

    segments = normalize_segments(raw_segments, page_count)
    if not segments:
        return [(1, page_count)], []

    if len(segments) != len(items_ordered):
        print(
            "   ⚠️  Stage 1: segment count after normalize (%s) != invoice objects (%s); "
            "skipping differences_from_previous log"
            % (len(segments), len(items_ordered))
        )
        return segments, []

    return segments, items_ordered


def parse_detection_json(raw: str, page_count: int) -> Tuple[int, List[Tuple[int, int]]]:
    """Parse model output into (invoice_count, [(start,end), ...]). On any error, fallback to one segment."""
    segments, _ = parse_stage1_response(raw, page_count)
    if not segments:
        return 1, [(1, page_count)]
    return len(segments), segments


def log_stage1_differences_from_previous(
    segments: List[Tuple[int, int]], items_ordered: List[dict]
) -> None:
    """Log only field names that differ vs previous invoice (no values)."""
    if len(items_ordered) <= 1 or len(segments) != len(items_ordered):
        return

    print("   📎 Stage 1 — differences_from_previous:")
    for i in range(1, len(items_ordered)):
        s, e = segments[i]
        raw_diffs = items_ordered[i].get("differences_from_previous")
        cleaned = _sanitize_differences_from_previous(raw_diffs)
        label = f"Invoice {i + 1} (pages {s}–{e}):"
        if not cleaned:
            print(f"      {label} (not provided by model)")
        else:
            print(f"      {label} {cleaned!r}")


def normalize_segments(segments: List[Tuple[int, int]], page_count: int) -> List[Tuple[int, int]]:
    """Clamp to [1, page_count], drop empty, merge overlaps into conservative single segment if messy."""
    if page_count < 1:
        return []

    cleaned: List[Tuple[int, int]] = []
    for a, b in segments:
        try:
            s = max(1, min(int(a), page_count))
            e = max(1, min(int(b), page_count))
        except (TypeError, ValueError):
            continue
        if s > e:
            s, e = e, s
        cleaned.append((s, e))

    if not cleaned:
        return [(1, page_count)]

    cleaned.sort(key=lambda x: (x[0], x[1]))
    merged: List[Tuple[int, int]] = []
    cur_s, cur_e = cleaned[0]
    for s, e in cleaned[1:]:
        # Merge only on overlap; adjacent (1,1)+(2,2) are two invoices, not one range 1-2
        if s <= cur_e:
            cur_e = max(cur_e, e)
        else:
            merged.append((cur_s, cur_e))
            cur_s, cur_e = s, e
    merged.append((cur_s, cur_e))

    if len(merged) == 1 and merged[0] == (1, page_count):
        return merged

    covered = set()
    for s, e in merged:
        for p in range(s, e + 1):
            covered.add(p)
    if len(covered) != page_count or min(covered) != 1 or max(covered) != page_count:
        print(
            "   ⚠️  Stage 1 segments do not cover all pages cleanly; defaulting to single invoice 1–%s"
            % page_count
        )
        return [(1, page_count)]

    return merged


def build_scoped_extraction_instructions(
    base_extraction_prompt: str, total_pages: int, start_page: int, end_page: int
) -> str:
    """Prepend page scope so the model ignores other pages when multiple invoices exist in one PDF."""
    scope = f"""SCOPE FOR THIS EXTRACTION (read first):
- The PDF was sent as {total_pages} page image(s) in order (page 1 = first image).
- For THIS task you must extract exactly ONE invoice using ONLY pages {start_page} through {end_page} (inclusive).
- Ignore all other pages for field values. Do not mix data from invoices outside this page range.
- If a field appears only outside this range, return "N/A" for that field.

---

"""
    return scope + base_extraction_prompt


def _classifications_for_segments(
    segments: List[Tuple[int, int]], detail_items: List[dict]
) -> List[Optional[str]]:
    """Align per-invoice classification labels to segments (None when unavailable)."""
    if detail_items and len(detail_items) == len(segments):
        return [normalize_classification(it.get("classification")) for it in detail_items]
    return [None] * len(segments)


def _apply_hummingbird_logo_to_segments(
    segments: List[Tuple[int, int]],
    classifications: List[Optional[str]],
    image_inputs: list,
    file_name: str,
) -> List[Optional[str]]:
    """If reference logo appears on a segment's pages, set classification to hummingbird."""
    from backend.app.modules.ingest.classifiers.hummingbird_logo import (
        invoice_contains_hummingbird_logo,
        slice_image_inputs_for_pages,
    )

    if not segments:
        return classifications
    out: List[Optional[str]] = []
    for idx, (start_p, end_p) in enumerate(segments):
        base = classifications[idx] if idx < len(classifications) else None
        pages = slice_image_inputs_for_pages(image_inputs, start_p, end_p)
        ctx = f"{file_name} segment {idx + 1} pages {start_p}-{end_p}"
        if invoice_contains_hummingbird_logo(pages, context=ctx):
            out.append("hummingbird")
        else:
            out.append(base)
    return out


def detect_invoice_page_segments(
    image_inputs: list,
    page_count: int,
    file_name: str,
    *,
    max_retries: int = 15,
) -> Tuple[List[Tuple[int, int]], List[Optional[str]]]:
    """
    Stage 1 vision call (Prompt A): returns (segments, classifications).

    - segments: list of (start_page, end_page) inclusive, 1-based.
    - classifications: same length as segments; each is "hummingbird" | "mmt" | "b2b" | "food"
      | "not_invoice" or None (other/unknown).
    """
    if page_count <= 1:
        return [(1, max(1, page_count))], [None]

    from backend.app.integrations.azure_openai import client

    if not client:
        print("   ⚠️  Stage 1 skipped: OpenAI client not initialized; single invoice assumed")
        return [(1, page_count)], [None]

    import time

    azure_api_key = os.getenv("AZURE_OPENAI_API_KEY")
    azure_base_url = os.getenv("AZURE_OPENAI_BASE_URL")
    is_azure = bool(azure_api_key and azure_base_url)
    model_name = os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-5-mini")
    instructions = load_classify_detect_prompt()

    def verify_image_url_accessible(url: str, timeout: int = 5) -> bool:
        try:
            import requests

            response = requests.head(url, timeout=timeout, allow_redirects=True)
            return response.status_code == 200
        except Exception:
            return False

    print(f"   🔎 Stage 1: invoice structure detection for {file_name} ({page_count} pages)...")
    time.sleep(1)

    if image_inputs:
        for idx, img_input in enumerate(image_inputs[: min(3, len(image_inputs))], 1):
            if isinstance(img_input, dict) and img_input.get("type") == "image_url":
                img_url = img_input.get("image_url", {}).get("url", "")
                if img_url and not verify_image_url_accessible(img_url):
                    time.sleep(1)
                    verify_image_url_accessible(img_url)

    last_raw = ""
    for attempt in range(1, max_retries + 1):
        try:
            if attempt > 1:
                wait = 2 * attempt
                print(f"   🔄 Stage 1 retry {attempt}/{max_retries} (wait {wait}s)...")
                time.sleep(wait)

            if is_azure:
                messages_content: List[Any] = [{"type": "text", "text": instructions}]
                messages_content.extend(image_inputs)
                response = client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "user", "content": messages_content}],
                    max_completion_tokens=4096,
                )
                output_text = response.choices[0].message.content or ""
            else:
                response = client.responses.create(
                    model=model_name,
                    input=[
                        {
                            "role": "user",
                            "content": [{"type": "input_text", "text": instructions}, *image_inputs],
                        }
                    ],
                )
                output_text = (
                    response.output_text if hasattr(response, "output_text") else str(response)
                ) or ""

            last_raw = output_text
            if output_text.strip():
                segments, detail_items = parse_stage1_response(output_text, page_count)
                classifications = _classifications_for_segments(segments, detail_items)
                classifications = _apply_hummingbird_logo_to_segments(
                    segments, classifications, image_inputs, file_name
                )
                print(f"   ✅ Stage 1 result: {len(segments)} invoice segment(s): {segments}")
                print(f"   🏷️  Stage 1 classifications: {classifications}")
                log_stage1_differences_from_previous(segments, detail_items)
                return segments, classifications
        except Exception as e:
            print(f"   ⚠️  Stage 1 attempt {attempt} error: {e}")

    print(f"   ⚠️  Stage 1 failed after retries; defaulting to single invoice. Last output: {last_raw[:200]!r}")
    return [(1, page_count)], [None]


def classify_single_invoice(
    image_inputs: list,
    file_name: str,
    *,
    max_retries: int = 5,
) -> Optional[str]:
    """
    Prompt B: classify a SINGLE-invoice PDF (Stage-1 skipped) as one of
    "hummingbird" | "mmt" | "b2b" | "food" | "not_invoice", or None for other/unknown.

    Hummingbird: dedicated logo detect (reference logo.png vs invoice pages).
    mmt/b2b/food/not_invoice/other: Prompt B classify call.
    """
    from backend.app.modules.ingest.classifiers.hummingbird_logo import invoice_contains_hummingbird_logo
    from backend.app.integrations.azure_openai import client

    if not client:
        print("   ⚠️  Classification skipped: OpenAI client not initialized")
        return None
    if not image_inputs:
        return None

    if invoice_contains_hummingbird_logo(image_inputs, context=file_name):
        return "hummingbird"

    import time

    azure_api_key = os.getenv("AZURE_OPENAI_API_KEY")
    azure_base_url = os.getenv("AZURE_OPENAI_BASE_URL")
    is_azure = bool(azure_api_key and azure_base_url)
    model_name = os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-5-mini")
    instructions = load_classify_single_prompt()

    print(f"   🔎 Classifying single-page invoice {file_name} (Prompt B)...")
    last_raw = ""
    for attempt in range(1, max_retries + 1):
        try:
            if attempt > 1:
                time.sleep(min(2.0 * attempt, 10.0))

            if is_azure:
                messages_content: List[Any] = [{"type": "text", "text": instructions}]
                messages_content.extend(image_inputs)
                response = client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "user", "content": messages_content}],
                    max_completion_tokens=512,
                )
                output_text = response.choices[0].message.content or ""
            else:
                response = client.responses.create(
                    model=model_name,
                    input=[
                        {
                            "role": "user",
                            "content": [{"type": "input_text", "text": instructions}, *image_inputs],
                        }
                    ],
                )
                output_text = (
                    response.output_text if hasattr(response, "output_text") else str(response)
                ) or ""

            last_raw = output_text
            if output_text.strip():
                try:
                    data = json.loads(_strip_json_fence(output_text))
                except json.JSONDecodeError:
                    continue
                if isinstance(data, dict):
                    return normalize_classification(data.get("classification"))
        except Exception as e:
            print(f"   ⚠️  Classification attempt {attempt} error: {e}")

    print(f"   ⚠️  Single-page classification failed after retries. Last output: {last_raw[:200]!r}")
    return None
