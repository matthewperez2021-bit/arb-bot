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
from config.strategies import STRATEGIES, ACTIVE_STRATEGY


def connect():
    if not os.path.exists(SQLITE_DB_PATH):
        print(f"  No database found at {SQLITE_DB_PATH}. Run the paper test first.")
        sys.exit(1)
    conn = sqlite3.connect(SQLITE_DB_PATH)
    conn.row_factory = sqlite3.Row
    # Make sure resolution + versioning columns exist (idempotent)
    for col in [
        "ALTER TABLE sports_paper_trades ADD COLUMN outcome TEXT DEFAULT 'open'",
        "ALTER TABLE sports_paper_trades ADD COLUMN actual_profit REAL DEFAULT NULL",
        "ALTER TABLE sports_paper_trades ADD COLUMN resolved_at TEXT DEFAULT NULL",
        "ALTER TABLE sports_paper_trades ADD COLUMN bankroll_after REAL DEFAULT NULL",
        "ALTER TABLE sports_paper_trades ADD COLUMN strategy_version TEXT DEFAULT 'v1'",
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


def show_status(conn):
    """Compact one-screen dashboard: bankroll + live positions + session count."""
    from datetime import datetime, timezone

    # ── Bankroll ────────────────────────────────────────────────────────────
    try:
        br = conn.execute("SELECT * FROM bankroll WHERE id=1").fetchone()
    except Exception:
        br = None

    if br:
        br = dict(br)
        start   = br["starting_capital"]
        current = br["current_capital"]
        wins    = br["wins"]
        losses  = br["losses"]
        settled = br["total_trades"]
    else:
        start   = STARTING_CAPITAL_USD
        settled_profit = conn.execute(
            "SELECT COALESCE(SUM(actual_profit),0) FROM sports_paper_trades "
            "WHERE outcome IN ('won','lost')"
        ).fetchone()[0] or 0
        wins    = conn.execute("SELECT COUNT(*) FROM sports_paper_trades WHERE outcome='won'").fetchone()[0]
        losses  = conn.execute("SELECT COUNT(*) FROM sports_paper_trades WHERE outcome='lost'").fetchone()[0]
        settled = wins + losses
        current = start + settled_profit

    pnl     = current - start
    pnl_pct = pnl / start * 100
    win_rate = wins / settled * 100 if settled else 0

    # ── Open positions ───────────────────────────────────────────────────────
    open_row = conn.execute(
        "SELECT COUNT(*) AS cnt, "
        "       COALESCE(SUM(total_stake), 0) AS deployed, "
        "       COALESCE(SUM(expected_profit), 0) AS exp_profit "
        "FROM sports_paper_trades WHERE outcome='open' OR outcome IS NULL"
    ).fetchone()
    open_cnt  = open_row["cnt"]
    deployed  = open_row["deployed"]
    exp_open  = open_row["exp_profit"]
    deployed_pct = deployed / current * 100 if current else 0

    # ── Sessions ─────────────────────────────────────────────────────────────
    sessions  = conn.execute(
        "SELECT COUNT(DISTINCT session_id) FROM sports_paper_trades"
    ).fetchone()[0]
    last_scan = conn.execute(
        "SELECT MAX(opened_at) FROM sports_paper_trades"
    ).fetchone()[0]
    if last_scan:
        try:
            dt  = datetime.fromisoformat(last_scan.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            ago = int((now - dt).total_seconds() / 60)
            if ago < 60:
                last_scan_str = f"{ago}m ago"
            else:
                last_scan_str = f"{ago // 60}h {ago % 60}m ago"
        except Exception:
            last_scan_str = last_scan[:16].replace("T", " ")
    else:
        last_scan_str = "never"

    # ── Per-version open-position breakdown ──────────────────────────────────
    by_version = conn.execute(
        "SELECT strategy_version, COUNT(*) AS cnt, "
        "       COALESCE(SUM(total_stake), 0) AS dep "
        "FROM sports_paper_trades "
        "WHERE outcome='open' OR outcome IS NULL "
        "GROUP BY strategy_version ORDER BY strategy_version"
    ).fetchall()

    # ── Print ────────────────────────────────────────────────────────────────
    w = 52
    print()
    print("  " + "=" * w)
    print(f"  {'ARB-BOT STATUS':^{w}}")
    print("  " + "=" * w)
    print(f"  Active strategy:   {ACTIVE_STRATEGY}")
    if ACTIVE_STRATEGY in STRATEGIES:
        notes = STRATEGIES[ACTIVE_STRATEGY].notes
        print(f"  ({notes[:48]}{'...' if len(notes) > 48 else ''})")
    print()
    print(f"  {'Bankroll':<24} {'':>4}")
    print(f"    Starting capital   ${start:>10,.2f}")
    print(f"    Current capital    ${current:>10,.2f}  ({'+'if pnl>=0 else ''}{pnl_pct:.1f}%)")
    print(f"    Realised P&L       {'+'if pnl>=0 else ''}${pnl:>9.2f}")
    print()
    print(f"  {'Open Positions':<24}")
    print(f"    Active trades      {open_cnt:>10}")
    print(f"    Capital deployed   ${deployed:>10.2f}  ({deployed_pct:.1f}% of bankroll)")
    print(f"    Expected profit    ${exp_open:>10.2f}  (if all open win)")
    print()
    print(f"  {'Settled Trades':<24}")
    print(f"    Total settled      {settled:>10}")
    print(f"    Won / Lost         {wins:>4} / {losses:<4}"
          + (f"   ({win_rate:.0f}% win rate)" if settled else ""))
    print()
    print(f"  {'History':<24}")
    print(f"    Sessions run       {sessions:>10}")
    print(f"    Last scan          {last_scan_str:>10}")
    if by_version and len(by_version) > 0:
        print()
        print(f"  {'Open positions by strategy':<28}")
        for r in by_version:
            v = r["strategy_version"] or "?"
            print(f"    {v:<8}           {r['cnt']:>4} trades  ${r['dep']:>8.2f}")
    print("  " + "=" * w)
    print()


def show_strategies(conn):
    """Print the strategy version log: registered versions + live performance."""
    # Aggregate stats per version
    rows = conn.execute("""
        SELECT strategy_version,
               COUNT(*)                                          AS n,
               SUM(CASE WHEN outcome='won'  THEN 1 ELSE 0 END)  AS w,
               SUM(CASE WHEN outcome='lost' THEN 1 ELSE 0 END)  AS l,
               SUM(CASE WHEN outcome IN ('won','lost')
                        THEN total_stake ELSE 0 END)             AS settled_stake,
               COALESCE(SUM(actual_profit), 0)                   AS pnl,
               SUM(CASE WHEN outcome='open' OR outcome IS NULL
                        THEN 1 ELSE 0 END)                       AS open_cnt
        FROM sports_paper_trades
        GROUP BY strategy_version
    """).fetchall()
    by_version = {r["strategy_version"]: dict(r) for r in rows}

    print()
    print("  " + "=" * 90)
    print(f"  {'STRATEGY VERSION LOG':^90}")
    print("  " + "=" * 90)
    print(f"  {'Ver':<4} {'Created':<11} {'Status':<8} {'N':>4} "
          f"{'W/L':<8} {'Open':>4} {'P&L':>10} {'ROI':>7}  Notes")
    print("  " + "-" * 90)

    # Iterate registry order (so future v2,v3 appear in order),
    # then any DB-only versions not in registry at the end.
    seen = set()
    for name, strat in STRATEGIES.items():
        seen.add(name)
        d = by_version.get(name, {})
        n   = d.get("n", 0) or 0
        w   = d.get("w", 0) or 0
        l   = d.get("l", 0) or 0
        op  = d.get("open_cnt", 0) or 0
        pnl = d.get("pnl", 0) or 0
        sts = d.get("settled_stake", 0) or 0
        roi = (pnl / sts * 100) if sts else 0
        status = "ACTIVE" if name == ACTIVE_STRATEGY else ("PAST" if n else "DEFINED")
        wl_str = f"{w}/{l}" if (w + l) else "—"
        roi_str = f"{roi:+.1f}%" if sts else "—"
        pnl_str = f"${pnl:+.2f}" if (w + l) else "—"
        print(f"  {name:<4} {strat.created_at:<11} {status:<8} {n:>4} "
              f"{wl_str:<8} {op:>4} {pnl_str:>10} {roi_str:>7}  {strat.notes[:40]}")

    # Any versions in DB but not in registry (orphans)
    for name, d in by_version.items():
        if name in seen or not name:
            continue
        n  = d["n"]; w = d["w"] or 0; l = d["l"] or 0; op = d["open_cnt"] or 0
        pnl = d["pnl"] or 0; sts = d["settled_stake"] or 0
        roi = (pnl / sts * 100) if sts else 0
        wl_str = f"{w}/{l}" if (w + l) else "—"
        print(f"  {name:<4} {'(orphan)':<11} {'?':<8} {n:>4} "
              f"{wl_str:<8} {op:>4} ${pnl:+9.2f} {roi:>+6.1f}%  (not in registry)")

    print("  " + "=" * 90)

    # Detailed parameter dump
    print()
    print("  -- Strategy Parameters " + "-" * 70)
    for name, strat in STRATEGIES.items():
        marker = " (ACTIVE)" if name == ACTIVE_STRATEGY else ""
        print(f"\n  {name}{marker}  [{strat.created_at}]")
        print(f"    {strat.notes}")
        print(f"    min_net_edge          = {strat.min_net_edge:.1%}")
        print(f"    max_per_trade_usd     = ${strat.max_per_trade_usd:,.0f}")
        print(f"    max_total_deployed    = ${strat.max_total_deployed_usd:,.0f}")
        print(f"    kelly_fraction        = {strat.kelly_fraction}")
        print(f"    min_books             = {strat.min_books}")
        print(f"    max_legs              = {strat.max_legs}")
        print(f"    max_trusted_edge_pct  = {strat.max_trusted_edge_pct:g}%")
        excl = ", ".join(sorted(strat.excluded_sports)) or "(none)"
        print(f"    excluded_sports       = {excl}")
        sides = ", ".join(sorted(strat.allowed_sides))
        print(f"    allowed_sides         = {sides}")
    print()


def main():
    parser = argparse.ArgumentParser(description="View paper trades and bankroll")
    parser.add_argument("--status", action="store_true",
                        help="Compact dashboard: bankroll, deployed, active trades")
    parser.add_argument("--strategies", action="store_true",
                        help="Show the strategy version log (registered "
                             "versions + live performance)")
    parser.add_argument("--session", type=str, default=None,
                        help="Session ID to view in detail ('latest' for most recent)")
    parser.add_argument("--all", action="store_true",
                        help="Show every trade across all sessions")
    args = parser.parse_args()

    conn = connect()

    if args.status:
        show_status(conn)
    elif args.strategies:
        show_strategies(conn)
    elif args.all:
        show_all(conn)
    elif args.session:
        show_trades(conn, args.session)
    else:
        show_sessions(conn)

    conn.close()


if __name__ == "__main__":
    main()
