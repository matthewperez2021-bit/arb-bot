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

import json
import logging
import os
import time
import unicodedata
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

# ─────────────────────────────────────────────────────────────────────────────
# Player prop market keys per sport (Odds API event-level endpoint)
# ─────────────────────────────────────────────────────────────────────────────

PLAYER_PROP_MARKETS: dict = {
    "basketball_nba": [
        "player_points",
        "player_rebounds",
        "player_assists",
        "player_threes",
        "player_blocks",
        "player_steals",
        "player_points_rebounds_assists",
        "player_points_rebounds",
        "player_points_assists",
        "player_rebounds_assists",
    ],
    "baseball_mlb": [
        "batter_hits",
        "batter_rbis",
        "batter_home_runs",
        "pitcher_strikeouts",
        "batter_runs_scored",
    ],
    "icehockey_nhl": [
        "player_points",
        "player_goals",
        "player_assists",
        "player_shots_on_goal",
    ],
    "soccer_usa_mls": [
        "player_shots",
        "player_shots_on_target",
    ],
}

# Prop cache TTL (seconds) — props don't move as fast as h2h lines
PROP_CACHE_TTL_SECS = 1800   # 30 minutes
PROP_CACHE_PATH     = "data/player_prop_cache.json"

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
    # Player props
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    def normalize_player_name(name: str) -> str:
        """Lowercase + strip diacritics for fuzzy player matching."""
        nfkd = unicodedata.normalize("NFKD", name)
        ascii_name = "".join(c for c in nfkd if not unicodedata.combining(c))
        return ascii_name.lower().strip()

    def get_player_props(
        self,
        sport_key: str,
        event_id: str,
        regions: str = "us",
        odds_format: str = "american",
    ) -> dict:
        """
        Fetch player prop odds for one event.

        Returns a prop_cache fragment:
            {normalized_player_name: [(line, over_prob, n_books, market_key), ...]}

        One API credit per call — call sparingly and cache the result.
        """
        markets = PLAYER_PROP_MARKETS.get(sport_key, [])
        if not markets:
            return {}

        params = {
            "markets":    ",".join(markets),
            "regions":    regions,
            "oddsFormat": odds_format,
        }

        try:
            data = self._get(
                f"/v4/sports/{sport_key}/events/{event_id}/odds",
                params=params,
            )
        except OddsAPIError as exc:
            if exc.status_code in (404, 422):
                log.debug("No player props for event %s (%s)", event_id, exc)
                return {}
            raise

        return self._parse_player_props(data)

    def _parse_player_props(self, event_data: dict) -> dict:
        """
        Parse an event-level odds response into the prop_cache structure.

        prop_cache: {normalized_player: [(line, over_prob, n_books, mkt_key)]}

        Devigging: each book supplies an Over + Under line; we devig the pair.
        If only Over is available (some books), use raw implied prob.
        """
        # Accumulate per (player_norm, line, mkt_key, book_key): {over/under: raw_prob}
        book_sides: dict = {}

        for book in event_data.get("bookmakers", []):
            book_key = book.get("key", "")
            for market in book.get("markets", []):
                mkt_key = market.get("key", "")
                for outcome in market.get("outcomes", []):
                    # Player name lives in "description", bet type in "name"
                    player_raw = outcome.get("description") or outcome.get("name", "")
                    bet_type   = outcome.get("name", "").lower()  # "over" / "under"
                    point      = outcome.get("point")
                    price      = outcome.get("price")

                    if not player_raw or point is None or price is None:
                        continue
                    if bet_type not in ("over", "under"):
                        continue

                    key = (
                        self.normalize_player_name(player_raw),
                        float(point),
                        mkt_key,
                        book_key,
                    )
                    book_sides.setdefault(key, {})[bet_type] = self.american_to_implied(price)

        # Devig Over/Under pairs and aggregate across books
        # Group by (player_norm, line, mkt_key)
        per_entry: dict = {}    # (player_norm, line, mkt_key) → [devigged_over_prob]

        for (player_norm, line, mkt_key, _book), sides in book_sides.items():
            over_raw  = sides.get("over")
            under_raw = sides.get("under")
            if over_raw is None:
                continue

            if under_raw is not None:
                total = over_raw + under_raw
                devigged_over = over_raw / total if total > 0 else over_raw
            else:
                devigged_over = over_raw   # can't devig without both sides

            per_entry.setdefault((player_norm, line, mkt_key), []).append(devigged_over)

        # Average across books → final cache
        result: dict = {}
        for (player_norm, line, mkt_key), probs in per_entry.items():
            avg_prob = sum(probs) / len(probs)
            result.setdefault(player_norm, []).append(
                (line, avg_prob, len(probs), mkt_key)
            )

        log.debug(
            "Parsed player props: %d players from event",
            len(result),
        )
        return result

    def build_player_prop_cache(
        self,
        events_to_fetch: list,       # list of (sport_key, event_id) tuples
        max_events: int = 15,
        cache_path: str = PROP_CACHE_PATH,
        cache_ttl: int = PROP_CACHE_TTL_SECS,
    ) -> dict:
        """
        Build a merged player prop cache for a list of (sport_key, event_id) pairs.

        Reads from disk cache first (TTL = cache_ttl seconds).
        Only fetches events not already in the cache.

        Returns merged prop_cache dict:
            {normalized_player_name: [(line, over_prob, n_books, market_key), ...]}
        """
        os.makedirs(os.path.dirname(cache_path) if os.path.dirname(cache_path) else ".", exist_ok=True)

        # Load existing cache if fresh enough
        cached_data: dict = {}
        cached_event_ids: set = set()
        cache_ts = 0.0

        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                cache_ts = saved.get("_ts", 0.0)
                if time.time() - cache_ts < cache_ttl:
                    cached_data       = saved.get("prop_cache", {})
                    cached_event_ids  = set(saved.get("event_ids", []))
                    log.debug(
                        "Player prop cache HIT (age=%.0fs, %d players)",
                        time.time() - cache_ts, len(cached_data),
                    )
            except Exception as exc:
                log.debug("Failed to read prop cache: %s", exc)

        # Only fetch events not already cached
        to_fetch = [
            (sk, eid) for sk, eid in events_to_fetch[:max_events]
            if eid not in cached_event_ids
        ]

        if to_fetch:
            log.info("Fetching player props for %d events", len(to_fetch))

        merged = dict(cached_data)
        new_event_ids = set(cached_event_ids)

        for sport_key, event_id in to_fetch:
            try:
                fragment = self.get_player_props(sport_key, event_id)
                for player, entries in fragment.items():
                    if player not in merged:
                        merged[player] = []
                    # Merge without duplicates (same line + mkt_key)
                    existing_keys = {(e[0], e[3]) for e in merged[player]}
                    for entry in entries:
                        if (entry[0], entry[3]) not in existing_keys:
                            merged[player].append(entry)
                new_event_ids.add(event_id)
                log.debug("Props fetched for event %s: %d players", event_id, len(fragment))
            except Exception as exc:
                log.warning("Failed to fetch props for %s/%s: %s", sport_key, event_id, exc)

        # Save updated cache to disk
        if to_fetch:
            try:
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump({
                        "_ts":        time.time(),
                        "event_ids":  list(new_event_ids),
                        "prop_cache": merged,
                    }, f)
            except Exception as exc:
                log.warning("Failed to write prop cache: %s", exc)

        return merged

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
