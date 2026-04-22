"""
detection/arb_detector.py — Arbitrage opportunity scanner.

Checks all four leg combinations for a matched market pair and
identifies profitable arb opportunities after accounting for
taker fees on both platforms.

The two valid arb directions for a binary market:
  Direction A: Buy YES on Kalshi  + Buy NO  on Polymarket
  Direction B: Buy NO  on Kalshi  + Buy YES on Polymarket

An arb exists when:
  leg_1_ask + leg_2_ask < 1.00  (before fees)
  (after fees) net_profit_pct >= MIN_NET_EDGE_LIVE

Usage:
    from detection.arb_detector import ArbDetector
    detector = ArbDetector()
    opp = detector.analyze(kalshi_market, poly_market, kalshi_book, poly_book)
    if opp:
        print(f"Arb found: {opp.net_profit_pct:.2%} edge")
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from clients.normalizer import NormalizedMarket, NormalizedMarketBook
from config.settings import (
    KALSHI_TAKER_FEE,
    MAX_SLIPPAGE_PCT,
    MIN_NET_EDGE_LIVE,
    MIN_NET_EDGE_PAPER,
    POLY_TAKER_FEE,
)
from detection.book_walker import check_dual_leg_slippage, walk_book
from detection.scorer import days_until_close, edge_per_day

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Opportunity dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ArbOpportunity:
    """
    A confirmed arbitrage opportunity between one Kalshi and one Polymarket market.

    All prices are floats 0.0–1.0. All sizes are in contracts.
    Created by ArbDetector.analyze() — never construct directly.
    """

    # ── Kalshi leg ───────────────────────────────────────────────────
    kalshi_ticker:    str
    kalshi_side:      str       # "yes" or "no"
    kalshi_price:     float     # best ask price (0.0–1.0)
    kalshi_available: float     # contracts at best ask

    # ── Polymarket leg ───────────────────────────────────────────────
    poly_market_id:   str
    poly_token_id:    str       # the specific YES or NO token to buy
    poly_side:        str       # "yes" or "no"
    poly_price:       float     # best ask price (0.0–1.0)
    poly_available:   float     # contracts at best ask (converted from USD)

    # ── Economics ────────────────────────────────────────────────────
    gross_cost:       float     # kalshi_price + poly_price (total spent per contract)
    gross_profit_pct: float     # (1 - gross_cost) / gross_cost
    kalshi_fee:       float     # fee on Kalshi leg
    poly_fee:         float     # fee on Polymarket leg
    net_cost:         float     # gross_cost + both fees
    net_profit_pct:   float     # (1 - net_cost) / net_cost — the real edge
    max_contracts:    int       # contracts limited by liquidity on both sides
    max_profit_usd:   float     # max_contracts * net_profit_pct * gross_cost

    # ── Market metadata ──────────────────────────────────────────────
    kalshi_title:      str
    poly_question:     str
    match_score:       float    # LLM-verified confidence score
    close_time:        str      # ISO 8601 from Kalshi (used for timing)
    days_to_resolution: int     # calendar days until resolution
    edge_per_day:       float   # annualized edge velocity

    # ── Execution metadata ───────────────────────────────────────────
    detected_at:      float = field(default_factory=time.time)


# ─────────────────────────────────────────────────────────────────────────────
# Detector
# ─────────────────────────────────────────────────────────────────────────────

class ArbDetector:
    """
    Scans a matched market pair for arbitrage opportunities.

    Checks both arb directions, applies fee math, and returns the
    best opportunity if one exists above the minimum edge threshold.
    """

    def __init__(self, live_mode: bool = False):
        """
        Args:
            live_mode: if True, uses MIN_NET_EDGE_LIVE (stricter).
                       if False, uses MIN_NET_EDGE_PAPER (for paper trading).
        """
        self.live_mode = live_mode
        self.min_edge  = MIN_NET_EDGE_LIVE if live_mode else MIN_NET_EDGE_PAPER
        log.info(
            "ArbDetector initialized (mode=%s, min_edge=%.2f%%)",
            "LIVE" if live_mode else "PAPER", self.min_edge * 100
        )

    def analyze(
        self,
        kalshi_market: NormalizedMarket,
        poly_market:   NormalizedMarket,
        kalshi_book:   NormalizedMarketBook,
        poly_book:     NormalizedMarketBook,
        match_score:   float = 0.0,
        max_contracts: int   = 500,
    ) -> Optional[ArbOpportunity]:
        """
        Check both arb directions for a matched market pair.

        Args:
            kalshi_market:  NormalizedMarket from Kalshi
            poly_market:    NormalizedMarket from Polymarket
            kalshi_book:    NormalizedMarketBook for Kalshi
            poly_book:      NormalizedMarketBook for Polymarket
            match_score:    LLM confidence score (stored on the opportunity)
            max_contracts:  hard cap on position size (additional to liquidity)

        Returns:
            The best ArbOpportunity if one exists, or None.
        """
        # Validate freshness
        if not kalshi_book.is_fresh:
            log.debug("Skipping %s — Kalshi book is stale", kalshi_market.market_id)
            return None
        if not poly_book.is_fresh:
            log.debug("Skipping %s — Poly book is stale", poly_market.market_id)
            return None

        # Check both arb directions
        candidates = []

        # Direction A: Buy YES on Kalshi + Buy NO on Polymarket
        opp_a = self._check_direction(
            kalshi_market=  kalshi_market,
            poly_market=    poly_market,
            kalshi_book=    kalshi_book,
            poly_book=      poly_book,
            kalshi_side=    "yes",
            poly_side=      "no",
            match_score=    match_score,
            max_contracts=  max_contracts,
        )
        if opp_a:
            candidates.append(opp_a)

        # Direction B: Buy NO on Kalshi + Buy YES on Polymarket
        opp_b = self._check_direction(
            kalshi_market=  kalshi_market,
            poly_market=    poly_market,
            kalshi_book=    kalshi_book,
            poly_book=      poly_book,
            kalshi_side=    "no",
            poly_side=      "yes",
            match_score=    match_score,
            max_contracts=  max_contracts,
        )
        if opp_b:
            candidates.append(opp_b)

        if not candidates:
            return None

        # Return the direction with the highest net edge
        best = max(candidates, key=lambda o: o.net_profit_pct)
        log.info(
            "ARB FOUND: %s | K-%s @ %.3f + P-%s @ %.3f = gross %.3f | net %.2f%% | "
            "%d contracts | $%.2f profit | %dd to close",
            kalshi_market.market_id,
            best.kalshi_side, best.kalshi_price,
            best.poly_side, best.poly_price,
            best.gross_cost, best.net_profit_pct * 100,
            best.max_contracts, best.max_profit_usd,
            best.days_to_resolution,
        )
        return best

    def _check_direction(
        self,
        kalshi_market:  NormalizedMarket,
        poly_market:    NormalizedMarket,
        kalshi_book:    NormalizedMarketBook,
        poly_book:      NormalizedMarketBook,
        kalshi_side:    str,
        poly_side:      str,
        match_score:    float,
        max_contracts:  int,
    ) -> Optional[ArbOpportunity]:
        """
        Check one arb direction (e.g. K-YES + P-NO).

        Returns ArbOpportunity if profitable, None otherwise.
        """
        k_book = kalshi_book.yes if kalshi_side == "yes" else kalshi_book.no
        p_book = poly_book.no   if poly_side   == "no"  else poly_book.yes

        # Need liquidity on both sides
        if not k_book.best_ask or not p_book.best_ask:
            return None

        k_price = k_book.best_ask
        p_price = p_book.best_ask

        # ── Gross economics ──────────────────────────────────────────
        gross_cost = k_price + p_price
        if gross_cost >= 1.0:
            return None     # not an arb — costs more than guaranteed payout

        gross_profit_pct = (1.0 - gross_cost) / gross_cost

        # ── Fee math ─────────────────────────────────────────────────
        kalshi_fee = KALSHI_TAKER_FEE * k_price   # Kalshi charges % of leg cost
        poly_fee   = POLY_TAKER_FEE   * p_price   # Poly charges % of leg cost
        net_cost   = gross_cost + kalshi_fee + poly_fee

        if net_cost >= 1.0:
            return None     # fees wipe out the edge

        net_profit_pct = (1.0 - net_cost) / net_cost

        # ── Minimum edge check ───────────────────────────────────────
        if net_profit_pct < self.min_edge:
            log.debug(
                "Edge too thin: %.3f%% < %.3f%% | K-%s @ %.3f + P-%s @ %.3f",
                net_profit_pct * 100, self.min_edge * 100,
                kalshi_side, k_price, poly_side, p_price,
            )
            return None

        # ── Liquidity & slippage ─────────────────────────────────────
        # Start with best-ask liquidity, then walk deeper to find viable size
        k_available = k_book.best_ask_qty
        p_available = p_book.best_ask_qty
        raw_max     = min(k_available, p_available, float(max_contracts))

        if raw_max < 1:
            return None

        # Check slippage at the target size
        dual_slip = check_dual_leg_slippage(k_book, p_book, raw_max)
        if not dual_slip.is_viable:
            # Try smaller size if slippage is the problem
            if dual_slip.kalshi_leg.fillable_contracts >= 1 and dual_slip.poly_leg.fillable_contracts >= 1:
                raw_max = min(dual_slip.kalshi_leg.fillable_contracts,
                              dual_slip.poly_leg.fillable_contracts)
                dual_slip = check_dual_leg_slippage(k_book, p_book, raw_max)
            if not dual_slip.is_viable:
                log.debug("Slippage too high for K-%s+P-%s: %s", kalshi_side, poly_side, dual_slip.as_log_str())
                return None

        contracts = int(dual_slip.viable_contracts)
        if contracts < 1:
            return None

        # ── Capital velocity ─────────────────────────────────────────
        close_time = kalshi_market.close_time
        days       = days_until_close(close_time)
        epd        = edge_per_day(net_profit_pct, days)

        # ── Build opportunity ────────────────────────────────────────
        poly_token_id = (
            poly_market.no_token  if poly_side == "no"
            else poly_market.yes_token
        )

        return ArbOpportunity(
            kalshi_ticker=    kalshi_market.market_id,
            kalshi_side=      kalshi_side,
            kalshi_price=     k_price,
            kalshi_available= k_available,
            poly_market_id=   poly_market.market_id,
            poly_token_id=    poly_token_id,
            poly_side=        poly_side,
            poly_price=       p_price,
            poly_available=   p_available,
            gross_cost=       gross_cost,
            gross_profit_pct= gross_profit_pct,
            kalshi_fee=       kalshi_fee,
            poly_fee=         poly_fee,
            net_cost=         net_cost,
            net_profit_pct=   net_profit_pct,
            max_contracts=    contracts,
            max_profit_usd=   contracts * net_profit_pct * gross_cost,
            kalshi_title=     kalshi_market.title,
            poly_question=    poly_market.title,
            match_score=      match_score,
            close_time=       close_time,
            days_to_resolution= days,
            edge_per_day=     epd,
        )

    # ─────────────────────────────────────────────────────────────────
    # Batch scanning
    # ─────────────────────────────────────────────────────────────────

    def scan_all(
        self,
        matched_pairs: list,
        fetch_kalshi_book,
        fetch_poly_book,
    ) -> list[ArbOpportunity]:
        """
        Scan all matched market pairs and return ranked opportunities.

        Args:
            matched_pairs:     list of MarketMatch objects (from matcher.py)
            fetch_kalshi_book: callable(ticker) → NormalizedMarketBook
            fetch_poly_book:   callable(market) → NormalizedMarketBook

        Returns:
            List of ArbOpportunity sorted by edge_per_day descending.
        """
        opportunities = []
        errors = 0

        for match in matched_pairs:
            try:
                kalshi_book = fetch_kalshi_book(match.kalshi.market_id)
                poly_book   = fetch_poly_book(match.poly)

                opp = self.analyze(
                    kalshi_market=match.kalshi,
                    poly_market=  match.poly,
                    kalshi_book=  kalshi_book,
                    poly_book=    poly_book,
                    match_score=  match.score,
                )
                if opp:
                    opportunities.append(opp)

            except Exception as exc:
                log.warning("Scan error for %s: %s", match.kalshi.market_id, exc)
                errors += 1

        opportunities.sort(key=lambda o: o.edge_per_day, reverse=True)

        log.info(
            "scan_all: %d pairs checked → %d opportunities found (%d errors)",
            len(matched_pairs), len(opportunities), errors,
        )
        return opportunities


# ─────────────────────────────────────────────────────────────────────────────
# Smoke test
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import time
    logging.basicConfig(level=logging.INFO)

    from clients.normalizer import (
        NormalizedMarket, NormalizedMarketBook,
        NormalizedBook, PriceLevel,
    )

    # Scenario 1: Clear arb — K-YES @ 0.44 + P-NO @ 0.49 = 0.93 (7% gross)
    kalshi = NormalizedMarket(
        platform="kalshi", market_id="PRES-2024-DEM",
        title="Will the Democrat win the 2024 presidential election?",
        close_time="2024-11-05T23:59:00Z",
        yes_token="PRES-2024-DEM", no_token="PRES-2024-DEM",
    )
    poly = NormalizedMarket(
        platform="polymarket", market_id="0xabc123",
        title="Will Democrats win the 2024 US presidential election?",
        close_time="2024-11-05T23:59:00Z",
        yes_token="71321045xxx", no_token="52114320xxx",
    )

    kalshi_book = NormalizedMarketBook(
        yes=NormalizedBook(asks=[PriceLevel(0.44, 500), PriceLevel(0.45, 1000)]),
        no= NormalizedBook(asks=[PriceLevel(0.57, 400), PriceLevel(0.58, 800)]),
    )
    poly_book = NormalizedMarketBook(
        yes=NormalizedBook(asks=[PriceLevel(0.47, 300), PriceLevel(0.48, 700)]),
        no= NormalizedBook(asks=[PriceLevel(0.49, 250), PriceLevel(0.50, 600)]),
    )

    detector = ArbDetector(live_mode=False)
    opp = detector.analyze(kalshi, poly, kalshi_book, poly_book, match_score=0.92)

    if opp:
        print(f"\n✓ Opportunity found!")
        print(f"  Direction:       K-{opp.kalshi_side.upper()} + P-{opp.poly_side.upper()}")
        print(f"  Kalshi price:    {opp.kalshi_price:.4f}")
        print(f"  Poly price:      {opp.poly_price:.4f}")
        print(f"  Gross cost:      {opp.gross_cost:.4f}  ({opp.gross_profit_pct:.2%} gross edge)")
        print(f"  Fees:            K={opp.kalshi_fee:.4f} P={opp.poly_fee:.4f}")
        print(f"  Net cost:        {opp.net_cost:.4f}  ({opp.net_profit_pct:.2%} net edge)")
        print(f"  Max contracts:   {opp.max_contracts}")
        print(f"  Max profit:      ${opp.max_profit_usd:.2f}")
        print(f"  Edge/day:        {opp.edge_per_day:.1f}% annualized")
        print(f"  Days to close:   {opp.days_to_resolution}")
    else:
        print("\n✗ No opportunity found")

    # Scenario 2: No arb — both platforms agree (efficient market)
    kalshi_book_eff = NormalizedMarketBook(
        yes=NormalizedBook(asks=[PriceLevel(0.50, 500)]),
        no= NormalizedBook(asks=[PriceLevel(0.51, 400)]),
    )
    poly_book_eff = NormalizedMarketBook(
        yes=NormalizedBook(asks=[PriceLevel(0.50, 300)]),
        no= NormalizedBook(asks=[PriceLevel(0.51, 250)]),
    )

    opp2 = detector.analyze(kalshi, poly, kalshi_book_eff, poly_book_eff, match_score=0.92)
    print(f"\nEfficient market test: {'✓ found (unexpected!)' if opp2 else '✓ correctly found no arb'}")
