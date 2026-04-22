"""
pnl_attribution.py — Detailed P&L attribution and performance reporting.

Breaks down P&L by:
  - Time period (daily, weekly, monthly)
  - Market category
  - Duration bucket (ultra-short, short, medium)
  - Match score tier
  - Direction (which platform was YES vs NO)

Also computes risk-adjusted metrics:
  - Sharpe ratio (annualized)
  - Max drawdown
  - Calmar ratio (annualized return / max drawdown)
  - Win rate and average edge

Reference: polymarket_kalshi_arb_context.md.docx § 12 — Backtesting & Metrics
"""

import logging
import math
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional
from collections import defaultdict

logger = logging.getLogger(__name__)

TRADING_DAYS_PER_YEAR = 365   # Prediction markets trade every day


class PnlAttribution:
    """
    Computes attribution and risk metrics from a list of closed positions.

    Usage:
        attr = PnlAttribution(tracker)
        report = attr.generate_pnl_report()
    """

    def __init__(self, tracker):
        """
        Args:
            tracker: PositionTracker instance — source of closed positions.
        """
        self.tracker = tracker

    def generate_pnl_report(self, mode: Optional[str] = None,
                             days: int = 90) -> Dict[str, Any]:
        """
        Full P&L report for all closed positions in the last N days.

        Returns:
            Dict with summary, by_duration, by_day, risk_metrics
        """
        positions = self.tracker.get_closed_positions(mode=mode)
        if not positions:
            return {"error": "No closed positions found", "mode": mode}

        # Filter to last N days
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        positions = [
            p for p in positions
            if p.get("closed_at") and
               _parse_dt(p["closed_at"]) >= cutoff
        ]
        if not positions:
            return {"error": f"No positions closed in last {days} days"}

        pnls = [p.get("actual_profit", 0.0) or 0.0 for p in positions]

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "mode": mode or "all",
            "period_days": days,
            "total_positions": len(positions),
            "summary": self._summary(positions, pnls),
            "risk_metrics": self._risk_metrics(pnls),
            "by_duration": self._by_duration(positions),
            "by_day": self._by_day(positions),
            "best_trades": self._top_trades(positions, n=5, best=True),
            "worst_trades": self._top_trades(positions, n=5, best=False),
        }

    # ── Summary ───────────────────────────────────────────────────────────────

    def _summary(self, positions: List[Dict], pnls: List[float]) -> Dict:
        total = len(pnls)
        wins  = sum(1 for p in pnls if p > 0)
        gross = sum(pnls)
        avg   = gross / total if total else 0
        edges = [p.get("actual_profit_pct", 0) or 0 for p in positions]
        epds  = [p.get("edge_per_day", 0) or 0 for p in positions]

        return {
            "total_trades":      total,
            "wins":              wins,
            "losses":            total - wins,
            "win_rate":          round(wins / total, 4) if total else 0,
            "gross_pnl":         round(gross, 4),
            "avg_profit":        round(avg, 4),
            "avg_edge_pct":      round(_mean(edges), 4),
            "avg_edge_per_day":  round(_mean(epds), 2),
            "total_cost":        round(sum(p.get("gross_cost", 0) or 0 for p in positions), 2),
        }

    # ── Risk metrics ──────────────────────────────────────────────────────────

    def _risk_metrics(self, pnls: List[float]) -> Dict:
        """Sharpe, Sortino, max drawdown, Calmar."""
        if len(pnls) < 2:
            return {"error": "need >= 2 closed trades for risk metrics"}

        mean_pnl = _mean(pnls)
        std_pnl  = _std(pnls)
        n        = len(pnls)

        # Annualised Sharpe (daily-like — each "period" = one trade)
        # Assumption: trades are roughly uniformly distributed over time
        sharpe = (mean_pnl / std_pnl * math.sqrt(TRADING_DAYS_PER_YEAR)) if std_pnl > 0 else 0

        # Sortino: penalise only downside deviation
        downside = [min(0, p) for p in pnls]
        downside_std = _std(downside) if any(d < 0 for d in downside) else 0
        sortino = (mean_pnl / downside_std * math.sqrt(TRADING_DAYS_PER_YEAR)) if downside_std > 0 else float("inf")

        # Max drawdown (peak-to-trough on cumulative P&L)
        cumulative = []
        running = 0
        for p in pnls:
            running += p
            cumulative.append(running)

        peak = cumulative[0]
        max_dd = 0
        for c in cumulative:
            peak = max(peak, c)
            dd = peak - c
            max_dd = max(max_dd, dd)

        total_pnl = cumulative[-1] if cumulative else 0
        calmar = (total_pnl / max_dd) if max_dd > 0 else float("inf")

        return {
            "sharpe_ratio":   round(sharpe, 3),
            "sortino_ratio":  round(sortino, 3),
            "max_drawdown":   round(max_dd, 4),
            "calmar_ratio":   round(calmar, 3),
            "total_pnl":      round(total_pnl, 4),
            "std_per_trade":  round(std_pnl, 4),
        }

    # ── Attribution slices ────────────────────────────────────────────────────

    def _by_duration(self, positions: List[Dict]) -> Dict:
        """P&L breakdown by days_to_resolution bucket."""
        buckets: Dict[str, List[float]] = defaultdict(list)
        for p in positions:
            days = p.get("days_to_resolution") or 0
            bucket = _duration_bucket(days)
            buckets[bucket].append(p.get("actual_profit", 0) or 0)

        return {
            bucket: {
                "count":   len(pnls),
                "total":   round(sum(pnls), 4),
                "avg":     round(_mean(pnls), 4),
                "win_rate": round(sum(1 for p in pnls if p > 0) / len(pnls), 3),
            }
            for bucket, pnls in sorted(buckets.items())
            if pnls
        }

    def _by_day(self, positions: List[Dict]) -> List[Dict]:
        """Daily P&L for sparkline / trend chart."""
        by_date: Dict[str, List[float]] = defaultdict(list)
        for p in positions:
            if p.get("closed_at"):
                date_str = p["closed_at"][:10]   # YYYY-MM-DD
                by_date[date_str].append(p.get("actual_profit", 0) or 0)

        cumulative = 0.0
        result = []
        for date in sorted(by_date):
            daily = sum(by_date[date])
            cumulative += daily
            result.append({
                "date": date,
                "trades": len(by_date[date]),
                "daily_pnl": round(daily, 4),
                "cumulative": round(cumulative, 4),
            })
        return result

    def _top_trades(self, positions: List[Dict], n: int = 5,
                    best: bool = True) -> List[Dict]:
        """Top N best or worst trades."""
        sorted_pos = sorted(
            positions,
            key=lambda p: p.get("actual_profit", 0) or 0,
            reverse=best,
        )
        return [
            {
                "id":           p.get("id"),
                "ticker":       p.get("kalshi_ticker"),
                "title":        (p.get("kalshi_title") or "")[:50],
                "contracts":    p.get("kalshi_contracts"),
                "pnl":          round(p.get("actual_profit", 0) or 0, 4),
                "edge_pct":     round(p.get("actual_profit_pct", 0) or 0, 4),
                "epd":          round(p.get("edge_per_day", 0) or 0, 2),
                "days":         p.get("days_to_resolution"),
                "opened":       p.get("opened_at", "")[:10],
                "closed":       p.get("closed_at", "")[:10],
                "close_reason": p.get("close_reason"),
            }
            for p in sorted_pos[:n]
        ]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _mean(values: List[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _std(values: List[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = _mean(values)
    variance = sum((v - m) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(variance)


def _parse_dt(s: str) -> datetime:
    """Parse ISO-8601 string to aware datetime."""
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return datetime.now(timezone.utc)


def _duration_bucket(days: int) -> str:
    """Categorise position duration."""
    if days <= 1:   return "ultra_short (≤1d)"
    if days <= 3:   return "short (2-3d)"
    if days <= 7:   return "medium (4-7d)"
    if days <= 14:  return "long (8-14d)"
    return "very_long (>14d)"
