"""
test_detector.py — Unit tests for arb detection and slippage calculation.
"""
from clients.normalizer import (
    NormalizedMarket,
    NormalizedMarketBook,
    NormalizedBook,
    PriceLevel,
)
from detection.arb_detector import ArbDetector


def _mk_market(platform: str, market_id: str, title: str) -> NormalizedMarket:
    return NormalizedMarket(
        platform=platform,
        market_id=market_id,
        title=title,
        close_time="2099-01-01T00:00:00Z",
        yes_token=market_id + "_Y",
        no_token=market_id + "_N",
    )


def test_detector_finds_clear_arb_in_one_direction():
    # K-YES @ 0.44 + P-NO @ 0.49 = 0.93 gross (should be an arb even after fees in paper mode)
    kalshi = _mk_market("kalshi", "K_TICK", "Test market")
    poly = _mk_market("polymarket", "P_ID", "Test market")

    kalshi_book = NormalizedMarketBook(
        yes=NormalizedBook(asks=[PriceLevel(0.44, 500)]),
        no=NormalizedBook(asks=[PriceLevel(0.57, 500)]),
    )
    poly_book = NormalizedMarketBook(
        yes=NormalizedBook(asks=[PriceLevel(0.51, 500)]),
        no=NormalizedBook(asks=[PriceLevel(0.49, 500)]),
    )

    d = ArbDetector(live_mode=False)
    opp = d.analyze(kalshi, poly, kalshi_book, poly_book, match_score=0.9)
    assert opp is not None
    assert opp.gross_cost < 1.0
    assert opp.net_profit_pct > 0.0
    assert opp.kalshi_side in ("yes", "no")
    assert opp.poly_side in ("yes", "no")


def test_detector_rejects_when_gross_cost_ge_1():
    kalshi = _mk_market("kalshi", "K_TICK", "Test market")
    poly = _mk_market("polymarket", "P_ID", "Test market")

    kalshi_book = NormalizedMarketBook(
        yes=NormalizedBook(asks=[PriceLevel(0.55, 500)]),
        no=NormalizedBook(asks=[PriceLevel(0.55, 500)]),
    )
    poly_book = NormalizedMarketBook(
        yes=NormalizedBook(asks=[PriceLevel(0.50, 500)]),
        no=NormalizedBook(asks=[PriceLevel(0.50, 500)]),
    )

    d = ArbDetector(live_mode=False)
    opp = d.analyze(kalshi, poly, kalshi_book, poly_book, match_score=0.9)
    assert opp is None
