#!/usr/bin/env python3
"""
analyze_performance.py — Slice settled paper trades to find what works.

Buckets settled trades by side, sport, edge size, # legs, and books_used,
then prints win rate + ROI for each slice. Use the results to add filters
to sports_paper_test.py.

Usage:
    python scripts/analyze_performance.py
"""

import os
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import SQLITE_DB_PATH


def fetch_settled():
    conn = sqlite3.connect(SQLITE_DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM sports_paper_trades "
        "WHERE outcome IN ('won','lost') ORDER BY id"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def bucket_stats(trades, key_fn, label):
    """Group trades by key_fn(t) and print W/L/ROI per bucket."""
    buckets = defaultdict(list)
    for t in trades:
        buckets[key_fn(t)].append(t)

    print()
    print(f"  -- By {label} " + "-" * (50 - len(label)))
    print(f"  {label:<22} {'N':>4} {'W':>3} {'L':>3} {'Win%':>5}  "
          f"{'Stake':>8}  {'P&L':>9}  {'ROI':>6}")
    print("  " + "-" * 70)

    for k in sorted(buckets.keys(), key=lambda x: str(x)):
        ts = buckets[k]
        n  = len(ts)
        w  = sum(1 for t in ts if t["outcome"] == "won")
        l  = n - w
        stake = sum(t["total_stake"] for t in ts)
        pnl   = sum(t["actual_profit"] or 0 for t in ts)
        wr  = w / n * 100 if n else 0
        roi = pnl / stake * 100 if stake else 0
        print(f"  {str(k):<22} {n:>4} {w:>3} {l:>3} {wr:>4.0f}%  "
              f"${stake:>7.2f}  ${pnl:>+8.2f}  {roi:>+5.1f}%")


def edge_bucket(t):
    e = t["net_edge_pct"]
    if   e <  2: return "1.5-2%"
    elif e <  4: return "2-4%"
    elif e <  7: return "4-7%"
    elif e < 12: return "7-12%"
    else:        return "12%+"


def stake_bucket(t):
    s = t["total_stake"]
    if   s <  5: return "<$5"
    elif s < 15: return "$5-15"
    elif s < 30: return "$15-30"
    elif s < 45: return "$30-45"
    else:        return "$45+"


def cost_bucket(t):
    c = t["cost_per_contr"]
    if   c < 0.10: return "$0.00-0.10"
    elif c < 0.25: return "$0.10-0.25"
    elif c < 0.50: return "$0.25-0.50"
    elif c < 0.75: return "$0.50-0.75"
    else:           return "$0.75+"


def legs_bucket(t):
    n = t["legs_total"] or 0
    return f"{n} legs"


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Slice settled paper trades by side, sport, edge, etc."
    )
    parser.add_argument("--version", type=str, default=None,
                        help="Restrict report to one strategy version (e.g. v1)")
    args = parser.parse_args()

    trades = fetch_settled()
    if not trades:
        print("  No settled trades yet. Run resolve_trades.py first.")
        return

    if args.version:
        trades = [t for t in trades if (t.get("strategy_version") or "") == args.version]
        if not trades:
            print(f"  No settled trades for strategy '{args.version}'.")
            return

    n   = len(trades)
    w   = sum(1 for t in trades if t["outcome"] == "won")
    l   = n - w
    stake = sum(t["total_stake"] for t in trades)
    pnl   = sum(t["actual_profit"] or 0 for t in trades)
    label = f"strategy={args.version}" if args.version else "all strategies"
    print()
    print(f"  ============= PERFORMANCE BREAKDOWN ({n} settled trades, {label}) =============")
    print(f"  Overall: {w}W / {l}L = {w/n*100:.0f}% win rate  |  "
          f"P&L ${pnl:+.2f} on ${stake:.2f} deployed = {pnl/stake*100:+.1f}% ROI")

    # Show per-version breakdown FIRST when looking at the full set
    if not args.version:
        bucket_stats(trades, lambda t: t.get("strategy_version") or "?", "Strategy version")
    bucket_stats(trades, lambda t: t["kalshi_side"].upper(),       "Side (YES/NO)")
    bucket_stats(trades, lambda t: t["sport"] or "?",              "Sport")
    bucket_stats(trades, edge_bucket,                              "Edge size")
    bucket_stats(trades, lambda t: f"{t['books_used']} books",     "Books used")
    bucket_stats(trades, legs_bucket,                              "# Legs")
    bucket_stats(trades, cost_bucket,                              "Cost per contract")
    bucket_stats(trades, stake_bucket,                             "Stake size")

    print()
    print("  Tip: any slice with negative ROI is a candidate to filter out.")
    print("       Any slice with high N + positive ROI is your real edge.")
    print(f"       Use --version v2 to drill into a single strategy.")
    print()


if __name__ == "__main__":
    main()
