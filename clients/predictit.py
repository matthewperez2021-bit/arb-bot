"""
predictit.py — PredictIt market data client.

PredictIt is a CFTC no-action-letter prediction market focused on political
events. It is fully legal for US residents.

Auth:    None required for market reads (fully public REST API).
Docs:    https://www.predictit.org/api/marketdata/all/
         https://predictit.freshdesk.com/support/solutions/articles/12000001878

Fee structure (VERY expensive — factor into thresholds):
    10% fee on profits from each winning contract
    10% withdrawal fee on cash withdrawals
    Net: ~15–18% round-trip cost — you need large gross edges to profit.

Market format:
    Binary contracts (YES/NO) priced $0.01–$0.99
    Capped at $850 per contract per account
    Mostly US political elections, legislation, economic indicators

Usage:
    from clients.predictit import PredictItClient
    client = PredictItClient()
    markets = client.get_markets()
    book    = client.get_orderbook(market_id=7456)
"""

import logging
import time
from typing import Optional

import requests

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import PREDICTIT_BASE_URL

log = logging.getLogger(__name__)


class PredictItAPIError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}: {message}")


class PredictItClient:
    """
    Read-only client for PredictIt's public market data API.

    PredictIt has no order-placement API — execution must be done manually
    or via browser automation. Use this client as a data source to detect
    mispricing relative to Kalshi, then execute on Kalshi only.

    The public API returns ALL open markets in one snapshot call.
    Cache it: rate-limit to once every 30–60 seconds.
    """

    MAX_RETRIES  = 4
    BASE_BACKOFF = 2.0
    MAX_BACKOFF  = 30.0

    def __init__(self):
        self.session      = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self._cache: Optional[dict] = None
        self._cache_ts: float = 0.0
        self._cache_ttl: float = 30.0  # seconds — don't hammer PI's API
        log.info("PredictItClient initialized (public read-only)")

    # ─────────────────────────────────────────────────────────────────
    # HTTP layer
    # ─────────────────────────────────────────────────────────────────

    def _get(self, path: str) -> dict:
        """GET with retry/backoff. No auth required."""
        url     = PREDICTIT_BASE_URL + path
        backoff = self.BASE_BACKOFF

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                resp = self.session.get(url, timeout=15)
            except requests.exceptions.RequestException as exc:
                log.warning("PredictIt request error (attempt %d/%d): %s",
                            attempt, self.MAX_RETRIES, exc)
                if attempt == self.MAX_RETRIES:
                    raise
                time.sleep(min(backoff, self.MAX_BACKOFF))
                backoff *= 2
                continue

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code == 429:
                wait = min(backoff * 2, self.MAX_BACKOFF)
                log.warning("PredictIt rate limited. Waiting %.1fs", wait)
                time.sleep(wait)
                backoff *= 2
                continue

            if resp.status_code >= 500:
                wait = min(backoff, self.MAX_BACKOFF)
                log.warning("PredictIt server error %d. Waiting %.1fs",
                            resp.status_code, wait)
                time.sleep(wait)
                backoff *= 2
                continue

            raise PredictItAPIError(resp.status_code, resp.text)

        raise PredictItAPIError(0, "Max retries exceeded")

    # ─────────────────────────────────────────────────────────────────
    # Market data
    # ─────────────────────────────────────────────────────────────────

    def get_all_markets_raw(self, use_cache: bool = True) -> list:
        """
        Fetch all open PredictIt markets in one call.

        Returns the raw list of market dicts from PI's API.
        Results are cached for self._cache_ttl seconds to avoid rate limits.

        Raw market shape:
            {
                "id":       7456,
                "name":     "Which party will win the 2026 midterm elections?",
                "shortName":"2026 Midterms",
                "image":    "https://...",
                "url":      "https://www.predictit.org/markets/detail/7456/...",
                "contracts": [
                    {
                        "id":          35810,
                        "dateEnd":     "N/A",
                        "image":       "https://...",
                        "name":        "Republican",
                        "shortName":   "Republican",
                        "status":      "Open",
                        "lastTradePrice": 0.62,
                        "bestBuyYesCost":  0.63,
                        "bestBuyNoCost":   0.39,
                        "bestSellYesCost": 0.62,
                        "bestSellNoCost":  0.38,
                        "previousPrice":   0.61,
                        "volume":          45231,
                    },
                    ...
                ],
                "status": "Open",
                "timeStamp": "2026-04-24T12:00:00Z",
            }
        """
        now = time.time()
        if use_cache and self._cache and (now - self._cache_ts) < self._cache_ttl:
            return self._cache

        data = self._get("/marketdata/all/")
        markets = data.get("markets", [])
        self._cache = markets
        self._cache_ts = now
        log.info("PredictIt: fetched %d markets", len(markets))
        return markets

    def get_markets(
        self,
        status: str = "Open",
        min_contracts: int = 1,
    ) -> list:
        """
        Return open markets with at least min_contracts binary contracts.

        Each market is normalized to a consistent shape:
            {
                "id":        7456,
                "name":      "Which party will win...",
                "url":       "https://...",
                "contracts": [...],   # only Open contracts
                "status":    "Open",
            }

        For binary markets (1 contract), the contract IS the market question.
        For multi-contract markets (e.g. "Which Republican will win?"), each
        contract is a separate candidate — treat each as its own binary.
        """
        raw = self.get_all_markets_raw()
        result = []
        for mkt in raw:
            if mkt.get("status", "").lower() != status.lower():
                continue
            open_contracts = [
                c for c in mkt.get("contracts", [])
                if c.get("status", "").lower() == "open"
            ]
            if len(open_contracts) < min_contracts:
                continue
            result.append({**mkt, "contracts": open_contracts})
        return result

    def get_binary_markets(self) -> list:
        """
        Return only markets with exactly 1 open contract (true binary YES/NO).

        These are the only markets directly comparable to Kalshi binary contracts.
        Multi-contract markets need special handling and are excluded here.

        Returns list of (market_dict, contract_dict) tuples.
        """
        result = []
        for mkt in self.get_markets():
            contracts = mkt["contracts"]
            if len(contracts) == 1:
                result.append((mkt, contracts[0]))
        log.debug("PredictIt: %d binary markets", len(result))
        return result

    # ─────────────────────────────────────────────────────────────────
    # Order book
    # ─────────────────────────────────────────────────────────────────

    def get_orderbook(self, market_id: int, contract_id: Optional[int] = None) -> dict:
        """
        Extract best bid/ask prices for a specific PredictIt contract.

        PredictIt doesn't expose a full depth order book — only best prices.

        Args:
            market_id:   PredictIt market ID (integer)
            contract_id: specific contract ID (if market has multiple contracts).
                         If None, uses the first open contract.

        Returns normalized dict:
            {
                "yes": {
                    "best_ask": 0.63,    # bestBuyYesCost  (what you pay to buy YES)
                    "best_bid": 0.62,    # bestSellYesCost (what you receive selling YES)
                },
                "no": {
                    "best_ask": 0.39,    # bestBuyNoCost
                    "best_bid": 0.38,    # bestSellNoCost
                },
                "last_price":  0.62,
                "volume":      45231,
                "contract_id": 35810,
                "contract_name": "Republican",
                "timestamp":   1714000000.0,
            }
        Returns None if market/contract not found or illiquid.
        """
        markets = self.get_all_markets_raw()
        mkt = next((m for m in markets if m["id"] == market_id), None)
        if not mkt:
            log.warning("PredictIt market %d not found", market_id)
            return None

        contracts = [c for c in mkt.get("contracts", [])
                     if c.get("status", "").lower() == "open"]
        if not contracts:
            return None

        if contract_id is not None:
            contract = next((c for c in contracts if c["id"] == contract_id), None)
        else:
            contract = contracts[0]

        if not contract:
            return None

        yes_ask = contract.get("bestBuyYesCost")
        no_ask  = contract.get("bestBuyNoCost")
        yes_bid = contract.get("bestSellYesCost")
        no_bid  = contract.get("bestSellNoCost")

        # Skip illiquid contracts
        if yes_ask is None or no_ask is None:
            return None

        return {
            "yes": {
                "best_ask": float(yes_ask),
                "best_bid": float(yes_bid) if yes_bid else None,
            },
            "no": {
                "best_ask": float(no_ask),
                "best_bid": float(no_bid) if no_bid else None,
            },
            "last_price":    float(contract.get("lastTradePrice") or 0),
            "volume":        int(contract.get("volume") or 0),
            "contract_id":   contract["id"],
            "contract_name": contract.get("name", ""),
            "timestamp":     time.time(),
        }

    def get_normalized_book(self, market_id: int, contract_id: Optional[int] = None):
        """
        Return a NormalizedMarketBook for a PredictIt binary contract.

        Note: PredictIt only exposes best prices, so each side has at most
        one price level with an estimated quantity of 850 (the contract cap).
        """
        from clients.normalizer import NormalizedBook, NormalizedMarketBook, PriceLevel

        book = self.get_orderbook(market_id, contract_id)
        if not book:
            return None

        YES_MAX_CONTRACTS = 850   # PredictIt $850 cap
        NO_MAX_CONTRACTS  = 850

        yes_book = NormalizedBook(
            asks=[PriceLevel(book["yes"]["best_ask"], YES_MAX_CONTRACTS)]
                  if book["yes"]["best_ask"] else [],
            bids=[PriceLevel(book["yes"]["best_bid"], YES_MAX_CONTRACTS)]
                  if book["yes"]["best_bid"] else [],
            timestamp=book["timestamp"],
        )
        no_book = NormalizedBook(
            asks=[PriceLevel(book["no"]["best_ask"], NO_MAX_CONTRACTS)]
                  if book["no"]["best_ask"] else [],
            bids=[PriceLevel(book["no"]["best_bid"], NO_MAX_CONTRACTS)]
                  if book["no"]["best_bid"] else [],
            timestamp=book["timestamp"],
        )
        return NormalizedMarketBook(yes=yes_book, no=no_book)

    def to_normalized_market(self, market: dict, contract: dict):
        """
        Convert a (market, contract) pair to a NormalizedMarket.

        For binary markets, the contract name is appended to the market title
        so the matcher can correctly identify it.
        """
        from clients.normalizer import NormalizedMarket

        # Build a meaningful title
        mkt_name = market.get("name", "")
        ctr_name = contract.get("name", "")
        if len(market.get("contracts", [])) == 1:
            title = mkt_name   # binary — market title IS the question
        else:
            title = f"{mkt_name} — {ctr_name}"   # multi-contract: disambiguate

        market_id = f"PI-{market['id']}-{contract['id']}"

        return NormalizedMarket(
            platform=  "predictit",
            market_id= market_id,
            title=     title,
            close_time= contract.get("dateEnd", ""),
            yes_token=  str(contract["id"]),
            no_token=   str(contract["id"]),  # same ID — side specified at order time
            volume_24h= float(contract.get("volume") or 0),
            liquidity=  float(contract.get("volume") or 0),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Smoke test
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO)

    client = PredictItClient()

    print("=== PredictIt Binary Markets ===")
    binaries = client.get_binary_markets()
    for mkt, contract in binaries[:5]:
        print(f"\n  Market:    {mkt['name'][:60]}")
        print(f"  Contract:  {contract['name']}")
        print(f"  YES ask:   {contract.get('bestBuyYesCost')}  "
              f"YES bid: {contract.get('bestSellYesCost')}")
        print(f"  NO  ask:   {contract.get('bestBuyNoCost')}  "
              f"NO  bid: {contract.get('bestSellNoCost')}")
        print(f"  Last:      {contract.get('lastTradePrice')}  "
              f"Vol: {contract.get('volume')}")
