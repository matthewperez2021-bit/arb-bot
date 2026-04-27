from clients.normalizer import (
    NormalizedMarket,
    NormalizedMarketBook,
    NormalizedBook,
    PriceLevel,
)
from risk.risk_manager import RiskManager


class _Opp:
    kalshi_ticker = "K1"
    kalshi_price = 0.44
    poly_price = 0.49
    net_profit_pct = 0.03
    match_score = 0.9
    days_to_resolution = 3
    edge_per_day = 3.0


def _mk_books():
    k = NormalizedMarketBook(
        yes=NormalizedBook(asks=[PriceLevel(0.44, 100)]),
        no=NormalizedBook(asks=[PriceLevel(0.56, 100)]),
    )
    p = NormalizedMarketBook(
        yes=NormalizedBook(asks=[PriceLevel(0.49, 100)]),
        no=NormalizedBook(asks=[PriceLevel(0.51, 100)]),
    )
    return k, p


def test_risk_rejects_thin_edge():
    rm = RiskManager(bankroll=1000.0, live_mode=False)
    opp = _Opp()
    opp.net_profit_pct = 0.0
    ok, reason = rm.should_trade(opp, current_deployed=0.0)
    assert ok is False
    assert "edge" in reason.lower()


def test_risk_approves_basic_case_and_sizes():
    rm = RiskManager(bankroll=1000.0, live_mode=False)
    opp = _Opp()
    decision = rm.evaluate(opp, current_deployed=0.0)
    assert decision.approved is True
    assert decision.recommended_contracts >= 1


def test_risk_rejects_stale_books_when_provided():
    rm = RiskManager(bankroll=1000.0, live_mode=False)
    opp = _Opp()
    k, p = _mk_books()
    # Force stale by rewinding timestamps far into past
    k.yes.timestamp = 0.0
    k.no.timestamp = 0.0
    ok, reason = rm.should_trade(opp, current_deployed=0.0, k_book=k, p_book=p)
    assert ok is False
    assert "stale" in reason.lower()

