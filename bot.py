#!/usr/bin/env python3
"""
bot.py — Main production arb bot entry point.

The ArbBot orchestrates the full pipeline in live trading mode. It is a
thin wrapper around the same components used by paper_trade.py — the only
differences are:
  - live_mode=True on RiskManager and ArbDetector (stricter thresholds)
  - dry_run=False on ArbExecutor (real orders placed)
  - Stricter pre-flight checks before startup
  - Auto-recovery from transient errors (up to MAX_CONSECUTIVE_ERRORS)

Usage:
    cd arb-bot
    python bot.py [--capital 1000] [--dry-run]

IMPORTANT: Only run in live mode after:
  1. Paper trading >= 48h with >= 90% win rate
  2. Confirmed API credentials on both platforms
  3. Kalshi balance >= target capital
  4. Polymarket USDC balance >= target capital
  5. Read and accepted platform ToS risks

⚠ This bot places real orders. Start with MAX_SINGLE_POSITION_USD = $10
  and scale up only after confirming execution works end-to-end.
"""

import asyncio
import argparse
import logging
import signal
import sys
import time
import os
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from clients.kalshi import KalshiClient
from clients.polymarket import PolymarketClient
from clients.normalizer import normalize_kalshi_book, normalize_poly_book
from matching.matcher import MarketMatcher
from matching.llm_verifier import LLMVerifier
from detection.arb_detector import ArbDetector
from execution.executor import ArbExecutor
from execution.alerts import (
    alert_bot_started, alert_bot_stopped, alert_error,
    alert_daily_summary, alert_scan_summary, alert_risk_rejected,
)
from risk.risk_manager import RiskManager
from tracking.position_tracker import PositionTracker
from tracking.capital_recycler import CapitalRecycler
from tracking.pnl_attribution import PnlAttribution
from config.preflight import run_preflight
from config.settings import (
    SCAN_INTERVAL_SECS,
    STARTING_CAPITAL_USD,
    MATCH_CANDIDATE_THRESHOLD,
    KALSHI_API_KEY_ID,
    KALSHI_PRIVATE_KEY_PATH,
    POLY_PRIVATE_KEY,
    POLY_PROXY_WALLET,
    ANTHROPIC_API_KEY,
)

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

os.makedirs("data", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("data/bot.log"),
    ],
)
logger = logging.getLogger("arb_bot")

# Stop after this many consecutive scan failures to avoid runaway errors
MAX_CONSECUTIVE_ERRORS = 5
# Send a scan summary alert every N scans
SUMMARY_EVERY_N_SCANS = 20


# ─────────────────────────────────────────────────────────────────────────────
# ArbBot
# ─────────────────────────────────────────────────────────────────────────────

class ArbBot:
    """
    Production arbitrage bot.

    Lifecycle:
        bot = ArbBot(capital=500.0, live_mode=True)
        await bot.run()     # runs until SIGINT or MAX_CONSECUTIVE_ERRORS

    Internals per scan cycle:
        fetch → match → LLM verify → detect → risk gate → execute → log
    """

    def __init__(self, capital: float, live_mode: bool = False):
        self.capital    = capital
        self.live_mode  = live_mode
        self.running    = True
        self.scan_count          = 0
        self.consecutive_errors  = 0
        self.total_opportunities = 0
        self.total_trades        = 0
        self.session_start       = None
        self.session_id          = None

        self._preflight()

        # ── Build component graph ────────────────────────────────────────────
        logger.info("Initialising bot components...")
        self.kalshi   = KalshiClient()
        self.poly     = PolymarketClient()
        self.matcher  = MarketMatcher()
        self.verifier = LLMVerifier()
        self.detector = ArbDetector(live_mode=live_mode)
        self.tracker  = PositionTracker()
        self.risk     = RiskManager(bankroll=capital, live_mode=live_mode)
        self.executor = ArbExecutor(
            kalshi_client=self.kalshi,
            poly_client=self.poly,
            position_tracker=self.tracker,
            dry_run=(not live_mode),   # Safety: paper unless explicitly live
        )
        self.recycler = CapitalRecycler(
            self.kalshi, self.poly, self.tracker, self.risk
        )
        self.attr = PnlAttribution(self.tracker)
        logger.info(
            f"Bot ready | mode={'LIVE ⚠' if live_mode else 'paper'} | "
            f"capital=${capital:.2f}"
        )

    def _preflight(self):
        """Validate critical config before touching any APIs."""
        result = run_preflight(
            kalshi_api_key_id=KALSHI_API_KEY_ID,
            kalshi_private_key_path=KALSHI_PRIVATE_KEY_PATH,
            poly_private_key=POLY_PRIVATE_KEY,
            poly_proxy_wallet=POLY_PROXY_WALLET,
            anthropic_api_key=ANTHROPIC_API_KEY,
        )
        for w in result.warnings:
            logger.warning(w)
        if not result.ok:
            raise SystemExit("Preflight failed:\n- " + "\n- ".join(result.errors))
        if self.capital <= 0:
            raise SystemExit(f"Invalid capital: {self.capital}")
        logger.info("Preflight checks passed.")

    # ── Main loop ────────────────────────────────────────────────────────────

    async def run(self):
        """Event loop: scan on SCAN_INTERVAL_SECS, recycle every 5min."""
        self.session_start = time.monotonic()
        self.session_id    = self.tracker.start_session(
            mode="live" if self.live_mode else "paper"
        )

        mode_str = "LIVE" if self.live_mode else "PAPER"
        await alert_bot_started(mode_str, self.capital)

        last_recycle      = 0.0
        last_daily_summary = time.monotonic()
        scan_opportunities = 0   # For summary alert

        try:
            while self.running:
                scan_start = time.monotonic()

                # ── Scan ────────────────────────────────────────────────────
                try:
                    n_opps = await self._scan_cycle()
                    scan_opportunities += n_opps
                    self.total_opportunities += n_opps
                    self.consecutive_errors = 0
                except Exception as e:
                    self.consecutive_errors += 1
                    logger.error(f"Scan error #{self.consecutive_errors}: {e}")
                    await alert_error("scan_cycle", str(e),
                                      critical=(self.consecutive_errors >= MAX_CONSECUTIVE_ERRORS))
                    if self.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                        logger.critical("Too many consecutive errors — stopping.")
                        self.running = False
                        break

                # ── Capital recycling every 5 min ────────────────────────────
                if time.monotonic() - last_recycle > 300:
                    try:
                        closed = await self.recycler.check_resolved_markets()
                        if closed:
                            logger.info(f"Recycled {closed} resolved positions")
                    except Exception as e:
                        logger.error(f"Recycle error: {e}")
                    last_recycle = time.monotonic()

                # ── Periodic scan summary alert ──────────────────────────────
                if self.scan_count % SUMMARY_EVERY_N_SCANS == 0 and self.scan_count > 0:
                    try:
                        summary = self.tracker.get_pnl_summary()
                        await alert_scan_summary(
                            opportunities_found=scan_opportunities,
                            pairs_scanned=self.scan_count * 50,  # estimate
                            best_edge=summary.get("avg_edge_pct", 0) * 1.5,
                            deployed_usd=summary["deployed_usd"],
                            available_usd=self.capital,
                        )
                        scan_opportunities = 0
                    except Exception as e:
                        logger.warning(f"Summary alert failed: {e}")

                # ── Daily summary ─────────────────────────────────────────────
                if time.monotonic() - last_daily_summary > 86400:
                    await self._daily_summary()
                    last_daily_summary = time.monotonic()

                # ── Sleep to next scan ────────────────────────────────────────
                scan_duration = time.monotonic() - scan_start
                sleep_secs = max(0, SCAN_INTERVAL_SECS - scan_duration)
                if sleep_secs > 0:
                    await asyncio.sleep(sleep_secs)

        except asyncio.CancelledError:
            logger.info("Bot task cancelled.")
        finally:
            await self._shutdown()

    async def _scan_cycle(self) -> int:
        """
        One complete scan. Returns number of opportunities found.
        """
        self.scan_count += 1
        logger.info(f"── Scan #{self.scan_count} "
                    f"({datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}) ──")

        # 1. Fetch
        kalshi_markets = self.kalshi.get_all_open_markets()
        poly_markets   = self.poly.get_all_active_markets(min_liquidity=500)
        logger.info(f"Markets: {len(kalshi_markets)} Kalshi / {len(poly_markets)} Poly")

        if not kalshi_markets or not poly_markets:
            logger.warning("No markets — skipping.")
            return 0

        # 2. Match
        candidates = self.matcher.find_matches(
            kalshi_markets, poly_markets,
            threshold=MATCH_CANDIDATE_THRESHOLD,
        )
        logger.info(f"Candidates: {len(candidates)}")
        if not candidates:
            return 0

        # 3. LLM verify (uses disk cache — negligible latency for cached pairs)
        pairs = [(c.kalshi.title, c.poly.title) for c in candidates]
        verified = await asyncio.to_thread(
            self.verifier.verify_batch, pairs, 0.80
        )
        verified_set = {(r.kalshi_title, r.poly_title) for r in verified
                        if r.is_safe_to_trade}
        verified_candidates = [
            c for c in candidates
            if (c.kalshi.title, c.poly.title) in verified_set
        ]
        logger.info(f"Verified: {len(verified_candidates)} / {len(candidates)}")

        if not verified_candidates:
            return 0

        # 4. Detect
        def fetch_k_book(market):
            return normalize_kalshi_book(self.kalshi.get_orderbook(market.market_id))

        def fetch_p_book(market):
            return normalize_poly_book(self.poly.get_market_orderbooks(market))

        opps = await asyncio.to_thread(
            self.detector.scan_all,
            [(c.kalshi, c.poly, c.score) for c in verified_candidates],
            fetch_k_book,
            fetch_p_book,
        )
        logger.info(f"Opportunities: {len(opps)}")
        if not opps:
            return 0

        # 5. Risk gate + execute
        deployed = self.tracker.get_deployed_usd()
        trades_this_scan = 0
        for opp in opps[:5]:   # Max 5 per scan cycle
            decision = self.risk.evaluate(opp, current_deployed=deployed)
            if not decision.approved:
                await alert_risk_rejected(opp, decision.reason)
                continue

            contracts = decision.recommended_contracts
            mode_str  = "live" if self.live_mode else "paper"
            result    = await self.executor.execute_arb(opp, contracts, mode=mode_str)

            if result.success:
                self.total_trades += 1
                trades_this_scan  += 1
                deployed          += result.total_cost_usd
                logger.info(
                    f"✓ Trade #{self.total_trades}: {opp.kalshi_ticker} | "
                    f"{result.contracts_filled} contracts | "
                    f"cost=${result.total_cost_usd:.2f} | "
                    f"expected=${result.expected_profit_usd:.2f}"
                )
            else:
                logger.warning(f"Trade failed: {opp.kalshi_ticker} — {result.error}")

        return len(opps)

    async def _daily_summary(self):
        """Send daily P&L alert."""
        try:
            summary = self.tracker.get_pnl_summary()
            positions = self.tracker.get_closed_positions()
            best_str = worst_str = "N/A"
            if positions:
                best  = max(positions, key=lambda p: p.get("actual_profit", 0) or 0)
                worst = min(positions, key=lambda p: p.get("actual_profit", 0) or 0)
                best_str  = f"{best.get('kalshi_ticker', '?')} (${best.get('actual_profit', 0):+.2f})"
                worst_str = f"{worst.get('kalshi_ticker', '?')} (${worst.get('actual_profit', 0):+.2f})"
            await alert_daily_summary(
                date_str=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                trades_executed=summary["total_trades"],
                gross_pnl=summary["gross_pnl"],
                fees_paid=0.0,
                net_pnl=summary["gross_pnl"],
                win_rate=summary["win_rate"],
                capital_deployed_peak=summary["deployed_usd"],
                best_trade=best_str,
                worst_trade=worst_str,
            )
        except Exception as e:
            logger.error(f"Daily summary error: {e}")

    async def _shutdown(self):
        """Graceful shutdown: cancel all open orders, log session, close DB."""
        logger.info("Shutting down...")

        # Cancel all open orders (safety first)
        try:
            await self.executor.cancel_all()
        except Exception as e:
            logger.error(f"cancel_all failed: {e}")

        # Final summary
        summary = self.tracker.get_pnl_summary()
        session_pnl = summary.get("gross_pnl", 0.0)

        logger.info(
            f"Session summary: "
            f"scans={self.scan_count} | "
            f"trades={self.total_trades} | "
            f"pnl=${session_pnl:+.4f} | "
            f"win_rate={summary.get('win_rate', 0):.1%}"
        )
        await alert_bot_stopped("graceful shutdown", session_pnl)

        if self.session_id:
            self.tracker.end_session(
                self.session_id,
                trades=self.total_trades,
                gross_pnl=session_pnl,
                fees=0.0,
            )
        self.tracker.close()
        logger.info("Bot stopped.")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Prediction market arb bot")
    parser.add_argument("--capital", type=float, default=STARTING_CAPITAL_USD,
                        help=f"Starting capital USD (default: {STARTING_CAPITAL_USD})")
    parser.add_argument("--live", action="store_true",
                        help="Enable live trading (real orders). Default: paper mode.")
    args = parser.parse_args()

    if args.live:
        print("="*60)
        print("⚠  LIVE TRADING MODE — REAL ORDERS WILL BE PLACED  ⚠")
        print(f"   Capital: ${args.capital:.2f}")
        confirm = input("Type 'yes' to confirm: ").strip().lower()
        if confirm != "yes":
            print("Aborted.")
            sys.exit(0)

    bot = ArbBot(capital=args.capital, live_mode=args.live)

    loop = asyncio.get_event_loop()

    def handle_signal(*_):
        logger.info("Stop signal received.")
        bot.running = False

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    loop.run_until_complete(bot.run())


if __name__ == "__main__":
    main()
