"""
naked_handler.py — Detects and resolves naked (unhedged) leg exposure.

When one leg of an arb fills and the other doesn't, we're "naked":
holding a directional position that moves with the market instead of being
delta-neutral. The goal is to get flat as quickly as possible.

Strategy priority order:
  1. Try to fill the missing leg at LATE_FILL_PREMIUM above detected price.
     If the market has moved only slightly, this closes the arb at lower profit
     but still profits.
  2. If that fails (market moved too far, no liquidity), close the filled leg
     at a small loss — better to lock in a small loss than hold naked risk.
  3. If even closing fails within TIMEOUT_SECS, flag for manual intervention
     (holding_naked) and alert loudly.

Reference: polymarket_kalshi_arb_context.md.docx § 8 — Naked Exposure Handling
"""

import asyncio
import time
import logging
from enum import Enum
from typing import Optional

from config.settings import (
    NAKED_EXPOSURE_TIMEOUT_SECS,
    LATE_FILL_PREMIUM,
    ORDER_EXPIRY_SECS,
    SLIPPAGE_TOLERANCE,
)

logger = logging.getLogger(__name__)


class NakedOutcome(Enum):
    FILLED_LATE     = "filled_late"      # Missing leg filled at premium — still profitable
    CLOSED_AT_LOSS  = "closed_at_loss"   # Filled leg sold — small loss locked in
    HOLDING_NAKED   = "holding_naked"    # Neither worked — manual intervention needed
    NO_ACTION       = "no_action"        # naked_contracts == 0, nothing to do


class NakedExposureManager:
    """
    Resolves naked exposure after a leg mismatch.

    Designed to be awaited inside executor._reconcile() after detecting that
    k_filled != p_filled.

    Usage:
        handler = NakedExposureManager(kalshi_client, poly_client)
        outcome = await handler.handle_naked_leg(
            opp=opp,
            naked_contracts=3,
            which_leg_filled="kalshi",   # The leg that succeeded
        )
    """

    def __init__(self, kalshi_client, poly_client):
        self.kalshi = kalshi_client
        self.poly   = poly_client

    async def handle_naked_leg(
        self,
        opp,
        naked_contracts: int,
        which_leg_filled: str,         # "kalshi" | "polymarket"
        timeout_secs: float = NAKED_EXPOSURE_TIMEOUT_SECS,
    ) -> NakedOutcome:
        """
        Attempt to resolve naked exposure within timeout_secs.

        Args:
            opp:               Original ArbOpportunity — contains tickers, sides, prices
            naked_contracts:   Number of contracts that are unhedged
            which_leg_filled:  Which platform filled — the other one needs to be hedged
            timeout_secs:      Hard deadline before giving up (default 60s)

        Returns:
            NakedOutcome enum value
        """
        if naked_contracts <= 0:
            return NakedOutcome.NO_ACTION

        deadline = time.monotonic() + timeout_secs
        which_missing = "polymarket" if which_leg_filled == "kalshi" else "kalshi"

        logger.warning(
            f"NAKED EXPOSURE: {naked_contracts} contracts — "
            f"{which_leg_filled} filled, {which_missing} missing. "
            f"Deadline: {timeout_secs:.0f}s"
        )

        # ── Attempt 1: Fill the missing leg at a premium ──────────────────
        remaining = deadline - time.monotonic()
        if remaining > 5:  # Need at least 5s to attempt a fill
            outcome = await self._try_fill_late(
                opp=opp,
                naked_contracts=naked_contracts,
                which_missing=which_missing,
                deadline=deadline,
            )
            if outcome == NakedOutcome.FILLED_LATE:
                logger.info(f"Naked exposure resolved: filled late ({naked_contracts} contracts)")
                return outcome

        # ── Attempt 2: Close the filled leg at market ─────────────────────
        remaining = deadline - time.monotonic()
        if remaining > 2:
            outcome = await self._close_filled_leg(
                opp=opp,
                naked_contracts=naked_contracts,
                which_leg_filled=which_leg_filled,
                deadline=deadline,
            )
            if outcome == NakedOutcome.CLOSED_AT_LOSS:
                logger.info(f"Naked exposure resolved: closed filled leg at loss")
                return outcome

        # ── Fallthrough: manual intervention required ─────────────────────
        logger.error(
            f"NAKED EXPOSURE UNRESOLVED after {timeout_secs:.0f}s. "
            f"Platform: {which_leg_filled}, contracts: {naked_contracts}. "
            f"Market: {opp.kalshi_ticker}. MANUAL INTERVENTION REQUIRED."
        )
        return NakedOutcome.HOLDING_NAKED

    async def _try_fill_late(
        self,
        opp,
        naked_contracts: int,
        which_missing: str,
        deadline: float,
    ) -> NakedOutcome:
        """
        Attempt to fill the missing leg at LATE_FILL_PREMIUM above the
        originally detected price.

        We use a limit order with a higher price to jump the queue, but
        still within reason. If the market has moved more than LATE_FILL_PREMIUM,
        we'll fail here and fall through to closing the other leg.
        """
        try:
            if which_missing == "kalshi":
                # Kalshi leg is missing — place Kalshi order at premium
                base_price = opp.kalshi_price
                premium_price = min(0.98, base_price + LATE_FILL_PREMIUM)
                limit_cents = round(premium_price * 100)

                logger.info(
                    f"[naked] Attempting late fill on Kalshi: "
                    f"{naked_contracts}x {opp.kalshi_side} @ {limit_cents}¢ "
                    f"(premium: +{LATE_FILL_PREMIUM*100:.1f}¢)"
                )
                order = self.kalshi.place_order(
                    ticker=opp.kalshi_ticker,
                    side=opp.kalshi_side,
                    count=naked_contracts,
                    price_cents=limit_cents,
                    order_type="limit",
                    expiry_secs=min(ORDER_EXPIRY_SECS,
                                   max(5, int(deadline - time.monotonic()))),
                )
                order_id = (order.get("order", {}).get("order_id")
                            or order.get("order_id"))
                filled = await self._wait_kalshi_fill(
                    order_id, naked_contracts, deadline
                )

            else:
                # Polymarket leg is missing
                base_price = opp.poly_price
                premium_price = min(0.99, base_price + LATE_FILL_PREMIUM)
                size_usd = naked_contracts * premium_price

                logger.info(
                    f"[naked] Attempting late fill on Poly: "
                    f"${size_usd:.2f} @ {premium_price:.3f} "
                    f"(premium: +{LATE_FILL_PREMIUM*100:.1f}¢)"
                )
                order = self.poly.place_limit_order(
                    token_id=opp.poly_token_id,
                    side=opp.poly_side,
                    price=premium_price,
                    size_usd=size_usd,
                )
                order_id = (order.order_id if hasattr(order, "order_id")
                            else order.get("orderID") or order.get("order_id"))
                filled = await self._wait_poly_fill(
                    order_id, naked_contracts, base_price, deadline
                )

            if filled >= naked_contracts:
                return NakedOutcome.FILLED_LATE
            elif filled > 0:
                logger.warning(
                    f"[naked] Partial late fill: {filled}/{naked_contracts}. "
                    f"Still naked on {naked_contracts - filled} contracts."
                )
                # Partial is better than nothing — report as filled_late
                # (caller will see the mismatch and handle remaining)
                return NakedOutcome.FILLED_LATE
            else:
                logger.warning("[naked] Late fill failed — no fill received.")
                return NakedOutcome.HOLDING_NAKED

        except Exception as e:
            logger.error(f"[naked] _try_fill_late exception: {e}")
            return NakedOutcome.HOLDING_NAKED

    async def _close_filled_leg(
        self,
        opp,
        naked_contracts: int,
        which_leg_filled: str,
        deadline: float,
    ) -> NakedOutcome:
        """
        Sell/close the leg that successfully filled to get flat.

        On Kalshi: place a "sell" order (opposite side, same ticker).
        On Polymarket: place a SELL limit order on the token we hold.

        We accept up to LATE_FILL_PREMIUM below current market as a bid
        to encourage fast execution — this is a controlled loss.
        """
        try:
            if which_leg_filled == "kalshi":
                # We hold Kalshi YES (or NO). Sell it at market minus tolerance.
                # Selling YES = buying NO at (1 - yes_bid) ≈ market price for NO.
                # Kalshi API: to close a YES position, place an order for the
                # opposite side.
                close_side = "no" if opp.kalshi_side == "yes" else "yes"
                # Bid slightly above best bid to get filled quickly
                close_price_cents = max(
                    1, round((opp.kalshi_price - LATE_FILL_PREMIUM) * 100)
                )
                logger.info(
                    f"[naked] Closing Kalshi {opp.kalshi_side} position: "
                    f"{naked_contracts} contracts, placing {close_side} @ "
                    f"{close_price_cents}¢"
                )
                order = self.kalshi.place_order(
                    ticker=opp.kalshi_ticker,
                    side=close_side,
                    count=naked_contracts,
                    price_cents=close_price_cents,
                    order_type="limit",
                    expiry_secs=min(ORDER_EXPIRY_SECS,
                                   max(5, int(deadline - time.monotonic()))),
                )
                order_id = (order.get("order", {}).get("order_id")
                            or order.get("order_id"))
                filled = await self._wait_kalshi_fill(
                    order_id, naked_contracts, deadline
                )

            else:
                # We hold a Poly token. Place a SELL limit at market-minus-tolerance.
                close_price = max(0.01, opp.poly_price - LATE_FILL_PREMIUM)
                size_usd = naked_contracts * close_price

                logger.info(
                    f"[naked] Closing Poly position: "
                    f"{naked_contracts} contracts @ {close_price:.3f} "
                    f"(loss of ~${LATE_FILL_PREMIUM * naked_contracts:.2f})"
                )
                order = self.poly.place_limit_order(
                    token_id=opp.poly_token_id,
                    side="SELL",
                    price=close_price,
                    size_usd=size_usd,
                )
                order_id = (order.order_id if hasattr(order, "order_id")
                            else order.get("orderID") or order.get("order_id"))
                filled = await self._wait_poly_fill(
                    order_id, naked_contracts, close_price, deadline
                )

            if filled >= naked_contracts:
                loss_usd = LATE_FILL_PREMIUM * naked_contracts
                logger.info(
                    f"[naked] Closed filled leg. "
                    f"Estimated loss: ${loss_usd:.2f}"
                )
                return NakedOutcome.CLOSED_AT_LOSS
            else:
                logger.warning(
                    f"[naked] Close only partially filled ({filled}/{naked_contracts})"
                )
                return NakedOutcome.HOLDING_NAKED

        except Exception as e:
            logger.error(f"[naked] _close_filled_leg exception: {e}")
            return NakedOutcome.HOLDING_NAKED

    # ── Fill polling helpers ──────────────────────────────────────────────────

    async def _wait_kalshi_fill(
        self,
        order_id: str,
        requested: int,
        deadline: float,
        poll_interval: float = 0.5,
    ) -> int:
        """Poll Kalshi order until filled or deadline. Returns contracts filled."""
        while time.monotonic() < deadline:
            try:
                order = self.kalshi.get_order(order_id)
                status = order.get("status", "unknown")
                filled = order.get("contracts_filled", 0)

                if status in ("executed", "filled"):
                    return filled
                elif status in ("cancelled", "resting_cancelled", "expired"):
                    logger.warning(f"[naked] Kalshi order {order_id} is {status}")
                    return filled  # Return partial fill if any

                await asyncio.sleep(poll_interval)
            except Exception as e:
                logger.error(f"[naked] Kalshi poll {order_id}: {e}")
                await asyncio.sleep(poll_interval)

        # Deadline hit — cancel and return what was filled
        try:
            self.kalshi.cancel_order(order_id)
        except Exception:
            pass
        try:
            order = self.kalshi.get_order(order_id)
            return order.get("contracts_filled", 0)
        except Exception:
            return 0

    async def _wait_poly_fill(
        self,
        order_id: str,
        requested: int,
        price_per_contract: float,
        deadline: float,
        poll_interval: float = 0.5,
    ) -> int:
        """Poll Polymarket order until filled or deadline. Returns contracts filled."""
        while time.monotonic() < deadline:
            try:
                order = self.poly.clob.get_order(order_id)
                status = (order.status if hasattr(order, "status")
                          else order.get("status", "unknown"))
                size_matched = float(
                    order.size_matched if hasattr(order, "size_matched")
                    else order.get("size_matched", 0)
                )
                filled = int(size_matched / price_per_contract) if price_per_contract > 0 else 0

                if status in ("MATCHED", "filled"):
                    return filled
                elif status in ("CANCELLED", "cancelled"):
                    return filled

                await asyncio.sleep(poll_interval)
            except Exception as e:
                logger.error(f"[naked] Poly poll {order_id}: {e}")
                await asyncio.sleep(poll_interval)

        # Deadline — cancel and return partial
        try:
            self.poly.cancel_order(order_id)
        except Exception:
            pass
        try:
            order = self.poly.clob.get_order(order_id)
            size_matched = float(
                order.size_matched if hasattr(order, "size_matched")
                else order.get("size_matched", 0)
            )
            return int(size_matched / price_per_contract) if price_per_contract > 0 else 0
        except Exception:
            return 0
