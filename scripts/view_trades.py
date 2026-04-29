#!/usr/bin/env python3
"""
view_trades.py — View past paper trades from SQLite.

Usage:
    python scripts/view_trades.py                  # show all sessions
    python scripts/view_trades.py --session latest # show latest session trades
    python scripts/view_trades.py --session sports_1777413392
    python scripts/view_trades.py --all            # show every trade ever
"""

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import SQLITE_DB_PATH


def connect():
    if not os.path.exists(SQLITE_DB_PATH):
        print(f"No database found at {SQLITE_DB_PATH}. Run the paper test first.")
        sys.exit(1)
    conn = sqlite3.connect(SQLITE_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def show_sessions(conn):
    rows = conn.execute("""
        SELECT session_id,
               COUNT(*)            AS trades,
               MIN(opened_at)      AS opened,
               SUM(total_stake)    AS deployed,
               SUM(expected_profit) AS exp_profit,
               AVG(net_edge_pct)   AS avg_edge
        FROM sports_paper_trades
        GROUP BY session_id
        ORDER BY opened DESC
    """).fetchall()

    if not rows:
        print("  No paper trades found yet.")
        return

    print()
    print(f"  {'Session ID':<32} {'Trades':>6}  {'Deployed':>9}  {'ExpProfit':>9}  {'AvgEdge':>7}  Date")
    print("  " + "-" * 85)
    for r in rows:
        print(
            f"  {r['session_id']:<32} {r['trades']:>6}  "
            f"${r['deployed']:>8.2f}  ${r['exp_profit']:>8.4f}  "
            f"{r['avg_edge']:>6.1f}%  {r['opened'][:19]}"
        )
    print()
    print(f"  Total sessions: {len(rows)}")
    total_trades   = sum(r['trades']     for r in rows)
    total_deployed = sum(r['deployed']   for r in rows)
    total_profit   = sum(r['exp_profit'] for r in rows)
    print(f"  Total trades:   {total_trades}")
    print(f"  Total deployed: ${total_deployed:.2f}")
    print(f"  Total exp profit: ${total_profit:.4f}")
    print()
    print("  Tip: run with --session latest (or a specific session ID) to see trade details.")
    print()


def show_trades(conn, session_id: str):
    if session_id == "latest":
        row = conn.execute(
            "SELECT session_id FROM sports_paper_trades ORDER BY opened_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            print("  No trades found.")
            return
        session_id = row["session_id"]

    trades = conn.execute(
        "SELECT * FROM sports_paper_trades WHERE session_id=? ORDER BY id",
        (session_id,)
    ).fetchall()

    if not trades:
        print(f"  No trades found for session '{session_id}'.")
        return

    print()
    print(f"  Session: {session_id}")
    print(f"  Trades:  {len(trades)}")
    print()

    hdr = f"  {'#':>2}  {'Ticker':<50}  {'Side':4}  {'Ask':>5}  {'Fair':>5}  {'Edge':>6}  {'Contr':>5}  {'Stake':>7}  {'ExpProfit':>9}  Sport"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    for t in trades:
        print(
            f"  {t['id']:>2}  {t['kalshi_ticker']:<50}  {t['kalshi_side'].upper():<4}  "
            f"{t['kalshi_ask']:>5.3f}  {t['fair_prob']:>5.3f}  "
            f"{t['net_edge_pct']:>5.1f}%  {t['contracts']:>5}  "
            f"${t['total_stake']:>6.2f}  ${t['expected_profit']:>8.4f}  {t['sport'] or ''}"
        )

    print()
    total_stake  = sum(t['total_stake']    for t in trades)
    total_profit = sum(t['expected_profit'] for t in trades)
    avg_edge     = sum(t['net_edge_pct']    for t in trades) / len(trades)
    print(f"  Deployed:       ${total_stake:.2f}")
    print(f"  Exp profit:     ${total_profit:.4f}")
    print(f"  Avg edge:       {avg_edge:.1f}%")
    print()

    # Show title for each trade
    print("  -- TITLES --")
    for t in trades:
        title = t['kalshi_title'] or t['kalshi_ticker']
        print(f"  #{t['id']:>2}  {title}")
    print()


def show_all(conn):
    trades = conn.execute(
        "SELECT * FROM sports_paper_trades ORDER BY opened_at DESC"
    ).fetchall()

    if not trades:
        print("  No trades found.")
        return

    print()
    print(f"  All {len(trades)} paper trades (most recent first):")
    print()

    hdr = f"  {'#':>4}  {'Session':<22}  {'Date':<16}  {'Side':4}  {'Ask':>5}  {'Fair':>5}  {'Edge':>6}  {'Stake':>7}  {'ExpProfit':>9}  Sport"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    for t in trades:
        date_short = t['opened_at'][:16].replace("T", " ")
        sess_short = t['session_id'].replace("sports_", "s_")[-18:]
        print(
            f"  {t['id']:>4}  {sess_short:<22}  {date_short:<16}  "
            f"{t['kalshi_side'].upper():<4}  {t['kalshi_ask']:>5.3f}  "
            f"{t['fair_prob']:>5.3f}  {t['net_edge_pct']:>5.1f}%  "
            f"${t['total_stake']:>6.2f}  ${t['expected_profit']:>8.4f}  {t['sport'] or ''}"
        )
    print()


def main():
    parser = argparse.ArgumentParser(description="View past paper trades")
    parser.add_argument("--session", type=str, default=None,
                        help="Session ID to view (use 'latest' for most recent)")
    parser.add_argument("--all", action="store_true",
                        help="Show every trade across all sessions")
    args = parser.parse_args()

    conn = connect()

    if args.all:
        show_all(conn)
    elif args.session:
        show_trades(conn, args.session)
    else:
        show_sessions(conn)

    conn.close()


if __name__ == "__main__":
    main()
