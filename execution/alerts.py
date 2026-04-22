"""
alerts.py — Telegram alerting for all bot events.

All functions are async and safe to call without a Telegram token configured —
they fall back to structured console logging so paper trading works without setup.

Message format: Markdown (supported by Telegram Bot API).
"""

import httpx
import logging
from datetime import datetime

from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Core send
# ─────────────────────────────────────────────────────────────────────────────

async def send_alert(message: str, parse_mode: str = "Markdown") -> bool:
    """
    Send a Telegram message to the configured chat.
    Returns True on success, False on failure.
    Falls back to print if Telegram not configured.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        # Structured console fallback for paper trading / dev environments
        print(f"\n{'='*60}")
        print(f"[ALERT {datetime.utcnow().strftime('%H:%M:%S')}]")
        print(message.replace("*", "").replace("`", ""))
        print('='*60)
        return True

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": message,
                    "parse_mode": parse_mode,
                },
            )
            if resp.status_code != 200:
                logger.warning(f"Telegram returned {resp.status_code}: {resp.text[:200]}")
                return False
            return True
    except Exception as e:
        logger.error(f"Alert send failed: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Trade lifecycle alerts
# ─────────────────────────────────────────────────────────────────────────────

async def alert_execution(opp, contracts: int):
    """Fired when both legs fill cleanly."""
    # Estimate expected profit from detected edge and per-contract total cost.
    # (Avoid relying on non-existent fields like opp.total_cost.)
    total_cost_per_contract = getattr(opp, "gross_cost", None)
    if total_cost_per_contract is None:
        total_cost_per_contract = float(getattr(opp, "kalshi_price", 0.0)) + float(getattr(opp, "poly_price", 0.0))
    profit_usd = float(getattr(opp, "net_profit_pct", 0.0)) * total_cost_per_contract * contracts
    await send_alert(
        f"✅ *ARB EXECUTED*\n"
        f"Market: `{opp.kalshi_ticker}`\n"
        f"Title: {str(getattr(opp, 'kalshi_title', getattr(opp, 'kalshi_ticker', '')) )[:60]}\n"
        f"Contracts: {contracts}\n"
        f"Edge: {float(getattr(opp, 'net_profit_pct', 0.0)):.2%} | "
        f"EPD: {float(getattr(opp, 'edge_per_day', 0.0)):.1f}% ann.\n"
        f"Kalshi: {opp.kalshi_side.upper()} @ {opp.kalshi_price:.3f}\n"
        f"Poly:   {opp.poly_side.upper()} @ {opp.poly_price:.3f}\n"
        f"Expected profit: *${profit_usd:.2f}*\n"
        f"Days to resolution: {getattr(opp, 'days_to_resolution', '?')}"
    )


async def alert_naked_exposure(opp, k_filled: int, p_filled: int):
    """Fired immediately when a leg mismatch is detected."""
    naked = abs(k_filled - p_filled)
    which = "Kalshi" if k_filled > p_filled else "Polymarket"
    await send_alert(
        f"⚠️ *NAKED EXPOSURE DETECTED*\n"
        f"Market: `{opp.kalshi_ticker}`\n"
        f"Kalshi filled: {k_filled} | Poly filled: {p_filled}\n"
        f"Naked contracts: {naked} on {which}\n"
        f"Hedging immediately... (60s timeout)"
    )


async def alert_naked_resolved(opp, naked_contracts: int, outcome: str):
    """Fired after NakedExposureManager completes."""
    emoji = {
        "filled_late":    "✅",
        "closed_at_loss": "🔴",
        "holding_naked":  "🚨",
        "no_action":      "ℹ️",
    }.get(outcome, "❓")
    outcome_label = outcome.replace("_", " ").title()
    await send_alert(
        f"{emoji} *NAKED EXPOSURE RESOLVED*\n"
        f"Market: `{opp.kalshi_ticker}`\n"
        f"Naked contracts: {naked_contracts}\n"
        f"Outcome: *{outcome_label}*"
    )


async def alert_execution_failed(opp, reason: str,
                                  k_error: str = None, p_error: str = None):
    """Fired when both legs fail — no position opened."""
    await send_alert(
        f"❌ *EXECUTION FAILED*\n"
        f"Market: `{opp.kalshi_ticker}`\n"
        f"Reason: {reason}\n"
        + (f"Kalshi error: {k_error}\n" if k_error else "")
        + (f"Poly error:   {p_error}\n" if p_error else "")
    )


# ─────────────────────────────────────────────────────────────────────────────
# Position lifecycle alerts
# ─────────────────────────────────────────────────────────────────────────────

async def alert_position_closed(position_id: int, market_title: str,
                                 actual_profit: float, reason: str):
    """Fired when a position is marked closed in the DB."""
    emoji = "💰" if actual_profit > 0 else "📉"
    await send_alert(
        f"{emoji} *POSITION CLOSED*\n"
        f"ID: {position_id} | {market_title[:50]}\n"
        f"P&L: *${actual_profit:+.2f}*\n"
        f"Reason: {reason}"
    )


async def alert_market_resolved(ticker: str, title: str,
                                 resolution: str, payout_usd: float):
    """Fired when the bot detects a market has resolved."""
    await send_alert(
        f"🏁 *MARKET RESOLVED*\n"
        f"Ticker: `{ticker}`\n"
        f"Title: {title[:60]}\n"
        f"Resolution: *{resolution.upper()}*\n"
        f"Payout: ${payout_usd:.2f}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Scan / system alerts
# ─────────────────────────────────────────────────────────────────────────────

async def alert_opportunity_found(opp, rank: int = 1):
    """Fired when a tradeable opportunity is detected (before execution)."""
    await send_alert(
        f"🔍 *OPPORTUNITY #{rank} FOUND*\n"
        f"Market: `{opp.kalshi_ticker}`\n"
        f"Title: {opp.kalshi_title[:60]}\n"
        f"Direction: K-{opp.kalshi_side.upper()} + P-{opp.poly_side.upper()}\n"
        f"Net edge: {opp.net_profit_pct:.2%} | EPD: {opp.edge_per_day:.1f}%\n"
        f"Days: {opp.days_to_resolution} | Match score: {opp.match_score:.2f}"
    )


async def alert_scan_summary(opportunities_found: int, pairs_scanned: int,
                               best_edge: float, deployed_usd: float,
                               available_usd: float):
    """Periodic scan summary (every N scans or on significant events)."""
    await send_alert(
        f"📊 *SCAN COMPLETE*\n"
        f"Pairs scanned: {pairs_scanned}\n"
        f"Opportunities found: {opportunities_found}\n"
        f"Best edge: {best_edge:.2%}\n"
        f"Capital deployed: ${deployed_usd:.2f} / ${available_usd:.2f}"
    )


async def alert_risk_rejected(opp, reason: str):
    """Fired when RiskManager blocks a trade."""
    await send_alert(
        f"🛑 *TRADE BLOCKED BY RISK MANAGER*\n"
        f"Market: `{opp.kalshi_ticker}`\n"
        f"Reason: {reason}\n"
        f"Edge: {opp.net_profit_pct:.2%} | EPD: {opp.edge_per_day:.1f}%"
    )


async def alert_bot_started(mode: str, capital_usd: float):
    """Fired when the bot starts up."""
    await send_alert(
        f"🤖 *ARB BOT STARTED*\n"
        f"Mode: *{mode.upper()}*\n"
        f"Capital: ${capital_usd:.2f}\n"
        f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
    )


async def alert_bot_stopped(reason: str, session_pnl: float):
    """Fired when the bot shuts down cleanly."""
    emoji = "✅" if session_pnl >= 0 else "📉"
    await send_alert(
        f"🛑 *ARB BOT STOPPED*\n"
        f"Reason: {reason}\n"
        f"{emoji} Session P&L: ${session_pnl:+.2f}\n"
        f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
    )


async def alert_error(component: str, error: str, critical: bool = False):
    """Fired on unexpected errors."""
    emoji = "🚨" if critical else "⚠️"
    await send_alert(
        f"{emoji} *{'CRITICAL ' if critical else ''}ERROR*\n"
        f"Component: {component}\n"
        f"Error: {error[:300]}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Daily summary
# ─────────────────────────────────────────────────────────────────────────────

async def alert_daily_summary(
    date_str: str,
    trades_executed: int,
    gross_pnl: float,
    fees_paid: float,
    net_pnl: float,
    win_rate: float,
    capital_deployed_peak: float,
    best_trade: str,
    worst_trade: str,
):
    """End-of-day performance summary."""
    emoji = "📈" if net_pnl >= 0 else "📉"
    await send_alert(
        f"{emoji} *DAILY SUMMARY — {date_str}*\n"
        f"Trades executed: {trades_executed}\n"
        f"Gross P&L:  ${gross_pnl:+.2f}\n"
        f"Fees paid:  ${fees_paid:.2f}\n"
        f"*Net P&L:   ${net_pnl:+.2f}*\n"
        f"Win rate:   {win_rate:.1%}\n"
        f"Peak deployed: ${capital_deployed_peak:.2f}\n"
        f"Best trade:  {best_trade}\n"
        f"Worst trade: {worst_trade}"
    )
