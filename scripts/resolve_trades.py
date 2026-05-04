#!/usr/bin/env python3
"""
resolve_trades.py — Check open paper trades against Kalshi and settle them.

For each unresolved trade:
  1. Ask Kalshi if the market has settled (status == "settled", result = yes|no)
  2. Compute actual profit:
       WIN  → contracts * $1.00  (full payout) minus total_stake
       LOSS → -total_stake       (lose the stake)
  3. Update the trade row with outcome + actual_profit + bankroll_after
  4. Print a settlement report

The bankroll starts at STARTING_CAPITAL_USD and compounds with each settled trade.

Usage:
    python scripts/resolve_trades.py                # settle all open trades
    python scripts/resolve_trades.py --dry-run      # show what would settle, don't write
    python scripts/resolve_trades.py --verbose      # show every market checked
"""

import argparse
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from clients.kalshi import KalshiClient, KalshiAPIError
from config.settings import SQLITE_DB_PATH, STARTING_CAPITAL_USD
from scripts._display import print_resolve_summary

log = logging.getLogger("resolve_trades")

# ─────────────────────────────────────────────────────────────────────────────
# DB schema additions (run once, idempotent)
# ─────────────────────────────────────────────────────────────────────────────

MIGRATION_SQL = """
-- Resolution columns (added if not present)
ALTER TABLE sports_paper_trades ADD COLUMN outcome              TEXT DEFAULT 'open';
ALTER TABLE sports_paper_trades ADD COLUMN actual_profit        REAL DEFAULT NULL;
ALTER TABLE sports_paper_trades ADD COLUMN resolved_at          TEXT DEFAULT NULL;
ALTER TABLE sports_paper_trades ADD COLUMN bankroll_after       REAL DEFAULT NULL;
ALTER TABLE sports_paper_trades ADD COLUMN strategy_version     TEXT DEFAULT 'v1';

-- CLV (Closing Line Value) columns — proof that edge is real, not luck.
-- Captured at the moment of settlement fetch.
-- clv > 0: market moved in our favor after entry (confirms edge).
-- clv < 0: market moved against us (noise signal).
ALTER TABLE sports_paper_trades ADD COLUMN kalshi_closing_ask    REAL DEFAULT NULL;
ALTER TABLE sports_paper_trades ADD COLUMN kalshi_closing_no_ask REAL DEFAULT NULL;
ALTER TABLE sports_paper_trades ADD COLUMN clv                   REAL DEFAULT NULL;

-- Global bankroll tracker (one row, updated in place)
CREATE TABLE IF NOT EXISTS bankroll (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    starting_capital REAL    NOT NULL,
    current_capital  REAL    NOT NULL,
    total_trades     INTEGER NOT NULL DEFAULT 0,
    wins             INTEGER NOT NULL DEFAULT 0,
    losses           INTEGER NOT NULL DEFAULT 0,
    total_profit     REAL    NOT NULL DEFAULT 0.0,
    last_updated     TEXT    NOT NULL
);

-- Per-strategy bankroll tracker (one row per strategy)
CREATE TABLE IF NOT EXISTS strategy_bankrolls (
    strategy_name    TEXT PRIMARY KEY,
    starting_capital REAL    NOT NULL,
    current_capital  REAL    NOT NULL,
    total_trades     INTEGER NOT NULL DEFAULT 0,
    wins             INTEGER NOT NULL DEFAULT 0,
    losses           INTEGER NOT NULL DEFAULT 0,
    total_profit     REAL    NOT NULL DEFAULT 0.0,
    last_updated     TEXT    NOT NULL
);
"""


def migrate(conn: sqlite3.Connection):
    """Apply schema additions idempotently."""
    for stmt in MIGRATION_SQL.strip().split(";"):
        stmt = stmt.strip()
        if not stmt:
            continue
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as e:
            if "duplicate column" in str(e).lower() or "already exists" in str(e).lower():
                pass  # already migrated
            else:
                raise
    conn.commit()


def get_or_init_bankroll(conn: sqlite3.Connection, starting: float) -> dict:
    """Return the bankroll row, creating it if it doesn't exist."""
    row = conn.execute("SELECT * FROM bankroll WHERE id=1").fetchone()
    if row:
        return dict(row)
    # First time — seed from all already-resolved trades in the DB
    settled = conn.execute(
        "SELECT COALESCE(SUM(actual_profit), 0) FROM sports_paper_trades "
        "WHERE outcome IN ('won','lost')"
    ).fetchone()[0]
    wins   = conn.execute("SELECT COUNT(*) FROM sports_paper_trades WHERE outcome='won'").fetchone()[0]
    losses = conn.execute("SELECT COUNT(*) FROM sports_paper_trades WHERE outcome='lost'").fetchone()[0]
    current = starting + (settled or 0)
    conn.execute(
        "INSERT INTO bankroll (id,starting_capital,current_capital,total_trades,"
        "wins,losses,total_profit,last_updated) VALUES (1,?,?,?,?,?,?,?)",
        (starting, current, wins+losses, wins, losses, settled or 0,
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM bankroll WHERE id=1").fetchone())


def update_bankroll(conn: sqlite3.Connection, profit: float, won: bool):
    """Increment global bankroll after a trade settles."""
    conn.execute("""
        UPDATE bankroll SET
            current_capital = current_capital + ?,
            total_profit    = total_profit    + ?,
            total_trades    = total_trades    + 1,
            wins            = wins   + ?,
            losses          = losses + ?,
            last_updated    = ?
        WHERE id = 1
    """, (
        profit, profit,
        1 if won else 0,
        0 if won else 1,
        datetime.now(timezone.utc).isoformat(),
    ))
    conn.commit()


def update_strategy_bankroll(conn: sqlite3.Connection, strategy: str,
                              profit: float, won: bool, starting: float):
    """Upsert per-strategy bankroll row after a trade settles."""
    now = datetime.now(timezone.utc).isoformat()
    existing = conn.execute(
        "SELECT * FROM strategy_bankrolls WHERE strategy_name=?", (strategy,)
    ).fetchone()
    if existing:
        conn.execute("""
            UPDATE strategy_bankrolls SET
                current_capital = current_capital + ?,
                total_profit    = total_profit    + ?,
                total_trades    = total_trades    + 1,
                wins            = wins   + ?,
                losses          = losses + ?,
                last_updated    = ?
            WHERE strategy_name = ?
        """, (profit, profit,
              1 if won else 0, 0 if won else 1,
              now, strategy))
    else:
        # First settlement for this strategy — seed from all prior resolved trades
        settled = conn.execute(
            "SELECT COALESCE(SUM(actual_profit), 0) FROM sports_paper_trades "
            "WHERE outcome IN ('won','lost') AND strategy_version=?",
            (strategy,)
        ).fetchone()[0] or 0.0
        wins_ct = conn.execute(
            "SELECT COUNT(*) FROM sports_paper_trades "
            "WHERE outcome='won' AND strategy_version=?", (strategy,)
        ).fetchone()[0]
        loss_ct = conn.execute(
            "SELECT COUNT(*) FROM sports_paper_trades "
            "WHERE outcome='lost' AND strategy_version=?", (strategy,)
        ).fetchone()[0]
        conn.execute("""
            INSERT INTO strategy_bankrolls
                (strategy_name, starting_capital, current_capital,
                 total_trades, wins, losses, total_profit, last_updated)
            VALUES (?,?,?,?,?,?,?,?)
        """, (strategy, starting, starting + settled,
              wins_ct + loss_ct, wins_ct, loss_ct, settled, now))
    conn.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Resolver
# ─────────────────────────────────────────────────────────────────────────────

def resolve_all(dry_run: bool = False, verbose: bool = False):
    if not os.path.exists(SQLITE_DB_PATH):
        print(f"No database at {SQLITE_DB_PATH}. Run the paper test first.")
        return

    conn = sqlite3.connect(SQLITE_DB_PATH)
    conn.row_factory = sqlite3.Row
    migrate(conn)

    bankroll = get_or_init_bankroll(conn, STARTING_CAPITAL_USD)
    current_bankroll = bankroll["current_capital"]

    # Fetch all open trades
    open_trades = conn.execute(
        "SELECT * FROM sports_paper_trades WHERE outcome='open' OR outcome IS NULL "
        "ORDER BY opened_at"
    ).fetchall()

    if not open_trades:
        print(f"  No open trades to resolve.")
        _print_bankroll(conn)
        conn.close()
        return

    print(f"\n  Checking {len(open_trades)} open trades against Kalshi...\n")

    try:
        kalshi = KalshiClient()
    except Exception as e:
        print(f"  ERROR: Cannot connect to Kalshi — {e}")
        conn.close()
        return

    settled_count = 0
    wins = 0
    losses = 0
    total_profit = 0.0
    still_open = 0
    stale_count = 0
    errors = 0

    for trade in open_trades:
        ticker    = trade["kalshi_ticker"]
        side      = trade["kalshi_side"]     # "yes" or "no"
        contracts = trade["contracts"]
        stake     = trade["total_stake"]
        cost_per  = trade["cost_per_contr"]

        try:
            # Use get_market directly so we capture closing prices in one call
            raw      = kalshi.get_market(ticker)
            market   = raw.get("market", raw)
            status   = (market.get("status") or "").lower()
            result   = (market.get("result") or "").lower() or None
            resolved = status in ("settled", "finalized") and result in ("yes", "no")

            # Capture closing prices for CLV computation
            closing_yes_ask = market.get("yes_ask_dollars") or market.get("yes_ask")
            closing_no_ask  = market.get("no_ask_dollars")  or market.get("no_ask")
            try:
                closing_yes_ask = float(closing_yes_ask) if closing_yes_ask is not None else None
                closing_no_ask  = float(closing_no_ask)  if closing_no_ask  is not None else None
            except (TypeError, ValueError):
                closing_yes_ask = closing_no_ask = None

        except KalshiAPIError as e:
            if e.status_code == 404:
                # Market no longer exists on Kalshi — delisted or expired without
                # resolution. Mark as 'stale' so it stops blocking the open count.
                print(f"  [STALE] {ticker[:48]} — market not found (404), marking stale")
                stale_count += 1
                if not dry_run:
                    now = datetime.now(timezone.utc).isoformat()
                    conn.execute(
                        "UPDATE sports_paper_trades SET outcome='stale', "
                        "resolved_at=? WHERE id=?",
                        (now, trade["id"]),
                    )
                    conn.commit()
                continue
            if verbose:
                print(f"  [!] {ticker[:50]} — API error: {e}")
            errors += 1
            continue
        except Exception as e:
            if verbose:
                print(f"  [!] {ticker[:50]} — Unexpected error: {e}")
            errors += 1
            continue

        if not resolved:
            still_open += 1
            if verbose:
                print(f"  [ ] {ticker[:50]} — still open")
            continue

        # Market resolved — did our side win?
        won         = (result == side)
        # WIN:  receive $1.00 per contract, paid cost_per → net = (1 - cost_per) per contract
        # LOSS: receive $0, already paid stake → net = -stake
        if won:
            actual_profit = contracts * (1.0 - cost_per)
        else:
            actual_profit = -stake

        # CLV = closing_ask - entry_ask for the traded side.
        # Positive → market moved in our favor after entry (confirms edge).
        # Negative → market moved against us.
        entry_ask = trade["kalshi_ask"]
        if side == "yes" and closing_yes_ask is not None:
            clv = closing_yes_ask - entry_ask
        elif side == "no" and closing_no_ask is not None:
            clv = closing_no_ask - entry_ask
        else:
            clv = None

        current_bankroll += actual_profit
        total_profit     += actual_profit
        settled_count    += 1
        if won:
            wins += 1
        else:
            losses += 1

        outcome_str = "WON " if won else "LOST"
        marker      = "+" if won else "-"
        clv_str     = f"  clv={clv:+.3f}" if clv is not None else ""
        print(
            f"  [{outcome_str}] {ticker[:48]:<48}  "
            f"side={side.upper()}  result={str(result).upper():<3}  "
            f"{marker}${abs(actual_profit):.2f}{clv_str}"
        )

        if not dry_run:
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE sports_paper_trades SET outcome=?, actual_profit=?, "
                "resolved_at=?, bankroll_after=?, "
                "kalshi_closing_ask=?, kalshi_closing_no_ask=?, clv=? "
                "WHERE id=?",
                (
                    "won" if won else "lost",
                    actual_profit,
                    now,
                    current_bankroll,
                    closing_yes_ask,
                    closing_no_ask,
                    clv,
                    trade["id"],
                ),
            )
            conn.commit()
            update_bankroll(conn, actual_profit, won)
            strategy_ver = trade["strategy_version"] or "v1"
            update_strategy_bankroll(conn, strategy_ver, actual_profit, won,
                                     STARTING_CAPITAL_USD)

    # Summary
    print()
    print(f"  Settled:    {settled_count}  ({wins} won, {losses} lost)")
    print(f"  Still open: {still_open}")
    if stale_count:
        print(f"  Stale:      {stale_count}  (market delisted/expired — stake written off)")
    if errors:
        print(f"  Errors:     {errors}  (check Kalshi API connection)")
    if settled_count:
        print(f"  Net P&L:    {'+'if total_profit>=0 else ''}${total_profit:.2f}")

    print()
    _print_bankroll(conn)
    _show_all_sessions(conn)
    conn.close()


def _outcome_icon(outcome):
    if outcome == "won":   return "[WIN ]"
    if outcome == "lost":  return "[LOSS]"
    return "[open]"


def _show_all_sessions(conn: sqlite3.Connection):
    """Print per-session table + detail for every session that has activity."""
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
        return

    print(f"  -- All Sessions ----------------------------------------------")
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

    total_trades  = sum(r['trades']                  for r in rows)
    total_wins    = sum(r['wins']                    for r in rows)
    total_losses  = sum(r['losses']                  for r in rows)
    total_open    = sum(r['open_count']              for r in rows)
    total_actual  = sum((r['actual_profit'] or 0)    for r in rows)
    win_rate      = total_wins / (total_wins+total_losses) * 100 if (total_wins+total_losses) else 0
    print()
    print(f"  Totals: {total_trades} trades  |  {total_wins}W {total_losses}L {total_open} open"
          + (f"  |  {win_rate:.0f}% win rate" if (total_wins+total_losses) else "")
          + f"  |  Actual P&L: ${total_actual:+.2f}")
    print()

    # Per-session trade detail
    for r in rows:
        _show_session_detail(conn, r['session_id'])


def _show_session_detail(conn: sqlite3.Connection, session_id: str):
    trades = conn.execute(
        "SELECT * FROM sports_paper_trades WHERE session_id=? ORDER BY id",
        (session_id,)
    ).fetchall()
    if not trades:
        return

    wins   = sum(1 for t in trades if t["outcome"] == "won")
    losses = sum(1 for t in trades if t["outcome"] == "lost")
    open_c = sum(1 for t in trades if t["outcome"] not in ("won", "lost"))

    print(f"  -- Session: {session_id}  "
          f"({len(trades)} trades  {wins}W / {losses}L / {open_c} open) --")

    hdr = (f"  {'#':>2}  {'Status':<6}  {'Opened':<16}  {'Closed':<16}  {'Side':3}  "
           f"{'Edge':>5}  {'Stake':>7}  {'ExpProfit':>9}  {'ActProfit':>9}  Sport")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    for t in trades:
        status  = _outcome_icon(t["outcome"])
        act     = t["actual_profit"]
        act_str = f"${act:>+8.2f}" if act is not None else "   pending"
        opened  = (t["opened_at"]   or "")[:16].replace("T", " ")
        closed  = (t["resolved_at"] or "")[:16].replace("T", " ") or "open          "
        print(
            f"  {t['id']:>2}  {status}  {opened:<16}  {closed:<16}  "
            f"{t['kalshi_side'].upper():<3}  {t['net_edge_pct']:>4.1f}%  "
            f"${t['total_stake']:>6.2f}  ${t['expected_profit']:>8.2f}  "
            f"{act_str}  {t['sport'] or ''}"
        )

    total_stake  = sum(t['total_stake']     for t in trades)
    total_exp    = sum(t['expected_profit'] for t in trades)
    total_actual = sum(t['actual_profit'] or 0 for t in trades if t['actual_profit'] is not None)
    settled      = wins + losses
    print()
    print(f"  Deployed: ${total_stake:.2f}  |  Exp profit: ${total_exp:.2f}"
          + (f"  |  Actual P&L: ${total_actual:+.2f}  ({settled} settled)" if settled else ""))

    print()
    print("  Titles:")
    for t in trades:
        status  = _outcome_icon(t["outcome"])
        title   = t['kalshi_title'] or t['kalshi_ticker']
        opened  = (t["opened_at"]   or "")[:16].replace("T", " ")
        closed  = (t["resolved_at"] or "")[:16].replace("T", " ")
        timing  = f"opened {opened}" + (f"  closed {closed}" if closed else "  (open)")
        print(f"  {status} #{t['id']:>2}  {timing}  {title}")
    print()


def _print_bankroll(conn: sqlite3.Connection):
    try:
        row = conn.execute("SELECT * FROM bankroll WHERE id=1").fetchone()
    except sqlite3.OperationalError:
        row = None
    if not row:
        print("  (No bankroll record yet — no settled trades)")
        print()
        return
    r = dict(row)

    # Per-strategy breakdown rows
    strat_rows = []
    try:
        rows = conn.execute(
            "SELECT * FROM strategy_bankrolls ORDER BY strategy_name"
        ).fetchall()
        strat_rows = [dict(x) for x in rows]
    except sqlite3.OperationalError:
        pass

    print_resolve_summary(
        starting=r["starting_capital"],
        current=r["current_capital"],
        total_trades=r["total_trades"],
        wins=r["wins"],
        losses=r["losses"],
        last_updated=r["last_updated"],
        per_strategy_rows=strat_rows or None,
    )
    print()


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Settle open paper trades vs Kalshi")
    parser.add_argument("--dry-run", action="store_true",
                        help="Check resolution without writing to DB")
    parser.add_argument("--verbose", action="store_true",
                        help="Show every market checked, even still-open ones")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )

    if args.dry_run:
        print("  [DRY RUN — no changes will be written]\n")

    resolve_all(dry_run=args.dry_run, verbose=args.verbose)


if __name__ == "__main__":
    main()
