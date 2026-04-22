"""
false_match.py — Hard-reject rules for known false match patterns.
Phase 2 build target.
"""

# Pairs of terms that indicate two markets are NOT the same event
FALSE_MATCH_PATTERNS = [
    ("primary", "general"),
    ("margin", "win"),
    ("2024", "2025"),
    ("republican", "democrat"),
    ("electoral", "popular"),
]

def has_conflicting_qualifiers(title_a: str, title_b: str) -> bool:
    """Returns True if the two titles contain mutually exclusive qualifiers."""
    # TODO Phase 2: Implement full logic
    return False
