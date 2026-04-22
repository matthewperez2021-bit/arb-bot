"""
executor.py — Async dual-leg arbitrage execution engine.

Fires both legs of an arb trade concurrently using asyncio.gather(), then
reconciles fills. If one leg partially fills, the NakedExposureManager takes
over to hedge or close at a small loss.

Flow:
  ArbExecutor.execute_arb(opp, contracts)
    ├─ asyncio.gather(_place_kalshi_leg, _place_poly_leg)
    ├─ asyncio.gather(_poll_kalshi_fill, _poll_poly_fill)  ← concurrent polling
    ├─ ExecutionResult built from fills
    └─ On mismatch → NakedExposureManager.handle_naked_leg()
"""

import asyncio
import time
import logging
from dataclasses import dataclass, field
from typing import Optional

from config.settings import (
    ORDER_EXPIRY_SECS,
    SLIPPAGE_TOLERANCE,
    NAKED_EXPOSURE_TIMEOUT_SECS,
    MAX_NAKED_CONTRACTS,
)
from execution.naked_handler import NakedExposureManager, NakedOutcome
from execution.alerts import (
    alert_execution, alert_naked_exposure, alert_naked_resolved,
    alert_execution_failed
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Result types
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class LegResult:
    """Outcome for a single leg of the trade."""
    platform: str                   # "kalshi" | "polymarket"
    order_id: Optional[str]
    requested_contracts: int
    filled_contracts: int
    avg_fill_price: float           # 0.0–1.0
    fill_cost_usd: float
    status: str                     # "filled" | "partial" | "failed" | "cancelled"
    error: Optional[str] = None
    latency_ms: float = 0.0


@dataclass
class ExecutionResult:
    """Full outcome for a dual-leg arb trade."""
    success: bool
    contracts_filled: int           # Min of both legs after reconciliation
    kalshi_leg: Optional[LegResult] = None
    poly_leg: Optional[LegResult] = None
    naked_outcome: Optional[str] = None   # From NakedExposureManager if triggered
    total_cost_usd: float = 0.0
    expected_profit_usd: float = 0.0
    slippage_vs_detected_usd: float = 0.0
    execution_time_ms: float = 0.0
    position_id: Optional[int] = None
    error: Optional[str] = None

    @property
    def is_clean(self) -> bool:
        """True if both legs filled the same number of contracts."""
        if not self.kalshi_leg or not self.poly_leg:
            return False
        return self.kalshi_leg.filled_contracts == self.poly_leg.filled_contracts

    def log_str(self) -> str:
        k = self.kalshi_leg
        p = self.poly_leg
        return (
            f"ExecutionResult(ok={self.success}, contracts={self.contracts_filled}, "
            f"k_filled={k.filled_contracts if k else 0}@{k.avg_fill_price:.3f}, "
            f"p_filled={p.filled_contracts if p else 0}@{p.avg_fill_price:.3f}, "
            f"cost=${self.total_cost_usd:.2f}, profit=${self.expected_profit_usd:.2f}, "
            f"slippage=${self.slippage_vs_detected_usd:.4f}, "
            f"naked={self.naked_outcome}, t={self.execution_time_ms:.0f}ms)"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Poll helpers
# ─────────────────────────────────────────────────────────────────────────────

POLL_INTERVAL_SECS = 0.5
FILL_TIMEOUT_SECS  = 25          # Give each leg 25s to fill before treating as failed


async def _poll_kalshi_fill(kalshi_client, order_id: str,
                             requested: int) -> LegResult:
    """Poll Kalshi until the order is filled, cancelled, or timed out."""
    start = time.monotonic()
    while True:
        elapsed = time.monotonic() - start
        if elapsed > FILL_TIMEOUT_SECS:
            logger.warning(f"[kalshi] fill timeout for order {order_id}")
            return LegResult(
                platform="kalshi", order_id=order_id,
                requested_contracts=requested, filled_contracts=0,
                avg_fill_price=0.0, fill_cost_usd=0.0,
                status="failed", error="timeout",
                latency_ms=(elapsed * 1000),
            )
        try:
            order = kalshi_client.get_order(order_id)
            status = order.get("status", "unknown")
            filled = order.get("contracts_filled", 0)

            if status in ("executed", "filled"):
                # Calculate weighted average fill price from fills
                fills = kalshi_client.get_fills(order_id=order_id)
                if fills:
                    total_qty = sum(f["count"] for f in fills)
                    avg_price = (
                        sum(f["price"] / 100 * f["count"] for f in fills) / total_qty
                        if total_qty else 0
                    )
                    cost = sum(f["price"] / 100 * f["count"] for f in fills)
                else:
                    # Fallback: use order's average fill price field
                    avg_price = order.get("avg_execution_price", 0) / 100
                    cost = avg_price * filled

                return LegResult(
                    platform="kalshi", order_id=order_id,
                    requested_contracts=requested, filled_contracts=filled,
                    avg_fill_price=avg_price, fill_cost_usd=cost,
                    status="filled" if filled == requested else "partial",
                    latency_ms=(time.monotonic() - start) * 1000,
                )

            elif status in ("cancelled", "resting_cancelled"):
                return LegResult(
                    platform="kalshi", order_id=order_id,
                    requested_contracts=requested, filled_contracts=filled,
                    avg_fill_price=0.0, fill_cost_usd=0.0,
                    status="cancelled", error="order cancelled",
                    latency_ms=(time.monotonic() - start) * 1000,
                )

            # Still resting or pending — keep polling
            await asyncio.sleep(POLL_INTERVAL_SECS)

        except Exception as e:
            logger.error(f"[kalshi] poll error for {order_id}: {e}")
            await asyncio.sleep(POLL_INTERVAL_SECS)


async def _poll_poly_fill(poly_client, order_id: str, requested: int,
                          price_per_contract: float) -> LegResult:
    """Poll Polymarket CLOB until the order is filled or timed out."""
    start = time.monotonic()
    while True:
        elapsed = time.monotonic() - start
        if elapsed > FILL_TIMEOUT_SECS:
            logger.warning(f"[poly] fill timeout for order {order_id}")
            return LegResult(
                platform="polymarket", order_id=order_id,
                requested_contracts=requested, filled_contracts=0,
                avg_fill_price=0.0, fill_cost_usd=0.0,
                status="failed", error="timeout",
                latency_ms=(elapsed * 1000),
            )
        try:
            order = poly_client.clob.get_order(order_id)
            status = (order.status if hasattr(order, "status")
                      else order.get("status", "unknown"))

            # Polymarket reports size_matched in USDC
            size_matched = float(
                order.size_matched if hasattr(order, "size_matched")
                else order.get("size_matched", 0)
            )
            # Convert USDC → contracts (each contract costs price_per_contract USD)
            filled = int(size_matched / price_per_contract) if price_per_contract > 0 else 0
            avg_price = price_per_contract  # Limit orders fill at limit or better

            if status in ("MATCHED", "filled"):
                return LegResult(
                    platform="polymarket", order_id=order_id,
                    requested_contracts=requested, filled_contracts=filled,
                    avg_fill_price=avg_price, fill_cost_usd=size_matched,
                    status="filled" if filled >= requested else "partial",
                    latency_ms=(time.monotonic() - start) * 1000,
                )
            elif status in ("CANCELLED", "cancelled"):
                return LegResult(
                    platform="polymarket", order_id=order_id,
                    requested_contracts=requested, filled_contracts=filled,
                    avg_fill_price=avg_price, fill_cost_usd=size_matched,
                    status="cancelled", error="order cancelled",
                    latency_ms=(time.monotonic() - start) * 1000,
                )

            await asyncio.sleep(POLL_INTERVAL_SECS)

        except Exception as e:
            logger.error(f"[poly] poll error for {order_id}: {e}")
            await asyncio.sleep(POLL_INTERVAL_SECS)


# ─────────────────────────────────────────────────────────────────────────────
# Leg placement coroutines
# ─────────────────────────────────────────────────────────────────────────────

async def _place_and_fill_kalshi(kalshi_client, opp, contracts: int) -> LegResult:
    """Place the Kalshi leg and poll for fill."""
    start = time.monotonic()
    try:
        # Convert float price → cents, add slippage tolerance
        raw_price_cents = round(opp.kalshi_price * 100)
        # Accept up to SLIPPAGE_TOLERANCE above detected price
        limit_price_cents = min(99, round((opp.kalshi_price + SLIPPAGE_TOLERANCE) * 100))

        order = kalshi_client.place_order(
            ticker=opp.kalshi_ticker,
            side=opp.kalshi_side,          # "yes" or "no"
            count=contracts,
            price_cents=limit_price_cents,
            order_type="limit",
            expiry_secs=ORDER_EXPIRY_SECS,
        )
        order_id = order.get("order", {}).get("order_id") or order.get("order_id")
        logger.info(f"[kalshi] placed order {order_id}: {contracts}x {opp.kalshi_side} "
                    f"@ {limit_price_cents}¢ on {opp.kalshi_ticker}")
        return await _poll_kalshi_fill(kalshi_client, order_id, contracts)

    except Exception as e:
        elapsed = (time.monotonic() - start) * 1000
        logger.error(f"[kalshi] placement failed: {e}")
        return LegResult(
            platform="kalshi", order_id=None,
            requested_contracts=contracts, filled_contracts=0,
            avg_fill_price=0.0, fill_cost_usd=0.0,
            status="failed", error=str(e), latency_ms=elapsed,
        )


async def _place_and_fill_poly(poly_client, opp, contracts: int) -> LegResult:
    """Place the Polymarket leg and poll for fill."""
    start = time.monotonic()
    try:
        limit_price = min(0.99, opp.poly_price + SLIPPAGE_TOLERANCE)
        size_usd = contracts * limit_price   # Total USDC to spend

        order = poly_client.place_limit_order(
            token_id=opp.poly_token_id,
            side=opp.poly_side,             # "BUY"
            price=limit_price,
            size_usd=size_usd,
        )
        order_id = (order.order_id if hasattr(order, "order_id")
                    else order.get("orderID") or order.get("order_id"))
        logger.info(f"[poly] placed order {order_id}: ${size_usd:.2f} @ {limit_price:.3f} "
                    f"on token {opp.poly_token_id[:10]}...")
        return await _poll_poly_fill(poly_client, order_id, contracts, limit_price)

    except Exception as e:
        elapsed = (time.monotonic() - start) * 1000
        logger.error(f"[poly] placement failed: {e}")
        return LegResult(
            platform="polymarket", order_id=None,
            requested_contracts=contracts, filled_contracts=0,
            avg_fill_price=0.0, fill_cost_usd=0.0,
            status="failed", error=str(e), latency_ms=elapsed,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Main executor
# ─────────────────────────────────────────────────────────────────────────────

class ArbExecutor:
    """
    Fires both legs of an arb trade concurrently and manages the outcome.

    Usage:
        executor = ArbExecutor(kalshi_client, poly_client, position_tracker)
        result = await executor.execute_arb(opp, contracts=10)
    """

    def __init__(self, kalshi_client, poly_client, position_tracker=None,
                 dry_run: bool = False):
        """
        Args:
            kalshi_client:    Authenticated KalshiClient instance.
            poly_client:      Authenticated PolymarketClient instance.
            position_tracker: PositionTracker for DB logging (optional).
            dry_run:          If True, simulate without placing real orders.
        """
        self.kalshi = kalshi_client
        self.poly = poly_client
        self.tracker = position_tracker
        self.dry_run = dry_run
        self.naked_handler = NakedExposureManager(kalshi_client, poly_client)

    async def execute_arb(self, opp, contracts: int,
                          mode: str = "paper") -> ExecutionResult:
        """
        Execute both legs of an arbitrage opportunity concurrently.

        Args:
            opp:       ArbOpportunity from arb_detector.py
            contracts: Number of contracts to trade (after Kelly sizing)
            mode:      "paper" | "live" — used for position log

        Returns:
            ExecutionResult with full fill details
        """
        t_start = time.monotonic()

        if contracts <= 0:
            return ExecutionResult(
                success=False, contracts_filled=0,
                error="contracts must be > 0",
            )

        logger.info(
            f"Executing arb: {opp.kalshi_ticker} | {contracts} contracts | "
            f"K:{opp.kalshi_side}@{opp.kalshi_price:.3f} + "
            f"P:{opp.poly_side}@{opp.poly_price:.3f} | "
            f"net_edge={opp.net_profit_pct:.2%} | mode={mode}"
        )

        # ── Dry run: simulate fills at detected prices ─────────────────────
        if self.dry_run:
            return self._simulate_execution(opp, contracts, t_start)

        # ── Live: fire both legs concurrently ─────────────────────────────
        try:
            k_leg_coro = _place_and_fill_kalshi(self.kalshi, opp, contracts)
            p_leg_coro = _place_and_fill_poly(self.poly, opp, contracts)
            k_result, p_result = await asyncio.gather(k_leg_coro, p_leg_coro)
        except Exception as e:
            logger.error(f"Unexpected gather error: {e}")
            return ExecutionResult(
                success=False, contracts_filled=0,
                error=f"gather failed: {e}",
                execution_time_ms=(time.monotonic() - t_start) * 1000,
            )

        # ── Reconcile fills ────────────────────────────────────────────────
        result = await self._reconcile(opp, k_result, p_result, contracts,
                                       t_start, mode)

        logger.info(result.log_str())
        return result

    async def _reconcile(self, opp, k_leg: LegResult, p_leg: LegResult,
                          requested: int, t_start: float,
                          mode: str) -> ExecutionResult:
        """
        Compare fills, handle mismatches, and build the final ExecutionResult.
        """
        k_filled = k_leg.filled_contracts
        p_filled = p_leg.filled_contracts
        execution_ms = (time.monotonic() - t_start) * 1000

        # ── Both legs failed: nothing to unwind ───────────────────────────
        if k_filled == 0 and p_filled == 0:
            logger.warning("Both legs failed — no position opened.")
            await alert_execution_failed(opp, "both legs failed",
                                         k_leg.error, p_leg.error)
            return ExecutionResult(
                success=False, contracts_filled=0,
                kalshi_leg=k_leg, poly_leg=p_leg,
                error="both legs failed",
                execution_time_ms=execution_ms,
            )

        # ── Mismatch: one leg filled more than the other ───────────────────
        if k_filled != p_filled:
            naked_contracts = abs(k_filled - p_filled)
            which_filled = "kalshi" if k_filled > p_filled else "polymarket"
            logger.warning(
                f"Leg mismatch: kalshi={k_filled}, poly={p_filled}. "
                f"Naked exposure: {naked_contracts} contracts on {which_filled}."
            )
            await alert_naked_exposure(opp, k_filled, p_filled)

            # Cancel the open order on the underfilled leg before hedging
            await self._cancel_open_leg(k_leg, p_leg, k_filled, p_filled)

            # Let NakedExposureManager handle the hedge
            if naked_contracts <= MAX_NAKED_CONTRACTS:
                naked_outcome = await self.naked_handler.handle_naked_leg(
                    opp=opp,
                    naked_contracts=naked_contracts,
                    which_leg_filled=which_filled,
                )
                await alert_naked_resolved(opp, naked_contracts, naked_outcome.value)
            else:
                naked_outcome = NakedOutcome.HOLDING_NAKED
                logger.error(
                    f"Naked exposure {naked_contracts} exceeds MAX_NAKED_CONTRACTS "
                    f"({MAX_NAKED_CONTRACTS}). Manual intervention required."
                )

            # Contracts considered "clean" = the matched portion
            clean_contracts = min(k_filled, p_filled)
            total_cost = (
                clean_contracts * k_leg.avg_fill_price +
                clean_contracts * p_leg.avg_fill_price
            )
            expected_profit = max(0, (1.0 - total_cost / clean_contracts)
                                  * clean_contracts) if clean_contracts > 0 else 0

            result = ExecutionResult(
                success=(clean_contracts > 0),
                contracts_filled=clean_contracts,
                kalshi_leg=k_leg, poly_leg=p_leg,
                naked_outcome=naked_outcome.value,
                total_cost_usd=total_cost,
                expected_profit_usd=expected_profit,
                execution_time_ms=execution_ms,
            )

        else:
            # ── Clean fill: both legs matched ─────────────────────────────
            contracts_filled = min(k_filled, p_filled)
            total_cost = k_leg.fill_cost_usd + p_leg.fill_cost_usd
            detected_cost = (opp.kalshi_price + opp.poly_price) * contracts_filled
            slippage_vs_detected = total_cost - detected_cost
            expected_profit = opp.net_profit_pct * total_cost

            result = ExecutionResult(
                success=True,
                contracts_filled=contracts_filled,
                kalshi_leg=k_leg, poly_leg=p_leg,
                total_cost_usd=total_cost,
                expected_profit_usd=expected_profit,
                slippage_vs_detected_usd=slippage_vs_detected,
                execution_time_ms=execution_ms,
            )
            await alert_execution(opp, contracts_filled)

        # ── Log to DB ──────────────────────────────────────────────────────
        if self.tracker and result.contracts_filled > 0:
            try:
                result.position_id = self.tracker.log_position(
                    opp=opp,
                    contracts=result.contracts_filled,
                    k_avg_price=k_leg.avg_fill_price,
                    p_avg_price=p_leg.avg_fill_price,
                    total_cost=result.total_cost_usd,
                    mode=mode,
                )
            except Exception as e:
                logger.error(f"Failed to log position to DB: {e}")

        return result

    async def _cancel_open_leg(self, k_leg: LegResult, p_leg: LegResult,
                                k_filled: int, p_filled: int):
        """Cancel the resting portion of the underfilled leg."""
        try:
            if k_filled < p_filled and k_leg.order_id:
                # Kalshi underfilled — cancel any remaining resting Kalshi order
                # (order may have already expired, so swallow errors)
                self.kalshi.cancel_order(k_leg.order_id)
                logger.info(f"Cancelled underfilled Kalshi order {k_leg.order_id}")
            elif p_filled < k_filled and p_leg.order_id:
                self.poly.cancel_order(p_leg.order_id)
                logger.info(f"Cancelled underfilled Poly order {p_leg.order_id}")
        except Exception as e:
            logger.warning(f"Cancel attempt failed (may have already expired): {e}")

    def _simulate_execution(self, opp, contracts: int,
                             t_start: float) -> ExecutionResult:
        """Paper trading: assume fills at exactly the detected prices."""
        k_cost = opp.kalshi_price * contracts
        p_cost = opp.poly_price * contracts
        total_cost = k_cost + p_cost
        expected_profit = opp.net_profit_pct * total_cost

        k_leg = LegResult(
            platform="kalshi", order_id="SIM",
            requested_contracts=contracts, filled_contracts=contracts,
            avg_fill_price=opp.kalshi_price, fill_cost_usd=k_cost,
            status="filled", latency_ms=0.0,
        )
        p_leg = LegResult(
            platform="polymarket", order_id="SIM",
            requested_contracts=contracts, filled_contracts=contracts,
            avg_fill_price=opp.poly_price, fill_cost_usd=p_cost,
            status="filled", latency_ms=0.0,
        )
        logger.info(
            f"[DRY RUN] Simulated fill: {contracts} contracts @ "
            f"${total_cost:.2f} | expected profit ${expected_profit:.2f}"
        )
        return ExecutionResult(
            success=True, contracts_filled=contracts,
            kalshi_leg=k_leg, poly_leg=p_leg,
            total_cost_usd=total_cost,
            expected_profit_usd=expected_profit,
            slippage_vs_detected_usd=0.0,
            execution_time_ms=(time.monotonic() - t_start) * 1000,
        )

    async def cancel_all(self):
        """Emergency: cancel all open orders on both platforms."""
        logger.warning("Cancelling all open orders on both platforms.")
        try:
            open_orders = self.kalshi.get_open_orders()
            for order in open_orders:
                try:
                    self.kalshi.cancel_order(order["order_id"])
                except Exception as e:
                    logger.error(f"Kalshi cancel {order['order_id']}: {e}")
        except Exception as e:
            logger.error(f"Kalshi get_open_orders failed: {e}")

        try:
            self.poly.cancel_all_orders()
        except Exception as e:
            logger.error(f"Poly cancel_all_orders failed: {e}")
