#!/usr/bin/env python3
"""
seed_odds_api_historical.py — Seed data/historical_odds/ with snapshots
fetched from The Odds API's /v4/historical endpoints.

This is a one-time / periodic utility, NOT part of the live scan loop.
Output:
    data/historical_odds/{sport}/{snapshot_iso}.json

Each file is the raw envelope returned by The Odds API:
    {
      "timestamp":          "2026-04-01T12:00:00Z",
      "previous_timestamp": "2026-04-01T11:55:00Z" or null,
      "next_timestamp":     "2026-04-01T12:05:00Z" or null,
      "data": [ {event, bookmakers...}, ... ]
    }

Walks the previous_timestamp chain backward from --start until either
--end is reached, --max-snapshots is hit, or previous_timestamp is null.

Historical endpoints cost more quota than live ones — see The Odds API
pricing page. Use --max-snapshots and --quota-floor to bound spend.

Usage:
    cd arb-bot
    python scripts/seed_odds_api_historical.py --sport mlb --max-snapshots 5
    python scripts/seed_odds_api_historical.py --sport mlb,nba --start 2026-05-06T00:00:00Z --end 2026-05-01T00:00:00Z
    python scripts/seed_odds_api_historical.py --markets h2h,spreads,totals
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("seed_odds_api_historical")

from clients.odds_api import OddsAPIClient, SPORTS, OddsAPIError
from config.settings import HISTORICAL_ODDS_DIR, ODDS_API_ACTIVE_SPORTS


def _snapshot_path(sport_key: str, snapshot_iso: str) -> str:
    sport_dir = os.path.join(HISTORICAL_ODDS_DIR, sport_key)
    os.makedirs(sport_dir, exist_ok=True)
    safe = snapshot_iso.replace(":", "-")
    return os.path.join(sport_dir, f"{safe}.json")


def _seed_sport(
    client:        OddsAPIClient,
    sport:         str,
    start_iso:     str,
    end_iso:       str | None,
    markets:       str,
    regions:       str,
    max_snapshots: int,
    quota_floor:   int,
    overwrite:     bool,
) -> int:
    """Walk the previous_timestamp chain. Returns count of snapshots saved."""
    sport_key = SPORTS.get(sport, sport)
    cursor    = start_iso
    saved     = 0

    while cursor and saved < max_snapshots:
        if end_iso and cursor < end_iso:
            log.info("[%s] Reached --end %s — stopping.", sport_key, end_iso)
            break

        remaining = client.quota_remaining()
        if remaining is not None and 0 <= remaining < quota_floor:
            log.warning(
                "[%s] Quota %d below floor %d — stopping to preserve budget.",
                sport_key, remaining, quota_floor,
            )
            break

        out_path = _snapshot_path(sport_key, cursor)
        if os.path.exists(out_path) and not overwrite:
            with open(out_path, "r", encoding="utf-8") as f:
                envelope = json.load(f)
            log.info("[%s] SKIP %s (cached). Use --overwrite to re-fetch.",
                     sport_key, cursor)
        else:
            try:
                envelope = client.get_historical_events_snapshot(
                    sport=sport_key,
                    snapshot_iso=cursor,
                    regions=regions,
                    markets=markets,
                )
            except OddsAPIError as exc:
                log.error("[%s] %s — aborting this sport.", sport_key, exc)
                break

            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(envelope, f, indent=2, ensure_ascii=False)
            saved += 1
            log.info(
                "[%s] Saved snapshot @ %s (%d events) → %s",
                sport_key,
                envelope.get("timestamp", cursor),
                len(envelope.get("data", []) or []),
                out_path,
            )

        cursor = envelope.get("previous_timestamp") if isinstance(envelope, dict) else None

    log.info("[%s] Done — %d new snapshots saved.", sport_key, saved)
    return saved


def main():
    parser = argparse.ArgumentParser(
        description="Seed data/historical_odds/ from The Odds API historical endpoints."
    )
    parser.add_argument("--sport", type=str, default=None,
                        help="Comma-separated arb-bot sport keys (e.g. mlb,nba). "
                             "Default: ODDS_API_ACTIVE_SPORTS.")
    parser.add_argument("--start", type=str,
                        default=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        help="ISO start timestamp (default: now UTC). "
                             "First snapshot fetched at-or-before this time.")
    parser.add_argument("--end", type=str, default=None,
                        help="ISO end timestamp. Walk stops once cursor < this. "
                             "Default: no end (bounded only by --max-snapshots).")
    parser.add_argument("--markets", type=str, default="h2h",
                        help="Comma-separated markets (default: h2h). "
                             "Try h2h,spreads,totals.")
    parser.add_argument("--regions", type=str, default="us",
                        help="Region (default: us)")
    parser.add_argument("--max-snapshots", type=int, default=24,
                        help="Per-sport cap on snapshots fetched (default: 24)")
    parser.add_argument("--quota-floor", type=int, default=100,
                        help="Stop fetching when remaining quota drops below this "
                             "(default: 100)")
    parser.add_argument("--overwrite", action="store_true",
                        help="Re-fetch and overwrite existing snapshot files")
    args = parser.parse_args()

    sports = (
        [s.strip() for s in args.sport.split(",")] if args.sport
        else list(ODDS_API_ACTIVE_SPORTS)
    )

    client = OddsAPIClient()
    log.info("Seeding historical snapshots:")
    log.info("  Sports:        %s", sports)
    log.info("  Start:         %s", args.start)
    log.info("  End:           %s", args.end or "(walk until max/null)")
    log.info("  Markets:       %s", args.markets)
    log.info("  Regions:       %s", args.regions)
    log.info("  Max/sport:     %d", args.max_snapshots)
    log.info("  Quota floor:   %d", args.quota_floor)
    log.info("  Output:        %s", os.path.abspath(HISTORICAL_ODDS_DIR))

    total_saved = 0
    for sport in sports:
        saved = _seed_sport(
            client=client,
            sport=sport,
            start_iso=args.start,
            end_iso=args.end,
            markets=args.markets,
            regions=args.regions,
            max_snapshots=args.max_snapshots,
            quota_floor=args.quota_floor,
            overwrite=args.overwrite,
        )
        total_saved += saved

    remaining = client.quota_remaining()
    log.info(
        "Done. %d snapshots saved across %d sports. Quota remaining: %s",
        total_saved, len(sports),
        remaining if remaining is not None else "unknown",
    )


if __name__ == "__main__":
    main()
