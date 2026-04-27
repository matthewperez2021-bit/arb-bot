"""
economic_arb_scanner.py — Finds arb between Kalshi economic markets and
external financial reference data.

Strategies
----------
  KXBTC  ← BTC options-implied probability (Deribit, free)
  KXFED  ← CME FedWatch Fed Funds futures probabilities (free)
  KXCPI  ← Cleveland Fed / TIPS breakeven (future)
  KXGDP  ← Atlanta Fed GDPNow (future)

These markets are ALL liquid (confirmed: every KXBTC/KXFED/KXCPI/KXGDP
market has real order-book depth), so the comparison is actionable.

Usage:
    scanner = EconomicArbScanner()
    opps    = scanner.scan(kalshi_markets, fetch_book)
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

log = logging.getLogger(__name__)

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from clients.normalizer import NormalizedMarket, NormalizedMarketBook
from clients.btc_pricer import BTCPricer
from clients.fed_pricer import FedPricer
from config.settings import KALSHI_TAKER_FEE


# ─────────────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class EconArbOpportunity:
    """A detected arb between a Kalshi economic market and external reference."""
    kalshi_ticker:  str
    kalshi_title:   str
    kalshi_side:    str       # "yes" or "no"
    kalshi_price:   float     # best ask
    fair_prob:      float     # model-derived fair probability
    net_edge:       float     # fair_prob - cost_with_fee
    source:         str       # "btc_options" | "cme_fedwatch" | "fallback"
    series:         str       # "KXBTC" | "KXFED" | "KXCPI"
    contracts:      int       = 1
    detected_at:    float     = field(default_factory=time.time)

    @property
    def max_profit_usd(self) -> float:
        return self.net_edge * self.contracts

    @property
    def max_contracts(self) -> int:
        return self.contracts

    @property
    def net_profit_pct(self) -> float:
        return self.net_edge


# ─────────────────────────────────────────────────────────────────────────────
# Scanner
# ─────────────────────────────────────────────────────────────────────────────

class EconomicArbScanner:
    """
    Scans Kalshi economic series markets for mispricings vs external data.

    Covers:
      - KXBTC: BTC price-range binary options priced via log-normal model
      - KXFED: Fed funds rate markets priced via CME FedWatch probabilities

    Parameters
    ----------
    min_edge   Minimum net edge after Kalshi taker fee (default 2%)
               Lower than sports arb because these are higher-confidence pricings.
    """

    # Minimum edge — lower threshold than sports because:
    # - Reference data (CME, Deribit) is highly accurate
    # - Kalshi economic markets can be slow to reprice after data releases
    DEFAULT_MIN_EDGE = 0.02   # 2%

    def __init__(self, min_edge: float = DEFAULT_MIN_EDGE):
        self.min_edge   = min_edge
        self.btc_pricer = BTCPricer()
        self.fed_pricer = FedPricer()
        log.info("EconomicArbScanner initialized (min_edge=%.1f%%)", min_edge * 100)

    # ──────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────

    def scan(
        self,
        kalshi_markets: list,
        fetch_book: Callable[[NormalizedMarket], Optional[NormalizedMarketBook]],
    ) -> list:
        """
        Scan Kalshi economic markets for mispricings vs external reference data.

        Args:
            kalshi_markets: list of NormalizedMarket (any mix of KXBTC, KXFED, etc.)
            fetch_book:     callable(market) -> NormalizedMarketBook | None

        Returns list of EconArbOpportunity, sorted by net_edge desc.
        """
        opportunities = []

        btc_markets = [m for m in kalshi_markets
                       if m.market_id.startswith("KXBTC")]
        fed_markets = [m for m in kalshi_markets
                       if m.market_id.startswith("KXFED")]

        log.info(
            "EconomicArbScanner: %d total | %d KXBTC | %d KXFED",
            len(kalshi_markets), len(btc_markets), len(fed_markets),
        )

        # Scan KXBTC markets
        if btc_markets:
            try:
                btc_opps = self._scan_btc(btc_markets, fetch_book)
                opportunities.extend(btc_opps)
            except Exception as exc:
                log.error("KXBTC scan failed: %s", exc)

        # Scan KXFED markets
        if fed_markets:
            try:
                fed_opps = self._scan_fed(fed_markets, fetch_book)
                opportunities.extend(fed_opps)
            except Exception as exc:
                log.error("KXFED scan failed: %s", exc)

        opportunities.sort(key=lambda o: o.net_edge, reverse=True)
        log.info(
            "EconomicArbScanner: %d opportunities found "
            "(%d BTC + %d FED)",
            len(opportunities),
            sum(1 for o in opportunities if o.series == "KXBTC"),
            sum(1 for o in opportunities if o.series == "KXFED"),
        )
        return opportunities

    # ──────────────────────────────────────────────────────────────────
    # KXBTC scanner
    # ──────────────────────────────────────────────────────────────────

    def _scan_btc(
        self,
        markets:    list,
        fetch_book: Callable,
    ) -> list:
        """
        Compare KXBTC market prices to BTC options-implied probabilities.

        Uses structured market fields (floor_strike, cap_strike, strike_type)
        stored in market.extra — NOT the title, which is uninformative.

        strike_type values:
          'greater'  → YES if BTC > floor_strike
          'less'     → YES if BTC < cap_strike
          'between'  → YES if floor_strike < BTC < cap_strike
        """
        opps = []
        spot = self.btc_pricer.spot_price()
        iv   = self.btc_pricer.implied_vol()
        log.info("KXBTC: spot=$%.0f  IV=%.1f%%  scanning %d markets",
                 spot, iv * 100, len(markets))

        priced = 0
        for km in markets:
            extra = getattr(km, "extra", {}) or {}
            strike_type  = extra.get("strike_type")
            floor_strike = extra.get("floor_strike")
            cap_strike   = extra.get("cap_strike")

            if not strike_type:
                continue

            # Days to expiry from close_time
            days = 1.0
            if km.close_time:
                try:
                    from datetime import datetime, timezone
                    close = datetime.fromisoformat(
                        km.close_time.replace("Z", "+00:00")
                    )
                    days = max(0.05, (close - datetime.now(timezone.utc)).total_seconds() / 86400)
                except Exception:
                    pass

            if strike_type == "greater" and floor_strike:
                fair_prob = self.btc_pricer.prob_above(floor_strike, days)
            elif strike_type == "less" and cap_strike:
                fair_prob = self.btc_pricer.prob_below(cap_strike, days)
            elif strike_type == "between" and floor_strike and cap_strike:
                fair_prob = self.btc_pricer.prob_in_range(floor_strike, cap_strike, days)
            else:
                continue

            # Pre-filter: skip book fetch if summary prices show no possible edge.
            # Uses yes_ask/no_ask from the market list response (free, already in memory).
            # Edge = fair_prob - ask*(1+fee).  If both sides are clearly negative, skip.
            yes_ask_summary = extra.get("yes_ask")
            no_ask_summary  = extra.get("no_ask")
            if not self._possible_edge(fair_prob, yes_ask_summary, no_ask_summary):
                continue

            try:
                book = fetch_book(km)
            except Exception:
                continue
            if book is None:
                continue

            priced += 1
            opp = self._check_edge(
                km, book, fair_prob, series="KXBTC", source="btc_options"
            )
            if opp:
                opps.append(opp)
                log.info(
                    "BTC ARB: %s | BUY_%s @%.3f | fair=%.4f | edge=%.2f%%",
                    km.market_id, opp.kalshi_side.upper(),
                    opp.kalshi_price, fair_prob, opp.net_edge * 100,
                )

        log.info("KXBTC: priced %d/%d markets -> %d opportunities",
                 priced, len(markets), len(opps))
        return opps

    # ──────────────────────────────────────────────────────────────────
    # KXFED scanner
    # ──────────────────────────────────────────────────────────────────

    def _scan_fed(
        self,
        markets:    list,
        fetch_book: Callable,
    ) -> list:
        """
        Compare KXFED market prices to CME FedWatch probabilities.
        """
        opps = []
        meetings = self.fed_pricer.get_meeting_probabilities()
        if not meetings:
            log.warning("No FOMC meeting data available — skipping KXFED scan")
            return []

        log.info("KXFED: %d meetings loaded | scanning %d markets",
                 len(meetings), len(markets))

        priced = 0
        for km in markets:
            extra = getattr(km, "extra", {}) or {}
            floor_strike = extra.get("floor_strike")
            cap_strike   = extra.get("cap_strike")
            strike_type  = extra.get("strike_type")

            # Derive rate and direction from structured fields
            if strike_type == "greater" and floor_strike is not None:
                rate, direction = float(floor_strike), "above"
            elif strike_type == "less" and cap_strike is not None:
                rate, direction = float(cap_strike), "below"
            else:
                continue

            # Use close_time as the meeting date (ISO → YYYY-MM-DD)
            meeting = km.close_time[:10] if km.close_time else None
            if not meeting:
                continue

            # Get market-implied probability for this outcome
            if direction == "above":
                fair_prob = self.fed_pricer.prob_above(rate, meeting)
            else:
                fair_prob = self.fed_pricer.prob_at_or_below(rate, meeting)

            if fair_prob is None:
                continue

            # Determine if this is a fallback (lower confidence)
            meeting_data = next(
                (m for m in meetings if m["date"] == meeting or
                 m["date"].startswith(meeting[:7])), None
            )
            is_fallback = meeting_data.get("is_fallback", False) if meeting_data else True

            # Use higher edge threshold for fallback data
            effective_min_edge = self.min_edge * 2.0 if is_fallback else self.min_edge
            source = "fallback" if is_fallback else "cme_fedwatch"

            # Pre-filter: skip book fetch if summary prices show no possible edge
            yes_ask_summary = extra.get("yes_ask")
            no_ask_summary  = extra.get("no_ask")
            if not self._possible_edge(
                fair_prob, yes_ask_summary, no_ask_summary,
                threshold=effective_min_edge,
            ):
                continue

            try:
                book = fetch_book(km)
            except Exception:
                continue
            if book is None:
                continue

            priced += 1
            opp = self._check_edge(
                km, book, fair_prob, series="KXFED", source=source,
                min_edge_override=effective_min_edge,
            )
            if opp:
                opps.append(opp)
                log.info(
                    "FED ARB: %s | BUY_%s @%.3f | fair=%.4f | edge=%.2f%% | %s",
                    km.market_id, opp.kalshi_side.upper(),
                    opp.kalshi_price, fair_prob, opp.net_edge * 100, source,
                )

        log.info("KXFED: priced %d/%d markets -> %d opportunities",
                 priced, len(markets), len(opps))
        return opps

    # ──────────────────────────────────────────────────────────────────
    # Edge calculation
    # ──────────────────────────────────────────────────────────────────

    def _possible_edge(
        self,
        fair_prob:      float,
        yes_ask:        Optional[float],
        no_ask:         Optional[float],
        threshold:      Optional[float] = None,
    ) -> bool:
        """
        Quick pre-filter using summary prices (from market list, no API call needed).
        Returns True if either side COULD have edge >= threshold after fee.
        Uses a loose threshold (half the actual) to avoid false negatives from
        spread differences between summary price and order book best ask.
        """
        thr = (threshold if threshold is not None else self.min_edge) * 0.5
        if yes_ask is not None:
            if fair_prob - yes_ask * (1 + KALSHI_TAKER_FEE) >= thr:
                return True
        if no_ask is not None:
            if (1.0 - fair_prob) - no_ask * (1 + KALSHI_TAKER_FEE) >= thr:
                return True
        if yes_ask is None and no_ask is None:
            return True  # no summary prices — fetch book to be safe
        return False

    def _check_edge(
        self,
        km:                 NormalizedMarket,
        book:               NormalizedMarketBook,
        fair_prob:          float,
        series:             str,
        source:             str,
        min_edge_override:  Optional[float] = None,
    ) -> Optional[EconArbOpportunity]:
        """
        Check YES and NO sides for edge. Return best opportunity or None.
        """
        threshold = min_edge_override if min_edge_override is not None else self.min_edge
        yes_ask   = book.yes.best_ask
        no_ask    = book.no.best_ask
        best_opp  = None

        def make_opp(side, price, edge):
            return EconArbOpportunity(
                kalshi_ticker = km.market_id,
                kalshi_title  = km.title[:120],
                kalshi_side   = side,
                kalshi_price  = price,
                fair_prob     = round(fair_prob, 5),
                net_edge      = round(edge, 5),
                source        = source,
                series        = series,
            )

        if yes_ask is not None:
            cost = yes_ask * (1 + KALSHI_TAKER_FEE)
            edge = fair_prob - cost
            if edge >= threshold:
                best_opp = make_opp("yes", yes_ask, edge)

        if no_ask is not None:
            cost = no_ask * (1 + KALSHI_TAKER_FEE)
            edge = (1.0 - fair_prob) - cost
            if edge >= threshold and (
                best_opp is None or edge > best_opp.net_edge
            ):
                best_opp = make_opp("no", no_ask, edge)

        return best_opp
