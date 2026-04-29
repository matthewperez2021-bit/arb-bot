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

log = logging.getLogger("resolve_trades")

# ─────────────────────────────────────────────────────────────────────────────
# DB schema additions (run once, idempotent)
# ─────────────────────────────────────────────────────────────────────────────

MIGRATION_SQL = """
-- Resolution columns (added if not present)
ALTER TABLE sports_paper_trades ADD COLUMN outcome       TEXT    DEFAULT 'open';
ALTER TABLE sports_paper_trades ADD COLUMN actual_profit REAL    DEFAULT NULL;
ALTER TABLE sports_paper_trades ADD COLUMN resolved_at   TEXT    DEFAULT NULL;
ALTER TABLE sports_paper_trades ADD COLUMN bankroll_after REAL   DEFAULT NULL;

-- Bankroll tracker (one row, updated in place)
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
    """Increment bankroll after a trade settles."""
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
    errors = 0

    for trade in open_trades:
        ticker    = trade["kalshi_ticker"]
        side      = trade["kalshi_side"]     # "yes" or "no"
        contracts = trade["contracts"]
        stake     = trade["total_stake"]
        cost_per  = trade["cost_per_contr"]

        try:
            resolved, result = kalshi.is_market_resolved(ticker)
        except KalshiAPIError as e:
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

        current_bankroll += actual_profit
        total_profit     += actual_profit
        settled_count    += 1
        if won:
            wins += 1
        else:
            losses += 1

        outcome_str = "WON " if won else "LOST"
        marker      = "+" if won else "-"
        print(
            f"  [{outcome_str}] {ticker[:48]:<48}  "
            f"side={side.upper()}  result={str(result).upper():<3}  "
            f"{marker}${abs(actual_profit):.2f}"
        )

        if not dry_run:
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE sports_paper_trades SET outcome=?, actual_profit=?, "
                "resolved_at=?, bankroll_after=? WHERE id=?",
                (
                    "won" if won else "lost",
                    actual_profit,
                    now,
                    current_bankroll,
                    trade["id"],
                ),
            )
            conn.commit()
            update_bankroll(conn, actual_profit, won)

    # Summary
    print()
    print(f"  Settled:    {settled_count}  ({wins} won, {losses} lost)")
    print(f"  Still open: {still_open}")
    if errors:
        print(f"  Errors:     {errors}  (check Kalshi API connection)")
    if settled_count:
        print(f"  Net P&L:    {'+'if total_profit>=0 else ''}${total_profit:.2f}")

    print()
    _print_bankroll(conn)
    conn.close()


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
    pnl      = r["current_capital"] - r["starting_capital"]
    pnl_pct  = pnl / r["starting_capital"] * 100
    win_rate = (r["wins"] / r["total_trades"] * 100) if r["total_trades"] else 0

    print(f"  -- Bankroll --------------------------------------------------")
    print(f"  Starting:       ${r['starting_capital']:>10,.2f}")
    print(f"  Current:        ${r['current_capital']:>10,.2f}  "
          f"({'+'if pnl>=0 else ''}{pnl_pct:.1f}%)")
    print(f"  Realised P&L:   {'+'if pnl>=0 else ''}${pnl:>9.2f}")
    print(f"  Settled trades: {r['total_trades']:>10}  "
          f"({r['wins']} won / {r['losses']} lost"
          + (f"  |  {win_rate:.0f}% win rate" if r['total_trades'] else "")
          + ")")
    print(f"  Last updated:   {r['last_updated'][:19]}")
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
