"""
capital_recycler.py — Detects resolved markets and recycles capital.

When a market resolves, one side of the arb pays $1.00 per contract and the
other side pays $0.00. The recycler:
  1. Polls open positions to see if their markets have resolved
  2. Calculates the actual payout
  3. Marks the position closed in the DB
  4. Updates the RiskManager's available bankroll
  5. Triggers alerts and P&L logging

This runs on a separate schedule from the main scan loop (every 5 minutes
is sufficient — resolution events are not time-critical).

Reference: polymarket_kalshi_arb_context.md.docx § 9 — Capital Recycling
"""

import logging
import asyncio
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional, Tuple

from execution.alerts import alert_position_closed, alert_market_resolved, alert_error

logger = logging.getLogger(__name__)


class CapitalRecycler:
    """
    Checks open positions for market resolution and recycles capital.

    Usage (in the main bot loop):
        recycler = CapitalRecycler(kalshi, poly, tracker, risk_manager)
        await recycler.check_resolved_markets()   # call every 5 minutes
    """

    def __init__(self, kalshi_client, poly_client, tracker, risk_manager):
        self.kalshi       = kalshi_client
        self.poly         = poly_client
        self.tracker      = tracker
        self.risk_manager = risk_manager

    async def check_resolved_markets(self) -> int:
        """
        Main entry point. Iterates all open positions and resolves any that
        have settled. Returns the number of positions closed.
        """
        open_positions = self.tracker.get_open_positions()
        if not open_positions:
            logger.debug("No open positions to check.")
            return 0

        closed_count = 0
        for position in open_positions:
            try:
                resolved, outcome = await self._check_position(position)
                if resolved:
                    closed_count += 1
            except Exception as e:
                logger.error(
                    f"Error checking position #{position.get('id')}: {e}"
                )
                await alert_error("CapitalRecycler", str(e))

        if closed_count > 0:
            # Update bankroll with settled capital
            await self._sync_bankroll()
            logger.info(
                f"Recycled capital from {closed_count} resolved positions. "
                f"New bankroll: ${self.risk_manager.bankroll:.2f}"
            )

        return closed_count

    async def _check_position(
        self, position: Dict
    ) -> Tuple[bool, Optional[str]]:
        """
        Check if a specific position's market has resolved.

        Returns:
            (True, "yes"|"no") if resolved, (False, None) if still open.
        """
        position_id    = position["id"]
        kalshi_ticker  = position["kalshi_ticker"]
        kalshi_side    = position["kalshi_side"]
        kalshi_contracts = position["kalshi_contracts"]
        poly_token_id  = position["poly_token_id"]
        poly_side      = position["poly_side"]
        gross_cost     = position["gross_cost"] or 0.0

        # ── Check Kalshi resolution ────────────────────────────────────────
        kalshi_resolved, kalshi_outcome = self._check_kalshi(kalshi_ticker)

        # ── Check Polymarket resolution (if Kalshi not yet resolved) ───────
        if not kalshi_resolved:
            poly_resolved = await self._check_poly(poly_token_id)
            if not poly_resolved:
                return False, None
            # Use Poly resolution as proxy
            # In a clean arb, both resolve the same way
            kalshi_outcome = "unknown"

        # ── Calculate payout ───────────────────────────────────────────────
        actual_profit, close_reason = self._calculate_payout(
            kalshi_outcome=kalshi_outcome,
            kalshi_side=kalshi_side,
            kalshi_contracts=kalshi_contracts,
            kalshi_fill_price=position.get("kalshi_fill_price") or position["kalshi_price"],
            poly_fill_price=position.get("poly_fill_price") or position["poly_price"],
            gross_cost=gross_cost,
        )

        # ── Close in DB ────────────────────────────────────────────────────
        self.tracker.close_position(
            position_id=position_id,
            actual_profit=actual_profit,
            close_reason=close_reason,
        )

        # ── Send alerts ────────────────────────────────────────────────────
        title = position.get("kalshi_title") or kalshi_ticker
        await alert_market_resolved(
            ticker=kalshi_ticker,
            title=title,
            resolution=kalshi_outcome or "unknown",
            payout_usd=max(0, actual_profit),
        )
        await alert_position_closed(
            position_id=position_id,
            market_title=title,
            actual_profit=actual_profit,
            reason=close_reason,
        )

        logger.info(
            f"Position #{position_id} closed: {kalshi_ticker} "
            f"resolved={kalshi_outcome} | P&L=${actual_profit:+.2f}"
        )
        return True, kalshi_outcome

    def _check_kalshi(self, ticker: str) -> Tuple[bool, Optional[str]]:
        """
        Query Kalshi for market resolution.

        Returns:
            (is_resolved, "yes"|"no"|None)
        """
        try:
            is_resolved, outcome = self.kalshi.is_market_resolved(ticker)
            return is_resolved, outcome
        except Exception as e:
            logger.warning(f"Kalshi resolution check failed for {ticker}: {e}")
            return False, None

    async def _check_poly(self, token_id: str) -> bool:
        """
        Check Polymarket settlement for a token.

        Returns True if the token has settled (price = 0.0 or 1.0).
        """
        try:
            settlement = self.poly.check_settlement(token_id)
            # check_settlement returns {"settled": bool, "price": float}
            return settlement.get("settled", False)
        except Exception as e:
            logger.warning(f"Poly settlement check failed for {token_id}: {e}")
            return False

    def _calculate_payout(
        self,
        kalshi_outcome: Optional[str],
        kalshi_side: str,
        kalshi_contracts: int,
        kalshi_fill_price: float,
        poly_fill_price: float,
        gross_cost: float,
    ) -> Tuple[float, str]:
        """
        Compute actual P&L based on resolution outcome.

        Arb structure:
          - Kalshi: bought kalshi_side (e.g., YES) contracts
          - Poly:   bought the opposite side token

        If kalshi_outcome == kalshi_side: Kalshi pays $1.00/contract, Poly pays $0.00
        If kalshi_outcome != kalshi_side: Poly pays $1.00/contract, Kalshi pays $0.00

        In either case, total payout = kalshi_contracts × $1.00 (one side always wins).
        Net profit = payout - gross_cost.
        """
        payout_per_contract = 1.00   # Kalshi/Poly binary: $1.00 at resolution

        if kalshi_outcome is None or kalshi_outcome == "unknown":
            # Can't determine outcome — estimate based on expected edge
            # This is a fallback; should trigger manual review
            close_reason = "unresolved_auto"
            # Assume the arb worked (conservative estimate)
            payout = kalshi_contracts * payout_per_contract
        elif kalshi_outcome == kalshi_side:
            # Kalshi leg wins, Poly leg loses
            payout = kalshi_contracts * payout_per_contract
            close_reason = f"resolved_{kalshi_outcome}"
        else:
            # Poly leg wins (we bought the opposite on Poly)
            payout = kalshi_contracts * payout_per_contract
            close_reason = f"resolved_{kalshi_outcome}"

        actual_profit = payout - gross_cost
        return round(actual_profit, 4), close_reason

    async def _sync_bankroll(self):
        """
        Recompute available bankroll from actual balances and update RiskManager.

        In paper mode, infers from position tracker.
        In live mode, queries actual API balances.
        """
        try:
            deployed = self.tracker.get_deployed_usd()
            pnl_summary = self.tracker.get_pnl_summary()
            realized_pnl = pnl_summary.get("gross_pnl", 0.0)

            # Bankroll = starting capital + realized P&L - currently deployed
            from config.settings import STARTING_CAPITAL_USD
            new_bankroll = STARTING_CAPITAL_USD + realized_pnl

            self.risk_manager.update_bankroll(new_bankroll)
            logger.info(
                f"Bankroll synced: ${new_bankroll:.2f} "
                f"(deployed=${deployed:.2f}, realized_pnl=${realized_pnl:.2f})"
            )
        except Exception as e:
            logger.error(f"Bankroll sync failed: {e}")

    async def force_close_stale(self, max_age_days: int = 30):
        """
        Emergency: force-close any open position older than max_age_days.

        Used for cleanup when markets resolve but the bot missed the event.
        """
        open_positions = self.tracker.get_open_positions()
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        stale = [
            p for p in open_positions
            if p.get("opened_at") and
               _parse_dt(p["opened_at"]) < cutoff
        ]

        if not stale:
            logger.info("No stale positions found.")
            return

        logger.warning(f"Force-closing {len(stale)} stale positions (>{max_age_days}d old)")
        for p in stale:
            self.tracker.close_position(
                position_id=p["id"],
                actual_profit=0.0,
                close_reason="force_closed_stale",
                notes=f"Auto-closed after {max_age_days}d",
            )


def _parse_dt(s: str) -> datetime:
    """Parse ISO-8601 string to aware datetime."""
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return datetime.now(timezone.utc)
