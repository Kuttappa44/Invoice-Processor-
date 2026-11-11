"""Property / hotel name matching for comparing two display strings."""

from __future__ import annotations

import re

_STOP = frozenset(
    {"hotel", "the", "a", "an", "and", "of", "by", "at", "in"}
)

# When neither keyword set is a subset of the other, require at least this
# Jaccard similarity on keyword sets for "sufficient keyword overlap".
_OVERLAP_JACCARD_MIN = 0.5


def _keyword_set(name: str) -> frozenset[str]:
    """Lowercase tokens from alphanumeric runs; drop stop words and empties."""
    if name is None:
        return frozenset()
    tokens = re.findall(r"[a-z0-9]+", str(name).lower())
    return frozenset(t for t in tokens if t and t not in _STOP)


def match_properties(name1: str, name2: str) -> bool:
    """
    Return True if two property name strings refer to the same property.

    Rules:
    1. Tokenize both (lowercase; punctuation removed by taking alphanumeric runs).
    2. Remove stop words: hotel, the, a, an, and, of, by, at, in.
    3. True if one keyword set is a complete subset of the other (both non-empty).
    4. Else true if Jaccard similarity of keyword sets >= 0.5 (sufficient overlap).
    5. Both empty after tokenization/stopwords: True. Exactly one empty: False.
    """
    a = _keyword_set(name1)
    b = _keyword_set(name2)

    if not a and not b:
        return True
    if not a or not b:
        return False
    if a <= b or b <= a:
        return True

    inter = len(a & b)
    if inter == 0:
        return False
    union = len(a | b)
    return (inter / union) >= _OVERLAP_JACCARD_MIN
