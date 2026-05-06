#!/usr/bin/env python3
"""
backtest.py — Two modes for replaying historical data through the bot.

MODES
─────
timeline (default)
    Replay seeded Odds API snapshots into a per-(snapshot, event) consensus
    timeline. Foundation for any deeper backtest. Reads
    data/historical_odds/{sport_key}/*.json. Optional --csv export.

strategy-replay
    Re-evaluate every settled trade in sports_paper_trades against every
    strategy in config/strategies.py. Tells you what each strategy WOULD
    have decided given the same observed opportunity, plus the simulated
    P&L using each strategy's own Kelly fraction and per-trade cap.
    Optional --enrich attempts to use seeded Kalshi candles for entry
    price (when available); falls back to the trade's recorded kalshi_ask.

USAGE
─────
    cd arb-bot
    python scripts/backtest.py                                  # timeline (all sports)
    python scripts/backtest.py --sport mlb --csv                # one sport, export CSV

    python scripts/backtest.py --mode strategy-replay           # all 4 strategies
    python scripts/backtest.py --mode strategy-replay --enrich  # use Kalshi candles when present
    python scripts/backtest.py --mode strategy-replay --strategies v2,v4

WHAT THIS DOES NOT DO (yet)
───────────────────────────
- Snapshot-based fair_prob enrichment. To re-derive fair_prob from a
  matching Odds API snapshot at opened_at, we'd need to parse the
  trade's KXMVE title, price each leg from the snapshot, and apply the
  same-game correlation uplift — same pipeline the live scanner runs.
  Worth adding once seeded snapshots overlap settled-trade timestamps.
"""

import argparse
import csv
import json
import logging
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from glob import glob
from typing import Iterable, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("backtest")

from clients.odds_api import OddsAPIClient, SPORTS, DEFAULT_BOOKMAKERS
from config.settings import (
    HISTORICAL_ODDS_DIR,
    ODDS_API_ACTIVE_SPORTS,
    KALSHI_TAKER_FEE,
    SQLITE_DB_PATH,
)
from config.strategies import STRATEGIES, all_versions

HISTORICAL_KALSHI_DIR = "data/historical_kalshi/"


# ═════════════════════════════════════════════════════════════════════
# Mode 1: timeline replay
# ═════════════════════════════════════════════════════════════════════

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
    """Walk all snapshots for one sport and emit one row per (snapshot, event)."""
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


def summarize_timeline(sport_key: str, rows: list[dict]) -> None:
    if not rows:
        print(f"  {sport_key}: no rows")
        return

    snapshots = sorted({r["snapshot_iso"] for r in rows})
    events    = sorted({r["event_id"]     for r in rows})
    books = [r["n_books_h2h"] for r in rows if r["n_books_h2h"] > 0]
    avg_books = (sum(books) / len(books)) if books else 0
    min_books = min(books) if books else 0
    max_books = max(books) if books else 0

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


def run_timeline(args) -> None:
    sports = (
        [s.strip() for s in args.sport.split(",")] if args.sport
        else list(ODDS_API_ACTIVE_SPORTS)
    )
    sport_keys = [SPORTS.get(s, s) for s in sports]

    client = OddsAPIClient.__new__(OddsAPIClient)
    # Bypass __init__ — we only call methods that operate on on-disk dicts,
    # so a missing ODDS_API_KEY shouldn't block pure offline replay.

    print("Backtest timeline replay:")
    print(f"  Source: {os.path.abspath(HISTORICAL_ODDS_DIR)}")
    print(f"  Sports: {sport_keys}\n")

    all_rows: list[dict] = []
    for sport_key in sport_keys:
        rows = build_timeline(sport_key, client)
        summarize_timeline(sport_key, rows)
        all_rows.extend(rows)

    print(f"\nTotal rows across all sports: {len(all_rows)}")

    if args.csv:
        write_csv(all_rows, args.csv)


# ═════════════════════════════════════════════════════════════════════
# Mode 2: strategy replay
# ═════════════════════════════════════════════════════════════════════

def _load_settled_trades() -> list[dict]:
    """Read every settled trade from the DB."""
    conn = sqlite3.connect(SQLITE_DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT kalshi_ticker, opened_at, sport, kalshi_side, kalshi_ask, "
        "       cost_per_contr, fair_prob, net_edge, net_edge_pct, "
        "       legs_total, books_used, outcome, strategy_version "
        "FROM sports_paper_trades "
        "WHERE outcome IN ('won', 'lost') "
        "ORDER BY opened_at ASC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _load_kalshi_candle(ticker: str) -> Optional[dict]:
    """Try to load a candle file for one ticker. Returns None if absent."""
    path = os.path.join(HISTORICAL_KALSHI_DIR, f"{ticker}.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _entry_price_from_candle(candle_envelope: dict, side: str,
                              opened_at_iso: str) -> Optional[float]:
    """
    From a Kalshi candle envelope, return the open price of the candle bucket
    nearest to opened_at, on the requested side. Returns 0–1 float or None.
    """
    candles = candle_envelope.get("candlesticks") or []
    if not candles:
        return None
    try:
        opened_ts = int(datetime.fromisoformat(
            opened_at_iso.replace("Z", "+00:00")
        ).timestamp())
    except Exception:
        return None

    # Find candle whose end_period_ts is closest to opened_ts
    best = min(candles, key=lambda c: abs(c.get("end_period_ts", 0) - opened_ts))
    bucket_key = "yes_ask" if side == "yes" else "yes_bid"  # for NO side, taker buys yes_bid worth → use 1 - yes_bid
    bucket = best.get(bucket_key) or {}
    open_str = bucket.get("open_dollars")
    if open_str in (None, "", "0.0000"):
        return None
    try:
        price = float(open_str)
    except ValueError:
        return None
    if side == "no":
        # Taker buying NO pays (1 - best_yes_bid) ... but Kalshi candle
        # data exposes both sides directly. For NO, the candle's yes_bid
        # close is essentially what a NO-buyer would face. Use as-is and
        # convert to NO-side price.
        return max(0.0, min(1.0, 1.0 - price))
    return max(0.0, min(1.0, price))


def _passes_filters(strategy, trade: dict) -> tuple[bool, str]:
    """Apply strategy-level filter checks. Returns (passed, reason_if_not)."""
    if trade["net_edge"] is None or trade["net_edge"] < strategy.min_net_edge:
        return False, "below_min_edge"
    # max_trusted_edge_pct is stored as a percentage (8 = 8%); net_edge_pct same scale
    if (trade["net_edge_pct"] or 0) > strategy.max_trusted_edge_pct:
        return False, "above_edge_cap"
    if (trade["legs_total"] or 0) > strategy.max_legs:
        return False, "too_many_legs"
    if trade["sport"] in strategy.excluded_sports:
        return False, "sport_excluded"
    if trade["kalshi_side"] not in strategy.allowed_sides:
        return False, "side_disallowed"
    if (trade["books_used"] or 0) < strategy.min_books:
        return False, "too_few_books"
    return True, ""


def _simulate_size(strategy, net_edge: float, cost_per_contract: float,
                    bankroll: float) -> tuple[int, float]:
    """Kelly sizing using strategy.kelly_fraction. Returns (contracts, stake)."""
    if net_edge <= 0 or cost_per_contract <= 0 or cost_per_contract >= 1:
        return 0, 0.0
    f_star = net_edge / (1.0 - cost_per_contract)
    f_kelly = f_star * strategy.kelly_fraction
    kelly_usd = max(0.0, bankroll * f_kelly)
    capped_usd = min(kelly_usd, strategy.max_per_trade_usd)
    contracts = max(1, int(capped_usd / cost_per_contract))
    return contracts, contracts * cost_per_contract


def _simulate_pnl(contracts: int, cost_per_contract: float, outcome: str) -> float:
    """Paper-trade PnL: win pays (1 - cost) per contract, loss = -stake."""
    stake = contracts * cost_per_contract
    if outcome == "won":
        return contracts * (1.0 - cost_per_contract)
    return -stake


def run_strategy_replay(args) -> None:
    """Replay each settled trade through each requested strategy."""
    # Resolve which strategies to evaluate
    if args.strategies:
        names = [s.strip() for s in args.strategies.split(",")]
        strategies = [STRATEGIES[n] for n in names if n in STRATEGIES]
    else:
        strategies = all_versions()

    trades = _load_settled_trades()
    if not trades:
        print("No settled trades in DB — nothing to replay.")
        return

    coverage = {"candle_match": 0, "candle_used": 0, "fallback": 0}

    # results[strategy_name] = { placed, won, lost, stake, pnl, rejected_reasons{} }
    results: dict = {
        s.name: {
            "placed": 0, "won": 0, "lost": 0,
            "stake": 0.0, "pnl": 0.0,
            "rejected": defaultdict(int),
        }
        for s in strategies
    }

    for trade in trades:
        # Try to enrich entry price from a seeded Kalshi candle
        cost_per_contract = trade["cost_per_contr"]
        if args.enrich:
            candle_env = _load_kalshi_candle(trade["kalshi_ticker"])
            if candle_env is not None:
                coverage["candle_match"] += 1
                price = _entry_price_from_candle(
                    candle_env, trade["kalshi_side"], trade["opened_at"],
                )
                if price is not None and 0.0 < price < 1.0:
                    cost_per_contract = price * (1.0 + KALSHI_TAKER_FEE)
                    coverage["candle_used"] += 1
                else:
                    coverage["fallback"] += 1
            else:
                coverage["fallback"] += 1

        for strat in strategies:
            ok, reason = _passes_filters(strat, trade)
            if not ok:
                results[strat.name]["rejected"][reason] += 1
                continue

            contracts, stake = _simulate_size(
                strat,
                net_edge=trade["net_edge"],
                cost_per_contract=cost_per_contract,
                bankroll=strat.max_total_deployed_usd,
            )
            if contracts == 0:
                results[strat.name]["rejected"]["zero_contracts"] += 1
                continue

            pnl = _simulate_pnl(contracts, cost_per_contract, trade["outcome"])
            r = results[strat.name]
            r["placed"] += 1
            r["stake"]  += stake
            r["pnl"]    += pnl
            if trade["outcome"] == "won":
                r["won"]  += 1
            else:
                r["lost"] += 1

    # Print results
    print(f"\nStrategy replay over {len(trades)} settled trades")
    print("=" * 78)
    print(f"{'Strategy':<8} {'Placed':>7} {'Won':>5} {'Lost':>5} "
          f"{'Stake':>10} {'P&L':>10} {'ROI':>8} {'Win%':>6}")
    print("-" * 78)
    for s in strategies:
        r = results[s.name]
        roi  = (r["pnl"] / r["stake"] * 100) if r["stake"] > 0 else 0.0
        wr   = (r["won"] / r["placed"] * 100) if r["placed"] > 0 else 0.0
        sign = "+" if r["pnl"] >= 0 else ""
        print(f"{s.name:<8} {r['placed']:>7} {r['won']:>5} {r['lost']:>5} "
              f"${r['stake']:>9,.2f} {sign}${r['pnl']:>8,.2f} "
              f"{roi:>+7.2f}% {wr:>5.1f}%")

    if args.verbose:
        print("\nRejection reasons (per strategy, decisions where filters declined):")
        for s in strategies:
            r = results[s.name]
            if r["rejected"]:
                breakdown = ", ".join(f"{k}={v}" for k, v in
                                      sorted(r["rejected"].items()))
                print(f"  {s.name}: {breakdown}")

    if args.enrich:
        n = len(trades)
        print(f"\nEnrichment coverage:")
        print(f"  Kalshi candle file matched: {coverage['candle_match']}/{n} "
              f"({100*coverage['candle_match']/n:.1f}%)")
        print(f"  Candle price used:          {coverage['candle_used']}/{n} "
              f"({100*coverage['candle_used']/n:.1f}%)")
        if coverage["candle_match"] == 0:
            print("  (Run scripts/seed_kalshi_historical.py --from-trades "
                  "to backfill candle data.)")


# ═════════════════════════════════════════════════════════════════════
# CLI
# ═════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Replay seeded historical data through the bot."
    )
    parser.add_argument("--mode", choices=["timeline", "strategy-replay"],
                        default="timeline",
                        help="timeline (consensus drift) or strategy-replay "
                             "(per-strategy P&L over settled trades)")

    # timeline-mode flags
    parser.add_argument("--sport", type=str, default=None,
                        help="[timeline] Comma-separated arb-bot sport keys.")
    parser.add_argument("--csv", type=str, nargs="?",
                        const="data/backtest_timeline.csv", default=None,
                        help="[timeline] Write combined CSV.")

    # strategy-replay flags
    parser.add_argument("--strategies", type=str, default=None,
                        help="[strategy-replay] Comma-separated names "
                             "(default: all defined in config/strategies.py)")
    parser.add_argument("--enrich", action="store_true",
                        help="[strategy-replay] Use seeded Kalshi candles for "
                             "entry-price enrichment when available.")
    parser.add_argument("--verbose", action="store_true",
                        help="[strategy-replay] Print rejection-reason breakdown.")

    args = parser.parse_args()

    if args.mode == "timeline":
        run_timeline(args)
    else:
        run_strategy_replay(args)


if __name__ == "__main__":
    main()
