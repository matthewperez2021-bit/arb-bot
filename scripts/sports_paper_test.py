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

from scripts._display import (
    print_strategy_header,
    print_portfolio_summary,
    print_scan_dashboard,
)
from clients.kalshi import KalshiClient
from clients.normalizer import normalize_kalshi_book, normalize_kalshi_market
from detection.odds_arb_scanner import OddsArbScanner, OddsArbOpportunity
from config.settings import (
    KALSHI_TAKER_FEE,
    KALSHI_KXMVE_MAX_PAGES,
    ODDS_API_ACTIVE_SPORTS,
    ODDS_API_KEY,
    STARTING_CAPITAL_USD,
    SQLITE_DB_PATH,
)
from config.strategies import Strategy, get as get_strategy, ACTIVE_STRATEGY

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
    session_id      TEXT,
    strategy_version TEXT DEFAULT 'v1'      -- which Strategy produced this trade
);
CREATE INDEX IF NOT EXISTS idx_sports_session ON sports_paper_trades(session_id);
CREATE INDEX IF NOT EXISTS idx_sports_strategy ON sports_paper_trades(strategy_version);
"""


class SportsPaperTracker:
    """Lightweight SQLite logger for sports paper trades."""

    def __init__(self, db_path: str = SQLITE_DB_PATH):
        os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else ".", exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SPORTS_SCHEMA)
        self.conn.execute("PRAGMA journal_mode=WAL")
        # Idempotent migration: add strategy_version column on existing DBs.
        try:
            self.conn.execute(
                "ALTER TABLE sports_paper_trades "
                "ADD COLUMN strategy_version TEXT DEFAULT 'v1'"
            )
        except sqlite3.OperationalError:
            pass  # column already exists
        # Backfill any NULLs from earlier rows
        self.conn.execute(
            "UPDATE sports_paper_trades SET strategy_version='v1' "
            "WHERE strategy_version IS NULL"
        )
        self.conn.commit()

    def log_trade(self, trade: dict) -> int:
        cur = self.conn.execute("""
            INSERT INTO sports_paper_trades (
                opened_at, mode, kalshi_ticker, kalshi_title,
                kalshi_side, kalshi_ask, cost_per_contr,
                fair_prob, net_edge, net_edge_pct,
                contracts, total_stake, expected_profit,
                kelly_fraction, books_used, legs_priced, legs_total,
                sport, session_id, strategy_version
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            trade["opened_at"], "paper",
            trade["kalshi_ticker"], trade.get("kalshi_title"),
            trade["kalshi_side"], trade["kalshi_ask"], trade["cost_per_contr"],
            trade["fair_prob"], trade["net_edge"], trade["net_edge_pct"],
            trade["contracts"], trade["total_stake"], trade["expected_profit"],
            trade.get("kelly_fraction"), trade.get("books_used"),
            trade.get("legs_priced"), trade.get("legs_total"),
            trade.get("sport"), trade.get("session_id"),
            trade.get("strategy_version", "v1"),
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
    kelly_fraction: float = 0.5,
    sport: str = "",
) -> tuple:
    """
    Half-Kelly position size for a binary prediction market bet.

    Kelly formula for binary market:
        f* = (p - q) / b
        where p = win prob, q = 1-p, b = net odds (profit per dollar risked)

    For a Kalshi YES contract at price `c` (including fee):
        b = (1 - c) / c       (you profit (1-c) per dollar risked, risking c)
        f* = (fair_prob - c) / (1 - c)   ← simplified Kelly for prediction market

    Per-(sport, edge_bucket) calibration override is applied as a multiplier
    on the Kelly fraction. See risk/kelly.calibration_factor() and
    config.settings.CALIBRATION_OVERRIDES. If no override exists for this
    sport+bucket, the multiplier is 1.0 (current behavior).

    Returns (contracts, stake_usd, kelly_frac).
    """
    cost = kalshi_ask * (1 + KALSHI_TAKER_FEE)  # cost including Kalshi 7% fee
    if cost <= 0 or cost >= 1:
        return 0, 0.0, 0.0

    # Kelly fraction for prediction market (no vig already included in net_edge)
    f_star = (fair_prob - cost) / (1.0 - cost)
    if f_star <= 0:
        return 0, 0.0, 0.0

    # Apply historical calibration override based on sport + edge bucket
    from risk.kelly import calibration_factor
    net_edge = fair_prob - cost
    cal_mult = calibration_factor(sport, net_edge)
    effective_kf = kelly_fraction * cal_mult

    half_k = f_star * effective_kf
    stake_usd = min(bankroll * half_k, max_stake_usd)
    contracts = max(1, int(stake_usd / cost))
    actual_stake = contracts * cost
    return contracts, actual_stake, half_k


# ─────────────────────────────────────────────────────────────────────────────
# Dynamic Kelly calibration
# ─────────────────────────────────────────────────────────────────────────────

def calibrated_kelly(tracker: "SportsPaperTracker", strategy: "Strategy",
                     min_settled: int = 20) -> float:
    """
    Return an effective Kelly fraction scaled by this strategy's realized win rate.

    If fewer than `min_settled` trades have resolved for this strategy, fall back
    to the configured strategy.kelly_fraction (not enough data to recalibrate).

    Scaling rule:
        effective = strategy.kelly_fraction * (realized_win_rate / theoretical_win_rate)
    where theoretical_win_rate is back-solved from the average net_edge of settled
    trades (edge = fair_prob - cost_per_contr, so fair_prob ≈ cost_per_contr + edge).
    We cap effective kelly at strategy.kelly_fraction so good runs never push us
    above the configured ceiling, and floor it at 0.1 so we always trade something.
    """
    try:
        row = tracker.conn.execute(
            """
            SELECT
                COUNT(*)                                          AS settled,
                SUM(CASE WHEN outcome='won' THEN 1 ELSE 0 END)   AS wins,
                AVG(fair_prob)                                    AS avg_fair_prob
            FROM sports_paper_trades
            WHERE outcome IN ('won','lost') AND strategy_version=?
            """,
            (strategy.name,)
        ).fetchone()
    except sqlite3.OperationalError:
        return strategy.kelly_fraction

    if not row or (row[0] or 0) < min_settled:
        return strategy.kelly_fraction

    settled        = row[0]
    wins           = row[1] or 0
    avg_fair_prob  = row[2] or 0.55
    realized_wr    = wins / settled

    # Theoretical win rate ≈ average fair_prob (that's what we're betting on)
    theoretical_wr = max(avg_fair_prob, 0.50)
    scale          = realized_wr / theoretical_wr
    effective      = strategy.kelly_fraction * scale
    # Clamp: never exceed configured ceiling; never drop below 10%
    return max(0.10, min(effective, strategy.kelly_fraction))


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
    close_time: str = ""


def _bar(label: str, width: int = 66) -> str:
    """Print a header bar."""
    pad = max(0, width - len(label) - 4)
    return f"  {'-' * 2} {label} {'-' * pad}"


def _fmt_close_time(iso: str) -> str:
    """Format an ISO 8601 UTC close_time into a readable 'When' string."""
    if not iso:
        return "unknown"
    try:
        from datetime import timezone
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = dt - now
        days  = delta.days
        hours = int(delta.total_seconds() // 3600)
        mins  = int((delta.total_seconds() % 3600) // 60)

        if days < 0:
            label = "already closed"
        elif hours < 1:
            label = f"in ~{mins}m"
        elif hours < 24:
            label = f"in ~{hours}h {mins}m"
        elif days == 1:
            label = "tomorrow"
        else:
            label = f"in {days} days"

        return dt.strftime("%Y-%m-%d %H:%M UTC") + f"  ({label})"
    except Exception:
        return iso


def print_trade_card(trade: "PaperTrade", opp: "OddsArbOpportunity"):
    """Print a WHO / WHAT / WHEN / WHY card for one paper trade."""
    w = 68

    # WHO — teams and their individual win probabilities
    if opp.leg_details:
        who_parts = [f"{name} ({prob*100:.0f}%)" for name, prob in opp.leg_details]
        who_str = "  +  ".join(who_parts)
    else:
        who_str = trade.ticker

    # WHAT — side, contracts, price, total stake
    side_word = "BUY YES" if trade.side == "YES" else "BUY NO"
    what_str  = (
        f"{side_word}  |  {trade.contracts} contracts "
        f"@ ${trade.cost:.4f} ea  =  ${trade.stake:.2f} total"
    )

    # WHEN — resolution time
    when_str = _fmt_close_time(trade.close_time)

    # WHY — the mispricing logic
    cost_pct    = trade.cost * 100
    fair_pct    = trade.fair_prob * 100
    edge_pct    = trade.net_edge * 100
    fee_drag    = trade.ask * KALSHI_TAKER_FEE * 100
    why_line1   = (
        f"Kalshi prices at {trade.ask*100:.1f}c  |  "
        f"sportsbooks imply {fair_pct:.1f}c fair value  |  "
        f"7% fee costs {fee_drag:.1f}c"
    )
    why_line2 = (
        f"Net edge: {fair_pct:.1f}% fair - {cost_pct:.1f}% cost = {edge_pct:+.1f}%  |  "
        f"{trade.books_used} books  |  {trade.legs_priced}/{trade.legs_total} legs fully priced"
    )

    # EXP — expected outcome
    exp_str = (
        f"${trade.expected_profit:.4f} expected profit  |  "
        f"Kelly fraction: {trade.kelly_frac*100:.1f}% of bankroll"
    )

    print(f"  {'─'*w}")
    print(f"  Trade #{trade.id}  [{trade.sport}]")
    print(f"  {'─'*w}")
    print(f"  WHO  : {who_str}")
    print(f"  WHAT : {what_str}")
    print(f"  WHEN : {when_str}")
    print(f"  WHY  : {why_line1}")
    print(f"         {why_line2}")
    print(f"  EXP  : {exp_str}")
    print()


def run_paper_test(
    capital: float,
    strategy: Strategy,
    verbose: bool = False,
    shared_data: dict = None,
    taken_tickers: set = None,
    summaries: dict = None,
):
    """
    Execute the full sports paper test pipeline for ONE strategy version.

    Args:
        capital:        Bankroll for this run.
        strategy:       The Strategy version controlling filters / sizing / cap.
        verbose:        Forward to logging level.
        shared_data:    Pre-fetched market + odds data to avoid redundant API
                        calls in A/B mode. Keys: "markets", "opportunities".
                        When provided, Steps 1-5 are skipped entirely.
        taken_tickers:  Mutable set of Kalshi tickers already traded this scan
                        by an earlier strategy pass. Opportunities whose ticker
                        is in this set are skipped to avoid doubling up on the
                        same position across strategies. Newly traded tickers
                        are added to the set so subsequent passes see them.

    Returns:
        None.
    """
    max_per_trade = strategy.max_per_trade_usd
    session_id = f"sports_{int(time.time())}_{strategy.name}"
    now_str    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Compute per-strategy bankroll before printing the header so the display
    # reflects actual remaining capital, not just the starting --capital arg.
    tracker = SportsPaperTracker()

    strategy_deployed = tracker.conn.execute(
        "SELECT COALESCE(SUM(total_stake), 0) FROM sports_paper_trades "
        "WHERE (outcome='open' OR outcome IS NULL) AND strategy_version=?",
        (strategy.name,)
    ).fetchone()[0] or 0.0

    try:
        strategy_pnl = tracker.conn.execute(
            "SELECT COALESCE(SUM(actual_profit), 0.0) FROM sports_paper_trades "
            "WHERE outcome IN ('won','lost') AND strategy_version=?",
            (strategy.name,)
        ).fetchone()[0] or 0.0
    except sqlite3.OperationalError:
        strategy_pnl = 0.0

    strategy_bankroll   = max(0.0, capital + strategy_pnl - strategy_deployed)
    effective_kelly     = calibrated_kelly(tracker, strategy)

    # Seed the dashboard summary immediately so the strategy's bankroll appears
    # even if we early-return below (no opportunities, capital cap, errors, etc.).
    # The trade-this-scan fields will be overwritten at the end if we get there.
    if summaries is not None:
        try:
            wlrow = tracker.conn.execute(
                "SELECT COUNT(*) AS n, "
                "SUM(CASE WHEN outcome='won' THEN 1 ELSE 0 END) AS w "
                "FROM sports_paper_trades "
                "WHERE outcome IN ('won','lost') AND strategy_version=?",
                (strategy.name,)
            ).fetchone()
            settled_n = wlrow["n"] or 0
            settled_w = wlrow["w"] or 0
        except sqlite3.OperationalError:
            settled_n = settled_w = 0
        summaries[strategy.name] = {
            "name":               strategy.name,
            "bankroll":           strategy_bankroll,
            "open_deployed":      strategy_deployed,
            "trades_this_scan":   0,
            "deployed_this_scan": 0.0,
            "expected_this_scan": 0.0,
            "realized_pnl":       strategy_pnl,
            "win_rate":           (settled_w / settled_n * 100) if settled_n else 0.0,
            "settled_count":      settled_n,
        }

    print_strategy_header(
        strategy_name=strategy.name,
        bankroll=strategy_bankroll,
        max_per_trade=max_per_trade,
        notes=f"{now_str}  •  {strategy.notes}",
        mode="PAPER",
    )
    print()

    # ── Shared-data fast path (A/B mode) ────────────────────────────────────
    # In A/B mode the caller passes a mutable dict. The first strategy pass
    # fetches everything and saves it; subsequent passes skip the API calls.
    _have_shared = bool(shared_data and "kxmve_markets" in shared_data)
    if _have_shared:
        kxmve_markets     = shared_data["kxmve_markets"]
        scannable_markets = shared_data["scannable_markets"]
        events            = shared_data["events"]
        prop_cache        = shared_data["prop_cache"]
        totals_cache      = shared_data.get("totals_cache", {})
        scanner           = shared_data["scanner"]
        matched_leg_count = shared_data["matched_leg_count"]
        match_rate        = shared_data["match_rate"]
        print(_bar("DATA  (shared from first strategy pass — no extra API calls)"))
        print(f"    {len(kxmve_markets):,} KXMVE markets  |  "
              f"{len(scannable_markets):,} scannable  |  "
              f"{len(events):,} sportsbook events  |  "
              f"{len(prop_cache):,} players in prop cache  |  "
              f"{len(totals_cache):,} events with totals")
        print()

    # Kalshi client is needed in Step 4 (fetch_kxmve_book) regardless of path.
    try:
        kalshi = KalshiClient()
    except Exception as e:
        print(f"    ERROR: Kalshi auth failed — {e}")
        print("    Check KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH in config/secrets.env")
        return

    if not _have_shared:
        # ── Step 1: Fetch KXMVE markets from Kalshi ──────────────────────────
        print(_bar("STEP 1  Kalshi KXMVE sports markets"))
        t0 = time.perf_counter()
        try:
            raw_markets = kalshi.get_all_open_markets(None, KALSHI_KXMVE_MAX_PAGES)
            kxmve_raw     = [m for m in raw_markets
                             if m.get("ticker", "").upper().startswith("KXMVE")]
            all_kxmve     = [normalize_kalshi_market(m) for m in kxmve_raw]
            kxmve_markets = [km for km in all_kxmve
                             if (km.extra.get("yes_ask") or 0) > 0]
            elapsed = time.perf_counter() - t0
            print(f"    Fetched {len(all_kxmve):,} KXMVE markets from Kalshi  [{elapsed:.1f}s]")
            print(f"    Markets with active YES price: {len(kxmve_markets):,} "
                  f"({len(all_kxmve)-len(kxmve_markets):,} have no ask — unseeded/illiquid)")
        except Exception as e:
            print(f"    ERROR: Kalshi fetch failed — {e}")
            return

        if not kxmve_markets:
            print("    No KXMVE markets found. Kalshi may be in maintenance or all sports are off-season.")
            return
        print()

        # ── Step 2: Fetch Odds API sportsbook events ──────────────────────────
        print(_bar("STEP 2  Sportsbook odds  (The Odds API)"))
        t0 = time.perf_counter()
        if not ODDS_API_KEY:
            print("    ODDS_API_KEY not set in config/secrets.env — cannot run sports scan.")
            return

        try:
            scanner = OddsArbScanner(min_edge=strategy.min_net_edge)
            events  = scanner.fetch_events(ODDS_API_ACTIVE_SPORTS)
            elapsed = time.perf_counter() - t0
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

        # ── Step 3: Diagnostic — KXMVE parsing and sportsbook matching ──────
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
            team_legs   = [l for l in legs if l.leg_type in ("team_win", "team_spread")]
            player_legs = [l for l in legs if l.leg_type == "player_over"]
            if player_legs and not team_legs:
                player_only_count += 1
            team_leg_count += len(team_legs)
            for leg in team_legs:
                if team_variants.get(leg.subject):
                    matched_leg_count += 1
                else:
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

        # ── Step 3b: Fetch player prop odds for relevant events ───────────────
        print(_bar("STEP 3b  Player prop odds  (per-event fetch)"))
        from detection.kxmve_parser import KXMVEParser as _Parser
        from detection.kxmve_parser import build_team_variants as _build_tv
        _team_variants_for_props = _build_tv(events)
        events_to_fetch: list = []
        seen_event_ids: set   = set()
        for km in kxmve_markets:
            legs = _Parser.parse(km.title)
            has_player = any(l.leg_type == "player_over" for l in legs)
            if not has_player:
                continue
            for leg in legs:
                if leg.leg_type not in ("team_win", "team_spread"):
                    continue
                match = _team_variants_for_props.get(leg.subject)
                if not match:
                    continue
                _, ev = match
                eid = ev.get("id", "")
                sk  = ev.get("sport_key", "")
                if eid and eid not in seen_event_ids:
                    seen_event_ids.add(eid)
                    events_to_fetch.append((sk, eid))

        t0 = time.perf_counter()
        prop_cache = scanner.odds_client.build_player_prop_cache(
            events_to_fetch, max_events=15
        )
        elapsed = time.perf_counter() - t0
        print(f"    Events with player legs:  {len(events_to_fetch):,}")
        print(f"    Players in prop cache:    {len(prop_cache):,}  [{elapsed:.1f}s]")
        quota = scanner.odds_client.quota_remaining()
        if quota is not None:
            print(f"    Odds API quota remaining: {quota} requests")
        print()

        # ── Step 3c: Build totals cache for over/under legs ─────────────────
        # Identify which sports have at least one total_over KXMVE market
        totals_sports: set = set()
        for km in kxmve_markets:
            legs = _Parser.parse(km.title)
            if any(l.leg_type == "total_over" for l in legs):
                # Best-effort sport detection from title — fall back to all active sports
                for sport in ODDS_API_ACTIVE_SPORTS:
                    if sport in km.title.lower():
                        totals_sports.add(sport)

        totals_cache: dict = {}
        if totals_sports:
            t0 = time.perf_counter()
            totals_cache = scanner.odds_client.build_totals_cache(list(totals_sports))
            elapsed = time.perf_counter() - t0
            print(f"    Totals cache: {len(totals_cache):,} events with totals "
                  f"across {len(totals_sports)} sports  [{elapsed:.1f}s]")
        else:
            # Even if title detection failed, fetch totals for all active sports —
            # totals_over legs are common and worth ~7 API credits per scan.
            t0 = time.perf_counter()
            totals_cache = scanner.odds_client.build_totals_cache(ODDS_API_ACTIVE_SPORTS)
            elapsed = time.perf_counter() - t0
            print(f"    Totals cache (all sports): {len(totals_cache):,} events  [{elapsed:.1f}s]")

        # ── Step 3d: All markets are now scannable (totals previously skipped) ──
        scannable_markets = [km for km in kxmve_markets if _Parser.parse(km.title)]
        n_with_props  = sum(
            1 for km in scannable_markets
            if any(l.leg_type == "player_over" for l in _Parser.parse(km.title))
        )
        n_with_totals = sum(
            1 for km in scannable_markets
            if any(l.leg_type == "total_over" for l in _Parser.parse(km.title))
        )
        print(f"    Scannable markets:        {len(scannable_markets):,} / {len(kxmve_markets):,}")
        print(f"    Include player legs:      {n_with_props:,}")
        print(f"    Include total_over legs:  {n_with_totals:,}  (now priced via totals_cache)")
        print()

        # Save for subsequent strategy passes in A/B mode
        if shared_data is not None:
            shared_data.update({
                "kxmve_markets":     kxmve_markets,
                "events":            events,
                "prop_cache":        prop_cache,
                "totals_cache":      totals_cache,
                "scannable_markets": scannable_markets,
                "scanner":           scanner,
                "matched_leg_count": matched_leg_count,
                "match_rate":        match_rate,
            })

    # ── Step 4: Scan for mispricings (per-strategy min_books) ────────────────
    print(_bar(f"STEP 4  Scanning for mispriced Kalshi markets  (min_books={strategy.min_books})"))
    t0 = time.perf_counter()

    scanner.min_books = strategy.min_books
    scanner.min_edge  = strategy.min_net_edge

    # Lookup for close_time by ticker (used in trade cards)
    market_lookup = {km.market_id: km for km in scannable_markets}

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
            scannable_markets, fetch_kxmve_book, events,
            prop_cache=prop_cache,
            totals_cache=totals_cache,
        )
        elapsed = time.perf_counter() - t0
        print(f"    Scanned {len(scannable_markets):,} markets in {elapsed:.1f}s "
              f"-> {len(opportunities)} opportunities (min_edge={strategy.min_net_edge:.1%}, min_books=2)")
    except Exception as e:
        print(f"    ERROR: scan failed — {e}")
        return

    print()

    # ── Step 5: Display opportunities ─────────────────────────────────────────
    if not opportunities:
        print(_bar("RESULTS"))
        print(f"    No opportunities above {strategy.min_net_edge:.1%} net edge threshold.")
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
        print(f"    - Lower min_net_edge in current strategy ({strategy.name}) if you want more trades")
        print()

        # Near-miss scan: run with min_edge=0 to show best sub-threshold opportunities
        print(_bar("NEAR-MISS SCAN  (best opportunities found today)"))
        print()
        try:
            scanner.min_edge = 0.0
            scanner.min_books = 1
            near_misses_all: List[OddsArbOpportunity] = scanner.scan(
                scannable_markets, fetch_kxmve_book, events,
                prop_cache=prop_cache,
                totals_cache=totals_cache,
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
                    gap = (opp.net_edge - strategy.min_net_edge) * 100
                    print(
                        f"  {i:>2}  {opp.kalshi_ticker:<22}  {opp.kalshi_side.upper():<4}  "
                        f"{opp.kalshi_price:>5.3f}  {opp.fair_prob:>5.3f}  "
                        f"{opp.net_edge*100:>7.2f}%  {gap:>+8.2f}%  {opp.books_used}"
                    )
                avg_edge = sum(o.net_edge for o in near_misses_all) / len(near_misses_all)
                max_edge = near_misses_all[0].net_edge
                print()
                print(f"    Total matchable markets: {len(near_misses_all)}")
                print(f"    Best net edge today:     {max_edge*100:.2f}%  (need {strategy.min_net_edge*100:.1f}%)")
                print(f"    Average net edge:        {avg_edge*100:.2f}%")
                print()
                if max_edge >= strategy.min_net_edge * 0.5:
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
    kelly_pct   = int(effective_kelly * 100)
    kelly_label = (f"{kelly_pct}%-Kelly"
                   if effective_kelly == strategy.kelly_fraction
                   else f"{kelly_pct}%-Kelly (calibrated from {int(strategy.kelly_fraction*100)}%)")
    print(_bar(f"PAPER TRADES  ({kelly_label} sizing, ${strategy_bankroll:,.0f} bankroll)"))
    print()

    # ── Cross-session capital guard (per-strategy) ───────────────────────────
    # Use the per-strategy deployed amount computed at the top of this function
    # so v1 and v2 each manage their own cap independently.
    session_budget = max(0.0, strategy.max_total_deployed_usd - strategy_deployed)

    print(f"    Already deployed (open positions): ${strategy_deployed:,.2f}")
    print(f"    Hard cap (strategy {strategy.name}):       ${strategy.max_total_deployed_usd:,.2f}")
    print(f"    Budget available this session:      ${session_budget:,.2f}")
    print()

    if session_budget < 1.0:
        print("    Capital fully deployed — no new trades until open positions settle.")
        print(f"    Run resolve_trades.py to check for settlements.")
        tracker.close()
        return

    trades: List[PaperTrade] = []
    deployed  = 0.0
    remaining = min(strategy_bankroll, session_budget)

    # ── Strategy-level filter counters (for the report) ──────────────────────
    skipped = {"sport": 0, "legs": 0, "edge_too_high": 0, "side": 0, "books": 0, "dupe": 0}

    for i, opp in enumerate(opportunities, 1):
        # Stop when this session has hit its share of the global cap
        if deployed >= session_budget:
            print(f"    Session budget reached (${deployed:.2f} deployed this scan). Stopping.")
            break

        # ── Strategy filters ─────────────────────────────────────────────────
        if opp.sport in strategy.excluded_sports:
            skipped["sport"] += 1
            continue
        if (opp.legs_total or 0) > strategy.max_legs:
            skipped["legs"] += 1
            continue
        if (opp.net_edge * 100) > strategy.max_trusted_edge_pct:
            skipped["edge_too_high"] += 1
            continue
        if opp.kalshi_side not in strategy.allowed_sides:
            skipped["side"] += 1
            continue
        if (opp.books_used or 0) < strategy.min_books:
            skipped["books"] += 1
            continue
        if taken_tickers is not None and opp.kalshi_ticker in taken_tickers:
            skipped["dupe"] += 1
            continue

        # For YES trades win prob = fair_prob; for NO trades it's (1 - fair_prob)
        win_prob = opp.fair_prob if opp.kalshi_side == "yes" else (1.0 - opp.fair_prob)
        contracts, stake, kelly_frac = kelly_contracts(
            fair_prob=win_prob,
            kalshi_ask=opp.kalshi_price,
            bankroll=remaining,
            max_stake_usd=max_per_trade,
            kelly_fraction=effective_kelly,
            sport=opp.sport or "",
        )
        if contracts < 1:
            continue

        cost          = opp.kalshi_price * (1 + KALSHI_TAKER_FEE)
        actual_stake  = contracts * cost
        exp_profit    = contracts * opp.net_edge
        net_edge_pct  = opp.net_edge * 100

        km = market_lookup.get(opp.kalshi_ticker)
        trade = PaperTrade(
            id            = i,
            ticker        = opp.kalshi_ticker,
            title         = opp.kalshi_title[:60] if opp.kalshi_title else "",
            close_time    = km.close_time if km else "",
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
        if taken_tickers is not None:
            taken_tickers.add(opp.kalshi_ticker)

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
            "strategy_version": strategy.name,
        })

    # Print strategy-filter skip summary
    total_skipped = sum(skipped.values())
    if total_skipped:
        print(f"    Strategy filters dropped {total_skipped} opportunities  "
              f"(sport={skipped['sport']}, legs={skipped['legs']}, "
              f"edge>{strategy.max_trusted_edge_pct:g}%={skipped['edge_too_high']}, "
              f"side={skipped['side']}, books<{strategy.min_books}={skipped['books']}, "
              f"cross-strategy dupe={skipped['dupe']})")
        print()

    if not trades:
        print("    No trades passed Kelly sizing / strategy filters.")
        print("    (Possible causes: fair_prob too close to ask, or strategy filters too strict)")
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

    # ── Step 6: Trade detail cards ───────────────────────────────────────────
    print()
    print(_bar("TRADE DETAILS  (WHO / WHAT / WHEN / WHY)"))
    print()
    opp_by_ticker = {o.kalshi_ticker: o for o in opportunities}
    for trade in trades:
        opp = opp_by_ticker.get(trade.ticker)
        if opp:
            print_trade_card(trade, opp)

    # ── Step 7: Portfolio summary ─────────────────────────────────────────────
    total_expected   = sum(t.expected_profit for t in trades)
    total_deployed   = sum(t.stake for t in trades)
    total_contracts  = sum(t.contracts for t in trades)
    avg_edge         = sum(t.net_edge for t in trades) / len(trades)
    avg_books        = sum(t.books_used for t in trades) / len(trades)
    utilization      = total_deployed / strategy_bankroll * 100 if strategy_bankroll > 0 else 0
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
    print_portfolio_summary(
        strategy_name=strategy.name,
        bankroll=strategy_bankroll,
        total_deployed=total_deployed,
        total_expected=total_expected,
        total_contracts=total_contracts,
        n_trades=len(trades),
        avg_edge=avg_edge,
        avg_books=avg_books,
    )
    print()
    print(f"    Annualized (optimistic, 365d):     ${ann_profit_optimistic:>8,.0f}  "
          f"({ann_profit_optimistic/strategy_bankroll*100:,.0f}% ROI)")
    print(f"    Annualized (conservative, 250d):   ${ann_profit_conservative:>8,.0f}  "
          f"({ann_profit_conservative/strategy_bankroll*100:,.0f}% ROI)")
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
    print(f"    Session ID:        {session_id}")
    print(f"    Strategy version:  {strategy.name}  ({strategy.notes[:50]})")
    print()
    print("  " + "=" * 64)
    print()

    # Update the dashboard summary with this scan's actual trade data. The
    # bankroll/realized/win-rate fields were already seeded earlier so they
    # show even on early exit.
    if summaries is not None and strategy.name in summaries:
        summaries[strategy.name].update({
            "open_deployed":      strategy_deployed + total_deployed,
            "trades_this_scan":   len(trades),
            "deployed_this_scan": total_deployed,
            "expected_this_scan": total_expected,
        })

    tracker.close()


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard renderer (A/B mode)
# ─────────────────────────────────────────────────────────────────────────────

def _render_dashboard(strategies, summaries: dict, mode: str = "PAPER"):
    """Read global bankroll and emit the unified end-of-scan dashboard."""
    main_starting = STARTING_CAPITAL_USD
    main_current  = STARTING_CAPITAL_USD
    main_settled  = main_wins = main_losses = 0
    try:
        conn = sqlite3.connect(SQLITE_DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM bankroll WHERE id=1").fetchone()
        if row:
            r = dict(row)
            main_starting = r["starting_capital"]
            main_current  = r["current_capital"]
            main_settled  = r["total_trades"]
            main_wins     = r["wins"]
            main_losses   = r["losses"]
        conn.close()
    except sqlite3.OperationalError:
        pass

    scan_label = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    per_strategy = []
    for s in strategies:
        summary = summaries.get(s.name)
        if summary is None:
            per_strategy.append({
                "name":               s.name,
                "bankroll":           0.0,
                "open_deployed":      0.0,
                "trades_this_scan":   0,
                "deployed_this_scan": 0.0,
                "expected_this_scan": 0.0,
                "realized_pnl":       0.0,
                "win_rate":           0.0,
                "settled_count":      0,
            })
        else:
            per_strategy.append(summary)

    print_scan_dashboard(
        scan_label=scan_label,
        mode=mode,
        main_current=main_current,
        main_starting=main_starting,
        main_settled=main_settled,
        main_wins=main_wins,
        main_losses=main_losses,
        per_strategy=per_strategy,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Sports arb paper test — Kalshi KXMVE vs sportsbook consensus"
    )
    parser.add_argument("--capital",    type=float, default=STARTING_CAPITAL_USD,
                        help=f"Starting bankroll (default: ${STARTING_CAPITAL_USD:.0f})")
    parser.add_argument("--strategy",   type=str, default=None,
                        help="Strategy version to run (e.g. v1). "
                             "Overrides ACTIVE_STRATEGY in config/strategies.py.")
    parser.add_argument("--strategies", type=str, default=None,
                        help="A/B mode: comma-separated list, e.g. 'v1,v2'. "
                             "Each strategy gets the FULL --capital bankroll "
                             "(total exposure can be N * capital).")
    parser.add_argument("--verbose",    action="store_true",
                        help="Show DEBUG logs")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # ── A/B mode: run multiple strategies serially with split capital ────────
    if args.strategies:
        names = [n.strip() for n in args.strategies.split(",") if n.strip()]
        if len(names) < 2:
            print("  --strategies needs 2+ comma-separated names. "
                  "Use --strategy for single mode.")
            sys.exit(1)
        try:
            strategies = [get_strategy(n) for n in names]
        except KeyError as e:
            print(f"  {e}")
            sys.exit(1)

        # Each strategy gets the FULL bankroll (no split). The hard cap on
        # total capital deployed is enforced per-strategy by
        # strategy.max_total_deployed_usd, so runaway exposure is still bounded.
        per_strategy_capital = args.capital
        total_max_exposure = sum(s.max_total_deployed_usd for s in strategies)
        print(f"\n  [A/B MODE]  {len(strategies)} strategies, "
              f"${per_strategy_capital:,.2f} bankroll EACH "
              f"(combined max exposure: ${total_max_exposure:,.2f})\n")

        # shared_data: Kalshi + Odds API data fetched once, reused by all passes.
        # taken_tickers: tickers already traded this scan — prevents doubling up.
        # summaries: per-strategy results collected for the end-of-scan dashboard.
        shared_data   = {}
        taken_tickers = set()
        summaries: dict = {}
        for s in strategies:
            run_paper_test(
                capital       = per_strategy_capital,
                strategy      = s,
                verbose       = args.verbose,
                shared_data   = shared_data,
                taken_tickers = taken_tickers,
                summaries     = summaries,
            )

        # ── End-of-scan unified dashboard ────────────────────────────────────
        _render_dashboard(strategies, summaries, mode="PAPER")
        return

    # ── Single-strategy mode ────────────────────────────────────────────────
    try:
        strategy = get_strategy(args.strategy)   # None → ACTIVE_STRATEGY
    except KeyError as e:
        print(f"  {e}")
        sys.exit(1)

    run_paper_test(
        capital  = args.capital,
        strategy = strategy,
        verbose  = args.verbose,
    )


if __name__ == "__main__":
    main()
