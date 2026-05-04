"""
kelly.py — Kelly criterion position sizing utilities.

Includes two layers of calibration:
  1. Theoretical Kelly: f* = (p - c) / (1 - c) for a binary market
  2. Per-(sport, edge_bucket) calibration override from historical data

The override layer is the bridge between calibration_report.py findings
and live trade sizing. CALIBRATION_OVERRIDES is populated from the
calibration report (run `python scripts/calibration_report.py
--update-overrides`) and applied as a multiplier on the Kelly fraction.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from config.settings import CALIBRATION_OVERRIDES
except ImportError:
    CALIBRATION_OVERRIDES = {}


# Same edge buckets as calibration_report.py — keep in sync
_EDGE_BUCKETS = [
    (0.00, 0.02, "0-2%"),
    (0.02, 0.04, "2-4%"),
    (0.04, 0.06, "4-6%"),
    (0.06, 0.08, "6-8%"),
    (0.08, 0.12, "8-12%"),
    (0.12, 1.00, "12%+"),
]


def edge_bucket_label(net_edge: float) -> str:
    """Map a net_edge fraction (e.g. 0.05) to its bucket label."""
    for lo, hi, label in _EDGE_BUCKETS:
        if lo <= net_edge < hi:
            return label
    return "12%+"


def calibration_factor(sport: str, net_edge: float) -> float:
    """
    Look up the calibration multiplier for this (sport, edge_bucket).

    Returns 1.0 if no override is configured (default = no adjustment).

    Override key format: f"{sport}__{bucket}"
        e.g. "baseball_mlb__4-6%"  →  1.20  (over-size 20%)
        e.g. "soccer_usa_mls__6-8%"  →  0.75  (under-size 25%)

    Falls back to a sport-only key (e.g. "baseball_mlb") if specific
    bucket isn't listed. Falls back to global "default" if neither.
    """
    if not CALIBRATION_OVERRIDES:
        return 1.0

    bucket = edge_bucket_label(net_edge)
    keys_to_try = [
        f"{sport}__{bucket}",
        sport,
        "default",
    ]
    for key in keys_to_try:
        if key in CALIBRATION_OVERRIDES:
            return float(CALIBRATION_OVERRIDES[key])
    return 1.0


def half_kelly_contracts(edge: float, cost_per_contract: float,
                         bankroll: float, max_usd: float) -> int:
    """
    Returns the half-Kelly recommended number of contracts.
    edge: net profit fraction (e.g. 0.02 for 2%)
    cost_per_contract: total cost per arb pair (e.g. 0.93)
    bankroll: available capital in USD
    max_usd: hard cap on position size in USD
    """
    if edge <= 0 or cost_per_contract <= 0:
        return 0
    f_star = edge / (1.0 - cost_per_contract)
    half_k = f_star * 0.5
    kelly_usd = bankroll * half_k
    capped_usd = min(kelly_usd, max_usd)
    return max(1, int(capped_usd / cost_per_contract))
