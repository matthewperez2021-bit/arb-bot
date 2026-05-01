#!/usr/bin/env python3
"""
seed_backtest_data.py — Seed data/historical_odds/ with real per-bookmaker data
from OddsHarvester (OddsPortal scraper).

This is a one-time / periodic utility, NOT part of the live scan loop.
Run it once per season to build the historical dataset that backtest.py reads.

Output:
    data/historical_odds/{sport}_{league}_{season}.json
    e.g. data/historical_odds/basketball_nba_2024-2025.json

Each file is a list of match dicts in OddsHarvester's output format, augmented
with a top-level "arb_sport" key mapping back to the arb-bot convention.

Usage:
    cd arb-bot
    python scripts/seed_backtest_data.py                             # all sports, current season
    python scripts/seed_backtest_data.py --sport nba                 # one sport
    python scripts/seed_backtest_data.py --season 2023-2024          # specific season
    python scripts/seed_backtest_data.py --sport nba --season 2023-2024
    python scripts/seed_backtest_data.py --list-leagues              # show available leagues
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("seed_backtest")

from config.settings import ODDS_HARVESTER_HISTORICAL_DIR, ODDS_HARVESTER_SPORT_MAP

# ─────────────────────────────────────────────────────────────────────────────
# League mappings per arb-bot sport key → OddsPortal league slug
# These match the slugs in OddsHarvester's sport_league_constants.py
# ─────────────────────────────────────────────────────────────────────────────

SPORT_LEAGUES: dict = {
    "mlb": [
        "usa-mlb",
    ],
    "nba": [
        "usa-nba",
    ],
    "nhl": [
        "usa-nhl",
    ],
    "nfl": [
        "usa-nfl",
    ],
    "mls": [
        "usa-mls",
    ],
    "tennis_atp": [
        "atp-us-open",
        "atp-wimbledon",
        "atp-french-open",
        "atp-australian-open",
    ],
}

# Default season to seed (update each year)
DEFAULT_SEASON = "2024-2025"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _output_path(arb_sport: str, league: str, season: str) -> str:
    os.makedirs(ODDS_HARVESTER_HISTORICAL_DIR, exist_ok=True)
    safe_league = league.replace("/", "-").replace(" ", "_")
    return os.path.join(
        ODDS_HARVESTER_HISTORICAL_DIR,
        f"{arb_sport}_{safe_league}_{season}.json",
    )


async def _scrape_league(
    arb_sport: str,
    oh_sport: str,
    league: str,
    season: str,
    markets: list[str],
) -> list[dict]:
    """Scrape one league / one season. Returns list of match dicts."""
    try:
        from oddsharvester.core.scraper_app import run_scraper
        from oddsharvester.utils.command_enum import CommandEnum
    except ImportError:
        log.error(
            "OddsHarvester not installed. Run:\n"
            "  pip install oddsharvester\n"
            "  python -m playwright install chromium"
        )
        return []

    log.info("Scraping %s | league=%s | season=%s ...", oh_sport, league, season)
    try:
        result = await run_scraper(
            command=CommandEnum.HISTORIC,
            sport=oh_sport,
            leagues=[league],
            season=season,
            markets=markets,
            headless=True,
        )
    except Exception as exc:
        log.error("Scrape failed for %s/%s/%s: %s", oh_sport, league, season, exc)
        return []

    if result is None:
        log.warning("No result for %s/%s/%s", oh_sport, league, season)
        return []

    matches = result.success if hasattr(result, "success") else []
    log.info(
        "  → %d matches (%d failed)",
        len(matches),
        len(result.failed) if hasattr(result, "failed") else 0,
    )
    return matches


async def seed(
    sports: list[str],
    season: str,
    markets: list[str],
    overwrite: bool,
) -> None:
    total_matches = 0
    total_files   = 0

    for arb_sport in sports:
        oh_sport = ODDS_HARVESTER_SPORT_MAP.get(arb_sport)
        if not oh_sport:
            log.warning("No OddsHarvester sport mapping for '%s' — skipping", arb_sport)
            continue

        leagues = SPORT_LEAGUES.get(arb_sport, [])
        if not leagues:
            log.warning("No leagues configured for '%s' — skipping", arb_sport)
            continue

        for league in leagues:
            out_path = _output_path(arb_sport, league, season)

            if os.path.exists(out_path) and not overwrite:
                existing = json.load(open(out_path, encoding="utf-8"))
                log.info("SKIP %s (already exists, %d matches). Use --overwrite to re-scrape.",
                         out_path, len(existing.get("matches", [])))
                continue

            matches = await _scrape_league(arb_sport, oh_sport, league, season, markets)
            if not matches:
                continue

            payload = {
                "_scraped_at": datetime.utcnow().isoformat() + "Z",
                "arb_sport":   arb_sport,
                "oh_sport":    oh_sport,
                "league":      league,
                "season":      season,
                "markets":     markets,
                "match_count": len(matches),
                "matches":     matches,
            }

            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)

            log.info("Saved %d matches → %s", len(matches), out_path)
            total_matches += len(matches)
            total_files   += 1

    print(f"\nDone. {total_files} files written, {total_matches} total matches.")
    print(f"Output directory: {os.path.abspath(ODDS_HARVESTER_HISTORICAL_DIR)}")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Seed data/historical_odds/ with OddsPortal per-bookmaker data."
    )
    parser.add_argument("--sport",     type=str, default=None,
                        help="arb-bot sport key (mlb, nba, nhl, nfl, mls, tennis_atp). "
                             "Default: all mapped sports.")
    parser.add_argument("--season",    type=str, default=DEFAULT_SEASON,
                        help=f"Season string, e.g. 2024-2025 (default: {DEFAULT_SEASON})")
    parser.add_argument("--markets",   type=str, default="1x2",
                        help="Comma-separated OddsHarvester market names (default: 1x2)")
    parser.add_argument("--overwrite", action="store_true",
                        help="Re-scrape and overwrite existing files")
    parser.add_argument("--list-leagues", action="store_true",
                        help="Print configured leagues and exit")
    args = parser.parse_args()

    if args.list_leagues:
        print("Configured leagues per sport:")
        for sport, leagues in SPORT_LEAGUES.items():
            oh = ODDS_HARVESTER_SPORT_MAP.get(sport, "—")
            print(f"  {sport} ({oh}):")
            for lg in leagues:
                print(f"    {lg}")
        return

    sports  = [args.sport] if args.sport else list(SPORT_LEAGUES.keys())
    markets = [m.strip() for m in args.markets.split(",")]

    print(f"Seeding historical data:")
    print(f"  Sports:  {sports}")
    print(f"  Season:  {args.season}")
    print(f"  Markets: {markets}")
    print(f"  Output:  {os.path.abspath(ODDS_HARVESTER_HISTORICAL_DIR)}")
    print(f"  Note: each league takes 2–15 min depending on match count.\n")

    asyncio.run(seed(sports, args.season, markets, args.overwrite))


if __name__ == "__main__":
    main()
