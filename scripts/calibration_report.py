#!/usr/bin/env python3
"""
calibration_report.py — Compare predicted edge to realized win rate across buckets.

Answers the question: "When we estimate 6% edge, what does the market actually pay?"

If a bucket's calibration_factor < 1.0, our formula is optimistic → Kelly is oversized.
If a bucket's calibration_factor > 1.0, our formula is conservative → we're leaving size on.

Also reports average CLV (Closing Line Value) per bucket — the gold standard proof of edge.
Positive average CLV = we consistently buy into markets that confirm our direction.

Usage:
    cd arb-bot
    python scripts/calibration_report.py
    python scripts/calibration_report.py --min-trades 5        # require min 5 trades per bucket
    python scripts/calibration_report.py --strategy v2         # filter to one strategy
    python scripts/calibration_report.py --sport mlb           # filter to one sport
    python scripts/calibration_report.py --export csv          # write calibration.csv
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import SQLITE_DB_PATH


# ─────────────────────────────────────────────────────────────────────────────
# Edge buckets
# ─────────────────────────────────────────────────────────────────────────────

EDGE_BUCKETS = [
    (0.00, 0.02, "0-2%"),
    (0.02, 0.04, "2-4%"),
    (0.04, 0.06, "4-6%"),
    (0.06, 0.08, "6-8%"),
    (0.08, 0.12, "8-12%"),
    (0.12, 1.00, "12%+"),
]


def predicted_win_prob(trade: dict) -> float:
    """
    Our predicted probability of WINNING this trade.

    For YES trades: equals fair_prob (we win when YES outcome happens).
    For NO  trades: equals (1 - fair_prob) (we win when NO outcome happens).

    fair_prob is always stored as the parlay's YES probability, regardless
    of which side we traded.
    """
    fp = trade.get("fair_prob") or 0.0
    if trade.get("kalshi_side") == "no":
        return 1.0 - fp
    return fp


def bucket_label(net_edge_pct: float) -> str:
    """Map a net_edge_pct (e.g. 5.2) to its bucket label."""
    e = net_edge_pct / 100.0
    for lo, hi, label in EDGE_BUCKETS:
        if lo <= e < hi:
            return label
    return "12%+"


# ─────────────────────────────────────────────────────────────────────────────
# Queries
# ─────────────────────────────────────────────────────────────────────────────

def fetch_settled(conn: sqlite3.Connection, strategy: str = None, sport: str = None) -> list:
    """Return all settled (won/lost) trades, optionally filtered."""
    where = ["outcome IN ('won', 'lost')"]
    params = []
    if strategy:
        where.append("strategy_version = ?")
        params.append(strategy)
    if sport:
        where.append("sport = ?")
        params.append(sport)

    sql = f"""
        SELECT
            id, sport, strategy_version, kalshi_side,
            net_edge_pct, fair_prob, kalshi_ask,
            contracts, total_stake, actual_profit,
            outcome, opened_at, resolved_at,
            kalshi_closing_ask, kalshi_closing_no_ask, clv
        FROM sports_paper_trades
        WHERE {' AND '.join(where)}
        ORDER BY opened_at
    """
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# Analysis
# ─────────────────────────────────────────────────────────────────────────────

def build_buckets(trades: list, min_trades: int = 1) -> list:
    """
    Group trades into edge buckets and compute calibration metrics.

    Returns list of bucket dicts sorted by edge range.
    """
    groups: dict = {}

    for t in trades:
        lbl = bucket_label(t["net_edge_pct"])
        if lbl not in groups:
            groups[lbl] = []
        groups[lbl].append(t)

    result = []
    for lo, hi, lbl in EDGE_BUCKETS:
        bucket_trades = groups.get(lbl, [])
        n = len(bucket_trades)
        if n < min_trades:
            continue

        wins   = sum(1 for t in bucket_trades if t["outcome"] == "won")
        losses = n - wins

        actual_wr      = wins / n
        predicted_wr   = sum(predicted_win_prob(t) for t in bucket_trades) / n
        calibration_f  = actual_wr / predicted_wr if predicted_wr > 0 else None

        avg_edge_pct   = sum(t["net_edge_pct"] for t in bucket_trades) / n
        total_stake    = sum(t["total_stake"] for t in bucket_trades)
        total_profit   = sum(t["actual_profit"] for t in bucket_trades)
        roi_pct        = (total_profit / total_stake * 100) if total_stake > 0 else 0.0

        # CLV stats (only trades where CLV was captured)
        clv_vals = [t["clv"] for t in bucket_trades if t.get("clv") is not None]
        avg_clv  = sum(clv_vals) / len(clv_vals) if clv_vals else None

        result.append({
            "bucket":         lbl,
            "n":              n,
            "wins":           wins,
            "losses":         losses,
            "actual_wr":      actual_wr,
            "predicted_wr":   predicted_wr,
            "calibration_f":  calibration_f,
            "avg_edge_pct":   avg_edge_pct,
            "total_stake":    total_stake,
            "total_profit":   total_profit,
            "roi_pct":        roi_pct,
            "avg_clv":        avg_clv,
            "clv_n":          len(clv_vals),
        })

    return result


def build_sport_breakdown(trades: list, min_trades: int = 1) -> list:
    """Per-sport calibration summary."""
    groups: dict = {}
    for t in trades:
        sport = t.get("sport") or "unknown"
        groups.setdefault(sport, []).append(t)

    result = []
    for sport, st in sorted(groups.items()):
        n = len(st)
        if n < min_trades:
            continue
        wins         = sum(1 for t in st if t["outcome"] == "won")
        actual_wr    = wins / n
        predicted_wr = sum(predicted_win_prob(t) for t in st) / n
        cal_f        = actual_wr / predicted_wr if predicted_wr > 0 else None
        total_stake  = sum(t["total_stake"] for t in st)
        total_profit = sum(t["actual_profit"] for t in st)
        roi          = (total_profit / total_stake * 100) if total_stake > 0 else 0.0
        clv_vals     = [t["clv"] for t in st if t.get("clv") is not None]
        avg_clv      = sum(clv_vals) / len(clv_vals) if clv_vals else None

        result.append({
            "sport":        sport,
            "n":            n,
            "wins":         wins,
            "losses":       n - wins,
            "actual_wr":    actual_wr,
            "predicted_wr": predicted_wr,
            "cal_f":        cal_f,
            "roi_pct":      roi,
            "avg_clv":      avg_clv,
        })

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Display
# ─────────────────────────────────────────────────────────────────────────────

def _clv_str(avg_clv) -> str:
    if avg_clv is None:
        return "    n/a"
    sign = "+" if avg_clv >= 0 else ""
    return f"{sign}{avg_clv:.4f}"


def print_report(buckets: list, sport_rows: list, trades: list, filters: str):
    n_settled = len(trades)
    n_clv     = sum(1 for t in trades if t.get("clv") is not None)
    wins      = sum(1 for t in trades if t["outcome"] == "won")
    overall_wr = wins / n_settled if n_settled else 0
    overall_pred = sum(predicted_win_prob(t) for t in trades) / n_settled if n_settled else 0

    print(f"\n{'='*75}")
    print(f"  CALIBRATION REPORT{filters}")
    print(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Settled trades: {n_settled}  |  CLV captured: {n_clv}  |  "
          f"Overall win rate: {overall_wr:.1%} (predicted {overall_pred:.1%})")
    print(f"{'='*75}\n")

    # Edge bucket table
    print("  BY EDGE BUCKET")
    print(f"  {'Bucket':<8} {'N':>4} {'W':>4} {'L':>4}  "
          f"{'ActWR':>6} {'PredWR':>6} {'CalibF':>7}  "
          f"{'AvgEdge':>8} {'ROI%':>6}  {'AvgCLV':>8}")
    print("  " + "-" * 70)

    for b in buckets:
        cal_str = f"{b['calibration_f']:.3f}" if b["calibration_f"] is not None else "  n/a"
        flag    = " !" if b["calibration_f"] is not None and b["calibration_f"] < 0.80 else ""
        print(
            f"  {b['bucket']:<8} {b['n']:>4} {b['wins']:>4} {b['losses']:>4}  "
            f"{b['actual_wr']:>5.1%} {b['predicted_wr']:>6.1%} {cal_str:>7}  "
            f"{b['avg_edge_pct']:>7.1f}% {b['roi_pct']:>5.1f}%  "
            f"{_clv_str(b['avg_clv']):>8}{flag}"
        )

    if not buckets:
        print("  (no buckets with enough trades — lower --min-trades threshold)")

    # Sport table
    print(f"\n  BY SPORT")
    print(f"  {'Sport':<14} {'N':>4} {'W':>4} {'L':>4}  "
          f"{'ActWR':>6} {'PredWR':>6} {'CalibF':>7}  {'ROI%':>6}  {'AvgCLV':>8}")
    print("  " + "-" * 70)

    for s in sport_rows:
        cal_str = f"{s['cal_f']:.3f}" if s["cal_f"] is not None else "  n/a"
        flag    = " !" if s["cal_f"] is not None and s["cal_f"] < 0.80 else ""
        print(
            f"  {s['sport']:<14} {s['n']:>4} {s['wins']:>4} {s['losses']:>4}  "
            f"{s['actual_wr']:>5.1%} {s['predicted_wr']:>6.1%} {cal_str:>7}  "
            f"{s['roi_pct']:>5.1f}%  {_clv_str(s['avg_clv']):>8}{flag}"
        )

    if not sport_rows:
        print("  (no sports with enough trades)")

    # CLV summary
    clv_trades = [t for t in trades if t.get("clv") is not None]
    if clv_trades:
        avg_clv = sum(t["clv"] for t in clv_trades) / len(clv_trades)
        pos_clv = sum(1 for t in clv_trades if t["clv"] > 0)
        print(f"\n  CLV SUMMARY ({len(clv_trades)} trades with CLV data)")
        print(f"  Average CLV: {avg_clv:+.4f}  |  "
              f"Positive CLV: {pos_clv}/{len(clv_trades)} ({pos_clv/len(clv_trades):.0%})")
        if avg_clv > 0:
            print("  [+] Positive avg CLV — entries are beating the closing line (real edge signal)")
        else:
            print("  [-] Negative avg CLV — market moves against entries on average (review edge formula)")
    else:
        print(f"\n  CLV: no data yet (captured on next resolve cycle after today's trades settle)")

    # Calibration guidance
    bad_buckets = [b for b in buckets
                   if b["calibration_f"] is not None and b["calibration_f"] < 0.80]
    if bad_buckets:
        print(f"\n  !  CALIBRATION FLAGS (factor < 0.80 — Kelly oversized for these buckets):")
        for b in bad_buckets:
            print(f"     {b['bucket']}: predicted {b['predicted_wr']:.1%} WR, "
                  f"actual {b['actual_wr']:.1%} — factor {b['calibration_f']:.3f}")
        print(f"     -> Consider adding CALIBRATION_OVERRIDES to config/settings.py")

    print()


def export_csv(buckets: list, sport_rows: list, path: str):
    import csv
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["type", "group", "n", "wins", "losses",
                         "actual_wr", "predicted_wr", "calibration_factor",
                         "roi_pct", "avg_clv"])
        for b in buckets:
            writer.writerow(["edge_bucket", b["bucket"], b["n"], b["wins"], b["losses"],
                              f"{b['actual_wr']:.4f}", f"{b['predicted_wr']:.4f}",
                              f"{b['calibration_f']:.4f}" if b["calibration_f"] else "",
                              f"{b['roi_pct']:.2f}",
                              f"{b['avg_clv']:.4f}" if b["avg_clv"] is not None else ""])
        for s in sport_rows:
            writer.writerow(["sport", s["sport"], s["n"], s["wins"], s["losses"],
                             f"{s['actual_wr']:.4f}", f"{s['predicted_wr']:.4f}",
                             f"{s['cal_f']:.4f}" if s["cal_f"] else "",
                             f"{s['roi_pct']:.2f}",
                             f"{s['avg_clv']:.4f}" if s["avg_clv"] is not None else ""])
    print(f"  Exported to {path}")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Calibration report: predicted edge vs realized win rate"
    )
    parser.add_argument("--min-trades", type=int, default=3,
                        help="Minimum settled trades per bucket to include (default: 3)")
    parser.add_argument("--strategy",   type=str, default=None,
                        help="Filter to one strategy version (e.g. v1)")
    parser.add_argument("--sport",      type=str, default=None,
                        help="Filter to one sport (e.g. mlb)")
    parser.add_argument("--export",     type=str, default=None,
                        choices=["csv"], help="Export report to file")
    args = parser.parse_args()

    if not os.path.exists(SQLITE_DB_PATH):
        print(f"No database at {SQLITE_DB_PATH}. Run a paper test first.")
        sys.exit(1)

    conn = sqlite3.connect(SQLITE_DB_PATH)
    conn.row_factory = sqlite3.Row

    # Check CLV columns exist (migration may not have run yet)
    try:
        conn.execute("SELECT clv FROM sports_paper_trades LIMIT 1")
    except sqlite3.OperationalError:
        print("  Note: CLV columns not yet present. Run resolve_trades.py once to migrate.")
        print("  Showing report without CLV data.\n")

    trades     = fetch_settled(conn, strategy=args.strategy, sport=args.sport)
    conn.close()

    if not trades:
        filter_desc = ""
        if args.strategy: filter_desc += f" strategy={args.strategy}"
        if args.sport:    filter_desc += f" sport={args.sport}"
        print(f"No settled trades found{filter_desc}. Run resolve_trades.py first.")
        sys.exit(0)

    buckets    = build_buckets(trades, min_trades=args.min_trades)
    sport_rows = build_sport_breakdown(trades, min_trades=args.min_trades)

    filters = ""
    if args.strategy: filters += f" [strategy={args.strategy}]"
    if args.sport:    filters += f" [sport={args.sport}]"

    print_report(buckets, sport_rows, trades, filters)

    if args.export == "csv":
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = f"data/calibration_{ts}.csv"
        export_csv(buckets, sport_rows, path)


if __name__ == "__main__":
    main()
