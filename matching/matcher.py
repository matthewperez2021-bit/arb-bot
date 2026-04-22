"""
matching/matcher.py — Market matching pipeline.

Identifies which Kalshi and Polymarket markets describe the same
real-world event. Uses a three-signal scoring approach:
  1. Jaccard token overlap (50%)
  2. SequenceMatcher string similarity (30%)
  3. Close date compatibility (20%)

Only candidates above MATCH_CANDIDATE_THRESHOLD pass through to
LLM verification. Only verified matches above MATCH_TRADE_THRESHOLD
are eligible for trading.

Usage:
    from matching.matcher import MarketMatcher
    matcher  = MarketMatcher()
    matches  = matcher.find_matches(kalshi_markets, poly_markets)
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Optional

from clients.normalizer import NormalizedMarket
from config.settings import (
    DATE_TOLERANCE_DAYS,
    MATCH_CANDIDATE_THRESHOLD,
    MATCH_TRADE_THRESHOLD,
)

log = logging.getLogger(__name__)

# Words that carry no semantic signal — strip before comparing
STOPWORDS = frozenset(
    "will the a an in of to by for be is are was were has have had "
    "that this these those it its at on or and but not no if when "
    "which who whom how what where from with".split()
)

# Conflicting qualifier pairs — if one title has term_a and
# the other has term_b (and not both have both) → reject
FALSE_MATCH_PATTERNS: list[tuple[str, str]] = [
    ("primary",    "general"),
    ("primary",    "runoff"),
    ("margin",     "win"),
    ("margin",     "lose"),
    ("2024",       "2025"),
    ("2025",       "2026"),
    ("republican", "democrat"),
    ("gop",        "democrat"),
    ("electoral",  "popular"),
    ("national",   "state"),
    ("senate",     "house"),
    ("yes",        "no"),         # catch inverted contracts
]


# ─────────────────────────────────────────────────────────────────────────────
# Result type
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MarketMatch:
    """A candidate match between one Kalshi and one Polymarket market."""
    kalshi:       NormalizedMarket
    poly:         NormalizedMarket
    score:        float             # combined similarity score 0.0–1.0
    token_score:  float             # Jaccard component
    string_score: float             # SequenceMatcher component
    date_bonus:   float             # 0.0 or 0.20
    llm_verified: bool = False
    llm_confidence: float = 0.0
    llm_risk:     str = ""          # any flags noted by the LLM

    @property
    def is_tradeable(self) -> bool:
        """True only when LLM-verified AND score above live trade threshold."""
        return self.llm_verified and self.score >= MATCH_TRADE_THRESHOLD


# ─────────────────────────────────────────────────────────────────────────────
# Matcher
# ─────────────────────────────────────────────────────────────────────────────

class MarketMatcher:
    """
    Multi-signal market matcher.

    Step 1: Normalize text (lowercase, strip stopwords, remove punctuation)
    Step 2: Score all Kalshi × Poly pairs using combined signal
    Step 3: Filter by MATCH_CANDIDATE_THRESHOLD
    Step 4: Hard-reject known false match patterns
    Step 5: Return ranked candidate list for LLM verification
    """

    # ──────────────────────────────────────────────────────────────────
    # Text normalization
    # ──────────────────────────────────────────────────────────────────

    def normalize(self, text: str) -> str:
        """
        Lowercase, strip punctuation and stopwords, collapse whitespace.

        "Will the Democrat win the 2024 presidential election?"
        → "democrat 2024 presidential election"
        """
        text = text.lower()
        text = re.sub(r"[^\w\s]", " ", text)           # remove punctuation
        words = [w for w in text.split() if w not in STOPWORDS]
        return " ".join(words)

    # ──────────────────────────────────────────────────────────────────
    # Scoring signals
    # ──────────────────────────────────────────────────────────────────

    def token_overlap(self, a: str, b: str) -> float:
        """
        Jaccard similarity on word token sets.

        J(A, B) = |A ∩ B| / |A ∪ B|

        High weight (50%) because it's robust to word order changes.
        """
        a_tokens = set(self.normalize(a).split())
        b_tokens = set(self.normalize(b).split())

        if not a_tokens or not b_tokens:
            return 0.0

        intersection = len(a_tokens & b_tokens)
        union        = len(a_tokens | b_tokens)
        return intersection / union

    def string_similarity(self, a: str, b: str) -> float:
        """
        SequenceMatcher ratio on normalized strings.

        Captures ordering and partial overlaps that Jaccard misses
        (e.g. "2024 presidential election" vs "presidential 2024 election").
        """
        return SequenceMatcher(
            None,
            self.normalize(a),
            self.normalize(b),
        ).ratio()

    def date_compatible(self, kalshi_close: str, poly_end: str) -> bool:
        """
        Returns True if close dates are within DATE_TOLERANCE_DAYS of each other.

        A loose tolerance (3 days default) handles edge cases where platforms
        list the same event with slightly different deadlines.
        """
        if not kalshi_close or not poly_end:
            return True  # can't reject on missing data

        try:
            k_dt = datetime.fromisoformat(kalshi_close.replace("Z", "+00:00"))
            p_dt = datetime.fromisoformat(poly_end.replace("Z", "+00:00"))
            return abs((k_dt - p_dt).days) <= DATE_TOLERANCE_DAYS
        except (ValueError, TypeError):
            return True  # parse failure → don't reject

    def combined_score(
        self,
        kalshi: NormalizedMarket,
        poly:   NormalizedMarket,
    ) -> tuple[float, float, float, float]:
        """
        Compute the combined similarity score between two markets.

        Returns (combined, token_score, string_score, date_bonus).

        Weights:
            token_score  × 0.50
            string_score × 0.30
            date_bonus   (0.0 or 0.20)
        """
        tok  = self.token_overlap(kalshi.title, poly.title)
        seq  = self.string_similarity(kalshi.title, poly.title)
        date = 0.20 if self.date_compatible(kalshi.close_time, poly.close_time) else 0.0

        combined = (tok * 0.50) + (seq * 0.30) + date
        return combined, tok, seq, date

    # ──────────────────────────────────────────────────────────────────
    # False match detection
    # ──────────────────────────────────────────────────────────────────

    def has_conflicting_qualifiers(
        self,
        title_a: str,
        title_b: str,
    ) -> tuple[bool, str]:
        """
        Check if two titles contain mutually exclusive qualifiers.

        Returns (has_conflict: bool, conflicting_pair: str).

        Example:
            "Who will win the Republican primary?" vs
            "Who will win the general election?"
            → (True, "primary/general")
        """
        a = title_a.lower()
        b = title_b.lower()

        for term_x, term_y in FALSE_MATCH_PATTERNS:
            a_has_x = term_x in a
            a_has_y = term_y in a
            b_has_x = term_x in b
            b_has_y = term_y in b

            # Conflict: one title has term_x, the other has term_y (exclusively)
            if (a_has_x and b_has_y and not a_has_y and not b_has_x):
                return True, f"{term_x}/{term_y}"
            if (a_has_y and b_has_x and not a_has_x and not b_has_y):
                return True, f"{term_y}/{term_x}"

        return False, ""

    # ──────────────────────────────────────────────────────────────────
    # Main matching pipeline
    # ──────────────────────────────────────────────────────────────────

    def find_matches(
        self,
        kalshi_markets: list[NormalizedMarket],
        poly_markets:   list[NormalizedMarket],
        threshold:      float = MATCH_CANDIDATE_THRESHOLD,
        max_per_kalshi: int   = 3,
    ) -> list[MarketMatch]:
        """
        Find all candidate market matches above the similarity threshold.

        For each Kalshi market, scores it against every Polymarket market
        and returns the top max_per_kalshi candidates.

        Args:
            kalshi_markets:  list of NormalizedMarket from Kalshi
            poly_markets:    list of NormalizedMarket from Polymarket
            threshold:       minimum combined score to include (default 0.55)
            max_per_kalshi:  max candidates per Kalshi market to keep

        Returns:
            Sorted list of MarketMatch objects (highest score first).
            These are CANDIDATES — not yet verified by LLM.
        """
        matches: list[MarketMatch] = []
        rejected_false  = 0
        rejected_thresh = 0

        for kalshi in kalshi_markets:
            candidates: list[MarketMatch] = []

            for poly in poly_markets:
                combined, tok, seq, date = self.combined_score(kalshi, poly)

                if combined < threshold:
                    rejected_thresh += 1
                    continue

                conflict, conflict_pair = self.has_conflicting_qualifiers(
                    kalshi.title, poly.title
                )
                if conflict:
                    log.debug(
                        "Rejected false match (conflict: %s): '%s' vs '%s'",
                        conflict_pair, kalshi.title[:40], poly.title[:40]
                    )
                    rejected_false += 1
                    continue

                candidates.append(MarketMatch(
                    kalshi=kalshi, poly=poly,
                    score=combined, token_score=tok,
                    string_score=seq, date_bonus=date,
                ))

            # Keep only the top N candidates per Kalshi market
            candidates.sort(key=lambda m: m.score, reverse=True)
            matches.extend(candidates[:max_per_kalshi])

        matches.sort(key=lambda m: m.score, reverse=True)

        log.info(
            "find_matches: %d kalshi × %d poly → %d candidates "
            "(rejected: %d below threshold, %d false match)",
            len(kalshi_markets), len(poly_markets), len(matches),
            rejected_thresh, rejected_false,
        )
        return matches

    def find_best_match(
        self,
        kalshi: NormalizedMarket,
        poly_markets: list[NormalizedMarket],
    ) -> Optional[MarketMatch]:
        """
        Find the single best Polymarket match for one Kalshi market.
        Returns None if no candidate exceeds the threshold.
        """
        results = self.find_matches([kalshi], poly_markets, max_per_kalshi=1)
        return results[0] if results else None


# ─────────────────────────────────────────────────────────────────────────────
# Smoke test
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import time
    logging.basicConfig(level=logging.DEBUG)

    matcher = MarketMatcher()

    # Test normalization
    raw = "Will the Democrat win the 2024 presidential election?"
    print(f"Normalized: '{matcher.normalize(raw)}'")

    # Test scoring
    kalshi_title = "Will the Democrat win the 2024 presidential election?"
    poly_title   = "Will Democrats win the 2024 US presidential election?"
    tok  = matcher.token_overlap(kalshi_title, poly_title)
    seq  = matcher.string_similarity(kalshi_title, poly_title)
    print(f"\nToken overlap:    {tok:.3f}")
    print(f"String similarity: {seq:.3f}")
    print(f"Combined (no date): {(tok*0.5 + seq*0.3):.3f}")

    # Test false match rejection
    title_a = "Will the Republican win the primary?"
    title_b = "Will the Republican win the general election?"
    conflict, pair = matcher.has_conflicting_qualifiers(title_a, title_b)
    print(f"\nConflict check: {conflict} ({pair})")

    # Test with fake NormalizedMarket objects
    from clients.normalizer import NormalizedMarket
    k = NormalizedMarket("kalshi", "PRES-2024-DEM", kalshi_title, "2024-11-05T23:59:00Z", "PRES-2024-DEM", "PRES-2024-DEM")
    p = NormalizedMarket("polymarket", "0xabc", poly_title, "2024-11-05T23:59:00Z", "71321...", "52114...")

    match = matcher.find_best_match(k, [p])
    if match:
        print(f"\nBest match score: {match.score:.3f}")
        print(f"  Token: {match.token_score:.3f}  String: {match.string_score:.3f}  Date: {match.date_bonus:.2f}")
        print(f"  Tradeable (pre-LLM): {match.is_tradeable}")
