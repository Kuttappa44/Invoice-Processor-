"""Guest-name comparison used by email/PDF merge and Step-7 matching."""

from __future__ import annotations

from rapidfuzz import fuzz

from backend.app.shared.text import normalize_field_text


def guest_names_match_for_merge(guest_name_1: str, guest_name_2: str) -> bool:
    """
    Check if two guest names match using the same logic as email/PDF matching.
    This is a standalone version of the guest_names_match_single function for use in merge logic.

    Args:
        guest_name_1: First guest name to compare
        guest_name_2: Second guest name to compare

    Returns:
        bool: True if names match, False otherwise
    """
    if not guest_name_1 or not guest_name_2:
        return False

    # User request: keep ONLY title-removal normalization; RapidFuzz score threshold > 60.
    titles = {"mr", "mrs", "ms", "miss", "dr", "prof", "sir", "madam"}

    def remove_titles_only(value: str) -> str:
        if not value:
            return ""
        text_value = normalize_field_text(value).strip()
        if not text_value:
            return ""
        tokens = text_value.split()
        while tokens:
            first = tokens[0].strip()
            first_key = first.rstrip(".").lower()
            if first_key in titles:
                tokens.pop(0)
                continue
            break
        return " ".join(tokens).strip()

    a = remove_titles_only(guest_name_1).lower()
    b = remove_titles_only(guest_name_2).lower()
    if not a or not b:
        return False

    score = max(
        fuzz.ratio(a, b),
        fuzz.partial_ratio(a, b),
        fuzz.token_sort_ratio(a, b),
        fuzz.token_set_ratio(a, b),
    )
    return score > 70
