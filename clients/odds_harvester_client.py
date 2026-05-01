"""
odds_harvester_client.py — Supplemental sportsbook odds via OddsPortal scraping.

Uses OddsHarvester (Playwright-based) to fetch per-bookmaker decimal odds from
OddsPortal.com. Converts them to devigged implied probabilities and returns a
HarvesterCache that OddsArbScanner can consult alongside The Odds API.

Architecture:
  - Batch-only (Playwright takes 10-40s per sport). Never call in the scan loop.
  - Called every 2h by sports_scheduler.py and cached to disk.
  - The scan loop reads from the in-memory / disk cache; it never waits on Playwright.

Output format (HarvesterCache):
    {
        "normalized_team_name": {
            "prob":    0.612,       # devigged win probability averaged across books
            "n_books": 7,
            "sport":   "basketball",
            "opponent": "boston celtics",
            "commence_time": "2026-05-02T19:00:00Z",
            "scraped_at": 1746123456.0,
        },
        ...
    }

Usage:
    from clients.odds_harvester_client import OddsHarvesterClient
    client = OddsHarvesterClient()
    cache  = await client.fetch_upcoming(sports=["nba", "mlb"])
    prob   = client.lookup_team("Lakers", cache)
"""

import asyncio
import json
import logging
import os
import sys
import time
import unicodedata
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import (
    ODDS_HARVESTER_ENABLED,
    ODDS_HARVESTER_CACHE_PATH,
    ODDS_HARVESTER_REFRESH_SECS,
    ODDS_HARVESTER_SPORT_MAP,
)

log = logging.getLogger(__name__)

# Minimum bookmakers required for a consensus probability to be trustworthy
MIN_BOOKS_REQUIRED = 2


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_name(name: str) -> str:
    """Lowercase, strip diacritics, collapse whitespace — for fuzzy lookup."""
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_name = "".join(c for c in nfkd if not unicodedata.combining(c))
    return " ".join(ascii_name.lower().split())


def _decimal_to_prob(decimal_odds: str | float) -> Optional[float]:
    """Convert decimal odds string (e.g. '2.45') → raw implied probability."""
    try:
        d = float(decimal_odds)
        return 1.0 / d if d > 1.0 else None
    except (TypeError, ValueError):
        return None


def _devig(probs: list[float]) -> list[float]:
    """
    Remove bookmaker overround via multiplicative normalization.
    Returns probabilities that sum to 1.0.
    """
    total = sum(probs)
    if total <= 0:
        return probs
    return [p / total for p in probs]


def _parse_1x2_market(market_entries: list[dict]) -> list[dict]:
    """
    Parse OddsHarvester 1x2_market entries into a clean list of
    {bookmaker, home_prob, draw_prob, away_prob} dicts (devigged).

    OddsHarvester 1x2 entry keys: "1" (home), "X" (draw), "2" (away).
    For sports without draws (NBA, NHL, NFL, MLB, tennis) the "X" key is absent.
    """
    results = []
    for entry in market_entries:
        bookie = entry.get("bookmaker_name", "unknown")
        period = entry.get("period", "")

        # Only use full-time/regulation odds; skip halftime/OT periods
        if period and period.lower() not in ("fulltime", "fullincludingot", ""):
            continue

        home_raw = entry.get("1") or entry.get("odds_home")
        away_raw = entry.get("2") or entry.get("odds_away")
        draw_raw = entry.get("X") or entry.get("odds_draw")

        home_prob = _decimal_to_prob(home_raw)
        away_prob = _decimal_to_prob(away_raw)
        draw_prob = _decimal_to_prob(draw_raw)

        if home_prob is None or away_prob is None:
            continue

        if draw_prob is not None:
            raw = [home_prob, draw_prob, away_prob]
            dv  = _devig(raw)
            results.append({
                "bookmaker":  bookie,
                "home_prob":  dv[0],
                "draw_prob":  dv[1],
                "away_prob":  dv[2],
            })
        else:
            raw = [home_prob, away_prob]
            dv  = _devig(raw)
            results.append({
                "bookmaker":  bookie,
                "home_prob":  dv[0],
                "draw_prob":  None,
                "away_prob":  dv[1],
            })

    return results


def _build_team_entries(match: dict, sport: str) -> list[dict]:
    """
    From one OddsHarvester match dict, produce team-keyed probability entries
    for insertion into the HarvesterCache.

    Returns list of dicts, each representing one team:
        {
            "team_norm": "los angeles lakers",
            "opponent_norm": "boston celtics",
            "prob": 0.612,
            "n_books": 7,
            "sport": "basketball",
            "commence_time": "2026-05-02T19:00:00Z",
            "scraped_at": float,
        }
    """
    home_raw = match.get("home_team", "")
    away_raw = match.get("away_team", "")
    if not home_raw or not away_raw:
        return []

    market_data = match.get("1x2_market") or match.get("home_away_market") or []
    if not market_data:
        return []

    parsed = _parse_1x2_market(market_data)
    if not parsed:
        return []

    # Average devigged probability across all bookmakers
    home_probs = [e["home_prob"] for e in parsed]
    away_probs = [e["away_prob"] for e in parsed]
    n_books    = len(parsed)

    if n_books < MIN_BOOKS_REQUIRED:
        return []

    avg_home = sum(home_probs) / n_books
    avg_away = sum(away_probs) / n_books

    # ISO commence time from match_date string ("2026-05-02 15:00:00 UTC")
    raw_date = match.get("match_date", "")
    commence_time = ""
    if raw_date:
        try:
            dt = datetime.strptime(raw_date, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
            commence_time = dt.isoformat()
        except ValueError:
            commence_time = raw_date

    scraped_at = time.time()
    home_norm  = _normalize_name(home_raw)
    away_norm  = _normalize_name(away_raw)

    # Per-bookmaker breakdown for transparency
    home_breakdown = {e["bookmaker"]: round(e["home_prob"], 4) for e in parsed}
    away_breakdown = {e["bookmaker"]: round(e["away_prob"], 4) for e in parsed}

    return [
        {
            "team_norm":      home_norm,
            "opponent_norm":  away_norm,
            "prob":           round(avg_home, 5),
            "n_books":        n_books,
            "sport":          sport,
            "commence_time":  commence_time,
            "scraped_at":     scraped_at,
            "bookmaker_breakdown": home_breakdown,
        },
        {
            "team_norm":      away_norm,
            "opponent_norm":  home_norm,
            "prob":           round(avg_away, 5),
            "n_books":        n_books,
            "sport":          sport,
            "commence_time":  commence_time,
            "scraped_at":     scraped_at,
            "bookmaker_breakdown": away_breakdown,
        },
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Client
# ─────────────────────────────────────────────────────────────────────────────

class OddsHarvesterClient:
    """
    Async wrapper around OddsHarvester's run_scraper().

    Call fetch_upcoming() once every 2h from sports_scheduler.
    The live scan loop then calls lookup_team() against the in-memory cache.
    """

    def __init__(self):
        if not ODDS_HARVESTER_ENABLED:
            log.debug("OddsHarvesterClient: disabled (ODDS_HARVESTER_ENABLED=False)")
        os.makedirs(os.path.dirname(ODDS_HARVESTER_CACHE_PATH)
                    if os.path.dirname(ODDS_HARVESTER_CACHE_PATH) else ".", exist_ok=True)

    # ──────────────────────────────────────────────────────────────────
    # Main public interface
    # ──────────────────────────────────────────────────────────────────

    async def fetch_upcoming(
        self,
        sports: list[str] | None = None,
        date: str | None = None,
        force_refresh: bool = False,
    ) -> dict:
        """
        Scrape upcoming odds from OddsPortal and return a HarvesterCache dict.

        Args:
            sports:        arb-bot sport keys to scrape (default: all mapped sports)
            date:          YYYYMMDD string (default: today)
            force_refresh: ignore disk cache and re-scrape

        Returns HarvesterCache: {normalized_team_name: {...}} or {} on failure/disabled.
        """
        if not ODDS_HARVESTER_ENABLED:
            return {}

        # Return disk cache if still fresh
        if not force_refresh:
            cached = self._load_cache()
            if cached is not None:
                log.info("OddsHarvester: cache HIT (age<%.0fs)", ODDS_HARVESTER_REFRESH_SECS)
                return cached

        try:
            from oddsharvester.core.scraper_app import run_scraper
            from oddsharvester.utils.command_enum import CommandEnum
        except ImportError:
            log.error(
                "OddsHarvester not installed. Run: pip install oddsharvester && "
                "python -m playwright install chromium"
            )
            return {}

        target_sports = sports or list(ODDS_HARVESTER_SPORT_MAP.keys())
        date_str      = date or datetime.now().strftime("%Y%m%d")
        cache: dict   = {}

        # Scrape each sport sequentially (each opens its own browser instance)
        for arb_sport in target_sports:
            oh_sport = ODDS_HARVESTER_SPORT_MAP.get(arb_sport)
            if not oh_sport:
                log.debug("OddsHarvester: no mapping for sport %s — skipping", arb_sport)
                continue

            log.info("OddsHarvester: scraping %s (%s) for %s", arb_sport, oh_sport, date_str)
            try:
                result = await run_scraper(
                    command=CommandEnum.UPCOMING_MATCHES,
                    sport=oh_sport,
                    date=date_str,
                    markets=["1x2"],
                    headless=True,
                    request_delay=1.0,
                )
            except Exception as exc:
                log.warning("OddsHarvester scrape failed for %s: %s", arb_sport, exc)
                continue

            if result is None:
                log.warning("OddsHarvester: no result for %s", arb_sport)
                continue

            matches = result.success if hasattr(result, "success") else []
            for match in matches:
                for entry in _build_team_entries(match, arb_sport):
                    team_norm = entry.pop("team_norm")
                    cache[team_norm] = entry

            log.info(
                "OddsHarvester: %s → %d matches, %d/%d successful",
                arb_sport, len(matches),
                result.stats.successful if hasattr(result, "stats") else len(matches),
                result.stats.total_urls if hasattr(result, "stats") else len(matches),
            )

        self._save_cache(cache)
        log.info("OddsHarvester: cache built (%d teams)", len(cache))
        return cache

    def lookup_team(
        self,
        team_name: str,
        cache: dict,
        max_age_secs: float = ODDS_HARVESTER_REFRESH_SECS,
    ) -> Optional[tuple[float, int, str]]:
        """
        Look up a team in the HarvesterCache.

        Returns (devigged_win_prob, n_books, sport) or None if not found / stale.

        Matching strategy:
          1. Exact normalized name
          2. Substring match (handles e.g. "Lakers" matching "los angeles lakers")
        """
        if not cache:
            return None

        team_norm = _normalize_name(team_name)
        now = time.time()

        def _check_entry(entry: dict) -> Optional[tuple]:
            if now - entry.get("scraped_at", 0) > max_age_secs:
                return None
            return entry["prob"], entry["n_books"], entry["sport"]

        # Exact match
        if team_norm in cache:
            return _check_entry(cache[team_norm])

        # Substring match — handles partial team names from KXMVE titles
        for cached_name, entry in cache.items():
            if (team_norm in cached_name or cached_name in team_norm) and len(team_norm) >= 4:
                result = _check_entry(entry)
                if result is not None:
                    return result

        return None

    # ──────────────────────────────────────────────────────────────────
    # Disk cache
    # ──────────────────────────────────────────────────────────────────

    def _load_cache(self) -> Optional[dict]:
        """Load cache from disk if it exists and is within TTL. Returns None if stale/missing."""
        if not os.path.exists(ODDS_HARVESTER_CACHE_PATH):
            return None
        try:
            with open(ODDS_HARVESTER_CACHE_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
            ts = saved.get("_ts", 0.0)
            if time.time() - ts > ODDS_HARVESTER_REFRESH_SECS:
                log.debug("OddsHarvester: disk cache stale (age=%.0fs)", time.time() - ts)
                return None
            return saved.get("cache", {})
        except Exception as exc:
            log.debug("OddsHarvester: failed to read cache: %s", exc)
            return None

    def _save_cache(self, cache: dict) -> None:
        """Persist HarvesterCache to disk with a timestamp."""
        try:
            with open(ODDS_HARVESTER_CACHE_PATH, "w", encoding="utf-8") as f:
                json.dump({"_ts": time.time(), "cache": cache}, f)
        except Exception as exc:
            log.warning("OddsHarvester: failed to save cache: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Smoke test
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # Override enabled flag for smoke test
    import config.settings as s
    s.ODDS_HARVESTER_ENABLED = True

    client = OddsHarvesterClient()

    async def _smoke():
        print("Scraping NBA upcoming odds (this takes ~30s)...")
        cache = await client.fetch_upcoming(sports=["nba"], force_refresh=True)
        print(f"Cache has {len(cache)} teams")
        for name, entry in list(cache.items())[:5]:
            print(f"  {name}: prob={entry['prob']:.3f} n_books={entry['n_books']}")

        # Test lookup
        if cache:
            first_team = next(iter(cache))
            result = client.lookup_team(first_team.split()[0], cache)
            print(f"\nLookup '{first_team.split()[0]}': {result}")

    asyncio.run(_smoke())
