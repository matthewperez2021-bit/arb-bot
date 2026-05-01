"""
odds_arb_scanner.py — Detects arb between Kalshi KXMVE markets and sportsbooks.

Strategy (single-leg Kalshi trade):
  1. Fetch Kalshi KXMVE multi-variant markets (each is a parlay of 2-8 outcomes)
  2. Parse each market title into individual legs (team wins, player overs, totals)
  3. Price each leg independently using devigged sportsbook consensus
  4. Multiply leg probabilities → fair combined probability for the YES outcome
  5. Compare fair prob to live Kalshi YES/NO ask price
  6. Flag opportunities where Kalshi is mispriced by > min_edge after fees

Why multi-leg?
  KXMVE markets bundle several outcomes into one contract. Sportsbooks price
  each outcome efficiently; Kalshi's market makers may misprice the bundle.
  The bigger the parlay, the more likely Kalshi lags the market.

Execution note:
  Only the Kalshi leg is automated. Sportsbooks ban arb accounts.

Usage:
    scanner = OddsArbScanner()
    events  = scanner.fetch_events(["mlb","nba","nhl","nfl","mma"])
    opps    = scanner.scan(kxmve_markets, fetch_book, events)
"""

import logging
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Callable, Optional

log = logging.getLogger(__name__)

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from clients.normalizer import NormalizedMarket, NormalizedMarketBook
from clients.odds_api import OddsAPIClient
from detection.kxmve_parser import KXMVEParser, KXMVELeg, build_team_variants
from config.settings import (
    KALSHI_TAKER_FEE,
    ODDS_API_MIN_BOOKS,
    ODDS_API_MIN_EDGE,
)


# ─────────────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class OddsArbOpportunity:
    """A detected single-leg Kalshi arb vs sportsbook consensus."""
    kalshi_ticker:    str
    kalshi_title:     str
    kalshi_side:      str       # "yes" or "no"
    kalshi_price:     float     # best ask (0.0–1.0)
    fair_prob:        float     # computed combined fair probability
    net_edge:         float     # fair_prob - cost_with_fee  (positive = trade it)
    books_used:       int       # min books across all legs
    legs_priced:      int       # how many of the title's legs we successfully priced
    legs_total:       int       # total legs in the title
    leg_details:      list      # list of (subject, prob) per priced leg
    sport:            str       # primary sport key
    contracts:        int       = 1
    detected_at:      float     = field(default_factory=time.time)

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

class OddsArbScanner:
    """
    Compares live Kalshi KXMVE markets against devigged sportsbook consensus.

    Multi-leg pricing:
      For each KXMVE market we parse the title into N legs, price each one
      independently using the Odds API, then multiply probabilities together
      (assuming independence) to get the fair combined YES probability.

    Parameters
    ----------
    min_edge             Minimum net edge after Kalshi fee (default from settings)
    min_books            Minimum bookmakers contributing to any leg's consensus
    min_legs_priced      Minimum legs successfully priced to trust the fair prob
    near_miss_pct        Log near-misses within this multiple of min_edge (e.g. 2x)
    """

    # Minimum fraction of total legs that must be priced.
    # If only 1 of 6 legs is priced we can't trust the combined probability.
    MIN_LEG_COVERAGE = 0.5     # need at least 50% of legs priced

    def __init__(
        self,
        min_edge:        float = ODDS_API_MIN_EDGE,
        min_books:       int   = ODDS_API_MIN_BOOKS,
        min_legs_priced: int   = 1,
        near_miss_pct:   float = 2.0,
    ):
        self.min_edge         = min_edge
        self.min_books        = min_books
        self.min_legs_priced  = min_legs_priced
        self.near_miss_pct    = near_miss_pct
        self.odds_client      = OddsAPIClient()
        self._harvester_cache: dict = {}
        log.info(
            "OddsArbScanner initialized (min_edge=%.1f%%, min_books=%d)",
            min_edge * 100, min_books,
        )

    # ──────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────

    def fetch_events(self, sports: list) -> list:
        """
        Fetch sportsbook events for all requested sports.
        Silently skips sports that return 404/422 (off-season).
        Returns flat list of event dicts.
        """
        all_events = []
        for sport in sports:
            try:
                events = self.odds_client.get_events(sport)
                all_events.extend(events)
                log.info("OddsAPI: %d events for %s", len(events), sport)
            except Exception as exc:
                log.debug("Skipping sport %s: %s", sport, exc)

        log.info(
            "OddsAPI: %d total events across %d sports (quota remaining: %s)",
            len(all_events), len(sports),
            self.odds_client.quota_remaining(),
        )
        return all_events

    def scan(
        self,
        kalshi_markets: list,
        fetch_book: Callable[[NormalizedMarket], Optional[NormalizedMarketBook]],
        sportsbook_events: list,
        prop_cache: dict = None,
        harvester_cache: dict = None,
    ) -> list:
        """
        Main entry point. Returns OddsArbOpportunity list sorted by net_edge desc.

        Args:
            kalshi_markets:    list of NormalizedMarket (KXMVE series)
            fetch_book:        callable(market) -> NormalizedMarketBook | None
            sportsbook_events: pre-fetched list of Odds API event dicts
            prop_cache:        optional player prop cache from OddsAPIClient
            harvester_cache:   optional HarvesterCache from OddsHarvesterClient
                               consulted when Odds API has no line for a team
        """
        if not kalshi_markets or not sportsbook_events:
            log.info(
                "OddsArbScanner.scan: skipped (markets=%d, events=%d)",
                len(kalshi_markets), len(sportsbook_events),
            )
            return []

        # Build team-name variant lookup once from all events
        team_variants  = build_team_variants(sportsbook_events)
        self._prop_cache     = prop_cache or {}
        self._harvester_cache = harvester_cache or {}
        log.info(
            "OddsArbScanner: %d markets | %d sportsbook events | %d team variants | "
            "%d harvester teams",
            len(kalshi_markets), len(sportsbook_events), len(team_variants),
            len(self._harvester_cache),
        )

        opportunities = []
        near_misses   = 0

        for km in kalshi_markets:
            opp, near_miss = self._evaluate_market(km, fetch_book, team_variants)
            if opp:
                opportunities.append(opp)
            elif near_miss:
                near_misses += 1

        opportunities.sort(key=lambda o: o.net_edge, reverse=True)
        log.info(
            "OddsArbScanner: %d markets -> %d opportunities | %d near-misses",
            len(kalshi_markets), len(opportunities), near_misses,
        )
        return opportunities

    # ──────────────────────────────────────────────────────────────────
    # Per-market evaluation
    # ──────────────────────────────────────────────────────────────────

    def _evaluate_market(
        self,
        km:           NormalizedMarket,
        fetch_book:   Callable,
        team_variants: dict,
    ) -> tuple:
        """
        Evaluate one KXMVE market.

        Returns (OddsArbOpportunity|None, is_near_miss:bool).
        """
        # Parse title into legs
        legs = KXMVEParser.parse(km.title)
        if not legs:
            return None, False

        # Price each leg
        priced: list = []     # (prob, books_used, subject)
        min_books = 999
        primary_sport = ""

        for leg in legs:
            result = self._price_leg(leg, team_variants)
            if result is None:
                continue
            prob, books, sport = result

            # Flip probability for "no" position legs
            if leg.position == "no":
                prob = 1.0 - prob

            priced.append((prob, books, leg.subject, sport))
            min_books = min(min_books, books)
            if not primary_sport and sport:
                primary_sport = sport

        if not priced:
            return None, False

        # Require minimum leg coverage
        coverage = len(priced) / max(len(legs), 1)
        if (len(priced) < self.min_legs_priced or
                coverage < self.MIN_LEG_COVERAGE):
            return None, False

        # Combined fair probability (multiply independent legs)
        fair_prob = 1.0
        for prob, _, _, _ in priced:
            fair_prob *= prob

        if min_books == 999:
            min_books = 0

        if min_books < self.min_books:
            return None, False

        # Fetch live order book
        try:
            book = fetch_book(km)
        except Exception as exc:
            log.debug("Book fetch failed for %s: %s", km.market_id, exc)
            return None, False
        if book is None:
            return None, False

        leg_details = [(s, round(p, 4)) for p, _, s, _ in priced]

        # Check YES and NO sides
        opp, near_miss = self._check_opportunity(
            km, book, fair_prob, min_books,
            len(priced), len(legs), leg_details, primary_sport,
        )
        return opp, near_miss

    @staticmethod
    def _normalize_player(name: str) -> str:
        """Lowercase + strip diacritics for player name matching."""
        nfkd = unicodedata.normalize("NFKD", name)
        return "".join(c for c in nfkd if not unicodedata.combining(c)).lower().strip()

    def _price_leg(
        self,
        leg:           KXMVELeg,
        team_variants: dict,
    ) -> Optional[tuple]:
        """
        Price one leg. Returns (probability: float, books_used: int, sport: str) or None.

        Handles:
          - team_win / team_spread: devigged sportsbook moneyline consensus
          - player_over:            sportsbook player prop (requires prop_cache)
          - total_over:             not priced (no totals market fetched)
        """
        if leg.leg_type in ("team_win", "team_spread"):
            return self._price_team_leg(leg, team_variants)

        if leg.leg_type == "player_over":
            return self._price_player_leg(leg)

        # total_over: would need a separate totals market fetch — skip for now
        return None

    def _price_player_leg(self, leg: KXMVELeg) -> Optional[tuple]:
        """
        Look up a player_over leg in the prop_cache.

        KXMVE "25+" → sportsbook "Over 24.5" → target_line = threshold - 0.5
        Searches within ±1.0 point tolerance to handle line variations.

        Returns (over_prob, n_books, "") or None.
        """
        if not self._prop_cache or leg.threshold is None:
            return None

        player_norm  = self._normalize_player(leg.subject)
        target_line  = leg.threshold - 0.5   # "25+" → 24.5

        entries = self._prop_cache.get(player_norm)

        # Fallback: last-name match (handles slight spelling differences)
        if not entries:
            last_name = player_norm.split()[-1] if player_norm.split() else ""
            if len(last_name) >= 4:
                for cached_player, cached_entries in self._prop_cache.items():
                    if cached_player.split()[-1] == last_name:
                        entries = cached_entries
                        break

        if not entries:
            return None

        # Find the entry whose line is closest to target_line (within ±1.0)
        best      = None
        best_dist = float("inf")
        for (line, prob, n_books, mkt_key) in entries:
            dist = abs(line - target_line)
            if dist < best_dist and dist <= 1.0:
                best_dist = dist
                best = (prob, n_books, mkt_key)

        if best is None:
            return None

        prob, n_books, _mkt = best
        log.debug(
            "Player prop: %s threshold=%.1f target_line=%.1f prob=%.3f books=%d",
            leg.subject, leg.threshold, target_line, prob, n_books,
        )
        return prob, n_books, ""

    def _price_team_leg(
        self,
        leg:           KXMVELeg,
        team_variants: dict,
    ) -> Optional[tuple]:
        """
        Find the team in team_variants and return its devigged win probability.

        Falls back to OddsHarvester cache when Odds API has no line for the team.
        Uses the source with the higher book count when both are available.
        """
        # ── Primary: The Odds API ──────────────────────────────────────
        match = team_variants.get(leg.subject)

        if match is None:
            for variant, val in team_variants.items():
                if variant in leg.subject or leg.subject in variant:
                    if len(variant) >= 4:
                        match = val
                        break

        odds_api_result = None
        if match is not None:
            full_team_name, event = match
            prob, books = self._consensus_prob(event, full_team_name)
            if prob is not None:
                sport = event.get("sport_key", "")
                odds_api_result = (prob, books, sport)

        # ── Fallback / supplement: OddsHarvester cache ────────────────
        harvester_result = None
        if self._harvester_cache:
            from clients.odds_harvester_client import OddsHarvesterClient
            client = OddsHarvesterClient.__new__(OddsHarvesterClient)
            harvester_lookup = client.lookup_team(leg.subject, self._harvester_cache)
            if harvester_lookup is not None:
                h_prob, h_books, h_sport = harvester_lookup
                harvester_result = (h_prob, h_books, h_sport)

        # ── Pick best source (highest n_books; prefer Odds API on tie) ─
        if odds_api_result is None and harvester_result is None:
            return None

        if odds_api_result is None:
            log.debug(
                "OddsArbScanner: using OddsHarvester for '%s' (no Odds API line)",
                leg.subject,
            )
            return harvester_result

        if harvester_result is None:
            return odds_api_result

        # Both available — use the one with more bookmakers
        if harvester_result[1] > odds_api_result[1]:
            log.debug(
                "OddsArbScanner: OddsHarvester has more books for '%s' "
                "(%d vs %d) — using harvester prob=%.4f",
                leg.subject, harvester_result[1], odds_api_result[1], harvester_result[0],
            )
            return harvester_result

        return odds_api_result

    def _consensus_prob(
        self,
        event:     dict,
        team_name: str,
    ) -> tuple:
        """
        Return (devigged_win_prob, n_books) for a team across all available books.
        """
        devigged_probs = []

        for book in event.get("bookmakers", []):
            for market in book.get("markets", []):
                if market["key"] != "h2h":
                    continue
                outcomes = market.get("outcomes", [])
                if len(outcomes) < 2:
                    continue

                raw_probs  = [
                    OddsAPIClient.american_to_implied(o["price"])
                    for o in outcomes
                ]
                dv         = OddsAPIClient.devig(raw_probs)
                target_idx = None
                for i, o in enumerate(outcomes):
                    if o["name"].lower() == team_name.lower():
                        target_idx = i
                        break

                if target_idx is not None:
                    devigged_probs.append(dv[target_idx])

        if not devigged_probs:
            return None, 0

        return sum(devigged_probs) / len(devigged_probs), len(devigged_probs)

    # ──────────────────────────────────────────────────────────────────
    # Edge check
    # ──────────────────────────────────────────────────────────────────

    def _check_opportunity(
        self,
        km:          NormalizedMarket,
        book:        NormalizedMarketBook,
        fair_prob:   float,
        books_used:  int,
        legs_priced: int,
        legs_total:  int,
        leg_details: list,
        sport:       str,
    ) -> tuple:
        """
        Check YES and NO sides. Returns (OddsArbOpportunity|None, is_near_miss).

        Net edge formula:
          YES: fair_prob     - yes_ask * (1 + KALSHI_TAKER_FEE)
          NO:  (1-fair_prob) - no_ask  * (1 + KALSHI_TAKER_FEE)
        """
        yes_ask = book.yes.best_ask
        no_ask  = book.no.best_ask
        best_opp   = None
        is_near_miss = False
        near_threshold = self.min_edge / self.near_miss_pct

        def make_opp(side, price, edge):
            return OddsArbOpportunity(
                kalshi_ticker = km.market_id,
                kalshi_title  = km.title[:120],
                kalshi_side   = side,
                kalshi_price  = price,
                fair_prob     = round(fair_prob, 5),
                net_edge      = round(edge, 5),
                books_used    = books_used,
                legs_priced   = legs_priced,
                legs_total    = legs_total,
                leg_details   = leg_details,
                sport         = sport,
            )

        # YES side
        if yes_ask is not None:
            cost = yes_ask * (1 + KALSHI_TAKER_FEE)
            edge = fair_prob - cost
            if edge >= self.min_edge:
                best_opp = make_opp("yes", yes_ask, edge)
                log.info(
                    "ODDS ARB: %s | BUY_YES @%.3f | fair=%.4f | "
                    "edge=%.2f%% | legs=%d/%d | books=%d",
                    km.market_id, yes_ask, fair_prob,
                    edge * 100, legs_priced, legs_total, books_used,
                )
            elif edge >= near_threshold:
                is_near_miss = True
                log.debug(
                    "Near-miss YES: %s | edge=%.2f%% (need %.1f%%)",
                    km.market_id, edge * 100, self.min_edge * 100,
                )

        # NO side
        if no_ask is not None:
            cost = no_ask * (1 + KALSHI_TAKER_FEE)
            edge = (1.0 - fair_prob) - cost
            if edge >= self.min_edge:
                opp_no = make_opp("no", no_ask, edge)
                log.info(
                    "ODDS ARB: %s | BUY_NO  @%.3f | fair=%.4f | "
                    "edge=%.2f%% | legs=%d/%d | books=%d",
                    km.market_id, no_ask, fair_prob,
                    edge * 100, legs_priced, legs_total, books_used,
                )
                if best_opp is None or opp_no.net_edge > best_opp.net_edge:
                    best_opp = opp_no
            elif edge >= near_threshold:
                is_near_miss = True

        return best_opp, is_near_miss
