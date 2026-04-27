#!/usr/bin/env python3
"""
paper_trade_smoke.py — Offline smoke test for the full pipeline.

Why:
  - Lets you validate the plumbing (matching → detection → risk → execution → tracking)
    without real API keys.
  - Useful in CI and as a quick sanity check after refactors.

Usage:
  python scripts/paper_trade_smoke.py
"""

import asyncio
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from clients.normalizer import (
    NormalizedMarket,
    NormalizedMarketBook,
    NormalizedBook,
    PriceLevel,
)
from matching.matcher import MarketMatcher
from detection.arb_detector import ArbDetector
from risk.risk_manager import RiskManager
from execution.executor import ArbExecutor
from tracking.position_tracker import PositionTracker


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


class _NoopClient:
    """Executor needs cancel methods in shutdown paths."""

    def cancel_order(self, _order_id: str):
        return {}

    def cancel_all_orders(self):
        return {}


def _fake_books() -> tuple[NormalizedMarketBook, NormalizedMarketBook]:
    # Ensure an arb exists: K-YES 0.44 + P-NO 0.49 = 0.93
    kalshi_book = NormalizedMarketBook(
        yes=NormalizedBook(asks=[PriceLevel(0.44, 100)]),
        no=NormalizedBook(asks=[PriceLevel(0.57, 100)]),
    )
    poly_book = NormalizedMarketBook(
        yes=NormalizedBook(asks=[PriceLevel(0.51, 100)]),
        no=NormalizedBook(asks=[PriceLevel(0.49, 100)]),
    )
    return kalshi_book, poly_book


async def main():
    os.makedirs("data", exist_ok=True)

    kalshi = NormalizedMarket(
        platform="kalshi",
        market_id="K_TEST",
        title="Will the Democrat win the 2030 presidential election?",
        # Keep close_time near-term so RiskManager duration gate passes.
        close_time="2026-04-29T23:59:00Z",
        yes_token="K_TEST",
        no_token="K_TEST",
    )
    poly = NormalizedMarket(
        platform="polymarket",
        market_id="P_TEST",
        title="Will Democrats win the 2030 US presidential election?",
        close_time="2026-04-29T23:59:00Z",
        yes_token="YES",
        no_token="NO",
    )

    matcher = MarketMatcher()
    detector = ArbDetector(live_mode=False)
    risk = RiskManager(bankroll=1000.0, live_mode=False)
    tracker = PositionTracker()
    executor = ArbExecutor(_NoopClient(), _NoopClient(), position_tracker=tracker, dry_run=True)

    tracker.start_session(mode="paper_smoke")

    # Matching
    matches = matcher.find_matches([kalshi], [poly], threshold=0.0)
    assert matches, "no matches found"

    # Detection
    k_book, p_book = _fake_books()
    opp = detector.analyze(kalshi, poly, k_book, p_book, match_score=0.9)
    assert opp is not None, "expected an opportunity"

    # Risk + execution
    decision = risk.evaluate(opp, current_deployed=0.0)
    assert decision.approved

    res = await executor.execute_arb(opp, decision.recommended_contracts, mode="paper_smoke")
    assert res.success

    summary = tracker.get_pnl_summary(mode="paper_smoke")
    logging.info("Smoke summary: %s", summary)
    tracker.close()


if __name__ == "__main__":
    asyncio.run(main())

