#!/usr/bin/env python3
"""
sports_paper_test.py — Sports arb paper test on a $1,000 bankroll.

Strategy: buy mispriced Kalshi KXMVE sports contracts vs sportsbook consensus.
This is a SINGLE-LEG trade (Kalshi only) — sportsbooks ban arb accounts too fast.

Edge source:
  Kalshi's crowd-sourced market for sports events lags the sharp sportsbook
  consensus (Pinnacle/Consensus). When Kalshi YES ask < sportsbook fair prob
  after fees, the YES contract is underpriced — buy it.

Flow:
  1. Fetch live KXMVE markets from Kalshi (~200-600 sports contracts)
  2. Fetch live h2h odds from The Odds API across NBA/NHL/MLB/MLS/MMA/Tennis
  3. OddsArbScanner prices each KXMVE market leg using devigged consensus
  4. Half-Kelly size each opportunity (capped at $50)
  5. Simulate fills and log to SQLite (mode=paper)
  6. Print full P&L report

Usage:
    cd arb-bot
    python scripts/sports_paper_test.py [--capital 1000] [--max-per-trade 50]
"""

import argparse
import io
import logging
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Force UTF-8 on Windows console so any stray Unicode survives
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from clients.kalshi import KalshiClient
from clients.normalizer import normalize_kalshi_book, normalize_kalshi_market
from detection.odds_arb_scanner import OddsArbScanner, OddsArbOpportunity
from config.settings import (
    KALSHI_TAKER_FEE,
    KALSHI_KXMVE_MAX_PAGES,
    ODDS_API_ACTIVE_SPORTS,
    ODDS_API_KEY,
    STARTING_CAPITAL_USD,
    MAX_SINGLE_POSITION_USD,
    KELLY_FRACTION,
    MIN_NET_EDGE_PAPER,
    SQLITE_DB_PATH,
)

# ─────────────────────────────────────────────────────────────────────────────
# Logging (compact, sports-focused)
# ─────────────────────────────────────────────────────────────────────────────

os.makedirs("data", exist_ok=True)
logging.basicConfig(
    level=logging.WARNING,          # suppress library noise during the paper test
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler("data/sports_paper_test.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("sports_paper_test")


# ─────────────────────────────────────────────────────────────────────────────
# Sports-specific paper trade tracker (separate SQLite table)
# ─────────────────────────────────────────────────────────────────────────────

SPORTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS sports_paper_trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_at       TEXT NOT NULL,
    mode            TEXT NOT NULL DEFAULT 'paper',
    kalshi_ticker   TEXT NOT NULL,
    kalshi_title    TEXT,
    kalshi_side     TEXT NOT NULL,          -- yes | no
    kalshi_ask      REAL NOT NULL,          -- ask price before fee (0-1)
    cost_per_contr  REAL NOT NULL,          -- kalshi_ask * (1 + fee)
    fair_prob       REAL NOT NULL,          -- sportsbook devigged consensus
    net_edge        REAL NOT NULL,          -- fair_prob - cost_per_contr
    net_edge_pct    REAL NOT NULL,          -- net_edge as %
    contracts       INTEGER NOT NULL,
    total_stake     REAL NOT NULL,          -- contracts * cost_per_contr
    expected_profit REAL NOT NULL,          -- contracts * net_edge
    kelly_fraction  REAL,                   -- half-kelly fraction used
    books_used      INTEGER,
    legs_priced     INTEGER,
    legs_total      INTEGER,
    sport           TEXT,
    session_id      TEXT
);
CREATE INDEX IF NOT EXISTS idx_sports_session ON sports_paper_trades(session_id);
"""


class SportsPaperTracker:
    """Lightweight SQLite logger for sports paper trades."""

    def __init__(self, db_path: str = SQLITE_DB_PATH):
        os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else ".", exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SPORTS_SCHEMA)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.commit()

    def log_trade(self, trade: dict) -> int:
        cur = self.conn.execute("""
            INSERT INTO sports_paper_trades (
                opened_at, mode, kalshi_ticker, kalshi_title,
                kalshi_side, kalshi_ask, cost_per_contr,
                fair_prob, net_edge, net_edge_pct,
                contracts, total_stake, expected_profit,
                kelly_fraction, books_used, legs_priced, legs_total,
                sport, session_id
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            trade["opened_at"], "paper",
            trade["kalshi_ticker"], trade.get("kalshi_title"),
            trade["kalshi_side"], trade["kalshi_ask"], trade["cost_per_contr"],
            trade["fair_prob"], trade["net_edge"], trade["net_edge_pct"],
            trade["contracts"], trade["total_stake"], trade["expected_profit"],
            trade.get("kelly_fraction"), trade.get("books_used"),
            trade.get("legs_priced"), trade.get("legs_total"),
            trade.get("sport"), trade.get("session_id"),
        ))
        self.conn.commit()
        return cur.lastrowid

    def get_session_trades(self, session_id: str) -> list:
        rows = self.conn.execute(
            "SELECT * FROM sports_paper_trades WHERE session_id=? ORDER BY id",
            (session_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self):
        self.conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Kelly sizing for single-leg Kalshi prediction market contract
# ─────────────────────────────────────────────────────────────────────────────

def kelly_contracts(
    fair_prob: float,
    kalshi_ask: float,
    bankroll: float,
    max_stake_usd: float,
    kelly_fraction: float = KELLY_FRACTION,
) -> tuple:
    """
    Half-Kelly position size for a binary prediction market bet.

    Kelly formula for binary market:
        f* = (p - q) / b
        where p = win prob, q = 1-p, b = net odds (profit per dollar risked)

    For a Kalshi YES contract at price `c` (including fee):
        b = (1 - c) / c       (you profit (1-c) per dollar risked, risking c)
        f* = (fair_prob - c) / (1 - c)   ← simplified Kelly for prediction market

    Returns (contracts, stake_usd, kelly_frac).
    """
    cost = kalshi_ask * (1 + KALSHI_TAKER_FEE)  # cost including Kalshi 7% fee
    if cost <= 0 or cost >= 1:
        return 0, 0.0, 0.0

    # Kelly fraction for prediction market (no vig already included in net_edge)
    f_star = (fair_prob - cost) / (1.0 - cost)
    if f_star <= 0:
        return 0, 0.0, 0.0

    half_k = f_star * kelly_fraction
    stake_usd = min(bankroll * half_k, max_stake_usd)
    contracts = max(1, int(stake_usd / cost))
    actual_stake = contracts * cost
    return contracts, actual_stake, half_k


# ─────────────────────────────────────────────────────────────────────────────
# Paper test
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PaperTrade:
    """One simulated Kalshi sports paper trade."""
    id: int
    ticker: str
    title: str
    side: str
    ask: float              # raw ask (pre-fee)
    cost: float             # per contract (post-fee)
    fair_prob: float
    net_edge: float
    contracts: int
    stake: float
    expected_profit: float
    kelly_frac: float
    sport: str
    books_used: int
    legs_priced: int
    legs_total: int


def _bar(label: str, width: int = 66) -> str:
    """Print a header bar."""
    pad = max(0, width - len(label) - 4)
    return f"  {'-' * 2} {label} {'-' * pad}"


def run_paper_test(capital: float, max_per_trade: float, verbose: bool = False):
    """
    Execute the full sports paper test pipeline.
    Prints results to stdout.
    """
    session_id = f"sports_{int(time.time())}"
    now_str    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    print()
    print("  +" + "=" * 64 + "+")
    print(f"  |  SPORTS ARB PAPER TEST  -  {now_str:<35}|")
    print(f"  |  Bankroll: ${capital:,.2f}  -  Max/Trade: ${max_per_trade:.0f}  -  MODE: PAPER{' ' * 12}|")
    print("  +" + "=" * 64 + "+")
    print()

    # ── Step 1: Fetch KXMVE markets from Kalshi ──────────────────────────────
    print(_bar("STEP 1  Kalshi KXMVE sports markets"))
    t0 = time.perf_counter()
    try:
        kalshi = KalshiClient()
        raw_markets = kalshi.get_all_open_markets(None, KALSHI_KXMVE_MAX_PAGES)
        # Keep only KXMVE sports contracts (parlay-style game markets)
        kxmve_raw     = [m for m in raw_markets
                         if m.get("ticker", "").upper().startswith("KXMVE")]
        all_kxmve     = [normalize_kalshi_market(m) for m in kxmve_raw]
        # KXMVE order books are empty — prices come from the market data itself.
        # Filter to only markets that have an active yes_ask price.
        kxmve_markets = [km for km in all_kxmve
                         if (km.extra.get("yes_ask") or 0) > 0]
        elapsed = time.perf_counter() - t0
        print(f"    Fetched {len(all_kxmve):,} KXMVE markets from Kalshi  [{elapsed:.1f}s]")
        print(f"    Markets with active YES price: {len(kxmve_markets):,} "
              f"({len(all_kxmve)-len(kxmve_markets):,} have no ask — unseeded/illiquid)")
    except Exception as e:
        print(f"    ERROR: Kalshi fetch failed — {e}")
        print("    Check KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH in config/secrets.env")
        return

    if not kxmve_markets:
        print("    No KXMVE markets found. Kalshi may be in maintenance or all sports are off-season.")
        return
    print()

    # ── Step 2: Fetch Odds API sportsbook events ──────────────────────────────
    print(_bar("STEP 2  Sportsbook odds  (The Odds API)"))
    t0 = time.perf_counter()
    if not ODDS_API_KEY:
        print("    ODDS_API_KEY not set in config/secrets.env — cannot run sports scan.")
        return

    try:
        scanner = OddsArbScanner(min_edge=MIN_NET_EDGE_PAPER)
        events  = scanner.fetch_events(ODDS_API_ACTIVE_SPORTS)
        elapsed = time.perf_counter() - t0

        # Count unique books
        books_seen = set()
        for ev in events:
            for bm in ev.get("bookmakers", []):
                books_seen.add(bm.get("title", bm.get("key", "")))

        print(f"    Fetched {len(events):,} events across {len(ODDS_API_ACTIVE_SPORTS)} sports "
              f"from {len(books_seen)} books  [{elapsed:.1f}s]")
        print(f"    Sports: {', '.join(ODDS_API_ACTIVE_SPORTS)}")
        quota = scanner.odds_client.quota_remaining()
        if quota is not None:
            print(f"    Odds API quota remaining: {quota} requests")
    except Exception as e:
        print(f"    ERROR: Odds API fetch failed — {e}")
        return

    if not events:
        print("    No sportsbook events found. All covered sports may be off-season.")
        return
    print()

    # ── Step 3: Diagnostic — parse KXMVE titles and check leg matching ──────────
    print(_bar("STEP 3  Diagnostic — KXMVE parsing and sportsbook matching"))
    from detection.kxmve_parser import KXMVEParser, build_team_variants
    team_variants = build_team_variants(events)

    parsed_count = 0; team_leg_count = 0; matched_leg_count = 0
    player_only_count = 0; sample_titles = []

    for km in kxmve_markets[:5]:
        sample_titles.append(km.title[:90])

    for km in kxmve_markets:
        legs = KXMVEParser.parse(km.title)
        if not legs:
            continue
        parsed_count += 1
        team_legs  = [l for l in legs if l.leg_type in ("team_win", "team_spread")]
        player_legs = [l for l in legs if l.leg_type == "player_over"]
        if player_legs and not team_legs:
            player_only_count += 1
        team_leg_count += len(team_legs)
        for leg in team_legs:
            if team_variants.get(leg.subject):
                matched_leg_count += 1
            else:
                # substring fallback
                for variant in team_variants:
                    if variant in leg.subject or leg.subject in variant:
                        if len(variant) >= 4:
                            matched_leg_count += 1
                            break

    match_rate = (matched_leg_count / team_leg_count * 100) if team_leg_count else 0
    print(f"    Team-variant lookup table: {len(team_variants):,} entries")
    print(f"    KXMVE markets with parseable legs:  {parsed_count:,} / {len(kxmve_markets):,}")
    print(f"    Markets that are pure player props:  {player_only_count:,} (skipped — no team odds API)")
    print(f"    Team legs found:                     {team_leg_count:,}")
    print(f"    Team legs matched to sportsbook:     {matched_leg_count:,}  ({match_rate:.0f}%)")
    print()
    print("    Sample KXMVE titles:")
    for t in sample_titles:
        print(f"      {t}")
    print()

    # ── Step 3b: Filter to team-only KXMVE markets ───────────────────────────
    # Player props (player_over) and totals (total_over) cannot be priced via
    # the h2h Odds API endpoint. When a market mixes team_win + player_over legs
    # only the team_win leg is priced, making fair_prob >> true parlay prob and
    # creating a large spurious "edge". Filter them out entirely.
    from detection.kxmve_parser import KXMVEParser as _Parser
    unpriceable_types = {"player_over", "total_over"}

    team_only_markets = []
    for km in kxmve_markets:
        legs = _Parser.parse(km.title)
        if not legs:
            continue
        if any(l.leg_type in unpriceable_types for l in legs):
            continue   # has a player prop or total → can't price all legs → skip
        team_only_markets.append(km)

    print(f"    Team-only (no props/totals): {len(team_only_markets):,} / {len(kxmve_markets):,} markets")
    print(f"    Mixed/prop-only markets skipped: {len(kxmve_markets) - len(team_only_markets):,}")
    print(f"    (Prop/total markets skipped to avoid inflated edge from partial leg pricing)")
    print()

    # ── Step 4: Scan for mispricings (paper mode — min_books=2) ──────────────
    print(_bar("STEP 4  Scanning for mispriced Kalshi markets"))
    t0 = time.perf_counter()

    # Paper mode: lower min_books to 2 (live would require 3)
    scanner.min_books = 2

    def fetch_kxmve_book(market):
        from clients.normalizer import NormalizedBook, NormalizedMarketBook, PriceLevel
        raw = kalshi.get_orderbook(market.market_id)
        book = normalize_kalshi_book(raw)
        # KXMVE LOBs are always empty — fall back to market-level embedded prices
        if book.yes.best_ask is None and book.no.best_ask is None:
            yes_ask = float(market.extra.get("yes_ask") or 0)
            no_ask  = float(market.extra.get("no_ask")  or 0)
            # Synthesise a single-level book so the scanner can evaluate the market
            yes_book = NormalizedBook(
                asks=[PriceLevel(price=yes_ask, quantity=500)] if yes_ask > 0 else []
            )
            no_book = NormalizedBook(
                asks=[PriceLevel(price=no_ask,  quantity=500)] if no_ask  > 0 else []
            )
            return NormalizedMarketBook(yes=yes_book, no=no_book)
        return book

    try:
        opportunities: List[OddsArbOpportunity] = scanner.scan(
            team_only_markets, fetch_kxmve_book, events
        )
        elapsed = time.perf_counter() - t0
        print(f"    Scanned {len(team_only_markets):,} team-only markets in {elapsed:.1f}s "
              f"-> {len(opportunities)} opportunities (min_edge={MIN_NET_EDGE_PAPER:.1%}, min_books=2)")
    except Exception as e:
        print(f"    ERROR: scan failed — {e}")
        return

    print()

    # ── Step 5: Display opportunities ─────────────────────────────────────────
    if not opportunities:
        print(_bar("RESULTS"))
        print(f"    No opportunities above {MIN_NET_EDGE_PAPER:.1%} net edge threshold.")
        print()
        if matched_leg_count == 0:
            print("    ROOT CAUSE: 0 KXMVE legs matched sportsbook events.")
            print("    Today's KXMVE markets may be using team abbreviations not in the")
            print("    variant table, or all markets are pure player-prop parlays.")
        elif match_rate < 30:
            print(f"    ROOT CAUSE: low match rate ({match_rate:.0f}%). Most KXMVE legs")
            print("    could not be mapped to sportsbook team names.")
        else:
            print("    Matching worked but Kalshi prices are within the sportsbook consensus today.")
            print("    KXMVE markets may be efficiently priced — edge < 1.5% net after 7% Kalshi fee.")
        print()
        print("    Tips:")
        print("    - Try during peak sports days: NBA/NHL playoff nights, full MLB slate")
        print("    - Check logs for near-miss details: data/sports_paper_test.log")
        print("    - Lower MIN_NET_EDGE_PAPER in config/settings.py if you want more trades")
        print()

        # Near-miss scan: run with min_edge=0 to show best sub-threshold opportunities
        print(_bar("NEAR-MISS SCAN  (best opportunities found today)"))
        print()
        try:
            scanner.min_edge = 0.0
            scanner.min_books = 1
            near_misses_all: List[OddsArbOpportunity] = scanner.scan(
                team_only_markets, fetch_kxmve_book, events
            )
            if near_misses_all:
                top = near_misses_all[:10]
                hdr2 = (
                    f"  {'#':>2}  {'Ticker':<22}  {'Side':4}  {'Ask':>5}  "
                    f"{'Fair':>5}  {'Net Edge':>8}  {'vs Target':>9}  Books"
                )
                print(hdr2)
                print("  " + "-" * (len(hdr2) - 2))
                for i, opp in enumerate(top, 1):
                    gap = (opp.net_edge - MIN_NET_EDGE_PAPER) * 100
                    print(
                        f"  {i:>2}  {opp.kalshi_ticker:<22}  {opp.kalshi_side.upper():<4}  "
                        f"{opp.kalshi_price:>5.3f}  {opp.fair_prob:>5.3f}  "
                        f"{opp.net_edge*100:>7.2f}%  {gap:>+8.2f}%  {opp.books_used}"
                    )
                avg_edge = sum(o.net_edge for o in near_misses_all) / len(near_misses_all)
                max_edge = near_misses_all[0].net_edge
                print()
                print(f"    Total matchable markets: {len(near_misses_all)}")
                print(f"    Best net edge today:     {max_edge*100:.2f}%  (need {MIN_NET_EDGE_PAPER*100:.1f}%)")
                print(f"    Average net edge:        {avg_edge*100:.2f}%")
                print()
                if max_edge >= MIN_NET_EDGE_PAPER * 0.5:
                    print("    ASSESSMENT: Near-threshold markets exist. Try again in a few hours")
                    print("    when odds shift — lines move as game time approaches.")
                else:
                    print("    ASSESSMENT: Kalshi KXMVE is efficiently priced today. No exploitable")
                    print("    edge above vig. Best opportunities are on high-liquidity nights.")
            else:
                print("    No matchable KXMVE-sportsbook pairs found even with min_edge=0.")
                print("    This may indicate today's KXMVE slate is entirely player-prop parlays.")
        except Exception as e:
            print(f"    Near-miss scan failed: {e}")
        print()
        return

    # Separate fully-priced opportunities from partial-coverage ones
    full_opps    = [o for o in opportunities if o.legs_priced == o.legs_total]
    partial_opps = [o for o in opportunities if o.legs_priced < o.legs_total]

    print(_bar(f"OPPORTUNITIES FOUND  ({len(opportunities)} total, {len(full_opps)} fully-priced)"))
    print()
    hdr = (
        f"  {'#':>2}  {'Ticker':<22}  {'Side':4}  {'Ask':>6}  "
        f"{'Fair':>6}  {'Edge':>6}  {'Books':>5}  {'Legs':>6}  Sport"
    )
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    for i, opp in enumerate(full_opps[:20], 1):
        edge_pct = opp.net_edge * 100
        legs_fmt = f"{opp.legs_priced}/{opp.legs_total}"
        print(
            f"  {i:>2}  {opp.kalshi_ticker:<22}  {opp.kalshi_side.upper():<4}  "
            f"{opp.kalshi_price:>5.3f}  {opp.fair_prob:>6.3f}  "
            f"{edge_pct:>5.1f}%  {opp.books_used:>5}  {legs_fmt:>6}  {opp.sport}"
        )

    if partial_opps:
        print(f"\n  [!] {len(partial_opps)} partial-coverage opportunities skipped (team leg not found in sportsbook):")
        for opp in partial_opps:
            edge_pct = opp.net_edge * 100
            legs_fmt = f"{opp.legs_priced}/{opp.legs_total}"
            print(
                f"      {opp.kalshi_ticker:<50}  {opp.kalshi_side.upper():<4}  "
                f"edge={edge_pct:.1f}%  legs={legs_fmt}  {opp.sport}"
            )
        print("      (Edge may be inflated — unmatched team has unknown win probability)")

    if len(full_opps) > 20:
        print(f"  ... and {len(full_opps) - 20} more")
    print()

    # Only trade fully-priced opportunities
    opportunities = full_opps

    # ── Step 6: Kelly-size and simulate paper trades ──────────────────────────
    print(_bar("PAPER TRADES  (Half-Kelly sizing, $1,000 bankroll)"))
    print()

    tracker   = SportsPaperTracker()
    trades: List[PaperTrade] = []
    deployed  = 0.0
    remaining = capital

    for i, opp in enumerate(opportunities, 1):
        # Skip if we've used > 80% of capital
        if deployed >= capital * 0.80:
            print(f"    Capital limit reached (${deployed:.2f} deployed). Stopping.")
            break

        # For YES trades win prob = fair_prob; for NO trades it's (1 - fair_prob)
        win_prob = opp.fair_prob if opp.kalshi_side == "yes" else (1.0 - opp.fair_prob)
        contracts, stake, kelly_frac = kelly_contracts(
            fair_prob=win_prob,
            kalshi_ask=opp.kalshi_price,
            bankroll=remaining,
            max_stake_usd=max_per_trade,
        )
        if contracts < 1:
            continue

        cost          = opp.kalshi_price * (1 + KALSHI_TAKER_FEE)
        actual_stake  = contracts * cost
        exp_profit    = contracts * opp.net_edge
        net_edge_pct  = opp.net_edge * 100

        trade = PaperTrade(
            id            = i,
            ticker        = opp.kalshi_ticker,
            title         = opp.kalshi_title[:60] if opp.kalshi_title else "",
            side          = opp.kalshi_side.upper(),
            ask           = opp.kalshi_price,
            cost          = cost,
            fair_prob     = opp.fair_prob,
            net_edge      = opp.net_edge,
            contracts     = contracts,
            stake         = actual_stake,
            expected_profit=exp_profit,
            kelly_frac    = kelly_frac,
            sport         = opp.sport,
            books_used    = opp.books_used,
            legs_priced   = opp.legs_priced,
            legs_total    = opp.legs_total,
        )
        trades.append(trade)
        deployed  += actual_stake
        remaining -= actual_stake

        # Log to SQLite
        tracker.log_trade({
            "opened_at":      datetime.now(timezone.utc).isoformat(),
            "kalshi_ticker":  opp.kalshi_ticker,
            "kalshi_title":   opp.kalshi_title,
            "kalshi_side":    opp.kalshi_side,
            "kalshi_ask":     opp.kalshi_price,
            "cost_per_contr": cost,
            "fair_prob":      opp.fair_prob,
            "net_edge":       opp.net_edge,
            "net_edge_pct":   net_edge_pct,
            "contracts":      contracts,
            "total_stake":    actual_stake,
            "expected_profit":exp_profit,
            "kelly_fraction": kelly_frac,
            "books_used":     opp.books_used,
            "legs_priced":    opp.legs_priced,
            "legs_total":     opp.legs_total,
            "sport":          opp.sport,
            "session_id":     session_id,
        })

    if not trades:
        print("    No trades passed Kelly sizing filters.")
        print("    (Possible cause: fair_prob too close to ask → Kelly fraction < 1 contract)")
        tracker.close()
        return

    # Print trade table
    thdr = (
        f"  {'#':>2}  {'Ticker':<22}  {'Side':4}  {'Ask':>5}  {'Fair':>5}  "
        f"{'Edge':>5}  {'Contr':>5}  {'Stake':>7}  {'ExpProfit':>9}"
    )
    print(thdr)
    print("  " + "-" * (len(thdr) - 2))
    for t in trades:
        print(
            f"  {t.id:>2}  {t.ticker:<22}  {t.side:<4}  {t.ask:>5.3f}  "
            f"{t.fair_prob:>5.3f}  {t.net_edge*100:>4.1f}%  "
            f"{t.contracts:>5}  ${t.stake:>6.2f}  ${t.expected_profit:>8.4f}"
        )

    # ── Step 6: Portfolio summary ─────────────────────────────────────────────
    total_expected   = sum(t.expected_profit for t in trades)
    total_deployed   = sum(t.stake for t in trades)
    total_contracts  = sum(t.contracts for t in trades)
    avg_edge         = sum(t.net_edge for t in trades) / len(trades)
    avg_books        = sum(t.books_used for t in trades) / len(trades)
    utilization      = total_deployed / capital * 100
    expected_roi     = total_expected / total_deployed * 100 if total_deployed > 0 else 0

    # Annualized estimate: sports games resolve in ≤1 day
    # Average resolution: same day or next day → ~1 day
    # Assume we can redeploy capital daily
    days_per_cycle   = 1.0
    annual_cycles    = 365 / days_per_cycle
    # Conservative: assume we can only do 5 cycles per week (no weekend data)
    annual_cycles_conservative = 250
    ann_profit_optimistic     = total_expected * annual_cycles
    ann_profit_conservative   = total_expected * annual_cycles_conservative

    print()
    print(_bar("PORTFOLIO SUMMARY"))
    print()
    print(f"    Bankroll:               ${capital:>10,.2f}")
    print(f"    Capital deployed:       ${total_deployed:>10,.2f}  ({utilization:.1f}%)")
    print(f"    Capital idle:           ${capital - total_deployed:>10,.2f}  ({100-utilization:.1f}%)")
    print(f"    Trades placed:          {len(trades):>10}")
    print(f"    Total contracts:        {total_contracts:>10,}")
    print(f"    Average edge (net):     {avg_edge*100:>9.2f}%")
    print(f"    Average books/leg:      {avg_books:>10.1f}")
    print()
    print(f"    Expected profit today:  ${total_expected:>10.4f}")
    print(f"    Return on deployed:     {expected_roi:>9.2f}%")
    print(f"    Return on capital:      {total_expected/capital*100:>9.2f}%")
    print()
    print(f"    Annualized (optimistic, 365d):     ${ann_profit_optimistic:>8,.0f}  "
          f"({ann_profit_optimistic/capital*100:,.0f}% ROI)")
    print(f"    Annualized (conservative, 250d):   ${ann_profit_conservative:>8,.0f}  "
          f"({ann_profit_conservative/capital*100:,.0f}% ROI)")
    print()

    # ── Step 7: Sport breakdown ───────────────────────────────────────────────
    sport_groups: dict = {}
    for t in trades:
        sport_groups.setdefault(t.sport, []).append(t)

    print(_bar("BREAKDOWN BY SPORT"))
    print()
    sport_hdr = f"  {'Sport':<30}  {'Trades':>6}  {'Deployed':>9}  {'ExpProfit':>9}  {'AvgEdge':>7}"
    print(sport_hdr)
    print("  " + "-" * (len(sport_hdr) - 2))
    for sport, group in sorted(sport_groups.items(), key=lambda x: -sum(t.expected_profit for t in x[1])):
        dp    = sum(t.stake for t in group)
        ep    = sum(t.expected_profit for t in group)
        ae    = sum(t.net_edge for t in group) / len(group) * 100
        print(f"  {sport:<30}  {len(group):>6}  ${dp:>8.2f}  ${ep:>8.4f}  {ae:>6.1f}%")
    print()

    # ── Step 8: Risk notes ─────────────────────────────────────────────────────
    print(_bar("RISK NOTES"))
    print()
    print("    Strategy:    Buy underpriced Kalshi KXMVE contracts vs sportsbook fair value")
    print("    Execution:   Single-leg Kalshi-only (sportsbooks ban arb within 3-6 months)")
    print("    Resolution:  Same-day for game outcomes; next-day for some props")
    print("    Fees:        7% Kalshi taker fee already included in net_edge calculation")
    print("    Liquidity:   KXMVE books are often thin — real fills may be partial")
    print("    Slippage:    Book walker check skipped in paper mode; add in live mode")
    print()
    print(f"    All {len(trades)} trades logged to SQLite: {SQLITE_DB_PATH}")
    print(f"    Session ID: {session_id}")
    print()
    print("  " + "=" * 64)
    print()

    tracker.close()


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Sports arb paper test — Kalshi KXMVE vs sportsbook consensus"
    )
    parser.add_argument("--capital",       type=float, default=STARTING_CAPITAL_USD,
                        help=f"Starting bankroll (default: ${STARTING_CAPITAL_USD:.0f})")
    parser.add_argument("--max-per-trade", type=float, default=MAX_SINGLE_POSITION_USD,
                        help=f"Max USD per trade (default: ${MAX_SINGLE_POSITION_USD:.0f})")
    parser.add_argument("--verbose",       action="store_true",
                        help="Show DEBUG logs")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    run_paper_test(
        capital       = args.capital,
        max_per_trade = args.max_per_trade,
        verbose       = args.verbose,
    )


if __name__ == "__main__":
    main()
