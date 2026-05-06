#!/usr/bin/env python3
"""
merge_db.py — Merge sports_paper_trades rows from a source SQLite DB into
the target DB (default: data/arb_positions.db).

When and why
────────────
Two machines have been writing to their own copies of arb_positions.db
(running the scheduler concurrently is contrary to the single-writer
convention in CONTEXT.md, but mistakes happen). Git can't merge binary
SQLite, so this is the manual reconciliation tool.

Behavior
────────
- Reads sports_paper_trades from --source and INSERTs into --target any
  row whose (kalshi_ticker, opened_at, strategy_version) tuple isn't
  already present. That tuple is unique enough across machines because
  two scans on different machines won't produce the same ticker
  trade at the same microsecond timestamp.
- Other tables (bankroll, strategy_bankrolls, positions, fills,
  sessions) are NOT merged — they're aggregate state derivable from
  sports_paper_trades. After merging, run resolve_trades.py and a
  full scheduler scan to let bankroll catch up.

Usage
─────
    cd arb-bot
    python scripts/merge_db.py --source path/to/other.db                # default target = data/arb_positions.db
    python scripts/merge_db.py --source other.db --target some.db
    python scripts/merge_db.py --source other.db --dry-run              # preview, no writes
"""

import argparse
import os
import sqlite3
import sys
from collections import defaultdict


def _row_key(row: sqlite3.Row) -> tuple:
    """Composite uniqueness key for sports_paper_trades."""
    return (row["kalshi_ticker"], row["opened_at"], row["strategy_version"])


def merge(source: str, target: str, dry_run: bool) -> None:
    if not os.path.exists(source):
        sys.exit(f"Source DB not found: {source}")
    if not os.path.exists(target):
        sys.exit(f"Target DB not found: {target}")
    if os.path.abspath(source) == os.path.abspath(target):
        sys.exit("--source and --target must point to different files.")

    src = sqlite3.connect(source)
    src.row_factory = sqlite3.Row
    tgt = sqlite3.connect(target)
    tgt.row_factory = sqlite3.Row

    # Build set of existing keys in target
    tgt_keys: set = {
        _row_key(r)
        for r in tgt.execute(
            "SELECT kalshi_ticker, opened_at, strategy_version "
            "FROM sports_paper_trades"
        )
    }

    # Walk source rows; collect ones missing from target
    src_rows = src.execute("SELECT * FROM sports_paper_trades").fetchall()
    columns  = src_rows[0].keys() if src_rows else []
    to_insert: list[sqlite3.Row] = []
    for row in src_rows:
        if _row_key(row) not in tgt_keys:
            to_insert.append(row)

    # Per-strategy summary
    by_strategy: dict[str, int] = defaultdict(int)
    for row in to_insert:
        by_strategy[row["strategy_version"] or "unknown"] += 1

    print(f"Source DB: {source}")
    print(f"Target DB: {target}")
    print(f"  source rows: {len(src_rows)}")
    print(f"  target rows: {len(tgt_keys)}")
    print(f"  new rows to insert: {len(to_insert)}")
    if by_strategy:
        for strat, n in sorted(by_strategy.items()):
            print(f"    {strat}: +{n}")

    if not to_insert:
        print("\nNothing to merge — target already has all source rows.")
        src.close(); tgt.close()
        return

    if dry_run:
        print("\nDry-run — no writes. Re-run without --dry-run to apply.")
        src.close(); tgt.close()
        return

    # Insert. The id column is INTEGER PRIMARY KEY; let target assign new ids
    # to avoid collisions. Build the insert column list excluding id.
    insert_cols = [c for c in columns if c != "id"]
    placeholders = ",".join(["?"] * len(insert_cols))
    sql = (
        f"INSERT INTO sports_paper_trades "
        f"({','.join(insert_cols)}) VALUES ({placeholders})"
    )
    payload = [tuple(row[c] for c in insert_cols) for row in to_insert]
    tgt.executemany(sql, payload)
    tgt.commit()

    print(f"\nMerged {len(to_insert)} rows into {target}.")
    print("Note: bankroll / strategy_bankrolls tables are now stale.")
    print("Run `python scripts/resolve_trades.py` to refresh, then start "
          "the scheduler so bankroll updates flow through.")

    src.close()
    tgt.close()


def main():
    parser = argparse.ArgumentParser(
        description="Merge sports_paper_trades from one SQLite DB into another."
    )
    parser.add_argument("--source", type=str, required=True,
                        help="Path to the source DB (rows pulled FROM here).")
    parser.add_argument("--target", type=str,
                        default="data/arb_positions.db",
                        help="Path to the target DB (rows pushed TO here). "
                             "Default: data/arb_positions.db")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview the merge without writing.")
    args = parser.parse_args()

    merge(args.source, args.target, args.dry_run)


if __name__ == "__main__":
    main()
