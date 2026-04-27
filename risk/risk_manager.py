"""
risk_manager.py — Pre-trade risk gate and position sizing.

Every ArbOpportunity must pass through RiskManager.should_trade() before
the executor fires. This is the last line of defence before real money moves.

Check hierarchy (in order — first failure short-circuits):
  1. Edge threshold         — net_profit_pct >= MIN_NET_EDGE (mode-dependent)
  2. Match confidence       — score >= MATCH_LIVE_THRESHOLD for live trading
  3. Duration filter        — days_to_resolution in [MIN_DAYS, MAX_DURATION_DAYS]
  4. Single position cap    — cost_per_trade <= MAX_SINGLE_POSITION_USD
  5. Total deployed cap     — current_deployed + cost <= MAX_TOTAL_DEPLOYED_USD
  6. Book freshness         — both books are < MAX_BOOK_AGE_SECS old
  7. Minimum liquidity      — enough contracts available on both sides

kelly_size() implements Half-Kelly to size positions conservatively. The
full Kelly formula is theoretically optimal but causes devastating drawdowns
in practice — Half-Kelly is the production standard.

Reference: polymarket_kalshi_arb_context.md.docx § 11 — Risk Management
"""

import logging
from dataclasses import dataclass
from typing import Tuple, Optional

from config.settings import (
    MIN_NET_EDGE_PAPER,
    MIN_NET_EDGE_LIVE,
    MATCH_TRADE_THRESHOLD,
    MATCH_LIVE_THRESHOLD,
    MAX_SINGLE_POSITION_USD,
    MAX_TOTAL_DEPLOYED_USD,
    MAX_DURATION_DAYS,
    KELLY_FRACTION,
    MAX_BOOK_AGE_SECS,
    STARTING_CAPITAL_USD,
)

logger = logging.getLogger(__name__)

# Minimum duration — avoid markets resolving in < 1 hour (execution risk too high)
MIN_DURATION_HOURS = 1.0

# Minimum contracts that must be available on both books
MIN_LIQUIDITY_CONTRACTS = 5


@dataclass
class RiskDecision:
    """Structured output from should_trade()."""
    approved: bool
    reason: str                      # Human-readable pass/fail explanation
    recommended_contracts: int = 0   # Kelly-sized recommendation (0 if rejected)
    max_contracts: int = 0           # Hard cap from capital limits


class RiskManager:
    """
    Pre-trade risk gate with Kelly-based position sizing.

    Usage:
        rm = RiskManager(bankroll=1000.0, live_mode=False)
        decision = rm.evaluate(opp, current_deployed=150.0)
        if decision.approved:
            contracts = decision.recommended_contracts
            result = await executor.execute_arb(opp, contracts)
    """

    def __init__(self, bankroll: float = STARTING_CAPITAL_USD,
                 live_mode: bool = False):
        """
        Args:
            bankroll:  Total capital available (updates as positions open/close).
            live_mode: If True, applies stricter thresholds.
        """
        self.bankroll  = bankroll
        self.live_mode = live_mode
        self.min_edge  = MIN_NET_EDGE_LIVE if live_mode else MIN_NET_EDGE_PAPER
        self.min_match = MATCH_LIVE_THRESHOLD if live_mode else MATCH_TRADE_THRESHOLD

    def update_bankroll(self, new_bankroll: float):
        """Update bankroll after trades are settled — called by capital recycler."""
        self.bankroll = new_bankroll
        logger.debug(f"Bankroll updated to ${new_bankroll:.2f}")

    # ── Primary interface ─────────────────────────────────────────────────────

    def evaluate(self, opp, current_deployed: float,
                 k_book=None, p_book=None) -> RiskDecision:
        """
        Full risk evaluation. Returns RiskDecision with approved flag and sizing.

        Args:
            opp:               ArbOpportunity from arb_detector.py
            current_deployed:  Total USD currently deployed in open positions
            k_book:            Optional NormalizedMarketBook for freshness check
            p_book:            Optional NormalizedMarketBook for freshness check
        """
        # Legacy interface: should_trade() called directly in some places
        approved, reason = self.should_trade(opp, current_deployed, k_book, p_book)
        if not approved:
            return RiskDecision(approved=False, reason=reason)

        # Sizing
        max_contracts = self._capital_capped_contracts(opp, current_deployed)
        kelly_contracts = self.kelly_size(
            edge=opp.net_profit_pct,
            cost_per_contract=opp.kalshi_price + opp.poly_price,
            bankroll=self.bankroll - current_deployed,
        )
        recommended = min(kelly_contracts, max_contracts)
        recommended = max(1, recommended)  # Always trade at least 1 if approved

        return RiskDecision(
            approved=True,
            reason="all checks passed",
            recommended_contracts=recommended,
            max_contracts=max_contracts,
        )

    def should_trade(self, opp, current_deployed: float,
                     k_book=None, p_book=None) -> Tuple[bool, str]:
        """
        Gate function. Returns (True, "all checks passed") or (False, reason).

        Checks in order — first failure returns immediately.
        """
        checks = [
            self._check_edge(opp),
            self._check_match_score(opp),
            self._check_duration(opp),
            self._check_single_position_cap(opp),
            self._check_total_deployed_cap(opp, current_deployed),
            self._check_book_freshness(k_book, p_book),
            self._check_liquidity(opp, k_book, p_book),
        ]

        for passed, reason in checks:
            if not passed:
                logger.info(f"Risk rejected ({opp.kalshi_ticker}): {reason}")
                return False, reason

        logger.info(
            f"Risk approved: {opp.kalshi_ticker} | "
            f"edge={opp.net_profit_pct:.2%} | "
            f"EPD={opp.edge_per_day:.1f}% | "
            f"days={opp.days_to_resolution}"
        )
        return True, "all checks passed"

    # ── Position sizing ────────────────────────────────────────────────────────

    def kelly_size(self, edge: float, cost_per_contract: float,
                   bankroll: float) -> int:
        """
        Half-Kelly position sizing.

        Full Kelly formula for binary outcomes (each contract wins $1.00 or $0.00):
            f* = (b*p - q) / b
        where:
            b = (1 - cost) / cost   = payout-to-stake ratio (like odds)
            p = implied win prob    = 1 - cost_per_contract  (we win if it resolves)
            q = 1 - p               = implied loss prob

        Since in arb we always win (assuming market resolves correctly), we
        simplify to:
            f* = edge / (1 - gross_cost)
        and apply KELLY_FRACTION (0.5 = Half-Kelly) for safety.

        Args:
            edge:               Net profit as fraction (e.g. 0.03 for 3%)
            cost_per_contract:  Total cost per contract (kalshi + poly price)
            bankroll:           Available capital (total bankroll minus deployed)

        Returns:
            Number of contracts to trade (minimum 1, after Half-Kelly)
        """
        if cost_per_contract <= 0 or cost_per_contract >= 1.0:
            logger.warning(f"Invalid cost_per_contract={cost_per_contract}")
            return 1

        if bankroll <= 0:
            return 0

        if edge <= 0:
            return 0

        # Payout ratio: for every dollar spent, we get (1/cost) back
        payout_ratio = (1.0 - cost_per_contract) / cost_per_contract

        # Win probability (near-certainty for true arb, but respect edge)
        win_prob = (1 + edge) / (1 / cost_per_contract)
        win_prob = min(win_prob, 0.999)

        # Full Kelly fraction of bankroll
        loss_prob = 1.0 - win_prob
        kelly_fraction = (payout_ratio * win_prob - loss_prob) / payout_ratio
        kelly_fraction = max(0.0, kelly_fraction)

        # Apply Half-Kelly and cap at MAX_SINGLE_POSITION_USD
        scaled_fraction = kelly_fraction * KELLY_FRACTION
        position_usd = min(
            scaled_fraction * bankroll,
            MAX_SINGLE_POSITION_USD,
        )

        contracts = int(position_usd / cost_per_contract)
        contracts = max(1, contracts)

        logger.debug(
            f"Kelly sizing: edge={edge:.3%}, cost={cost_per_contract:.3f}, "
            f"bankroll=${bankroll:.2f}, kelly_f={kelly_fraction:.4f}, "
            f"half_kelly_usd=${position_usd:.2f}, contracts={contracts}"
        )
        return contracts

    # ── Individual checks ──────────────────────────────────────────────────────

    def _check_edge(self, opp) -> Tuple[bool, str]:
        """Net profit must exceed mode-appropriate minimum."""
        if opp.net_profit_pct < self.min_edge:
            return (
                False,
                f"edge {opp.net_profit_pct:.2%} below minimum "
                f"{self.min_edge:.2%} ({'live' if self.live_mode else 'paper'})",
            )
        return True, ""

    def _check_match_score(self, opp) -> Tuple[bool, str]:
        """Match score must meet live/paper threshold."""
        if opp.match_score < self.min_match:
            return (
                False,
                f"match score {opp.match_score:.2f} below threshold "
                f"{self.min_match:.2f}",
            )
        # In live mode, also require LLM verification
        if self.live_mode and hasattr(opp, "llm_verified") and not opp.llm_verified:
            return False, "LLM verification not passed for live mode"
        return True, ""

    def _check_duration(self, opp) -> Tuple[bool, str]:
        """Market must resolve within allowed window (not too soon, not too far)."""
        days = getattr(opp, "days_to_resolution", None)
        if days is None:
            return True, ""  # No duration info — skip check

        hours = days * 24
        if hours < MIN_DURATION_HOURS:
            return (
                False,
                f"market resolves in {hours:.1f}h — too soon (min {MIN_DURATION_HOURS}h)",
            )
        if days > MAX_DURATION_DAYS:
            return (
                False,
                f"market resolves in {days}d — too far out (max {MAX_DURATION_DAYS}d)",
            )
        return True, ""

    def _check_single_position_cap(self, opp) -> Tuple[bool, str]:
        """Cost of a single-contract trade must be under MAX_SINGLE_POSITION_USD."""
        cost_per = opp.kalshi_price + opp.poly_price
        if cost_per > MAX_SINGLE_POSITION_USD:
            return (
                False,
                f"cost per contract ${cost_per:.2f} exceeds "
                f"MAX_SINGLE_POSITION_USD ${MAX_SINGLE_POSITION_USD:.2f}",
            )
        return True, ""

    def _check_total_deployed_cap(self, opp, current_deployed: float) -> Tuple[bool, str]:
        """Adding even one contract must keep total deployed under cap."""
        cost_per = opp.kalshi_price + opp.poly_price
        projected = current_deployed + cost_per
        if projected > MAX_TOTAL_DEPLOYED_USD:
            return (
                False,
                f"projected deployed ${projected:.2f} exceeds "
                f"MAX_TOTAL_DEPLOYED_USD ${MAX_TOTAL_DEPLOYED_USD:.2f}",
            )
        return True, ""

    def _check_book_freshness(self, k_book, p_book) -> Tuple[bool, str]:
        """Order books must not be stale."""
        if k_book is None or p_book is None:
            return True, ""  # No books provided — skip

        if not k_book.is_fresh:
            return False, f"Kalshi book is stale (>{MAX_BOOK_AGE_SECS}s old)"
        if not p_book.is_fresh:
            return False, f"Polymarket book is stale (>{MAX_BOOK_AGE_SECS}s old)"
        return True, ""

    def _check_liquidity(self, opp, k_book, p_book) -> Tuple[bool, str]:
        """Both books must have enough contracts available."""
        if k_book is None or p_book is None:
            return True, ""  # Can't check without books

        # NormalizedMarketBook.has_liquidity is a boolean property, not a function.
        if not k_book.has_liquidity:
            return (
                False,
                f"Kalshi book has <{MIN_LIQUIDITY_CONTRACTS} contracts available",
            )
        if not p_book.has_liquidity:
            return (
                False,
                f"Polymarket book has <{MIN_LIQUIDITY_CONTRACTS} contracts available",
            )
        return True, ""

    # ── Helper ────────────────────────────────────────────────────────────────

    def _capital_capped_contracts(self, opp, current_deployed: float) -> int:
        """Max contracts limited by both single-position and total-deployed caps."""
        cost_per = opp.kalshi_price + opp.poly_price
        if cost_per <= 0:
            return 0

        # How much room is left before hitting either cap?
        room_from_single = MAX_SINGLE_POSITION_USD
        room_from_total  = MAX_TOTAL_DEPLOYED_USD - current_deployed
        room_usd = min(room_from_single, room_from_total)

        return max(0, int(room_usd / cost_per))
