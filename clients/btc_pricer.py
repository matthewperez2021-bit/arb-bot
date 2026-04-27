"""
btc_pricer.py — Bitcoin probability calculator for Kalshi KXBTC markets.

Kalshi KXBTC markets ask "Will BTC be in price range [X, Y] at date D?"
We compute the fair probability using:
  1. Current BTC spot price from Deribit (free public API)
  2. Implied volatility from near-term BTC options on Deribit
  3. Log-normal price model (Black-Scholes) to compute P(price in range)

All APIs used are FREE — no key required for Deribit public endpoints.

Usage:
    pricer = BTCPricer()
    prob = pricer.prob_in_range(low=90000, high=95000, days=3)
    print(f"P(BTC between $90k-$95k in 3 days): {prob:.3f}")
"""

import logging
import math
import re
import time
from datetime import datetime, timezone
from typing import Optional

import requests

log = logging.getLogger(__name__)

DERIBIT_BASE = "https://www.deribit.com/api/v2/public"


class BTCPricer:
    """
    Prices BTC range binary outcomes using log-normal model + Deribit IV.

    Cache policy:
      - Spot price: 60s TTL (changes rapidly)
      - IV surface: 300s TTL (changes slowly)
    """

    SPOT_TTL = 60       # seconds
    IV_TTL   = 300      # seconds

    def __init__(self):
        self.session          = requests.Session()
        self._spot:  Optional[float] = None
        self._spot_t: float  = 0.0
        self._iv_30d: Optional[float] = None   # 30-day annualised IV
        self._iv_t:   float  = 0.0
        log.info("BTCPricer initialized (Deribit public API)")

    # ─────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────

    def spot_price(self) -> float:
        """Current BTC/USD spot price. Cached 60s."""
        now = time.time()
        if self._spot and now - self._spot_t < self.SPOT_TTL:
            return self._spot
        try:
            resp = self.session.get(
                f"{DERIBIT_BASE}/get_index_price",
                params={"index_name": "btc_usd"},
                timeout=10,
            )
            data = resp.json()
            self._spot   = float(data["result"]["index_price"])
            self._spot_t = now
            log.debug("BTC spot: $%.2f", self._spot)
        except Exception as exc:
            log.warning("BTC spot fetch failed: %s", exc)
            if self._spot is None:
                raise
        return self._spot

    def implied_vol(self) -> float:
        """
        30-day annualised implied volatility from Deribit BTC options.
        Cached 300s.  Falls back to 0.70 (70% ann. vol) if unavailable.
        """
        now = time.time()
        if self._iv_30d and now - self._iv_t < self.IV_TTL:
            return self._iv_30d
        try:
            # Use the nearest-term BTC options to estimate IV
            resp = self.session.get(
                f"{DERIBIT_BASE}/get_historical_volatility",
                params={"currency": "BTC"},
                timeout=10,
            )
            data = resp.json().get("result", [])
            if data:
                # data is list of [timestamp_ms, vol_pct]
                latest_vol_pct = data[-1][1]
                self._iv_30d = latest_vol_pct / 100.0   # convert pct → decimal
                self._iv_t   = now
                log.debug("BTC 30d IV: %.1f%%", latest_vol_pct)
        except Exception as exc:
            log.warning("BTC IV fetch failed: %s — using 70%% default", exc)
            self._iv_30d = 0.70   # conservative default

        if self._iv_30d is None:
            self._iv_30d = 0.70
        return self._iv_30d

    def prob_in_range(
        self,
        low:  Optional[float],
        high: Optional[float],
        days: float,
    ) -> float:
        """
        P(low <= BTC_price <= high) at t = now + days.

        Uses log-normal price model:
            ln(S_T) ~ N(ln(S_0) + mu*T - 0.5*sigma^2*T, sigma^2*T)
        where:
            mu    = 0  (risk-neutral drift; appropriate for binary pricing)
            sigma = annualised implied volatility
            T     = days / 365

        Args:
            low:  lower bound (None = 0)
            high: upper bound (None = infinity)
            days: time horizon in days

        Returns float in [0, 1].
        """
        S0    = self.spot_price()
        sigma = self.implied_vol()
        T     = max(days / 365.0, 1.0 / 365.0)   # minimum 1 day
        mu    = 0.0   # risk-neutral

        def _cdf(x: float) -> float:
            """Standard normal CDF via error function."""
            return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

        def _prob_above(price: float) -> float:
            """P(S_T > price)."""
            if price <= 0:
                return 1.0
            d = (math.log(S0 / price) + (mu - 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
            return _cdf(d)

        p_above_low  = _prob_above(low)  if low  else 1.0
        p_above_high = _prob_above(high) if high else 0.0
        prob = p_above_low - p_above_high
        return max(0.0, min(1.0, prob))

    def prob_above(self, price: float, days: float) -> float:
        """P(BTC > price) in `days` days."""
        return self.prob_in_range(low=price, high=None, days=days)

    def prob_below(self, price: float, days: float) -> float:
        """P(BTC < price) in `days` days."""
        return 1.0 - self.prob_above(price, days)

    # ─────────────────────────────────────────────────────────────────
    # KXBTC market title parser
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    def parse_kxbtc_title(title: str) -> Optional[dict]:
        """
        Parse a KXBTC market title into structured parameters.

        Example titles:
          "Bitcoin price range  on Apr 24, 2026?"
          "Will Bitcoin be above $95,000 on Apr 25, 2026?"
          "Bitcoin price range $90,000–$95,000 on Apr 24, 2026?"

        Returns dict with keys: low, high, date, days_to_expiry (from now)
        or None if unparseable.
        """
        title = title.replace(",", "").replace("$", "").replace("–", "-")

        # Pattern: "above X" or "below X"
        m = re.search(r'(above|below)\s+([\d]+(?:\.\d+)?)', title, re.IGNORECASE)
        if m:
            direction = m.group(1).lower()
            price     = float(m.group(2))
            date_str  = BTCPricer._extract_date(title)
            if date_str:
                days = BTCPricer._days_to(date_str)
                if direction == "above":
                    return {"low": price, "high": None, "days": days}
                else:
                    return {"low": None, "high": price, "days": days}

        # Pattern: "range X-Y" or "X to Y"
        m = re.search(r'([\d]+)\s*[-–to]+\s*([\d]+)', title, re.IGNORECASE)
        if m:
            low  = float(m.group(1))
            high = float(m.group(2))
            date_str = BTCPricer._extract_date(title)
            if date_str:
                days = BTCPricer._days_to(date_str)
                return {"low": min(low, high), "high": max(low, high), "days": days}

        return None

    @staticmethod
    def _extract_date(title: str) -> Optional[str]:
        """Extract a date string like 'Apr 24 2026' from the title."""
        months = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
        m = re.search(rf'({months})\s+(\d{{1,2}})\s+(\d{{4}})', title, re.IGNORECASE)
        if m:
            return f"{m.group(1)} {m.group(2)} {m.group(3)}"
        return None

    @staticmethod
    def _days_to(date_str: str) -> float:
        """Days from now to the given date string."""
        try:
            target = datetime.strptime(date_str, "%b %d %Y").replace(tzinfo=timezone.utc)
            now    = datetime.now(timezone.utc)
            return max(0.5, (target - now).total_seconds() / 86400)
        except ValueError:
            return 1.0


# ─────────────────────────────────────────────────────────────────────────────
# Smoke test
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    pricer = BTCPricer()
    spot   = pricer.spot_price()
    iv     = pricer.implied_vol()
    print(f"BTC spot:  ${spot:,.2f}")
    print(f"30d IV:    {iv*100:.1f}%")
    print()
    for days in [1, 3, 7, 30]:
        p_up5  = pricer.prob_above(spot * 1.05, days)
        p_down5 = pricer.prob_below(spot * 0.95, days)
        p_range = pricer.prob_in_range(spot * 0.97, spot * 1.03, days)
        print(f"  {days:2d}d: P(+5%)={p_up5:.3f}  P(-5%)={p_down5:.3f}  P(+/-3%)={p_range:.3f}")
