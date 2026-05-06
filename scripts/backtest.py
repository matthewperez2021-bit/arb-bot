#!/usr/bin/env python3
"""
backtest.py — Replay seeded historical Odds API snapshots into a structured
consensus timeline.

Reads:    data/historical_odds/{sport_key}/{iso}.json
            (envelopes saved by scripts/seed_odds_api_historical.py)
Writes:   data/backtest_timeline.csv  (with --csv)
Outputs:  printed summary per sport — # snapshots, events, books, drift

For each (snapshot, event), this devigs the h2h market across DEFAULT_BOOKMAKERS
and records (timestamp, event_id, home_prob, away_prob, n_books). Same math the
live scanner uses in OddsAPIClient.extract_consensus_probability — just replayed
over snapshots instead of called against the live endpoint.

Usage:
    cd arb-bot
    python scripts/backtest.py                          # all sports on disk
    python scripts/backtest.py --sport mlb              # one sport
    python scripts/backtest.py --sport mlb,nba --csv    # export combined CSV

What this DOES today:
  - Builds a per-event timeline of devigged consensus h2h probabilities
  - Surfaces how much consensus drifted between earliest/latest snapshot
  - Foundation for any deeper backtest

What this does NOT do (yet):
  - No Kalshi historical prices on disk → no full strategy P&L replay.
    Kalshi exposes /markets/{ticker}/candlesticks; a sibling seeder for
    that would unlock full v1/v2/v3/v4 what-if simulation.
"""

import argparse
import csv
import json
import logging
import os
import sys
from collections import defaultdict
from glob import glob
from typing import Iterable

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("backtest")

from clients.odds_api import OddsAPIClient, SPORTS, DEFAULT_BOOKMAKERS
from config.settings import HISTORICAL_ODDS_DIR, ODDS_API_ACTIVE_SPORTS


def _list_snapshots(sport_key: str) -> list[str]:
    """Return sorted list of snapshot file paths for one sport_key."""
    pattern = os.path.join(HISTORICAL_ODDS_DIR, sport_key, "*.json")
    return sorted(glob(pattern))


def _count_h2h_books(event: dict, books_to_use: set) -> int:
    """How many of the requested bookmakers contributed an h2h market."""
    n = 0
    for book in event.get("bookmakers", []):
        if book.get("key") not in books_to_use:
            continue
        for market in book.get("markets", []):
            if market.get("key") == "h2h" and len(market.get("outcomes", [])) >= 2:
                n += 1
                break
    return n


def build_timeline(sport_key: str, client: OddsAPIClient) -> list[dict]:
    """
    Walk all snapshots for one sport and emit one row per (snapshot, event).
    Returns a list of dicts with consensus probabilities.
    """
    rows: list[dict] = []
    books_to_use = set(DEFAULT_BOOKMAKERS)

    for path in _list_snapshots(sport_key):
        with open(path, "r", encoding="utf-8") as f:
            envelope = json.load(f)

        ts = envelope.get("timestamp") or os.path.basename(path).rstrip(".json")
        for event in envelope.get("data", []) or []:
            home = event.get("home_team")
            away = event.get("away_team")
            if not home or not away:
                continue

            home_prob = client.extract_consensus_probability(event, home)
            away_prob = client.extract_consensus_probability(event, away)
            if home_prob is None and away_prob is None:
                continue

            rows.append({
                "snapshot_iso":  ts,
                "sport_key":     event.get("sport_key", sport_key),
                "event_id":      event.get("id", ""),
                "commence_time": event.get("commence_time", ""),
                "home_team":     home,
                "away_team":     away,
                "home_prob":     round(home_prob, 4) if home_prob is not None else None,
                "away_prob":     round(away_prob, 4) if away_prob is not None else None,
                "n_books_h2h":   _count_h2h_books(event, books_to_use),
            })

    return rows


def summarize(sport_key: str, rows: list[dict]) -> None:
    """Print compact summary for one sport's timeline."""
    if not rows:
        print(f"  {sport_key}: no rows")
        return

    snapshots = sorted({r["snapshot_iso"] for r in rows})
    events    = sorted({r["event_id"]     for r in rows})

    # books per row
    books = [r["n_books_h2h"] for r in rows if r["n_books_h2h"] > 0]
    avg_books = (sum(books) / len(books)) if books else 0
    min_books = min(books) if books else 0
    max_books = max(books) if books else 0

    # drift: per event, range(home_prob across snapshots)
    by_event: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        if r["home_prob"] is not None:
            by_event[r["event_id"]].append(r["home_prob"])
    drifts = [max(probs) - min(probs) for probs in by_event.values() if len(probs) >= 2]
    avg_drift = (sum(drifts) / len(drifts)) if drifts else 0

    print(f"  {sport_key}:")
    print(f"    snapshots:    {len(snapshots)}")
    print(f"    rows:         {len(rows)}")
    print(f"    unique events:{len(events)}")
    print(f"    books/event:  avg={avg_books:.1f}  min={min_books}  max={max_books}")
    if drifts:
        print(f"    home_prob drift across snapshots: avg={avg_drift:.3f} "
              f"(over {len(drifts)} multi-snapshot events)")
    else:
        print(f"    home_prob drift: n/a (need ≥2 snapshots per event)")


def write_csv(all_rows: Iterable[dict], path: str) -> None:
    rows = list(all_rows)
    if not rows:
        log.warning("No rows to write — skipping CSV.")
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fieldnames = [
        "snapshot_iso", "sport_key", "event_id", "commence_time",
        "home_team", "away_team", "home_prob", "away_prob", "n_books_h2h",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    log.info("Wrote %d rows → %s", len(rows), path)


def main():
    parser = argparse.ArgumentParser(
        description="Replay seeded Odds API snapshots into a consensus timeline."
    )
    parser.add_argument("--sport", type=str, default=None,
                        help="Comma-separated arb-bot sport keys (e.g. mlb,nba). "
                             "Default: ODDS_API_ACTIVE_SPORTS.")
    parser.add_argument("--csv", type=str, nargs="?",
                        const="data/backtest_timeline.csv",
                        default=None,
                        help="Write combined CSV. Default path: "
                             "data/backtest_timeline.csv")
    args = parser.parse_args()

    sports = (
        [s.strip() for s in args.sport.split(",")] if args.sport
        else list(ODDS_API_ACTIVE_SPORTS)
    )
    sport_keys = [SPORTS.get(s, s) for s in sports]

    client = OddsAPIClient.__new__(OddsAPIClient)
    # We don't need a working API key for replay — only the static + instance
    # methods that operate on already-on-disk event dicts. Bypass __init__ so
    # missing ODDS_API_KEY doesn't error out during pure offline replay.

    print("Backtest timeline replay:")
    print(f"  Source: {os.path.abspath(HISTORICAL_ODDS_DIR)}")
    print(f"  Sports: {sport_keys}\n")

    all_rows: list[dict] = []
    for sport_key in sport_keys:
        rows = build_timeline(sport_key, client)
        summarize(sport_key, rows)
        all_rows.extend(rows)

    print(f"\nTotal rows across all sports: {len(all_rows)}")

    if args.csv:
        write_csv(all_rows, args.csv)


if __name__ == "__main__":
    main()
