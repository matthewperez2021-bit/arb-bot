"""
test_executor.py — Tests for execution engine (paper mode).
"""
import asyncio

from execution.executor import ArbExecutor, LegResult


class _DummyTracker:
    def log_position(self, **_kwargs):
        return 123


class _DummyClient:
    def cancel_order(self, _order_id: str):
        return {}


def test_reconcile_clean_fill_marks_success():
    execu = ArbExecutor(_DummyClient(), _DummyClient(), position_tracker=_DummyTracker(), dry_run=False)

    class _Opp:
        kalshi_ticker = "K1"
        kalshi_price = 0.44
        poly_price = 0.49
        kalshi_side = "yes"
        poly_side = "no"
        net_profit_pct = 0.02

    opp = _Opp()

    k = LegResult(
        platform="kalshi",
        order_id="k1",
        requested_contracts=10,
        filled_contracts=10,
        avg_fill_price=0.44,
        fill_cost_usd=4.4,
        status="filled",
    )
    p = LegResult(
        platform="polymarket",
        order_id="p1",
        requested_contracts=10,
        filled_contracts=10,
        avg_fill_price=0.49,
        fill_cost_usd=4.9,
        status="filled",
    )

    res = asyncio.run(execu._reconcile(opp, k, p, requested=10, t_start=0.0, mode="paper"))
    assert res.success is True
    assert res.contracts_filled == 10
    assert res.kalshi_leg.filled_contracts == 10
    assert res.poly_leg.filled_contracts == 10


def test_reconcile_both_failed_marks_failure():
    execu = ArbExecutor(_DummyClient(), _DummyClient(), position_tracker=None, dry_run=False)

    class _Opp:
        kalshi_ticker = "K1"
        kalshi_price = 0.44
        poly_price = 0.49
        kalshi_side = "yes"
        poly_side = "no"
        net_profit_pct = 0.02

    opp = _Opp()

    k = LegResult(
        platform="kalshi",
        order_id=None,
        requested_contracts=10,
        filled_contracts=0,
        avg_fill_price=0.0,
        fill_cost_usd=0.0,
        status="failed",
        error="boom",
    )
    p = LegResult(
        platform="polymarket",
        order_id=None,
        requested_contracts=10,
        filled_contracts=0,
        avg_fill_price=0.0,
        fill_cost_usd=0.0,
        status="failed",
        error="boom",
    )

    res = asyncio.run(execu._reconcile(opp, k, p, requested=10, t_start=0.0, mode="paper"))
    assert res.success is False
    assert res.contracts_filled == 0
