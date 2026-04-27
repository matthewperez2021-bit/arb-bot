"""
config/preflight.py — Central startup validation.

Goal: fail fast with actionable errors before any network/API calls.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


_PLACEHOLDER_FRAGMENTS = (
    "your-",
    "sk-ant-your-",
    "0xyour-",
    "replace-me",
    "changeme",
)


def _looks_placeholder(value: str) -> bool:
    v = (value or "").strip().lower()
    if not v:
        return True
    return any(frag in v for frag in _PLACEHOLDER_FRAGMENTS)


def run_preflight(
    *,
    kalshi_api_key_id: str,
    kalshi_private_key_path: str,
    # Polymarket — geo-blocked in US since 2022. Both args are now optional.
    poly_private_key: str = "",
    poly_proxy_wallet: str = "",
    # PredictIt — US-legal, no API key needed for reads.
    predictit_enabled: bool = True,
    # Odds API — optional sportsbook signal source.
    odds_api_key: str = "",
    anthropic_api_key: str = "",
) -> PreflightResult:
    """
    Validate required config for paper trading / live.

    Second-platform requirement (exactly one must be available):
      - PredictIt  (default — read-only, no credentials needed)
      - Polymarket (geo-blocked in US; kept for non-US deployments)

    Notes:
    - Anthropic is optional (cache-only matching is allowed).
    - Telegram is optional (alerts fall back to console).
    - Odds API is optional (sportsbook signal layer).
    """
    errors: list[str] = []
    warnings: list[str] = []

    # ── Kalshi (always required) ─────────────────────────────────────
    if _looks_placeholder(kalshi_api_key_id):
        errors.append("KALSHI_API_KEY_ID is missing/placeholder in config/secrets.env")

    if not kalshi_private_key_path:
        errors.append("KALSHI_PRIVATE_KEY_PATH is missing in config/secrets.env")
    else:
        path = kalshi_private_key_path
        if not os.path.isabs(path):
            path = os.path.abspath(path)
        if not os.path.exists(path):
            errors.append(f"Kalshi private key file not found at: {kalshi_private_key_path}")

    # ── Second platform (PredictIt OR Polymarket) ────────────────────
    poly_ok = (
        not _looks_placeholder(poly_private_key)
        and not _looks_placeholder(poly_proxy_wallet)
    )
    if not predictit_enabled and not poly_ok:
        errors.append(
            "No second platform configured. "
            "Either enable PredictIt (default) or set POLY_PRIVATE_KEY + "
            "POLY_PROXY_WALLET for non-US Polymarket access."
        )

    if poly_ok:
        warnings.append(
            "Polymarket is geo-blocked for US users. "
            "Ensure you are accessing from a permitted jurisdiction."
        )

    # ── Optional keys ────────────────────────────────────────────────
    if not anthropic_api_key or _looks_placeholder(anthropic_api_key):
        warnings.append("ANTHROPIC_API_KEY not set (LLM verifier will run cache-only)")

    if not odds_api_key or _looks_placeholder(odds_api_key):
        warnings.append(
            "ODDS_API_KEY not set (sportsbook signal layer disabled). "
            "Get a free key at https://the-odds-api.com"
        )

    return PreflightResult(ok=(len(errors) == 0), errors=tuple(errors), warnings=tuple(warnings))

