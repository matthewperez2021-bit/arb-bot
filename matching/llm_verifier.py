"""
matching/llm_verifier.py — Claude-powered market match verification.

Uses claude-opus-4-6 to determine whether two prediction market questions
are asking about the exact same event with the same resolution criteria.

Results are cached to disk (JSON) for LLM_CACHE_TTL seconds to avoid
redundant API calls on every scan cycle.

Usage:
    from matching.llm_verifier import LLMVerifier
    verifier = LLMVerifier()
    result   = verifier.verify("Will Dems win 2024?", "Will Democrats win the 2024 US election?")
    if result.same_event and result.confidence > 0.80:
        # safe to trade
"""

import hashlib
import json
import logging
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import anthropic

from config.settings import (
    ANTHROPIC_API_KEY,
    LLM_CONFIDENCE_THRESHOLD,
    LLM_MAX_TOKENS,
    LLM_MODEL,
    MATCH_CACHE_PATH,
    MATCH_CACHE_TTL_SECS,
)

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Result type
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class VerificationResult:
    """Result of LLM market match verification."""
    same_event:  bool
    confidence:  float          # 0.0–1.0
    reason:      str            # brief explanation from the model
    risk:        str            # any resolution divergence risks flagged
    cached:      bool = False   # True if this came from cache
    error:       str = ""       # set if the API call failed

    @property
    def is_safe_to_trade(self) -> bool:
        """
        Returns True only when the LLM is confident this is a genuine match.
        Uses LLM_CONFIDENCE_THRESHOLD from settings (default 0.80).
        """
        return self.same_event and self.confidence >= LLM_CONFIDENCE_THRESHOLD and not self.error

    @classmethod
    def from_error(cls, error_msg: str) -> "VerificationResult":
        return cls(same_event=False, confidence=0.0, reason="", risk="", error=error_msg)

    @classmethod
    def uncertain(cls) -> "VerificationResult":
        """Conservative fallback when JSON parse fails."""
        return cls(same_event=False, confidence=0.0, reason="parse_failure", risk="unknown")


# ─────────────────────────────────────────────────────────────────────────────
# Verifier
# ─────────────────────────────────────────────────────────────────────────────

class LLMVerifier:
    """
    Verifies candidate market matches using Claude.

    Workflow:
      1. Hash the two titles → cache key
      2. Check disk cache — return cached result if fresh
      3. Call Claude API with structured prompt
      4. Parse JSON response → VerificationResult
      5. Write to cache
    """

    PROMPT_TEMPLATE = """\
You are a prediction market expert. Your job is to determine whether two prediction market questions are asking about the EXACT same real-world event with identical resolution criteria.

First market (Kalshi):  "{kalshi_title}"
Second market:          "{poly_title}"

Answer strictly in JSON with no other text:
{{
  "same_event":  true or false,
  "confidence":  0.0 to 1.0,
  "reason":      "one sentence explanation",
  "risk":        "any subtle differences in scope, timeframe, or resolution criteria that could cause different outcomes — empty string if none"
}}

Be strict. Reject if:
- Timeframes differ (primary vs general election, different years)
- Resolution criteria differ (different data sources, different thresholds)
- Geographic scope differs (state vs national, different countries)
- The questions are about related but distinct events

Only return true if you are confident both markets will resolve YES or NO on exactly the same real-world outcome."""

    def __init__(self, cache_path: Optional[str] = None):
        # Dev ergonomics: allow cache-only mode without an API key.
        # If ANTHROPIC_API_KEY is missing, we can still serve cached results
        # (and will conservatively reject uncached pairs).
        self._client = (
            anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            if ANTHROPIC_API_KEY
            else None
        )
        self._cache_path = Path(cache_path or MATCH_CACHE_PATH)
        self._cache:     dict[str, dict] = {}
        self._load_cache()

    # ─────────────────────────────────────────────────────────────────
    # Cache
    # ─────────────────────────────────────────────────────────────────

    def _cache_key(self, kalshi_title: str, poly_title: str) -> str:
        """Deterministic hash of the two titles (order-independent)."""
        combined = "|".join(sorted([kalshi_title.strip().lower(), poly_title.strip().lower()]))
        return hashlib.sha256(combined.encode()).hexdigest()[:16]

    def _load_cache(self) -> None:
        """Load the cache from disk if it exists."""
        if self._cache_path.exists():
            try:
                with open(self._cache_path) as f:
                    self._cache = json.load(f)
                log.debug("Loaded %d cached verifications from %s", len(self._cache), self._cache_path)
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("Could not load match cache: %s — starting fresh", exc)
                self._cache = {}

    def _save_cache(self) -> None:
        """Persist cache to disk."""
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._cache_path, "w") as f:
                json.dump(self._cache, f, indent=2)
        except OSError as exc:
            log.warning("Could not save match cache: %s", exc)

    def _get_cached(self, key: str) -> Optional[VerificationResult]:
        """Return cached result if it exists and is not stale."""
        entry = self._cache.get(key)
        if not entry:
            return None
        age = time.time() - entry.get("cached_at", 0)
        if age > MATCH_CACHE_TTL_SECS:
            log.debug("Cache entry %s expired (age=%.0fs)", key, age)
            return None
        result = VerificationResult(**{k: v for k, v in entry.items() if k != "cached_at"})
        result.cached = True
        return result

    def _write_cache(self, key: str, result: VerificationResult) -> None:
        self._cache[key] = {**asdict(result), "cached_at": time.time()}
        self._save_cache()

    def clear_cache(self) -> None:
        """Remove all cached entries. Forces re-verification on next call."""
        self._cache = {}
        if self._cache_path.exists():
            self._cache_path.unlink()
        log.info("Match cache cleared.")

    # ─────────────────────────────────────────────────────────────────
    # Verification
    # ─────────────────────────────────────────────────────────────────

    def verify(self, kalshi_title: str, poly_title: str) -> VerificationResult:
        """
        Verify whether two market titles describe the same event.

        Checks cache first. If not cached, calls Claude and caches the result.

        Args:
            kalshi_title: the Kalshi market title
            poly_title:   the Polymarket question text

        Returns:
            VerificationResult with same_event, confidence, reason, risk.
            Check result.is_safe_to_trade before using in execution.
        """
        key = self._cache_key(kalshi_title, poly_title)

        cached = self._get_cached(key)
        if cached:
            log.debug("Cache hit for key %s", key)
            return cached

        if self._client is None:
            return VerificationResult.from_error(
                "ANTHROPIC_API_KEY not set (cache-only mode; pair not cached)"
            )

        log.debug("Calling Claude to verify: '%s' vs '%s'", kalshi_title[:50], poly_title[:50])

        prompt = self.PROMPT_TEMPLATE.format(
            kalshi_title=kalshi_title,
            poly_title=poly_title,
        )

        try:
            response = self._client.messages.create(
                model=LLM_MODEL,
                max_tokens=LLM_MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
            )
            raw_text = response.content[0].text.strip()
        except anthropic.APIError as exc:
            log.error("Claude API error: %s", exc)
            return VerificationResult.from_error(str(exc))
        except Exception as exc:
            log.error("Unexpected error calling Claude: %s", exc)
            return VerificationResult.from_error(str(exc))

        result = self._parse_response(raw_text)
        if not result.error:
            self._write_cache(key, result)

        log.info(
            "Verified: same=%s conf=%.2f | '%s' vs '%s'",
            result.same_event, result.confidence,
            kalshi_title[:40], poly_title[:40],
        )
        return result

    def _parse_response(self, raw_text: str) -> VerificationResult:
        """
        Parse Claude's JSON response into a VerificationResult.

        Handles minor formatting issues (markdown fences, trailing text).
        Falls back to VerificationResult.uncertain() if JSON is invalid.
        """
        # Strip markdown fences if present
        text = raw_text
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]

        # Find the JSON object
        start = text.find("{")
        end   = text.rfind("}") + 1
        if start == -1 or end == 0:
            log.warning("No JSON object found in Claude response: %s", raw_text[:200])
            return VerificationResult.uncertain()

        try:
            data = json.loads(text[start:end])
        except json.JSONDecodeError as exc:
            log.warning("JSON parse error: %s | raw: %s", exc, raw_text[:200])
            return VerificationResult.uncertain()

        return VerificationResult(
            same_event=bool(data.get("same_event", False)),
            confidence=float(data.get("confidence", 0.0)),
            reason=    str(data.get("reason", "")),
            risk=      str(data.get("risk", "")),
        )

    # ─────────────────────────────────────────────────────────────────
    # Batch verification
    # ─────────────────────────────────────────────────────────────────

    def verify_batch(
        self,
        pairs: list[tuple[str, str]],
        min_confidence: float = LLM_CONFIDENCE_THRESHOLD,
    ) -> list[tuple[tuple[str, str], VerificationResult]]:
        """
        Verify a list of (kalshi_title, poly_title) pairs.

        Returns only pairs where is_safe_to_trade is True.
        Logs a summary at the end.

        Args:
            pairs:          list of (kalshi_title, poly_title) tuples
            min_confidence: override confidence threshold for this batch

        Returns:
            List of (pair, result) for verified matches only.
        """
        verified = []
        skipped  = 0

        for i, (k_title, p_title) in enumerate(pairs):
            result = self.verify(k_title, p_title)

            if result.error:
                log.warning("Skipping pair %d/%d due to error: %s", i+1, len(pairs), result.error)
                skipped += 1
                continue

            if result.same_event and result.confidence >= min_confidence:
                verified.append(((k_title, p_title), result))
                log.info(
                    "[%d/%d] ✓ VERIFIED (conf=%.2f): '%s'",
                    i+1, len(pairs), result.confidence, k_title[:50]
                )
            else:
                log.debug(
                    "[%d/%d] ✗ rejected (same=%s, conf=%.2f): '%s'",
                    i+1, len(pairs), result.same_event, result.confidence, k_title[:50]
                )

        log.info(
            "verify_batch: %d pairs → %d verified, %d rejected, %d errors",
            len(pairs), len(verified), len(pairs) - len(verified) - skipped, skipped
        )
        return verified


# ─────────────────────────────────────────────────────────────────────────────
# Smoke test (uses real Claude API — costs tokens)
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    verifier = LLMVerifier()

    test_pairs = [
        # Should be SAME
        (
            "Will the Democrat win the 2024 US presidential election?",
            "Will Democrats win the 2024 presidential election?",
        ),
        # Should be DIFFERENT (primary vs general)
        (
            "Will Trump win the 2024 Republican primary?",
            "Will Trump win the 2024 presidential election?",
        ),
        # Should be SAME
        (
            "Will the Fed raise rates in November 2024?",
            "Will the Federal Reserve increase interest rates at the November 2024 FOMC meeting?",
        ),
        # Should be DIFFERENT (different year)
        (
            "Will the US GDP grow in Q3 2024?",
            "Will the US GDP grow in Q3 2025?",
        ),
    ]

    print(f"\nRunning {len(test_pairs)} verification tests...\n")
    for k, p in test_pairs:
        result = verifier.verify(k, p)
        status = "✓ SAME" if result.is_safe_to_trade else "✗ DIFFERENT"
        cached = " [cached]" if result.cached else ""
        print(f"{status}{cached} (conf={result.confidence:.2f})")
        print(f"  K: {k[:60]}")
        print(f"  P: {p[:60]}")
        print(f"  Reason: {result.reason}")
        if result.risk:
            print(f"  Risk:   {result.risk}")
        print()
