"""
odds_api.py — The Odds API client (https://the-odds-api.com).

Aggregates live sportsbook odds from 40+ US books including:
    DraftKings, FanDuel, BetMGM, Caesars, PointsBet, DraftKings, etc.

Used as a SIGNAL SOURCE to detect when Kalshi event contracts are mispriced
relative to the sportsbook consensus. Execution happens on Kalshi only.

Auth:    Free API key at https://the-odds-api.com (500 req/month free)
Docs:    https://the-odds-api.com/liveapi/guides/v4/

Key concept:
    Sportsbooks use American odds (+150, -200). We:
      1. Convert to raw implied probabilities
      2. Remove the vig (sportsbook overround)
      3. Compare devigged probability to Kalshi's price
      4. Flag mispricing above FEE_BUFFER as a tradeable signal

Usage:
    from clients.odds_api import OddsAPIClient
    client = OddsAPIClient()
    events = client.get_events("americanfootball_nfl")
    signals = client.get_kalshi_signals(events, kalshi_markets)
"""

import logging
import time
from typing import Optional

import requests

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import ODDS_API_KEY, ODDS_API_BASE_URL

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Supported sports keys (subset — full list at the-odds-api.com/sports)
# ─────────────────────────────────────────────────────────────────────────────

SPORTS = {
    # American football
    "nfl":          "americanfootball_nfl",
    "ncaaf":        "americanfootball_ncaaf",
    # Basketball
    "nba":          "basketball_nba",
    "ncaab":        "basketball_ncaab",
    # Baseball
    "mlb":          "baseball_mlb",
    # Hockey
    "nhl":          "icehockey_nhl",
    # Soccer
    "mls":          "soccer_usa_mls",
    "epl":          "soccer_epl",
    "ucl":          "soccer_uefa_champs_league",
    "bundesliga":   "soccer_germany_bundesliga",
    "laliga":       "soccer_spain_la_liga",
    "seriea":       "soccer_italy_serie_a",
    "ligue1":       "soccer_france_ligue_one",
    # MMA / Combat sports
    "mma":          "mma_mixed_martial_arts",
    "ufc":          "mma_mixed_martial_arts",
    # Tennis
    "tennis_atp":   "tennis_atp_us_open",   # rotates — API resolves active tournaments
    "tennis_wta":   "tennis_wta_us_open",
    # Golf
    "pga":          "golf_pga_championship",
    "masters":      "golf_masters_tournament_winner",
    # Legacy short keys
    "tennis":       "tennis_atp_us_open",
    "golf_pga":     "golf_pga_championship",
}

# Books to pull (these are the most liquid US books)
DEFAULT_BOOKMAKERS = [
    "draftkings",
    "fanduel",
    "betmgm",
    "caesars",
    "pointsbetus",
    "bet365",
    "pinnacle",   # Included as reference — sharpest line in the market
]


class OddsAPIError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}: {message}")


class OddsAPIClient:
    """
    Client for The Odds API v4.

    Responsibilities:
    - Fetch live moneyline odds for US sports from major sportsbooks
    - Convert American odds → devigged implied probabilities
    - Produce "KalshiSignal" objects for any Kalshi market that appears
      mispriced relative to the sportsbook consensus

    This is READ-ONLY. Betting on sportsbooks is not automated here because:
    - DK/FD have no official API
    - They ban arb players rapidly
    - The edge is captured by trading only on Kalshi (the less-efficient side)
    """

    MAX_RETRIES  = 3
    BASE_BACKOFF = 2.0

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or ODDS_API_KEY
        if not self.api_key:
            raise ValueError(
                "ODDS_API_KEY is not set. Get a free key at https://the-odds-api.com"
            )
        self.session = requests.Session()
        self._requests_remaining: Optional[int] = None
        self._requests_used:      Optional[int] = None
        log.info("OddsAPIClient initialized")

    # ─────────────────────────────────────────────────────────────────
    # HTTP layer
    # ─────────────────────────────────────────────────────────────────

    def _get(self, path: str, params: dict = None) -> dict | list:
        """GET with retry/backoff. Updates quota tracking headers."""
        url     = ODDS_API_BASE_URL + path
        params  = {**(params or {}), "apiKey": self.api_key}
        backoff = self.BASE_BACKOFF

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                resp = self.session.get(url, params=params, timeout=15)
            except requests.exceptions.RequestException as exc:
                log.warning("OddsAPI request error (attempt %d): %s", attempt, exc)
                if attempt == self.MAX_RETRIES:
                    raise
                time.sleep(min(backoff, 30.0))
                backoff *= 2
                continue

            # Track quota from response headers
            self._requests_remaining = int(resp.headers.get("x-requests-remaining", -1))
            self._requests_used      = int(resp.headers.get("x-requests-used", -1))
            if self._requests_remaining >= 0:
                log.debug("OddsAPI quota: %d used, %d remaining",
                          self._requests_used, self._requests_remaining)

            if self._requests_remaining == 0:
                log.error("OddsAPI quota exhausted! Upgrade plan or wait for reset.")
                raise OddsAPIError(429, "Quota exhausted")

            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 401:
                raise OddsAPIError(401, "Invalid API key")
            if resp.status_code == 429:
                time.sleep(min(backoff * 2, 60.0))
                backoff *= 2
                continue
            if resp.status_code >= 500:
                time.sleep(min(backoff, 30.0))
                backoff *= 2
                continue

            raise OddsAPIError(resp.status_code, resp.text)

        raise OddsAPIError(0, "Max retries exceeded")

    def quota_remaining(self) -> Optional[int]:
        """Return cached request quota remaining (-1 if not yet fetched)."""
        return self._requests_remaining

    # ─────────────────────────────────────────────────────────────────
    # Sports / Events
    # ─────────────────────────────────────────────────────────────────

    def list_sports(self, active_only: bool = True) -> list:
        """
        List all available sports. Use to discover valid sport keys.

        Returns list of dicts: {"key": "americanfootball_nfl", "active": True, ...}
        """
        params = {"active": "true"} if active_only else {}
        return self._get("/v4/sports/", params=params)

    def get_events(
        self,
        sport: str,
        bookmakers: list = None,
        markets: str = "h2h",         # h2h=moneyline, spreads, totals
        regions: str = "us",
        odds_format: str = "american",
    ) -> list:
        """
        Fetch live events and odds for a sport.

        Args:
            sport:      sport key string (e.g. "americanfootball_nfl")
                        or shorthand key from SPORTS dict (e.g. "nfl")
            bookmakers: list of bookmaker keys (default: DEFAULT_BOOKMAKERS)
            markets:    "h2h" for moneylines (winner market, best for binary arb)
            regions:    "us" for US books
            odds_format:"american" (+150/-200) or "decimal" (2.50/1.50)

        Returns list of event dicts:
            {
                "id":            "abc123",
                "sport_key":     "americanfootball_nfl",
                "sport_title":   "NFL",
                "commence_time": "2026-09-10T17:00:00Z",
                "home_team":     "Kansas City Chiefs",
                "away_team":     "Baltimore Ravens",
                "bookmakers":    [
                    {
                        "key":   "draftkings",
                        "title": "DraftKings",
                        "last_update": "...",
                        "markets": [
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": "Kansas City Chiefs", "price": -165},
                                    {"name": "Baltimore Ravens",   "price": +140},
                                ]
                            }
                        ]
                    },
                    ...
                ]
            }
        """
        sport_key = SPORTS.get(sport, sport)
        books = ",".join(bookmakers or DEFAULT_BOOKMAKERS)

        params = {
            "bookmakers":  books,
            "markets":     markets,
            "regions":     regions,
            "oddsFormat":  odds_format,
            "dateFormat":  "iso",
        }

        events = self._get(f"/v4/sports/{sport_key}/odds/", params=params)
        log.info("OddsAPI: fetched %d events for %s", len(events), sport_key)
        return events

    def get_all_us_events(self, bookmakers: list = None) -> list:
        """
        Fetch events across all active US sports.

        Batches one API call per active US sport. Use sparingly — each call
        consumes quota. Cache results for at least 60 seconds.
        """
        all_events = []
        for short_key, sport_key in SPORTS.items():
            try:
                events = self.get_events(sport_key, bookmakers=bookmakers)
                all_events.extend(events)
            except OddsAPIError as e:
                if e.status_code == 422:
                    log.debug("No events for %s (off-season)", short_key)
                    continue
                raise
        log.info("OddsAPI: %d total events across all US sports", len(all_events))
        return all_events

    # ─────────────────────────────────────────────────────────────────
    # Probability math
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    def american_to_implied(american_odds: int) -> float:
        """
        Convert American moneyline odds to raw implied probability.

        +150  →  100 / (150 + 100)  = 0.400
        -200  →  200 / (200 + 100)  = 0.667

        Note: raw implied probs sum to > 1.0 due to vig. Call devig() to normalize.
        """
        if american_odds > 0:
            return 100 / (american_odds + 100)
        else:
            abs_odds = abs(american_odds)
            return abs_odds / (abs_odds + 100)

    @staticmethod
    def devig(probs: list[float]) -> list[float]:
        """
        Remove the sportsbook's overround (vig/juice) from a set of raw
        implied probabilities.

        Uses the basic normalization method (Shin method is more precise
        but requires more compute — basic is sufficient for our purposes).

        Example:
            raw probs [0.524, 0.524] (from -110/-110 lines)
            overround = 1.048
            devigged = [0.500, 0.500]  ← true market probability

        Args:
            probs: list of raw implied probabilities for all outcomes

        Returns:
            list of devigged probabilities (sum to 1.0)
        """
        total = sum(probs)
        if total <= 0:
            return probs
        return [p / total for p in probs]

    def extract_consensus_probability(
        self,
        event: dict,
        team_name: str,
        bookmaker_keys: list = None,
    ) -> Optional[float]:
        """
        Extract the consensus (averaged, devigged) win probability for a team.

        Args:
            event:          event dict from get_events()
            team_name:      exact team name (e.g. "Kansas City Chiefs")
            bookmaker_keys: which books to average (default: all available)

        Returns:
            float 0.0–1.0 devigged probability, or None if unavailable.
        """
        books_to_use = set(bookmaker_keys or DEFAULT_BOOKMAKERS)
        devigged_probs = []

        for book in event.get("bookmakers", []):
            if book["key"] not in books_to_use:
                continue

            for market in book.get("markets", []):
                if market["key"] != "h2h":
                    continue

                outcomes = market.get("outcomes", [])
                if len(outcomes) < 2:
                    continue

                # Get raw prices for all outcomes
                raw_probs = [self.american_to_implied(o["price"]) for o in outcomes]
                dv_probs  = self.devig(raw_probs)

                # Find our team
                for outcome, dv_prob in zip(outcomes, dv_probs):
                    if outcome["name"].lower() == team_name.lower():
                        devigged_probs.append(dv_prob)
                        break

        if not devigged_probs:
            return None

        consensus = sum(devigged_probs) / len(devigged_probs)
        log.debug(
            "Consensus prob for '%s': %.3f (from %d books)",
            team_name, consensus, len(devigged_probs)
        )
        return consensus

    # ─────────────────────────────────────────────────────────────────
    # Kalshi signal generation
    # ─────────────────────────────────────────────────────────────────

    def generate_kalshi_signals(
        self,
        kalshi_markets: list,       # list of NormalizedMarket
        sportsbook_events: list,    # from get_events() or get_all_us_events()
        min_edge: float = 0.03,     # 3% minimum edge after Kalshi fees
    ) -> list:
        """
        Compare Kalshi prices to sportsbook consensus and flag mispricings.

        For each Kalshi market that appears to cover a sporting event:
          1. Find the matching sportsbook event (fuzzy name match)
          2. Extract consensus devigged probability for each team
          3. Compare to Kalshi YES/NO ask prices
          4. Return a signal if Kalshi is priced more than min_edge away

        Returns list of KalshiSignal dicts:
            {
                "kalshi_ticker":     "NFLKC-2026-W1",
                "kalshi_title":      "Will KC Chiefs win Week 1?",
                "kalshi_yes_ask":    0.55,
                "fair_yes_prob":     0.62,      ← sportsbook consensus
                "edge":              0.07,       ← 7% (Kalshi is underpriced)
                "direction":         "BUY_YES",  ← action to take on Kalshi
                "books_used":        4,
                "sportsbook_event":  "Kansas City Chiefs vs Baltimore Ravens",
            }
        """
        from difflib import SequenceMatcher
        from clients.normalizer import NormalizedMarket

        signals = []
        KALSHI_FEE = 0.07   # 7% taker fee

        def fuzzy_match(a: str, b: str) -> float:
            a = a.lower().strip()
            b = b.lower().strip()
            return SequenceMatcher(None, a, b).ratio()

        for km in kalshi_markets:
            title = km.title.lower()

            # Skip non-sports markets (basic heuristic)
            sports_keywords = ["win", "beat", "champion", "series", "cup",
                                "super bowl", "playoff", "nfl", "nba", "mlb",
                                "nhl", "ufc", "fight", "match", "game"]
            if not any(kw in title for kw in sports_keywords):
                continue

            # Find best sportsbook event match
            best_event  = None
            best_team   = None
            best_score  = 0.0

            for event in sportsbook_events:
                home = event.get("home_team", "")
                away = event.get("away_team", "")

                for team in (home, away):
                    score = fuzzy_match(team, km.title)
                    if score > best_score and score > 0.45:
                        best_score  = score
                        best_event  = event
                        best_team   = team

            if not best_event or not best_team:
                continue

            # Get consensus probability for this team
            fair_prob = self.extract_consensus_probability(best_event, best_team)
            if fair_prob is None:
                continue

            # Compare to Kalshi price (need live book — caller passes it in)
            # For simplicity here we use the market's last price as proxy
            # In production, pass in the live NormalizedMarketBook
            kalshi_yes_ask = getattr(km, "_live_yes_ask", None)
            if kalshi_yes_ask is None:
                continue

            # Net edge after Kalshi taker fee
            cost_with_fee = kalshi_yes_ask * (1 + KALSHI_FEE)
            net_edge = fair_prob - cost_with_fee

            if abs(net_edge) < min_edge:
                continue

            direction = "BUY_YES" if net_edge > 0 else "BUY_NO"
            if direction == "BUY_NO":
                # Re-calculate edge for NO side
                kalshi_no_ask = getattr(km, "_live_no_ask", None)
                if kalshi_no_ask is None:
                    continue
                cost_with_fee = kalshi_no_ask * (1 + KALSHI_FEE)
                net_edge = (1 - fair_prob) - cost_with_fee
                if net_edge < min_edge:
                    continue

            home = best_event.get("home_team", "")
            away = best_event.get("away_team", "")

            signals.append({
                "kalshi_ticker":    km.market_id,
                "kalshi_title":     km.title,
                "kalshi_yes_ask":   kalshi_yes_ask,
                "fair_yes_prob":    round(fair_prob, 4),
                "net_edge":         round(net_edge, 4),
                "direction":        direction,
                "sportsbook_event": f"{away} @ {home}",
                "match_confidence": round(best_score, 3),
                "timestamp":        time.time(),
            })
            log.info(
                "SIGNAL: %s | fair=%.3f vs kalshi=%.3f | edge=%.2f%% | %s",
                km.market_id, fair_prob, kalshi_yes_ask,
                net_edge * 100, direction
            )

        log.info("Odds signal scan: %d Kalshi markets → %d signals", len(kalshi_markets), len(signals))
        return sorted(signals, key=lambda s: abs(s["net_edge"]), reverse=True)


# ─────────────────────────────────────────────────────────────────────────────
# Smoke test
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO)

    client = OddsAPIClient()

    print(f"=== Active US Sports ===")
    sports = client.list_sports(active_only=True)
    us_sports = [s for s in sports if not s.get("has_outrights", True)]
    for s in us_sports[:8]:
        print(f"  {s['key']:<40} active={s.get('active')}")

    print(f"\nQuota remaining: {client.quota_remaining()}")

    # Try NFL (or whichever sport is in season)
    for sport_key in ["nfl", "nba", "mlb", "nhl"]:
        try:
            events = client.get_events(SPORTS[sport_key], bookmakers=["draftkings", "fanduel"])
            if events:
                print(f"\n=== {sport_key.upper()} Events ({len(events)}) ===")
                for ev in events[:3]:
                    home = ev["home_team"]
                    away = ev["away_team"]
                    print(f"\n  {away} @ {home}  ({ev['commence_time'][:10]})")
                    for book in ev["bookmakers"][:2]:
                        print(f"    [{book['title']}]", end="")
                        for mkt in book["markets"]:
                            if mkt["key"] == "h2h":
                                for outcome in mkt["outcomes"]:
                                    p = OddsAPIClient.american_to_implied(outcome["price"])
                                    print(f"  {outcome['name']}: {outcome['price']:+d} ({p:.3f})", end="")
                        print()

                    # Show devigged consensus for home team
                    fair = client.extract_consensus_probability(ev, home)
                    if fair:
                        print(f"  → Consensus fair prob for {home}: {fair:.3f}")
                break
        except OddsAPIError as e:
            if e.status_code == 422:
                print(f"  {sport_key}: off-season")
                continue
            raise

    print(f"\nFinal quota remaining: {client.quota_remaining()}")
