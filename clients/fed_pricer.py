"""
fed_pricer.py — CME FedWatch probabilities for Kalshi KXFED markets.

Kalshi KXFED markets ask "Will the Fed funds rate upper bound be above/below X
at FOMC meeting Y?"

We fetch the market-implied probabilities from CME Group's FedWatch tool,
which computes the probability of each rate outcome at each scheduled FOMC
meeting directly from 30-Day Fed Funds futures prices.

Data source: CME FedWatch (free, public)
  https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html

No API key required.

Usage:
    pricer = FedPricer()
    meetings = pricer.get_meeting_probabilities()
    for m in meetings:
        print(m["date"], m["probabilities"])
"""

import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional

import requests

log = logging.getLogger(__name__)

# CME FedWatch JSON endpoint (scraped from the tool's network requests)
FEDWATCH_URL = (
    "https://www.cmegroup.com/CmeWS/mvc/PortfolioFedWatch/fedWatch/fedwatch"
)

# FRED API for current Fed funds rate (free, no key needed for basic data)
FRED_EFFR_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=EFFR"


class FedPricer:
    """
    Provides market-implied Fed funds rate probabilities for each FOMC meeting.

    Cache: probabilities are refreshed every 30 minutes (rarely change intraday).
    """

    TTL = 1800  # 30 minutes

    # Known FOMC meeting dates (update as they're scheduled)
    # Format: "YYYY-MM-DD" (day of decision announcement)
    FOMC_DATES_2026 = [
        "2026-01-28",
        "2026-03-18",
        "2026-05-06",
        "2026-06-17",
        "2026-07-29",
        "2026-09-16",
        "2026-10-28",
        "2026-12-09",
    ]
    FOMC_DATES_2027 = [
        "2027-01-27",
        "2027-03-17",
        "2027-04-28",   # Apr 27-28 meeting
        "2027-06-16",
        "2027-07-28",
        "2027-09-15",
        "2027-10-27",
        "2027-12-08",
    ]
    FOMC_DATES = FOMC_DATES_2026 + FOMC_DATES_2027

    def __init__(self):
        self.session            = requests.Session()
        self.session.headers["User-Agent"] = (
            "Mozilla/5.0 (compatible; ArbBot/1.0; research)"
        )
        self._cache:  list  = []
        self._cache_t: float = 0.0
        self._current_rate:  Optional[float] = None
        log.info("FedPricer initialized")

    # ─────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────

    def get_meeting_probabilities(self, force_refresh: bool = False) -> list:
        """
        Return list of FOMC meeting outcome dicts, sorted by date.

        Each dict:
            {
                "date":         "2026-05-06",         ← FOMC decision date
                "days_until":   12,
                "current_rate": 4.50,                 ← current upper bound (%)
                "probabilities": {                    ← market-implied probs
                    "5.25": 0.03,   # P(rate = 5.25%)
                    "5.00": 0.12,
                    "4.75": 0.51,
                    "4.50": 0.34,
                    ...
                },
                "expected_rate": 4.72,                ← probability-weighted rate
                "p_cut":  0.65,                       ← P(any cut from current)
                "p_hold": 0.34,                       ← P(no change)
                "p_hike": 0.01,                       ← P(any hike from current)
            }
        """
        now = time.time()
        if (not force_refresh and
                self._cache and
                now - self._cache_t < self.TTL):
            return self._cache

        try:
            meetings = self._fetch_fedwatch()
            if meetings:
                self._cache   = meetings
                self._cache_t = now
                log.info(
                    "FedPricer: %d meetings loaded (next: %s)",
                    len(meetings),
                    meetings[0]["date"] if meetings else "?",
                )
                return self._cache
        except Exception as exc:
            log.warning("FedWatch fetch failed: %s — using fallback model", exc)

        # Fallback: build and CACHE so CME is not retried on every call
        fallback = self._fallback_meetings()
        self._cache   = fallback
        self._cache_t = now
        return self._cache

    def prob_above(self, rate_pct: float, meeting_date: str) -> Optional[float]:
        """
        P(upper bound > rate_pct) at the given FOMC meeting.

        Args:
            rate_pct:     rate threshold in percent (e.g., 4.50)
            meeting_date: "YYYY-MM-DD" of the FOMC decision (or close_time ISO string)

        Returns float 0–1 or None if meeting not found.
        """
        # Normalise ISO timestamps → "YYYY-MM-DD"
        meeting_date = meeting_date[:10]
        meetings = self.get_meeting_probabilities()
        # Exact or same-month match
        for m in meetings:
            if m["date"] == meeting_date or m["date"].startswith(meeting_date[:7]):
                probs = m.get("probabilities", {})
                total = sum(
                    p for rate_str, p in probs.items()
                    if float(rate_str) > rate_pct
                )
                return min(1.0, max(0.0, total))
        # Nearest meeting within 7 days (handles slight date drift)
        try:
            target = datetime.strptime(meeting_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            best, best_days = None, 7
            for m in meetings:
                d = datetime.strptime(m["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                diff = abs((d - target).days)
                if diff <= best_days:
                    best_days = diff
                    best = m
            if best:
                probs = best.get("probabilities", {})
                total = sum(
                    p for rate_str, p in probs.items()
                    if float(rate_str) > rate_pct
                )
                return min(1.0, max(0.0, total))
        except Exception:
            pass
        return None

    def prob_at_or_below(self, rate_pct: float, meeting_date: str) -> Optional[float]:
        """P(upper bound <= rate_pct) at the given FOMC meeting."""
        p_above = self.prob_above(rate_pct, meeting_date)
        return None if p_above is None else 1.0 - p_above

    # ─────────────────────────────────────────────────────────────────
    # CME FedWatch fetch
    # ─────────────────────────────────────────────────────────────────

    def _fetch_fedwatch(self) -> list:
        """
        Fetch probabilities from CME FedWatch tool.
        Tries CME API only (no scraping fallback — scraping also requires browser session).
        """
        return self._fetch_cme_api()

    def _fetch_cme_api(self) -> list:
        """
        Try CME's internal FedWatch API (may require session/cookies).
        """
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        meetings = []

        # Try one date only — if CME API is accessible, all dates work.
        # Use a 3-second timeout to fail fast if CME blocks.
        test_date = next(
            (d for d in self.FOMC_DATES if self._days_until(d) > 0), None
        )
        if not test_date:
            return []

        try:
            resp = self.session.get(
                FEDWATCH_URL,
                params={"tradeDate": today, "expirationDate": test_date.replace("-", "")},
                timeout=3,
            )
            if resp.status_code != 200:
                raise ValueError(f"HTTP {resp.status_code}")
            data = resp.json()
            test_probs = self._parse_cme_response(data)
            if not test_probs:
                raise ValueError("empty response")
        except Exception as exc:
            log.debug("CME API test request failed (%s) — using fallback", exc)
            raise  # Let caller fall through to fallback

        # CME is accessible — fetch all future dates
        for date_str in self.FOMC_DATES:
            days_until = self._days_until(date_str)
            if days_until < -1:
                continue
            params = {"tradeDate": today, "expirationDate": date_str.replace("-", "")}
            try:
                resp = self.session.get(FEDWATCH_URL, params=params, timeout=5)
                if resp.status_code != 200:
                    continue
                probs = self._parse_cme_response(resp.json())
                if probs:
                    meetings.append(self._build_meeting(date_str, days_until, probs))
            except Exception:
                continue

        return meetings

    def _parse_cme_response(self, data: dict) -> dict:
        """Parse CME JSON response into {rate_str: prob} dict."""
        probs = {}
        # CME response format varies; try common structures
        for key in ("probabilities", "meetingData", "data"):
            if key in data:
                items = data[key]
                if isinstance(items, list):
                    for item in items:
                        rate = item.get("rate") or item.get("targetRate")
                        prob = item.get("probability") or item.get("prob")
                        if rate is not None and prob is not None:
                            probs[str(float(rate))] = float(prob) / 100.0
        return probs

    def _scrape_fedwatch(self) -> list:
        """
        Fallback: scrape FedWatch page HTML for rate/probability pairs.
        """
        resp = self.session.get(
            "https://www.cmegroup.com/markets/interest-rates/"
            "cme-fedwatch-tool.html",
            timeout=15,
        )
        # Extract embedded JSON data
        matches = re.findall(
            r'"probability"\s*:\s*([\d.]+)',
            resp.text
        )
        # This is a simplified scrape; production would use full JSON extraction
        if not matches:
            raise ValueError("No probability data found in FedWatch page")

        # Build minimal structure from what we can parse
        return self._fallback_meetings()

    def _fallback_meetings(self) -> list:
        """
        Fallback when FedWatch is unavailable.
        Returns upcoming meetings with flat probabilities centred on current rate.
        Marks these as low-confidence so the scanner can penalise them.
        """
        current = self._current_rate or 4.50   # best guess
        meetings = []

        for date_str in self.FOMC_DATES:
            days = self._days_until(date_str)
            if days < -1:
                continue
            # Simple assumption: 70% hold, 20% cut 25bps, 10% cut 50bps
            probs = {
                str(current + 0.25): 0.02,
                str(current):        0.70,
                str(current - 0.25): 0.20,
                str(current - 0.50): 0.08,
            }
            meetings.append(self._build_meeting(date_str, days, probs, fallback=True))

        return meetings

    def _build_meeting(
        self,
        date_str:   str,
        days_until: float,
        probs:      dict,
        fallback:   bool = False,
    ) -> dict:
        """Construct a meeting probability dict."""
        current_rate = self._current_rate or 4.50

        rates = {float(r): p for r, p in probs.items()}
        expected = sum(r * p for r, p in rates.items())

        p_cut  = sum(p for r, p in rates.items() if r < current_rate)
        p_hold = rates.get(current_rate, 0.0)
        p_hike = sum(p for r, p in rates.items() if r > current_rate)

        return {
            "date":          date_str,
            "days_until":    days_until,
            "current_rate":  current_rate,
            "probabilities": probs,
            "expected_rate": round(expected, 4),
            "p_cut":         round(p_cut, 4),
            "p_hold":        round(p_hold, 4),
            "p_hike":        round(p_hike, 4),
            "is_fallback":   fallback,
        }

    @staticmethod
    def _days_until(date_str: str) -> float:
        target = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return (target - datetime.now(timezone.utc)).total_seconds() / 86400

    # ─────────────────────────────────────────────────────────────────
    # KXFED market title parser
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    def parse_kxfed_title(title: str) -> Optional[dict]:
        """
        Parse a KXFED market title into {rate, direction, meeting_date}.

        Example titles:
          "Will the upper bound of the federal funds rate be above 4.50% at the May 2026 meeting?"
          "Will the Fed funds rate be at or below 4.25% after the June 2026 meeting?"

        Returns {"rate": 4.50, "direction": "above"/"below", "meeting": "2026-05-06"}
        or None.
        """
        # Extract rate
        m = re.search(r'(\d+\.?\d*)\s*%', title)
        if not m:
            return None
        rate = float(m.group(1))

        # Extract direction
        if re.search(r'\babove\b|\bgreater\b|\bhigher\b|\bexceed', title, re.I):
            direction = "above"
        elif re.search(r'\bbelow\b|\bat or below\b|\bless\b|\bnot exceed', title, re.I):
            direction = "below"
        else:
            return None

        # Extract meeting month/year
        month_match = re.search(
            r'(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})',
            title, re.IGNORECASE
        )
        if not month_match:
            return None

        month_str = month_match.group(1)
        year      = int(month_match.group(2))
        # Look up the FOMC date for that month/year
        month_num = datetime.strptime(month_str[:3], "%b").month
        meeting   = FedPricer._find_fomc_meeting(month_num, year)

        return {"rate": rate, "direction": direction, "meeting": meeting}

    @staticmethod
    def _find_fomc_meeting(month: int, year: int) -> Optional[str]:
        """Find the FOMC meeting date closest to a given month/year."""
        target_prefix = f"{year}-{month:02d}"
        for d in FedPricer.FOMC_DATES:
            if d.startswith(target_prefix):
                return d
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Smoke test
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    pricer = FedPricer()
    meetings = pricer.get_meeting_probabilities()
    print(f"Loaded {len(meetings)} FOMC meetings")
    for m in meetings[:3]:
        print(f"\n  {m['date']} (in {m['days_until']:.0f} days)")
        print(f"  P(cut)={m['p_cut']:.2f}  P(hold)={m['p_hold']:.2f}  P(hike)={m['p_hike']:.2f}")
        print(f"  Expected rate: {m['expected_rate']:.2f}%")
        if m.get("is_fallback"):
            print("  [FALLBACK — FedWatch unavailable]")
