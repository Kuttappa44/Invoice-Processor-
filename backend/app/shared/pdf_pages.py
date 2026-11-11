"""PDF page helpers; MySQL stores multi_page_pdf as 'yes' / 'no' via convert_row_for_mysql."""

from __future__ import annotations

from typing import Optional


def count_pdf_pages(pdf_bytes: Optional[bytes]) -> Optional[int]:
    """Return number of pages, or None if bytes missing or PDF invalid."""
    if not pdf_bytes or len(pdf_bytes) < 4:
        return None
    try:
        import fitz  # PyMuPDF (same stack as legacy_impl PDF processing)
    except ImportError:
        return None
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        try:
            n = len(doc)
            return int(n) if n >= 1 else None
        finally:
            doc.close()
    except Exception:
        return None


def is_multi_page_pdf(pdf_bytes: Optional[bytes]) -> bool:
    """True iff PDF has more than one page (False for single-page or unknown)."""
    n = count_pdf_pages(pdf_bytes)
    return n is not None and n > 1
