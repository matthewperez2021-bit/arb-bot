"""
detection/scorer.py — Capital velocity scoring and duration filtering.

The core insight: annualized return matters more than raw edge per trade.
A 1.5% edge resolving in 3 days beats a 4% edge resolving in 60 days.

edge_per_day = (net_profit_pct / days_to_resolution) * 365

All opportunities are ranked by edge_per_day before execution.

Usage:
    from detection.scorer import score_opportunity, filter_and_rank
    ranked = filter_and_rank(opportunities, max_days=14)
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from config.settings import MAX_DURATION_DAYS

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Duration helpers
# ─────────────────────────────────────────────────────────────────────────────

def days_until_close(close_time_iso: str) -> int:
    """
    Returns the number of whole days until a market closes.

    Args:
        close_time_iso: ISO 8601 string, e.g. "2024-11-05T23:59:00Z"

    Returns:
        Days remaining (0 if already closed or parse error).
    """
    if not close_time_iso:
        return 0
    try:
        close = datetime.fromisoformat(close_time_iso.replace("Z", "+00:00"))
        now   = datetime.now(timezone.utc)
        delta = (close - now).days
        return max(0, delta)
    except (ValueError, TypeError):
        return 0


def hours_until_close(close_time_iso: str) -> float:
    """
    Returns the number of hours until a market closes (fractional).
    Useful for very short-duration markets.
    """
    if not close_time_iso:
        return 0.0
    try:
        close   = datetime.fromisoformat(close_time_iso.replace("Z", "+00:00"))
        now     = datetime.now(timezone.utc)
        seconds = (close - now).total_seconds()
        return max(0.0, seconds / 3600)
    except (ValueError, TypeError):
        return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Edge velocity
# ─────────────────────────────────────────────────────────────────────────────

def edge_per_day(net_profit_pct: float, days_to_resolution: int) -> float:
    """
    Annualized edge per day — the key ranking metric.

    Formula:
        edge_per_day = (net_profit_pct / days_to_resolution) * 365

    Examples:
        4% edge, 60 days → 4/60 * 365 = 24.3% annualized
        1.5% edge, 3 days → 1.5/3 * 365 = 182.5% annualized  ← 7.5x better

    Args:
        net_profit_pct:       net profit as a fraction (0.02 = 2%)
        days_to_resolution:   calendar days until the market closes

    Returns:
        Annualized edge as a fraction. Returns 0 for invalid inputs.
    """
    if days_to_resolution <= 0 or net_profit_pct <= 0:
        return 0.0
    return (net_profit_pct / days_to_resolution) * 365


def edge_per_day_from_close(net_profit_pct: float, close_time_iso: str) -> float:
    """
    Convenience version: compute edge_per_day from a close time string.
    Falls back to 0.0 if close time is unparseable.
    """
    days = days_until_close(close_time_iso)
    return edge_per_day(net_profit_pct, days)


# ─────────────────────────────────────────────────────────────────────────────
# Opportunity scoring
# ─────────────────────────────────────────────────────────────────────────────

def score_opportunity(opp, close_time_iso: Optional[str] = None) -> float:
    """
    Compute and attach edge_per_day to an ArbOpportunity.

    Modifies opp in place (sets opp.edge_per_day and opp.days_to_resolution).

    Args:
        opp:           ArbOpportunity dataclass instance
        close_time_iso: ISO close time string (uses opp.close_time if not provided)

    Returns:
        The computed edge_per_day value.
    """
    close = close_time_iso or getattr(opp, "close_time", "")
    days  = days_until_close(close)

    epd = edge_per_day(opp.net_profit_pct, days)

    opp.days_to_resolution = days
    opp.edge_per_day       = epd

    return epd


# ─────────────────────────────────────────────────────────────────────────────
# Duration filter + ranking
# ─────────────────────────────────────────────────────────────────────────────

def filter_and_rank(
    opportunities: list,
    max_days: int = MAX_DURATION_DAYS,
    min_days: int = 1,
) -> list:
    """
    Filter opportunities to short-duration markets and rank by edge_per_day.

    Why short-duration?
        $1,000 at 2% edge:
          60-day market  →  $20 in 60 days  →  $120/year
          7-day market   →  $20 in 7 days   →  $1,040/year

    Args:
        opportunities: list of ArbOpportunity objects (already have edge_per_day set)
        max_days:      exclude markets closing more than this many days out
        min_days:      exclude markets closing within this many days
                       (too close → resolution timing risk)

    Returns:
        Filtered and ranked list, best edge_per_day first.
    """
    filtered = []
    excluded_long    = 0
    excluded_short   = 0

    for opp in opportunities:
        days = getattr(opp, "days_to_resolution", 0)

        if days < min_days:
            excluded_short += 1
            continue

        if days > max_days:
            excluded_long += 1
            continue

        filtered.append(opp)

    filtered.sort(key=lambda o: getattr(o, "edge_per_day", 0.0), reverse=True)

    log.debug(
        "filter_and_rank: %d → %d kept (excluded %d too-long, %d too-short)",
        len(opportunities), len(filtered), excluded_long, excluded_short,
    )

    return filtered


def categorize_duration(days: int) -> str:
    """
    Bucket a market's duration for P&L attribution reporting.

    Returns one of: "ultra_short", "short", "medium", "long", "very_long"
    """
    if days <= 1:
        return "ultra_short"    # same-day / next-day resolution
    if days <= 7:
        return "short"          # 2–7 days (best velocity)
    if days <= 14:
        return "medium"         # 8–14 days (good velocity)
    if days <= 30:
        return "long"           # 15–30 days (acceptable)
    return "very_long"          # 30+ days (avoid unless edge is huge)


# ─────────────────────────────────────────────────────────────────────────────
# Best event types (reference)
# ─────────────────────────────────────────────────────────────────────────────

# Markets with the best natural short-duration profile.
# Use these categories to prioritize matching effort.
HIGH_VELOCITY_CATEGORIES = [
    "Economic data releases",       # CPI, jobs report, GDP — exact release dates known
    "Sports — game outcomes",       # Resolves day-of or next day
    "Sports — weekly awards",       # MVP, player of the week etc.
    "Weekly political polls",       # Resolve as polls are published
    "Fed/FOMC decisions",           # Exact meeting dates known
    "Earnings — beat/miss",         # Resolves same day as earnings
    "Award shows",                  # Oscars, Grammys — single event
]


# ─────────────────────────────────────────────────────────────────────────────
# Smoke test
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== Edge Per Day Examples ===\n")

    examples = [
        ("4% edge, 60 days",   0.04,  60),
        ("2% edge, 30 days",   0.02,  30),
        ("1.5% edge, 7 days",  0.015,  7),
        ("1.5% edge, 3 days",  0.015,  3),
        ("2% edge, 7 days",    0.02,   7),
        ("3% edge, 14 days",   0.03,  14),
    ]

    for label, edge, days in examples:
        epd = edge_per_day(edge, days)
        print(f"  {label:<25}  edge/day = {epd:6.1f}%  annualized")

    print("\n=== Duration Buckets ===\n")
    for d in [0, 1, 3, 7, 10, 14, 20, 30, 60]:
        print(f"  {d:3d} days → {categorize_duration(d)}")
