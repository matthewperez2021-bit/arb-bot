#!/usr/bin/env python3
"""
paper_trade.py — Full paper trading loop (Kalshi <-> PredictIt).

Runs the complete detection -> risk -> execution pipeline in dry_run mode:
no real orders are placed, but all logic paths execute with live market data.

Second platform: PredictIt (US-legal, CFTC no-action letter, political markets).
PredictIt has no order-placement API — in live mode the PI leg must be placed
manually via the website. In paper mode both legs are simulated.

Phase gate: run for at least 48-72 hours and achieve >= 90% simulated win rate
before switching to live mode.

Usage:
    cd arb-bot
    python scripts/paper_trade.py [--hours 24] [--capital 1000]

Output:
    - Console logs (INFO level)
    - data/paper_trade.log
    - SQLite at data/arb_positions.db (mode='paper')
    - Summary printed at end / on Ctrl+C
"""

import asyncio
import argparse
import logging
import signal
import sys
import time
import os
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from clients.kalshi import KalshiClient
from clients.predictit import PredictItClient
from clients.normalizer import normalize_kalshi_book, normalize_kalshi_market, NormalizedMarket
from matching.matcher import MarketMatcher
from matching.llm_verifier import LLMVerifier
from detection.arb_detector import ArbDetector
from detection.odds_arb_scanner import OddsArbScanner
from detection.economic_arb_scanner import EconomicArbScanner
from execution.executor import ArbExecutor
from execution.alerts import alert_bot_started, alert_bot_stopped, alert_daily_summary
from risk.risk_manager import RiskManager
from tracking.position_tracker import PositionTracker
from tracking.pnl_attribution import PnlAttribution
from config.preflight import run_preflight
from config.settings import (
    SCAN_INTERVAL_SECS,
    STARTING_CAPITAL_USD,
    MATCH_CANDIDATE_THRESHOLD,
    KALSHI_API_KEY_ID,
    KALSHI_PRIVATE_KEY_PATH,
    ANTHROPIC_API_KEY,
    ODDS_API_KEY,
    KALSHI_ECONOMIC_SERIES,
    KALSHI_SERIES_MAX_PAGES,
    KALSHI_GENERAL_MAX_PAGES,
    KALSHI_KXMVE_MAX_PAGES,
    ODDS_API_ACTIVE_SPORTS,
    ODDS_API_REFRESH_SECS,
)

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

os.makedirs("data", exist_ok=True)
# Force UTF-8 on the console handler so Unicode in log messages (arrows,
# em-dashes, etc.) never crash the Windows cp1252 StreamHandler.
_console = logging.StreamHandler(sys.stdout)
_console.stream = open(sys.stdout.fileno(), mode="w", encoding="utf-8", buffering=1, closefd=False)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        _console,
        logging.FileHandler("data/paper_trade.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("paper_trade")


# ─────────────────────────────────────────────────────────────────────────────
# PredictIt normalizer helpers
# ─────────────────────────────────────────────────────────────────────────────

def _predictit_to_normalized_markets(pi_client: PredictItClient) -> list:
    """
    Fetch all PredictIt binary markets and convert to NormalizedMarket list.

    Only binary (single-contract) markets are included — these are the only
    ones directly comparable to Kalshi's YES/NO contracts.
    """
    from clients.normalizer import NormalizedMarket
    result = []
    for mkt, contract in pi_client.get_binary_markets():
        nm = pi_client.to_normalized_market(mkt, contract)
        result.append(nm)
    logger.debug("PredictIt: %d binary markets normalised", len(result))
    return result


def _fetch_predictit_book(pi_client: PredictItClient, market: NormalizedMarket):
    """
    Fetch and return a NormalizedMarketBook for one PredictIt binary market.

    market.market_id format: "PI-{market_id}-{contract_id}"
    """
    parts = market.market_id.split("-")
    if len(parts) < 3:
        return None
    try:
        market_id   = int(parts[1])
        contract_id = int(parts[2])
    except (ValueError, IndexError):
        return None

    return pi_client.get_normalized_book(market_id, contract_id)


# ─────────────────────────────────────────────────────────────────────────────
# Paper trader
# ─────────────────────────────────────────────────────────────────────────────

class PaperTrader:
    """
    Runs the full arb pipeline in paper mode (dry_run=True).

    Pipeline per scan cycle:
      1. Fetch all open Kalshi markets
      2. Fetch all PredictIt binary markets
      3. MarketMatcher  -> candidate pairs (text + date similarity)
      4. LLMVerifier    -> confirm same real-world event
      5. ArbDetector    -> pricing inefficiencies (with PredictIt fee model)
      6. RiskManager    -> gate + size each opportunity
      7. ArbExecutor    -> simulate fill (dry_run=True)
      8. PositionTracker -> log to SQLite
    """

    def __init__(self, capital: float, max_hours: float):
        self.capital      = capital
        self.max_hours    = max_hours
        self.running      = True
        self.scan_count            = 0
        self.opportunities_found   = 0
        self.trades_simulated      = 0
        self.session_id            = None

        # ── Preflight ────────────────────────────────────────────────────────
        pre = run_preflight(
            kalshi_api_key_id=KALSHI_API_KEY_ID,
            kalshi_private_key_path=KALSHI_PRIVATE_KEY_PATH,
            predictit_enabled=True,
            odds_api_key=ODDS_API_KEY,
            anthropic_api_key=ANTHROPIC_API_KEY,
        )
        for w in pre.warnings:
            logger.warning(w)
        if not pre.ok:
            raise SystemExit("Preflight failed:\n- " + "\n- ".join(pre.errors))

        # ── Components ───────────────────────────────────────────────────────
        logger.info("Initialising components (Kalshi <-> PredictIt + Odds API)...")
        self.kalshi   = KalshiClient()
        self.pi       = PredictItClient()
        self.matcher  = MarketMatcher()
        self.verifier = LLMVerifier()
        # PredictIt fee model + higher minimum edge threshold
        self.detector = ArbDetector(live_mode=False, second_platform="predictit")
        # Odds API sports arb scanner (kept for reference but KXMVE is illiquid)
        self.odds_scanner = OddsArbScanner() if ODDS_API_KEY else None
        self._odds_events_cache: list = []      # cached sportsbook events
        self._odds_cache_time:   float = 0.0    # when cache was last populated
        # Economic arb scanner (Strategy B — KXBTC/KXFED vs Deribit/CME)
        self.econ_scanner = EconomicArbScanner()
        self._last_kalshi_normalized: list = []  # shared between strategies
        self.tracker  = PositionTracker()
        self.risk     = RiskManager(bankroll=capital, live_mode=False)
        # Pass a dummy second client — executor only runs in dry_run mode here
        self.executor = ArbExecutor(
            kalshi_client=self.kalshi,
            poly_client=None,       # not needed in dry_run mode
            position_tracker=self.tracker,
            dry_run=True,
        )
        self.attr = PnlAttribution(self.tracker)

        logger.info(
            "Paper trader ready | capital=$%.2f | max=%.1fh | "
            "PI min_edge=%.1f%% | Odds API sports=%s | odds_refresh=%dmin",
            capital, max_hours,
            self.detector.min_edge * 100,
            ",".join(ODDS_API_ACTIVE_SPORTS) if ODDS_API_KEY else "DISABLED",
            ODDS_API_REFRESH_SECS // 60,
        )

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def run(self):
        self.session_id = self.tracker.start_session(mode="paper")
        start_time      = time.monotonic()
        last_daily      = time.monotonic()

        await alert_bot_started("paper (Kalshi/PredictIt)", self.capital)
        logger.info("Paper trading started.")

        try:
            while self.running:
                elapsed_hours = (time.monotonic() - start_time) / 3600
                if elapsed_hours >= self.max_hours:
                    logger.info("Time limit reached (%.1fh). Stopping.", self.max_hours)
                    break

                scan_start = time.monotonic()
                await self._scan_cycle()
                scan_duration = time.monotonic() - scan_start
                self.scan_count += 1

                if time.monotonic() - last_daily > 86400:
                    await self._send_daily_summary()
                    last_daily = time.monotonic()

                sleep_secs = max(0, SCAN_INTERVAL_SECS - scan_duration)
                if sleep_secs > 0:
                    logger.debug("Scan done in %.1fs. Sleeping %.0fs...",
                                 scan_duration, sleep_secs)
                    await asyncio.sleep(sleep_secs)

        except asyncio.CancelledError:
            logger.info("Bot cancelled.")
        except Exception as e:
            logger.exception("Unexpected error in main loop: %s", e)
        finally:
            await self._shutdown()

    # ── Scan cycle ────────────────────────────────────────────────────────────

    async def _scan_cycle(self):
        """Run both strategies independently each tick."""
        logger.info("--- Scan #%d (%s) ---",
                    self.scan_count + 1,
                    datetime.now(timezone.utc).strftime("%H:%M:%S UTC"))
        await self._strategy_a_predictit()
        await self._strategy_b_economic()

    async def _strategy_a_predictit(self):
        """Strategy A: Kalshi economic/political <-> PredictIt binary markets."""
        # 1. Fetch markets from both platforms.
        #
        # Kalshi fetch strategy — two passes, deduplicated:
        #
        #  Pass A – Economic series (series_ticker=):
        #    Directly targets KXCPI / KXFED / KXGDP / KXBTC.
        #    These are always available and return quickly.
        #    The simple paginated endpoint is useless here because Kalshi's
        #    4 000+ KXMVE sports markets fill every page before any
        #    economic/political market appears; ticker_prefix_exclude only
        #    filters results, not pagination, so we'd always get 0.
        #
        #  Pass B – General scan (1 page, no filter):
        #    Picks up any political / governance markets once Kalshi
        #    lists them for the 2026 midterm cycle.  We skip KXMVE
        #    tickers here — they will never match PredictIt's content.
        try:
            seen_tickers: set  = set()
            kalshi_raw:   list = []

            # Pass A: targeted economic series fetches (run concurrently)
            async def _fetch_series(series: str) -> list:
                try:
                    return await asyncio.to_thread(
                        self.kalshi.get_all_open_markets,
                        series,
                        KALSHI_SERIES_MAX_PAGES,
                    )
                except Exception as exc:
                    logger.warning("Kalshi series %s fetch failed: %s", series, exc)
                    return []

            series_results = await asyncio.gather(
                *[_fetch_series(s) for s in KALSHI_ECONOMIC_SERIES]
            )
            for batch in series_results:
                for m in batch:
                    t = m.get("ticker", "")
                    if t and t not in seen_tickers:
                        seen_tickers.add(t)
                        kalshi_raw.append(m)

            # Pass B: general 1-page scan for political markets (future-proofing)
            try:
                general = await asyncio.to_thread(
                    self.kalshi.get_all_open_markets,
                    None,
                    KALSHI_GENERAL_MAX_PAGES,
                )
                for m in general:
                    t = m.get("ticker", "")
                    if t and not t.upper().startswith("KXMVE") and t not in seen_tickers:
                        seen_tickers.add(t)
                        kalshi_raw.append(m)
            except Exception as exc:
                logger.warning("Kalshi general fetch failed: %s", exc)

            pi_normalized = await asyncio.to_thread(_predictit_to_normalized_markets, self.pi)
            logger.info(
                "Markets: %d Kalshi (econ series: %s) / %d PredictIt binary",
                len(kalshi_raw),
                ", ".join(KALSHI_ECONOMIC_SERIES),
                len(pi_normalized),
            )
        except Exception as e:
            logger.error("Market fetch failed: %s", e)
            return

        if not kalshi_raw or not pi_normalized:
            logger.warning("No markets on one or both platforms — skipping.")
            return

        # Normalize Kalshi markets and share with Strategy B
        kalshi_normalized = [normalize_kalshi_market(m) for m in kalshi_raw]
        self._last_kalshi_normalized = kalshi_normalized

        # 2. Match markets
        try:
            candidates = self.matcher.find_matches(
                kalshi_normalized, pi_normalized,
                threshold=MATCH_CANDIDATE_THRESHOLD,
            )
            logger.info("Matched %d candidate pairs (Kalshi x PredictIt)", len(candidates))
        except Exception as e:
            logger.error("Matching failed: %s", e)
            return

        if not candidates:
            logger.info(
                "No candidate pairs above threshold this scan. "
                "(Kalshi economic series <-> PredictIt political -- "
                "overlap expected once Kalshi lists 2026 midterm markets.)"
            )
            return

        # 3. LLM verification (Claude confirms same real-world event)
        try:
            pairs_to_verify = [(c.kalshi.title, c.poly.title) for c in candidates]
            verified_pairs  = await asyncio.to_thread(
                self.verifier.verify_batch,
                pairs_to_verify,
                min_confidence=0.80,
            )
            verified_titles = {
                (r.kalshi_title, r.poly_title)
                for r, _ in [(p, vr) for p, vr in
                             [(pair, vr) for pair, vr in verified_pairs]]
            }
            # verify_batch returns list of ((k_title, p_title), VerificationResult)
            verified_title_set = {pair for pair, vr in verified_pairs}
            verified_candidates = [
                c for c in candidates
                if (c.kalshi.title, c.poly.title) in verified_title_set
            ]
            logger.info("LLM verified %d / %d pairs",
                        len(verified_candidates), len(candidates))
        except Exception as e:
            logger.warning("LLM verification failed: %s — using unverified candidates", e)
            verified_candidates = candidates

        if not verified_candidates:
            logger.info("No pairs survived LLM verification this scan.")
            return

        # 4. Fetch order books and detect arb
        try:
            def fetch_k_book(market):
                raw = self.kalshi.get_orderbook(market.market_id)
                return normalize_kalshi_book(raw)

            def fetch_pi_book(market):
                return _fetch_predictit_book(self.pi, market)

            opportunities = await asyncio.to_thread(
                self.detector.scan_all,
                verified_candidates,
                fetch_k_book,
                fetch_pi_book,
            )
            self.opportunities_found += len(opportunities)
            if opportunities:
                best = opportunities[0]
                logger.info(
                    "Found %d opportunities. Best: %s | edge=%.2f%% | "
                    "K-%s@%.3f + PI-%s@%.3f | %dd to close",
                    len(opportunities),
                    best.kalshi_ticker,
                    best.net_profit_pct * 100,
                    best.kalshi_side, best.kalshi_price,
                    best.poly_side, best.poly_price,
                    best.days_to_resolution,
                )
        except Exception as e:
            logger.error("Arb detection failed: %s", e)
            return

        if not opportunities:
            logger.info("No arb opportunities this scan (Strategy A).")

        # ── Strategy A execution (Kalshi <-> PredictIt) ───────────────────────
        deployed = self.tracker.get_deployed_usd()
        for opp in opportunities[:3]:
            try:
                decision = self.risk.evaluate(opp, current_deployed=deployed)
                if not decision.approved:
                    logger.info("Risk rejected %s: %s", opp.kalshi_ticker, decision.reason)
                    continue

                contracts = decision.recommended_contracts
                logger.info(
                    "Simulating trade: %s | %d contracts | edge=%.2f%% | "
                    "max_profit=$%.2f",
                    opp.kalshi_ticker, contracts,
                    opp.net_profit_pct * 100, opp.max_profit_usd,
                )
                result = await self.executor.execute_arb(opp, contracts, mode="paper")
                if result.success:
                    self.trades_simulated += 1
                    deployed += result.total_cost_usd
                    logger.info(
                        "Simulated fill: %d contracts @ $%.2f | "
                        "expected profit $%.4f",
                        result.contracts_filled,
                        result.total_cost_usd,
                        result.expected_profit_usd,
                    )
                else:
                    logger.warning("Simulation failed: %s", result.error)
            except Exception as e:
                logger.error("Execution error for %s: %s", opp.kalshi_ticker, e)

    async def _strategy_b_odds_api(self):
        """Strategy B: Kalshi KXMVE sports vs Odds API sportsbook consensus."""
        if not self.odds_scanner:
            return  # Odds API key not configured

        # B1. Fetch KXMVE sports markets (Kalshi game/match/prop contracts).
        #
        # "KXMVE" is a ticker PREFIX, not a series_ticker — passing it as
        # series_ticker returns 0 results.  Instead we use a plain paginated
        # fetch (no series filter): Kalshi returns KXMVE sports markets first
        # by default, so 1 page = ~200 game/match/prop contracts.
        try:
            kxmve_raw = await asyncio.to_thread(
                self.kalshi.get_all_open_markets,
                None,                  # no series filter — gets KXMVE naturally
                KALSHI_KXMVE_MAX_PAGES,
            )
            # Keep only KXMVE-prefixed markets for sports arb
            kxmve_raw = [m for m in kxmve_raw
                         if m.get("ticker", "").upper().startswith("KXMVE")]
            kxmve_markets = [normalize_kalshi_market(m) for m in kxmve_raw]
            logger.info("Strategy B: %d KXMVE sports markets", len(kxmve_markets))
        except Exception as e:
            logger.error("KXMVE fetch failed: %s", e)
            return

        if not kxmve_markets:
            return

        # B2. Refresh Odds API cache (rate-limited to ODDS_API_REFRESH_SECS)
        now = time.monotonic()
        cache_age = now - self._odds_cache_time
        if cache_age >= ODDS_API_REFRESH_SECS or not self._odds_events_cache:
            logger.info(
                "Refreshing Odds API cache (age=%.0fm, sports=%s)...",
                cache_age / 60, ", ".join(ODDS_API_ACTIVE_SPORTS),
            )
            try:
                self._odds_events_cache = await asyncio.to_thread(
                    self.odds_scanner.fetch_events,
                    ODDS_API_ACTIVE_SPORTS,
                )
                self._odds_cache_time = time.monotonic()
                logger.info(
                    "Odds API cache refreshed: %d events | quota remaining: %s",
                    len(self._odds_events_cache),
                    self.odds_scanner.odds_client.quota_remaining(),
                )
            except Exception as e:
                logger.error("Odds API refresh failed: %s", e)
                return
        else:
            logger.debug(
                "Using cached Odds API data (age=%.0fm, %d events)",
                cache_age / 60, len(self._odds_events_cache),
            )

        if not self._odds_events_cache:
            logger.info("No sportsbook events available (off-season or quota hit).")
            return

        # B3. Scan for mispricings
        def fetch_kxmve_book(market):
            raw = self.kalshi.get_orderbook(market.market_id)
            return normalize_kalshi_book(raw)

        try:
            odds_opps = await asyncio.to_thread(
                self.odds_scanner.scan,
                kxmve_markets,
                fetch_kxmve_book,
                self._odds_events_cache,
            )
            self.opportunities_found += len(odds_opps)

            if odds_opps:
                logger.info(
                    "Strategy B: %d odds-arb opportunities found!",
                    len(odds_opps),
                )
                for opp in odds_opps[:5]:
                    logger.info(
                        "  [ODDS ARB] %s | BUY_%s @%.3f | fair=%.3f | "
                        "edge=%.2f%% | %s | conf=%.2f | books=%d",
                        opp.kalshi_ticker,
                        opp.kalshi_side.upper(),
                        opp.kalshi_price,
                        opp.fair_prob,
                        opp.net_edge * 100,
                        opp.sportsbook_event,
                        opp.match_confidence,
                        opp.books_used,
                    )
                # B4. Simulate execution (single-leg Kalshi-only trades)
                for opp in odds_opps[:3]:
                    cost = opp.kalshi_price * (1 + 0.07)  # Kalshi taker fee
                    self.trades_simulated += 1
                    logger.info(
                        "  [SIM] BUY_%-3s %s x1 @ $%.3f | "
                        "expected profit $%.4f/contract",
                        opp.kalshi_side.upper(),
                        opp.kalshi_ticker,
                        cost,
                        opp.net_edge,
                    )
            else:
                logger.info(
                    "Strategy B: no odds-arb opportunities this scan "
                    "(%d KXMVE markets | %d sportsbook events | min_edge=%.1f%%).",
                    len(kxmve_markets),
                    len(self._odds_events_cache),
                    self.odds_scanner.min_edge * 100,
                )
        except Exception as e:
            logger.error("Strategy B scan failed: %s", e)

    async def _strategy_b_economic(self):
        """Strategy B: Kalshi KXBTC/KXFED vs Deribit options / CME FedWatch."""
        markets = self._last_kalshi_normalized
        if not markets:
            logger.info("Strategy B: no Kalshi markets cached yet — skipping.")
            return

        btc_count = sum(1 for m in markets if m.market_id.startswith("KXBTC"))
        fed_count = sum(1 for m in markets if m.market_id.startswith("KXFED"))
        if btc_count + fed_count == 0:
            logger.info("Strategy B: no KXBTC/KXFED markets in cache — skipping.")
            return

        def fetch_k_book(market):
            raw = self.kalshi.get_orderbook(market.market_id)
            return normalize_kalshi_book(raw)

        try:
            econ_opps = await asyncio.to_thread(
                self.econ_scanner.scan,
                markets,
                fetch_k_book,
            )
            self.opportunities_found += len(econ_opps)

            if econ_opps:
                logger.info(
                    "Strategy B (Economic): %d opportunities found!",
                    len(econ_opps),
                )
                for opp in econ_opps[:5]:
                    logger.info(
                        "  [ECON ARB] %s | BUY_%s @%.3f | fair=%.4f | "
                        "edge=%.2f%% | src=%s",
                        opp.kalshi_ticker,
                        opp.kalshi_side.upper(),
                        opp.kalshi_price,
                        opp.fair_prob,
                        opp.net_edge * 100,
                        opp.source,
                    )
                # Simulate top-3 opportunities
                for opp in econ_opps[:3]:
                    cost = opp.kalshi_price * (1 + 0.07)
                    self.trades_simulated += 1
                    logger.info(
                        "  [SIM] BUY_%-3s %s x1 @ $%.3f | "
                        "expected profit $%.4f/contract",
                        opp.kalshi_side.upper(),
                        opp.kalshi_ticker,
                        cost,
                        opp.net_edge,
                    )
            else:
                logger.info(
                    "Strategy B (Economic): no opportunities "
                    "(%d KXBTC | %d KXFED | min_edge=%.1f%%).",
                    btc_count, fed_count,
                    self.econ_scanner.min_edge * 100,
                )
        except Exception as e:
            logger.error("Strategy B (Economic) scan failed: %s", e)

    # ── Summaries ─────────────────────────────────────────────────────────────

    async def _send_daily_summary(self):
        try:
            summary   = self.tracker.get_pnl_summary(mode="paper")
            positions = self.tracker.get_closed_positions(mode="paper")
            if positions:
                best  = max(positions, key=lambda p: p.get("actual_profit", 0) or 0)
                worst = min(positions, key=lambda p: p.get("actual_profit", 0) or 0)
                best_str  = "{} (${:+.2f})".format(
                    best.get("kalshi_ticker", "?"), best.get("actual_profit", 0))
                worst_str = "{} (${:+.2f})".format(
                    worst.get("kalshi_ticker", "?"), worst.get("actual_profit", 0))
            else:
                best_str = worst_str = "N/A"
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
            logger.error("Daily summary failed: %s", e)

    async def _shutdown(self):
        logger.info("Shutting down paper trader...")
        summary = self.tracker.get_pnl_summary(mode="paper")

        print("\n" + "=" * 62)
        print("PAPER TRADING SUMMARY  (Kalshi <-> PredictIt)")
        print("=" * 62)
        print("Scans completed:     {}".format(self.scan_count))
        print("Opportunities found: {}".format(self.opportunities_found))
        print("Trades simulated:    {}".format(self.trades_simulated))
        print("Closed positions:    {}".format(summary["total_trades"]))
        print("Win rate:            {:.1%}".format(summary["win_rate"]))
        print("Gross P&L:           ${:+.4f}".format(summary["gross_pnl"]))
        print("Avg edge:            {:.2%}".format(summary["avg_edge_pct"]))
        print("Avg EPD:             {:.1f}%".format(summary["avg_edge_per_day"]))
        print("Open positions:      {}".format(summary["open_positions"]))
        print("Deployed capital:    ${:.2f}".format(summary["deployed_usd"]))
        print("=" * 62)

        if summary["total_trades"] >= 2:
            try:
                report = self.attr.generate_pnl_report(mode="paper")
                risk   = report.get("risk_metrics", {})
                print("Sharpe ratio:        {}".format(risk.get("sharpe_ratio", "N/A")))
                print("Max drawdown:        ${:.4f}".format(risk.get("max_drawdown", 0)))
                print("Calmar ratio:        {}".format(risk.get("calmar_ratio", "N/A")))
                print("=" * 62)
            except Exception:
                pass

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
    parser = argparse.ArgumentParser(
        description="Arb bot paper trading — Kalshi vs PredictIt"
    )
    parser.add_argument("--hours",   type=float, default=24.0,
                        help="How long to run (default: 24h)")
    parser.add_argument("--capital", type=float, default=STARTING_CAPITAL_USD,
                        help="Starting capital in USD (default: {})".format(
                            STARTING_CAPITAL_USD))
    args = parser.parse_args()

    os.makedirs("data", exist_ok=True)
    trader = PaperTrader(capital=args.capital, max_hours=args.hours)

    def handle_sigint(*_):
        logger.info("Received SIGINT - stopping after current scan...")
        trader.running = False

    signal.signal(signal.SIGINT, handle_sigint)
    signal.signal(signal.SIGTERM, handle_sigint)
    asyncio.run(trader.run())


if __name__ == "__main__":
    main()
