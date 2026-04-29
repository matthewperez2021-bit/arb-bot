#!/usr/bin/env python3
"""
sports_scheduler.py — Run sports_paper_test.py every hour in the background.

Usage:
    python scripts/sports_scheduler.py                          # use ACTIVE_STRATEGY
    python scripts/sports_scheduler.py --interval 30            # every 30 minutes
    python scripts/sports_scheduler.py --strategy v2            # force a specific version
    python scripts/sports_scheduler.py --strategies v1,v2       # A/B mode
    python scripts/sports_scheduler.py --capital 2000           # custom bankroll
"""

import argparse
import subprocess
import sys
import time
from datetime import datetime


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval",   type=int,   default=60,
                        help="Minutes between scans (default: 60)")
    parser.add_argument("--capital",    type=float, default=2000,
                        help="Bankroll (default: 2000)")
    parser.add_argument("--strategy",   type=str,   default=None,
                        help="Strategy version (e.g. v1). "
                             "Default: ACTIVE_STRATEGY in config/strategies.py")
    parser.add_argument("--strategies", type=str,   default=None,
                        help="A/B mode: comma-separated, e.g. 'v1,v2'. "
                             "Splits capital evenly.")
    args = parser.parse_args()

    interval_secs = args.interval * 60
    log_path = "data/sports_scheduler.log"

    mode_label = (f"A/B [{args.strategies}]" if args.strategies
                  else f"single [{args.strategy or 'ACTIVE'}]")
    print(f"[scheduler] Starting - scan every {args.interval} min | "
          f"capital=${args.capital:.0f} | strategy={mode_label}")
    print(f"[scheduler] Output logged to: {log_path}")
    print(f"[scheduler] Press Ctrl+C to stop\n")

    # Build the strategy args once
    strategy_args = []
    if args.strategies:
        strategy_args = ["--strategies", args.strategies]
    elif args.strategy:
        strategy_args = ["--strategy", args.strategy]

    run_number = 0
    while True:
        run_number += 1
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[scheduler] === Run #{run_number} at {now} ===")

        with open(log_path, "a", encoding="utf-8") as logf:
            logf.write(f"\n{'='*70}\n")
            logf.write(f"Run #{run_number} at {now}\n")
            logf.write(f"{'='*70}\n")

            # Step 1: settle any open trades first
            resolve = subprocess.run(
                [sys.executable, "scripts/resolve_trades.py"],
                capture_output=True, text=True,
                encoding="utf-8", errors="replace",
            )
            logf.write("[resolve_trades]\n")
            logf.write(resolve.stdout)

            # Step 2: scan for new opportunities
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/sports_paper_test.py",
                    "--capital", str(args.capital),
                ] + strategy_args,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )

            logf.write(result.stdout)
            if result.stderr:
                logf.write("\n[STDERR]\n")
                logf.write(result.stderr)

        # Print a short summary to the console
        for line in resolve.stdout.splitlines():
            if any(kw in line for kw in ["Bankroll", "Current:", "Settled",
                                         "WIN", "LOSS", "ERROR"]):
                print(f"  {line.strip()}")
        lines = result.stdout.splitlines()
        for line in lines:
            if any(kw in line for kw in ["STRATEGY:", "deployed", "Expected profit",
                                         "Trades placed", "opportunities", "ERROR"]):
                print(f"  {line.strip()}")

        next_run = datetime.fromtimestamp(time.time() + interval_secs).strftime("%H:%M:%S")
        print(f"[scheduler] Next scan at {next_run} (in {args.interval} min)\n")

        try:
            time.sleep(interval_secs)
        except KeyboardInterrupt:
            print("\n[scheduler] Stopped.")
            break


if __name__ == "__main__":
    main()
