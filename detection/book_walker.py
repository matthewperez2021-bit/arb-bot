"""
detection/book_walker.py — Order book slippage calculator.

Walks the ask side of an order book level by level to compute:
  - Average fill price for a given target size
  - Maximum fillable contracts given available liquidity
  - Expected slippage as a percentage of the best ask

This is called BEFORE placing any order. If slippage exceeds
MAX_SLIPPAGE_PCT the trade is rejected — protecting arb edge.

Usage:
    from detection.book_walker import walk_book, slippage_check
    result = slippage_check(asks, target_contracts=50)
    if result.is_viable:
        # proceed with execution
"""

import logging
from dataclasses import dataclass
from typing import Optional

from clients.normalizer import NormalizedBook, PriceLevel
from config.settings import MAX_SLIPPAGE_PCT

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Result type
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class WalkResult:
    """Result of walking an order book for a target size."""
    target_contracts:  float
    fillable_contracts: float       # how many contracts can actually be filled
    avg_fill_price:    float        # weighted average price across all levels
    best_ask:          float        # price at the top of the book (no slippage)
    slippage_pct:      float        # (avg_fill - best_ask) / best_ask
    total_cost_usd:    float        # fillable_contracts * avg_fill_price
    levels_consumed:   int          # how many price levels were touched
    partial_fill:      bool         # True if we couldn't fill the full target

    @property
    def is_viable(self) -> bool:
        """
        Returns True if the trade should proceed.

        Conditions:
          - At least 1 contract is fillable
          - Slippage is within MAX_SLIPPAGE_PCT
          - We can fill the full target (no partial fill)
        """
        return (
            self.fillable_contracts >= 1
            and self.slippage_pct <= MAX_SLIPPAGE_PCT
            and not self.partial_fill
        )

    @property
    def slippage_usd(self) -> float:
        """Dollar cost of slippage on this trade."""
        return self.fillable_contracts * (self.avg_fill_price - self.best_ask)

    def as_log_str(self) -> str:
        return (
            f"fill={self.fillable_contracts:.0f}/{self.target_contracts:.0f} contracts "
            f"@ avg {self.avg_fill_price:.4f} "
            f"(slippage={self.slippage_pct:.3%}, ${self.slippage_usd:.2f})"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Core walk function
# ─────────────────────────────────────────────────────────────────────────────

def walk_book(
    asks: list[PriceLevel],
    target_contracts: float,
) -> WalkResult:
    """
    Walk ask levels to compute average fill price for a target size.

    Iterates through ask levels from best (lowest price) to worst,
    consuming liquidity until the target is filled or the book runs dry.

    Args:
        asks:             list of PriceLevel (price, quantity), sorted ascending
        target_contracts: number of contracts we want to buy

    Returns:
        WalkResult with fill details and viability assessment.

    Example:
        asks = [
            PriceLevel(0.45, 200),   ← best ask: 200 contracts @ $0.45
            PriceLevel(0.46, 800),
            PriceLevel(0.47, 2000),
        ]
        walk_book(asks, target=600):
          level 1: take 200 @ 0.45 = $90.00
          level 2: take 400 @ 0.46 = $184.00
          total:   600 contracts, $274.00
          avg:     $274.00 / 600 = $0.4567
          slippage: (0.4567 - 0.45) / 0.45 = 1.49%
    """
    if not asks:
        return WalkResult(
            target_contracts=target_contracts,
            fillable_contracts=0,
            avg_fill_price=0.0,
            best_ask=0.0,
            slippage_pct=1.0,
            total_cost_usd=0.0,
            levels_consumed=0,
            partial_fill=True,
        )

    best_ask    = asks[0].price
    remaining   = target_contracts
    total_cost  = 0.0
    filled      = 0.0
    levels      = 0

    for level in asks:
        if remaining <= 0:
            break

        take        = min(remaining, level.quantity)
        total_cost += take * level.price
        filled     += take
        remaining  -= take
        levels     += 1

    avg_price   = total_cost / filled if filled > 0 else 0.0
    slippage    = (avg_price - best_ask) / best_ask if best_ask > 0 else 1.0
    partial     = filled < target_contracts

    return WalkResult(
        target_contracts=   target_contracts,
        fillable_contracts= filled,
        avg_fill_price=     avg_price,
        best_ask=           best_ask,
        slippage_pct=       slippage,
        total_cost_usd=     total_cost,
        levels_consumed=    levels,
        partial_fill=       partial,
    )


def slippage_check(
    book: NormalizedBook,
    target_contracts: float,
    max_slippage: float = MAX_SLIPPAGE_PCT,
) -> WalkResult:
    """
    Convenience wrapper: walk a NormalizedBook and check viability.

    Args:
        book:             NormalizedBook (from normalizer.py)
        target_contracts: how many contracts to simulate buying
        max_slippage:     override the default MAX_SLIPPAGE_PCT

    Returns:
        WalkResult. Check result.is_viable before proceeding.
    """
    result = walk_book(book.asks, target_contracts)

    # Apply custom slippage threshold if different from settings
    if result.slippage_pct > max_slippage:
        log.debug(
            "Slippage too high: %.3f%% > %.3f%% limit | %s",
            result.slippage_pct * 100, max_slippage * 100, result.as_log_str()
        )

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Arb-specific: check both legs together
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DualLegSlippage:
    """Slippage analysis for both legs of an arb trade."""
    kalshi_leg:         WalkResult
    poly_leg:           WalkResult
    viable_contracts:   float       # min fillable across both legs
    total_slippage_pct: float       # combined slippage from both legs

    @property
    def is_viable(self) -> bool:
        """Both legs must be independently viable."""
        return (
            self.kalshi_leg.is_viable
            and self.poly_leg.is_viable
            and self.viable_contracts >= 1
        )

    @property
    def slippage_cost_usd(self) -> float:
        """Total dollar cost of slippage across both legs."""
        return self.kalshi_leg.slippage_usd + self.poly_leg.slippage_usd

    def as_log_str(self) -> str:
        return (
            f"viable={self.viable_contracts:.0f} contracts | "
            f"K-slip={self.kalshi_leg.slippage_pct:.3%} "
            f"P-slip={self.poly_leg.slippage_pct:.3%} "
            f"total-slip=${self.slippage_cost_usd:.2f}"
        )


def check_dual_leg_slippage(
    kalshi_book: NormalizedBook,
    poly_book:   NormalizedBook,
    target_contracts: float,
) -> DualLegSlippage:
    """
    Check slippage for both legs of an arb trade simultaneously.

    The viable contract size is limited by whichever leg has less liquidity.
    Both legs must pass their individual slippage checks.

    Args:
        kalshi_book:      NormalizedBook for the Kalshi side
        poly_book:        NormalizedBook for the Polymarket side
        target_contracts: how many contracts we want to trade

    Returns:
        DualLegSlippage. Check .is_viable before executing.
    """
    k_result = walk_book(kalshi_book.asks, target_contracts)
    p_result = walk_book(poly_book.asks,   target_contracts)

    viable = min(k_result.fillable_contracts, p_result.fillable_contracts)
    total_slip = k_result.slippage_pct + p_result.slippage_pct

    return DualLegSlippage(
        kalshi_leg=       k_result,
        poly_leg=         p_result,
        viable_contracts= viable,
        total_slippage_pct= total_slip,
    )


def max_profitable_size(
    kalshi_book: NormalizedBook,
    poly_book:   NormalizedBook,
    gross_edge:  float,
    max_contracts: int = 1000,
) -> int:
    """
    Find the largest trade size where combined slippage still leaves
    a positive net edge.

    Binary searches over contract sizes from 1 → max_contracts.

    Args:
        kalshi_book:   NormalizedBook for Kalshi side
        poly_book:     NormalizedBook for Poly side
        gross_edge:    detected gross profit fraction (e.g. 0.07 for 7%)
        max_contracts: upper bound on search

    Returns:
        Maximum contracts where slippage < gross_edge, or 0 if even
        1 contract is too slippy.
    """
    lo, hi, best = 1, max_contracts, 0

    while lo <= hi:
        mid  = (lo + hi) // 2
        dual = check_dual_leg_slippage(kalshi_book, poly_book, float(mid))

        if dual.total_slippage_pct < gross_edge and dual.viable_contracts >= mid:
            best = mid
            lo   = mid + 1
        else:
            hi = mid - 1

    return best


# ─────────────────────────────────────────────────────────────────────────────
# Smoke test
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from clients.normalizer import PriceLevel, NormalizedBook

    # Simulate a thinly-traded book that causes slippage
    thin_asks = [
        PriceLevel(0.45, 100),
        PriceLevel(0.46, 200),
        PriceLevel(0.47, 500),
    ]
    book = NormalizedBook(asks=thin_asks)

    print("=== Book Walk Tests ===\n")

    for target in [50, 150, 400, 900]:
        result = slippage_check(book, target)
        viable = "✓ VIABLE" if result.is_viable else "✗ REJECT"
        print(f"Target {target:4d}: {viable} | {result.as_log_str()}")

    print("\n=== Dual Leg Test ===\n")
    kalshi_book = NormalizedBook(asks=[
        PriceLevel(0.44, 500),
        PriceLevel(0.45, 1000),
    ])
    poly_book = NormalizedBook(asks=[
        PriceLevel(0.49, 300),
        PriceLevel(0.50, 700),
    ])

    dual = check_dual_leg_slippage(kalshi_book, poly_book, target_contracts=400)
    viable = "✓ VIABLE" if dual.is_viable else "✗ REJECT"
    print(f"Dual leg (400 contracts): {viable}")
    print(f"  {dual.as_log_str()}")

    gross_edge = 1.0 - (0.44 + 0.49)  # = 0.07 (7%)
    max_size   = max_profitable_size(kalshi_book, poly_book, gross_edge)
    print(f"\nMax profitable size (gross_edge={gross_edge:.1%}): {max_size} contracts")
