#!/usr/bin/env python3
"""
view_trades.py — View past paper trades and live bankroll from SQLite.

Usage:
    python scripts/view_trades.py                  # bankroll + all sessions
    python scripts/view_trades.py --session latest # latest session detail
    python scripts/view_trades.py --session sports_1777413392
    python scripts/view_trades.py --all            # every trade ever
"""

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import SQLITE_DB_PATH, STARTING_CAPITAL_USD


def connect():
    if not os.path.exists(SQLITE_DB_PATH):
        print(f"  No database found at {SQLITE_DB_PATH}. Run the paper test first.")
        sys.exit(1)
    conn = sqlite3.connect(SQLITE_DB_PATH)
    conn.row_factory = sqlite3.Row
    # Make sure resolution columns exist (in case resolve_trades hasn't been run yet)
    for col in [
        "ALTER TABLE sports_paper_trades ADD COLUMN outcome TEXT DEFAULT 'open'",
        "ALTER TABLE sports_paper_trades ADD COLUMN actual_profit REAL DEFAULT NULL",
        "ALTER TABLE sports_paper_trades ADD COLUMN resolved_at TEXT DEFAULT NULL",
        "ALTER TABLE sports_paper_trades ADD COLUMN bankroll_after REAL DEFAULT NULL",
    ]:
        try:
            conn.execute(col)
        except sqlite3.OperationalError:
            pass
    conn.commit()
    return conn


def _outcome_icon(outcome):
    if outcome == "won":   return "[WIN ]"
    if outcome == "lost":  return "[LOSS]"
    return "[open]"


def show_bankroll(conn):
    """Print the current bankroll summary at the top."""
    try:
        row = conn.execute("SELECT * FROM bankroll WHERE id=1").fetchone()
    except sqlite3.OperationalError:
        row = None

    # Fall back to computing from the trades table if bankroll table doesn't exist yet
    if not row:
        settled = conn.execute(
            "SELECT COALESCE(SUM(actual_profit),0) FROM sports_paper_trades "
            "WHERE outcome IN ('won','lost')"
        ).fetchone()[0]
        wins   = conn.execute("SELECT COUNT(*) FROM sports_paper_trades WHERE outcome='won'").fetchone()[0]
        losses = conn.execute("SELECT COUNT(*) FROM sports_paper_trades WHERE outcome='lost'").fetchone()[0]
        current = STARTING_CAPITAL_USD + (settled or 0)
        total   = wins + losses
        win_rate = wins / total * 100 if total else 0
        pnl      = current - STARTING_CAPITAL_USD
        pnl_pct  = pnl / STARTING_CAPITAL_USD * 100
        print()
        print(f"  -- Bankroll --------------------------------------------------")
        print(f"  Starting:        ${STARTING_CAPITAL_USD:>10,.2f}")
        print(f"  Current:         ${current:>10,.2f}  ({'+'if pnl>=0 else ''}{pnl_pct:.1f}%)")
        print(f"  Realised P&L:    {'+'if pnl>=0 else ''}${pnl:>9.2f}")
        print(f"  Settled trades:  {total:>4}  ({wins} won / {losses} lost"
              + (f"  |  {win_rate:.0f}% win rate" if total else "") + ")")
        print(f"  (Run resolve_trades.py to check for new settlements)")
        print()
        return

    r = dict(row)
    pnl      = r["current_capital"] - r["starting_capital"]
    pnl_pct  = pnl / r["starting_capital"] * 100
    win_rate = r["wins"] / r["total_trades"] * 100 if r["total_trades"] else 0
    open_cnt = conn.execute(
        "SELECT COUNT(*) FROM sports_paper_trades WHERE outcome='open' OR outcome IS NULL"
    ).fetchone()[0]

    print()
    print(f"  -- Bankroll --------------------------------------------------")
    print(f"  Starting:        ${r['starting_capital']:>10,.2f}")
    print(f"  Current:         ${r['current_capital']:>10,.2f}  "
          f"({'+'if pnl>=0 else ''}{pnl_pct:.1f}%)")
    print(f"  Realised P&L:    {'+'if pnl>=0 else ''}${pnl:>9.2f}")
    print(f"  Settled trades:  {r['total_trades']:>4}  "
          f"({r['wins']} won / {r['losses']} lost"
          + (f"  |  {win_rate:.0f}% win rate" if r['total_trades'] else "")
          + ")")
    print(f"  Open positions:  {open_cnt:>4}  (run resolve_trades.py to settle)")
    print(f"  Last updated:    {r['last_updated'][:19]}")
    print()


def show_sessions(conn):
    rows = conn.execute("""
        SELECT session_id,
               COUNT(*)                                         AS trades,
               SUM(CASE WHEN outcome='won'  THEN 1 ELSE 0 END) AS wins,
               SUM(CASE WHEN outcome='lost' THEN 1 ELSE 0 END) AS losses,
               SUM(CASE WHEN outcome NOT IN ('won','lost') THEN 1 ELSE 0 END) AS open_count,
               MIN(opened_at)                                   AS opened,
               SUM(total_stake)                                 AS deployed,
               SUM(expected_profit)                             AS exp_profit,
               COALESCE(SUM(actual_profit), 0)                  AS actual_profit,
               AVG(net_edge_pct)                                AS avg_edge
        FROM sports_paper_trades
        GROUP BY session_id
        ORDER BY opened DESC
    """).fetchall()

    if not rows:
        print("  No paper trades found yet.")
        return

    show_bankroll(conn)

    print(f"  {'Session ID':<28} {'Tr':>3} {'W':>3} {'L':>3} {'Op':>3}  "
          f"{'Deployed':>9}  {'ExpProfit':>9}  {'ActProfit':>9}  {'AvgEdge':>7}  Date")
    print("  " + "-" * 100)
    for r in rows:
        act = r['actual_profit'] or 0
        act_str = f"${act:>+8.2f}" if (r['wins'] + r['losses']) > 0 else "  pending"
        print(
            f"  {r['session_id']:<28} {r['trades']:>3} {r['wins']:>3} {r['losses']:>3} "
            f"{r['open_count']:>3}  ${r['deployed']:>8.2f}  "
            f"${r['exp_profit']:>8.2f}  {act_str}  "
            f"{r['avg_edge']:>6.1f}%  {r['opened'][:16].replace('T',' ')}"
        )

    print()
    total_trades  = sum(r['trades']        for r in rows)
    total_wins    = sum(r['wins']          for r in rows)
    total_losses  = sum(r['losses']        for r in rows)
    total_open    = sum(r['open_count']    for r in rows)
    total_actual  = sum((r['actual_profit'] or 0) for r in rows)
    total_exp     = sum(r['exp_profit']    for r in rows)
    win_rate      = total_wins / (total_wins+total_losses) * 100 if (total_wins+total_losses) else 0
    print(f"  Totals: {total_trades} trades  |  {total_wins}W {total_losses}L {total_open} open"
          + (f"  |  {win_rate:.0f}% win rate" if (total_wins+total_losses) else "")
          + f"  |  Actual P&L: ${total_actual:+.2f}  |  Expected: ${total_exp:.2f}")
    print()
    print("  Tip: --session latest  or  --session <ID>  for trade detail")
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

    wins   = sum(1 for t in trades if t["outcome"] == "won")
    losses = sum(1 for t in trades if t["outcome"] == "lost")
    open_c = sum(1 for t in trades if t["outcome"] not in ("won","lost"))

    print()
    print(f"  Session: {session_id}")
    print(f"  Trades:  {len(trades)}  ({wins} won / {losses} lost / {open_c} open)")
    print()

    hdr = (f"  {'#':>2}  {'Status':<6}  {'Opened':<16}  {'Closed':<16}  {'Side':3}  "
           f"{'Edge':>5}  {'Stake':>7}  {'ExpProfit':>9}  {'ActProfit':>9}  Sport")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    for t in trades:
        status   = _outcome_icon(t["outcome"])
        act      = t["actual_profit"]
        act_str  = f"${act:>+8.2f}" if act is not None else "   pending"
        opened   = (t["opened_at"] or "")[:16].replace("T", " ")
        closed   = (t["resolved_at"] or "")[:16].replace("T", " ") or "open          "
        print(
            f"  {t['id']:>2}  {status}  {opened:<16}  {closed:<16}  "
            f"{t['kalshi_side'].upper():<3}  {t['net_edge_pct']:>4.1f}%  "
            f"${t['total_stake']:>6.2f}  ${t['expected_profit']:>8.2f}  "
            f"{act_str}  {t['sport'] or ''}"
        )

    print()
    total_stake   = sum(t['total_stake']     for t in trades)
    total_exp     = sum(t['expected_profit'] for t in trades)
    total_actual  = sum(t['actual_profit'] or 0 for t in trades if t['actual_profit'] is not None)
    settled       = wins + losses
    print(f"  Deployed:     ${total_stake:.2f}")
    print(f"  Exp profit:   ${total_exp:.2f}")
    if settled:
        print(f"  Actual P&L:  ${total_actual:+.2f}  ({settled} settled)")
    print()

    print("  -- TITLES --")
    for t in trades:
        status   = _outcome_icon(t["outcome"])
        title    = t['kalshi_title'] or t['kalshi_ticker']
        opened   = (t["opened_at"] or "")[:16].replace("T", " ")
        closed   = (t["resolved_at"] or "")[:16].replace("T", " ")
        timing   = f"  opened {opened}" + (f"  closed {closed}" if closed else "  (open)")
        print(f"  {status} #{t['id']:>2}{timing}  {title}")
    print()


def show_all(conn):
    trades = conn.execute(
        "SELECT * FROM sports_paper_trades ORDER BY opened_at DESC"
    ).fetchall()

    if not trades:
        print("  No trades found.")
        return

    show_bankroll(conn)

    print(f"  All {len(trades)} trades (most recent first):")
    print()

    hdr = (f"  {'#':>4}  {'Status':<6}  {'Opened':<16}  {'Closed':<16}  "
           f"{'Side':3}  {'Edge':>5}  {'Stake':>7}  {'ActProfit':>9}  Sport")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    for t in trades:
        opened  = (t["opened_at"]   or "")[:16].replace("T", " ")
        closed  = (t["resolved_at"] or "")[:16].replace("T", " ") or "open          "
        status  = _outcome_icon(t["outcome"])
        act     = t["actual_profit"]
        act_str = f"${act:>+8.2f}" if act is not None else "   pending"
        print(
            f"  {t['id']:>4}  {status}  {opened:<16}  {closed:<16}  "
            f"{t['kalshi_side'].upper():<3}  {t['net_edge_pct']:>4.1f}%  "
            f"${t['total_stake']:>6.2f}  {act_str}  {t['sport'] or ''}"
        )
    print()


def main():
    parser = argparse.ArgumentParser(description="View paper trades and bankroll")
    parser.add_argument("--session", type=str, default=None,
                        help="Session ID to view in detail ('latest' for most recent)")
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
