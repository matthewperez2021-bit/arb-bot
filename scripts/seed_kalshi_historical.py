#!/usr/bin/env python3
"""
seed_kalshi_historical.py — Seed data/historical_kalshi/ with per-market
candlestick price history fetched from Kalshi's
/series/{series}/markets/{ticker}/candlesticks endpoint.

This pairs with seed_odds_api_historical.py: that one captures sportsbook
consensus over time, this one captures Kalshi prices over time. Together
they enable a true backtest of strategy decisions.

Output:
    data/historical_kalshi/{ticker}.json
        {
          "ticker":         "KXMVE...-...-...",
          "series_ticker":  "KXMVE...",
          "period_minutes": 60,
          "start_ts":       <unix>,
          "end_ts":         <unix>,
          "fetched_at":     "<iso>",
          "candlesticks":   [ {end_period_ts, yes_bid, yes_ask, price, ...}, ... ]
        }

Modes:
    --from-trades            Pull tickers from sports_paper_trades.
                             Time window per trade: [opened_at - pad, resolved_at + pad].
    --ticker T1,T2 ...       Explicit list. Time window from --start/--end.
    --series KXMVE --limit N Walk first N currently-open markets in the series.

Usage:
    cd arb-bot
    python scripts/seed_kalshi_historical.py --from-trades --limit 5         # 5 settled trades
    python scripts/seed_kalshi_historical.py --from-trades                   # ALL settled trades
    python scripts/seed_kalshi_historical.py --ticker KXMVE...-... --start 2026-05-01T00:00:00Z --end 2026-05-06T00:00:00Z
    python scripts/seed_kalshi_historical.py --series KXMVECROSSCATEGORY --limit 20
"""

import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("seed_kalshi_historical")

from clients.kalshi import KalshiClient, KalshiAPIError
from config.settings import SQLITE_DB_PATH

HISTORICAL_KALSHI_DIR = "data/historical_kalshi/"

# Kalshi caps candlesticks at ~5000 buckets per call. With 60-min period
# that's ~208 days — never an issue for our typical [open, resolve] windows.
# With 1-min period it's ~3.5 days; chunking can be added if needed.


def _series_from_ticker(ticker: str) -> str:
    """Series ticker is the prefix before the first '-'."""
    return ticker.split("-", 1)[0]


def _iso_to_unix(iso: str) -> int:
    """Parse ISO 8601 (with Z or offset) to unix seconds."""
    s = iso.replace("Z", "+00:00")
    return int(datetime.fromisoformat(s).timestamp())


def _output_path(ticker: str) -> str:
    os.makedirs(HISTORICAL_KALSHI_DIR, exist_ok=True)
    safe = ticker.replace("/", "-")
    return os.path.join(HISTORICAL_KALSHI_DIR, f"{safe}.json")


def _load_from_trades(limit: int | None, pad_hours: int) -> list[dict]:
    """
    Read settled trades from the SQLite DB. Returns list of:
        {"ticker", "series_ticker", "start_ts", "end_ts"}
    """
    conn = sqlite3.connect(SQLITE_DB_PATH)
    conn.row_factory = sqlite3.Row
    sql = (
        "SELECT DISTINCT kalshi_ticker, MIN(opened_at) AS opened, "
        "MAX(resolved_at) AS resolved "
        "FROM sports_paper_trades "
        "WHERE outcome IN ('won', 'lost') "
        "GROUP BY kalshi_ticker "
        "ORDER BY opened DESC"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"

    out: list[dict] = []
    for row in conn.execute(sql):
        ticker = row["kalshi_ticker"]
        try:
            start = _iso_to_unix(row["opened"])   - pad_hours * 3600
            end   = _iso_to_unix(row["resolved"]) + pad_hours * 3600
        except Exception as exc:
            log.warning("Skipping %s: bad timestamps (%s)", ticker, exc)
            continue
        out.append({
            "ticker":        ticker,
            "series_ticker": _series_from_ticker(ticker),
            "start_ts":      start,
            "end_ts":        end,
        })
    conn.close()
    return out


def _load_from_series(client: KalshiClient, series_ticker: str, limit: int,
                      start_ts: int, end_ts: int) -> list[dict]:
    """Pull the first `limit` open markets in a series."""
    markets = client.get_all_open_markets(series_ticker=series_ticker)[:limit]
    return [
        {
            "ticker":        m["ticker"],
            "series_ticker": series_ticker,
            "start_ts":      start_ts,
            "end_ts":        end_ts,
        }
        for m in markets
    ]


def _fetch_one(
    client:    KalshiClient,
    spec:      dict,
    period_minutes: int,
    overwrite: bool,
) -> bool:
    """Fetch + save one ticker. Returns True if a new file was written."""
    ticker = spec["ticker"]
    out_path = _output_path(ticker)

    if os.path.exists(out_path) and not overwrite:
        log.info("SKIP %s (cached). Use --overwrite to re-fetch.", ticker)
        return False

    try:
        envelope = client.get_candlesticks(
            series_ticker=spec["series_ticker"],
            market_ticker=ticker,
            start_ts=spec["start_ts"],
            end_ts=spec["end_ts"],
            period_minutes=period_minutes,
        )
    except KalshiAPIError as exc:
        log.error("Fetch failed for %s: %s", ticker, exc)
        return False

    candles = envelope.get("candlesticks", []) or []
    payload = {
        "ticker":         ticker,
        "series_ticker":  spec["series_ticker"],
        "period_minutes": period_minutes,
        "start_ts":       spec["start_ts"],
        "end_ts":         spec["end_ts"],
        "fetched_at":     datetime.now(timezone.utc).isoformat(),
        "candlesticks":   candles,
    }

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    log.info("Saved %d candles → %s", len(candles), out_path)
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Seed data/historical_kalshi/ with per-market candlestick data."
    )
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--from-trades", action="store_true",
                     help="Pull tickers from sports_paper_trades (default if no other source)")
    src.add_argument("--ticker", type=str, default=None,
                     help="Comma-separated explicit market tickers")
    src.add_argument("--series", type=str, default=None,
                     help="Series ticker (e.g. KXMVECROSSCATEGORY) — pulls open markets")

    parser.add_argument("--limit", type=int, default=None,
                        help="Cap the number of tickers (--from-trades / --series)")
    parser.add_argument("--start", type=str, default=None,
                        help="ISO start (used with --ticker / --series). "
                             "Default: 7 days ago.")
    parser.add_argument("--end", type=str, default=None,
                        help="ISO end (used with --ticker / --series). Default: now.")
    parser.add_argument("--pad-hours", type=int, default=2,
                        help="--from-trades only: hours of padding around "
                             "opened_at/resolved_at (default: 2)")
    parser.add_argument("--period", type=int, default=60,
                        choices=[1, 60, 1440],
                        help="Candle bucket size in minutes: 1 (minute), "
                             "60 (hour, default), 1440 (daily)")
    parser.add_argument("--overwrite", action="store_true",
                        help="Re-fetch and overwrite existing files")
    args = parser.parse_args()

    # Default mode: --from-trades
    if not (args.ticker or args.series):
        args.from_trades = True

    client = KalshiClient()

    # Build spec list
    specs: list[dict] = []
    if args.from_trades:
        specs = _load_from_trades(limit=args.limit, pad_hours=args.pad_hours)
        log.info("Loaded %d ticker windows from sports_paper_trades", len(specs))
    elif args.ticker:
        # Explicit time range
        end_ts = (_iso_to_unix(args.end) if args.end
                  else int(datetime.now(timezone.utc).timestamp()))
        start_ts = (_iso_to_unix(args.start) if args.start
                    else end_ts - 7 * 24 * 3600)
        for t in [s.strip() for s in args.ticker.split(",")]:
            specs.append({
                "ticker":        t,
                "series_ticker": _series_from_ticker(t),
                "start_ts":      start_ts,
                "end_ts":        end_ts,
            })
    elif args.series:
        end_ts = (_iso_to_unix(args.end) if args.end
                  else int(datetime.now(timezone.utc).timestamp()))
        start_ts = (_iso_to_unix(args.start) if args.start
                    else end_ts - 7 * 24 * 3600)
        specs = _load_from_series(client, args.series, args.limit or 20,
                                   start_ts, end_ts)
        log.info("Loaded %d open markets from series %s", len(specs), args.series)

    if not specs:
        log.warning("No tickers to fetch. Exiting.")
        return

    log.info("Output: %s", os.path.abspath(HISTORICAL_KALSHI_DIR))
    log.info("Period: %d minutes", args.period)

    saved = 0
    for spec in specs:
        if _fetch_one(client, spec, args.period, args.overwrite):
            saved += 1

    log.info("Done. %d new candle files written, %d total tickers attempted.",
             saved, len(specs))


if __name__ == "__main__":
    main()
