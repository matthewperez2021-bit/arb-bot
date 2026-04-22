#!/usr/bin/env python3
"""
paper_trade.py — Full paper trading loop for strategy validation.

Runs the complete detection → risk → execution pipeline in dry_run mode:
no real orders are placed, but all logic paths execute with live market data.

This is Phase 4 validation before going live. Run for at least 48–72 hours
and achieve >= 90% simulated win rate before switching to live mode.

Usage:
    cd arb-bot
    python scripts/paper_trade.py [--hours 24] [--capital 1000]

Output:
    - Console logs (INFO level)
    - Telegram alerts (if configured)
    - SQLite DB at data/arb_positions.db with mode='paper'
    - Summary report printed at end / on Ctrl+C

Environment:
    Copy config/secrets.env.example to config/secrets.env and fill in:
    - KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH (required)
    - POLY_PRIVATE_KEY, POLY_PROXY_WALLET (required)
    - ANTHROPIC_API_KEY (optional — uses cached matches without it)
    - TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (optional)
"""

import asyncio
import argparse
import logging
import signal
import sys
import time
from datetime import datetime, timezone

# Ensure the arb-bot package root is on the path when run from scripts/
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from clients.kalshi import KalshiClient
from clients.polymarket import PolymarketClient
from clients.normalizer import normalize_kalshi_book, normalize_poly_book
from matching.matcher import MarketMatcher
from matching.llm_verifier import LLMVerifier
from detection.arb_detector import ArbDetector
from execution.executor import ArbExecutor
from execution.alerts import alert_bot_started, alert_bot_stopped, alert_daily_summary
from risk.risk_manager import RiskManager
from tracking.position_tracker import PositionTracker
from tracking.capital_recycler import CapitalRecycler
from tracking.pnl_attribution import PnlAttribution
from config.settings import (
    SCAN_INTERVAL_SECS,
    STARTING_CAPITAL_USD,
    MAX_DURATION_DAYS,
    MATCH_CANDIDATE_THRESHOLD,
)

# ─────────────────────────────────────────────────────────────────────────────
# Logging setup
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("data/paper_trade.log"),
    ],
)
logger = logging.getLogger("paper_trade")


# ─────────────────────────────────────────────────────────────────────────────
# Main paper trading loop
# ─────────────────────────────────────────────────────────────────────────────

class PaperTrader:
    """
    Runs the full arb pipeline in paper mode (dry_run=True).

    Architecture:
      ┌──────────────────────────────────────────────────────────┐
      │ Every SCAN_INTERVAL_SECS seconds:                        │
      │   1. Fetch all open Kalshi markets                       │
      │   2. Fetch all active Poly markets                       │
      │   3. Run MarketMatcher → candidate pairs                 │
      │   4. LLMVerifier filters to confirmed pairs              │
      │   5. ArbDetector scans for pricing inefficiencies        │
      │   6. RiskManager gates + sizes each opportunity          │
      │   7. ArbExecutor.execute_arb(dry_run=True) simulates fill│
      │   8. PositionTracker logs to SQLite                      │
      │                                                          │
      │ Every 5 minutes:                                         │
      │   CapitalRecycler.check_resolved_markets()               │
      └──────────────────────────────────────────────────────────┘
    """

    def __init__(self, capital: float, max_hours: float):
        self.capital   = capital
        self.max_hours = max_hours
        self.running   = True
        self.scan_count       = 0
        self.opportunities_found = 0
        self.trades_simulated = 0
        self.session_id       = None

        # ── Components ──────────────────────────────────────────────────────
        logger.info("Initialising components...")
        self.kalshi   = KalshiClient()
        self.poly     = PolymarketClient()
        self.matcher  = MarketMatcher()
        self.verifier = LLMVerifier()
        self.detector = ArbDetector(live_mode=False)
        self.tracker  = PositionTracker()
        self.risk     = RiskManager(bankroll=capital, live_mode=False)
        self.executor = ArbExecutor(
            kalshi_client=self.kalshi,
            poly_client=self.poly,
            position_tracker=self.tracker,
            dry_run=True,
        )
        self.recycler = CapitalRecycler(self.kalshi, self.poly,
                                         self.tracker, self.risk)
        self.attr     = PnlAttribution(self.tracker)

    async def run(self):
        """Main event loop."""
        self.session_id = self.tracker.start_session(mode="paper")
        start_time = time.monotonic()
        last_recycle = 0.0
        last_daily_summary = time.monotonic()

        await alert_bot_started("paper", self.capital)
        logger.info(
            f"Paper trading started | capital=${self.capital:.2f} | "
            f"max_hours={self.max_hours:.1f}"
        )

        try:
            while self.running:
                # ── Time limit check ────────────────────────────────────────
                elapsed_hours = (time.monotonic() - start_time) / 3600
                if elapsed_hours >= self.max_hours:
                    logger.info(f"Time limit reached ({self.max_hours}h). Stopping.")
                    break

                # ── Main scan ───────────────────────────────────────────────
                scan_start = time.monotonic()
                await self._scan_cycle()
                scan_duration = time.monotonic() - scan_start
                self.scan_count += 1

                # ── Capital recycling every 5 minutes ───────────────────────
                if time.monotonic() - last_recycle > 300:
                    closed = await self.recycler.check_resolved_markets()
                    if closed > 0:
                        logger.info(f"Recycled capital from {closed} resolved markets")
                    last_recycle = time.monotonic()

                # ── Daily summary every 24 hours ────────────────────────────
                if time.monotonic() - last_daily_summary > 86400:
                    await self._send_daily_summary()
                    last_daily_summary = time.monotonic()

                # ── Respect scan interval ────────────────────────────────────
                sleep_secs = max(0, SCAN_INTERVAL_SECS - scan_duration)
                if sleep_secs > 0:
                    logger.debug(f"Scan done in {scan_duration:.1f}s. "
                                 f"Sleeping {sleep_secs:.0f}s...")
                    await asyncio.sleep(sleep_secs)

        except asyncio.CancelledError:
            logger.info("Bot cancelled.")
        except Exception as e:
            logger.exception(f"Unexpected error in main loop: {e}")
        finally:
            await self._shutdown()

    async def _scan_cycle(self):
        """One full scan: fetch → match → detect → risk → execute."""
        logger.info(f"─── Scan #{self.scan_count + 1} ───")

        # 1. Fetch markets
        try:
            kalshi_markets = self.kalshi.get_all_open_markets()
            poly_markets   = self.poly.get_all_active_markets(min_liquidity=500)
            logger.info(f"Fetched {len(kalshi_markets)} Kalshi / "
                        f"{len(poly_markets)} Poly markets")
        except Exception as e:
            logger.error(f"Market fetch failed: {e}")
            return

        if not kalshi_markets or not poly_markets:
            logger.warning("No markets available — skipping scan.")
            return

        # 2. Match markets
        try:
            candidates = self.matcher.find_matches(
                kalshi_markets, poly_markets,
                threshold=MATCH_CANDIDATE_THRESHOLD,
            )
            logger.info(f"Matched {len(candidates)} candidate pairs")
        except Exception as e:
            logger.error(f"Matching failed: {e}")
            return

        if not candidates:
            return

        # 3. LLM verify (uses cache — no API calls for cached pairs)
        try:
            pairs_to_verify = [
                (c.kalshi.title, c.poly.title) for c in candidates
            ]
            verified_pairs = await asyncio.to_thread(
                self.verifier.verify_batch,
                pairs_to_verify,
                min_confidence=0.80,
            )
            # Map back to candidates
            verified_titles = {
                (r.kalshi_title, r.poly_title)
                for r in verified_pairs
                if r.is_safe_to_trade
            }
            verified_candidates = [
                c for c in candidates
                if (c.kalshi.title, c.poly.title) in verified_titles
            ]
            logger.info(f"LLM verified {len(verified_candidates)} / {len(candidates)} pairs")
        except Exception as e:
            logger.warning(f"LLM verification failed: {e} — using unverified candidates")
            verified_candidates = candidates

        if not verified_candidates:
            return

        # 4. Detect arb opportunities
        try:
            def fetch_k_book(market):
                raw = self.kalshi.get_orderbook(market.market_id)
                return normalize_kalshi_book(raw)

            def fetch_p_book(market):
                raw = self.poly.get_market_orderbooks(market)
                return normalize_poly_book(raw)

            opportunities = await asyncio.to_thread(
                self.detector.scan_all,
                [(c.kalshi, c.poly, c.score) for c in verified_candidates],
                fetch_k_book,
                fetch_p_book,
            )
            self.opportunities_found += len(opportunities)
            if opportunities:
                best = opportunities[0]
                logger.info(
                    f"Found {len(opportunities)} opportunities. "
                    f"Best: {best.kalshi_ticker} | "
                    f"edge={best.net_profit_pct:.2%} | "
                    f"EPD={best.edge_per_day:.1f}%"
                )
        except Exception as e:
            logger.error(f"Arb detection failed: {e}")
            return

        if not opportunities:
            logger.info("No arb opportunities this scan.")
            return

        # 5. Risk gate + execute top opportunities
        deployed = self.tracker.get_deployed_usd()
        for opp in opportunities[:3]:   # Max 3 trades per scan
            try:
                decision = self.risk.evaluate(opp, current_deployed=deployed)
                if not decision.approved:
                    logger.info(f"Risk rejected {opp.kalshi_ticker}: {decision.reason}")
                    continue

                contracts = decision.recommended_contracts
                logger.info(
                    f"Executing (paper): {opp.kalshi_ticker} | "
                    f"{contracts} contracts | edge={opp.net_profit_pct:.2%}"
                )
                result = await self.executor.execute_arb(opp, contracts, mode="paper")
                if result.success:
                    self.trades_simulated += 1
                    deployed += result.total_cost_usd
                    logger.info(
                        f"✓ Simulated: {result.contracts_filled} contracts @ "
                        f"${result.total_cost_usd:.2f} | "
                        f"expected profit ${result.expected_profit_usd:.2f}"
                    )
                else:
                    logger.warning(f"Simulation failed: {result.error}")

            except Exception as e:
                logger.error(f"Execution error for {opp.kalshi_ticker}: {e}")

    async def _send_daily_summary(self):
        """Send Telegram daily P&L summary."""
        try:
            summary = self.tracker.get_pnl_summary(mode="paper")
            positions = self.tracker.get_closed_positions(mode="paper")
            if positions:
                best  = max(positions, key=lambda p: p.get("actual_profit", 0) or 0)
                worst = min(positions, key=lambda p: p.get("actual_profit", 0) or 0)
                best_str  = f"{best.get('kalshi_ticker', '?')} (${best.get('actual_profit', 0):+.2f})"
                worst_str = f"{worst.get('kalshi_ticker', '?')} (${worst.get('actual_profit', 0):+.2f})"
            else:
                best_str = worst_str = "N/A"

            await alert_daily_summary(
                date_str=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                trades_executed=summary["total_trades"],
                gross_pnl=summary["gross_pnl"],
                fees_paid=0.0,   # Paper mode — fees are netted into expected_profit
                net_pnl=summary["gross_pnl"],
                win_rate=summary["win_rate"],
                capital_deployed_peak=summary["deployed_usd"],
                best_trade=best_str,
                worst_trade=worst_str,
            )
        except Exception as e:
            logger.error(f"Daily summary failed: {e}")

    async def _shutdown(self):
        """Clean shutdown: log final summary and close DB."""
        logger.info("Shutting down paper trader...")
        summary = self.tracker.get_pnl_summary(mode="paper")

        print("\n" + "═"*60)
        print(f"PAPER TRADING SUMMARY")
        print("═"*60)
        print(f"Scans completed:     {self.scan_count}")
        print(f"Opportunities found: {self.opportunities_found}")
        print(f"Trades simulated:    {self.trades_simulated}")
        print(f"Closed positions:    {summary['total_trades']}")
        print(f"Win rate:            {summary['win_rate']:.1%}")
        print(f"Gross P&L:           ${summary['gross_pnl']:+.4f}")
        print(f"Avg edge:            {summary['avg_edge_pct']:.2%}")
        print(f"Avg EPD:             {summary['avg_edge_per_day']:.1f}%")
        print(f"Open positions:      {summary['open_positions']}")
        print(f"Deployed:            ${summary['deployed_usd']:.2f}")
        print("═"*60)

        # Risk metrics (need at least 2 closed trades)
        if summary["total_trades"] >= 2:
            report = self.attr.generate_pnl_report(mode="paper")
            risk = report.get("risk_metrics", {})
            print(f"Sharpe ratio:        {risk.get('sharpe_ratio', 'N/A')}")
            print(f"Max drawdown:        ${risk.get('max_drawdown', 0):.4f}")
            print(f"Calmar ratio:        {risk.get('calmar_ratio', 'N/A')}")
            print("═"*60)

        await alert_bot_stopped("graceful shutdown", summary["gross_pnl"])

        if self.session_id:
            self.tracker.end_session(
                self.session_id,
                trades=self.trades_simulated,
                gross_pnl=summary["gross_pnl"],
                fees=0.0,
            )
        self.tracker.close()
        logger.info("Paper trader shut down cleanly.")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Arb bot paper trading mode")
    parser.add_argument("--hours",   type=float, default=24.0,
                        help="How long to run (default: 24 hours)")
    parser.add_argument("--capital", type=float, default=STARTING_CAPITAL_USD,
                        help=f"Starting capital in USD (default: {STARTING_CAPITAL_USD})")
    args = parser.parse_args()

    # Ensure data dir exists
    os.makedirs("data", exist_ok=True)

    trader = PaperTrader(capital=args.capital, max_hours=args.hours)

    # Graceful Ctrl+C handling
    loop = asyncio.get_event_loop()

    def handle_sigint(*_):
        logger.info("Received SIGINT — stopping after current scan...")
        trader.running = False

    signal.signal(signal.SIGINT, handle_sigint)
    signal.signal(signal.SIGTERM, handle_sigint)

    loop.run_until_complete(trader.run())


if __name__ == "__main__":
    main()
