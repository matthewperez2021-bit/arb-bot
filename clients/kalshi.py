"""
kalshi.py — Kalshi Trade API v2 client.

Authentication: RSA-SHA256 signed headers.
Docs: https://trading-api.readme.io/reference/getting-started

Usage:
    from clients.kalshi import KalshiClient
    client = KalshiClient()
    markets = client.get_markets()
    book = client.get_orderbook("PRES-2024-DEM")
"""

import base64
import logging
import time
from typing import Optional
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.padding import PSS, MGF1

# Allow running this file directly: `python clients/kalshi.py`
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import (
    KALSHI_API_KEY_ID,
    KALSHI_BASE_URL,
    KALSHI_PRIVATE_KEY_PATH,
)

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Exceptions
# ─────────────────────────────────────────────────────────────────────────────

class KalshiAuthError(Exception):
    """Raised when authentication fails (bad key, bad signature)."""

class KalshiRateLimitError(Exception):
    """Raised when the API returns 429 Too Many Requests."""

class KalshiAPIError(Exception):
    """Raised on any non-2xx response not covered above."""
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}: {message}")


# ─────────────────────────────────────────────────────────────────────────────
# Client
# ─────────────────────────────────────────────────────────────────────────────

class KalshiClient:
    """
    Authenticated client for the Kalshi Trade API v2.

    Handles:
    - RSA-SHA256 request signing
    - Pagination (cursor-based)
    - Exponential backoff on rate limits and transient errors
    - Order book normalization to float probabilities
    """

    MAX_RETRIES = 5
    BASE_BACKOFF = 1.0      # seconds; doubles on each retry
    MAX_BACKOFF  = 30.0     # seconds; ceiling on retry wait

    def __init__(
        self,
        api_key_id: Optional[str] = None,
        private_key_path: Optional[str] = None,
    ):
        self.api_key_id = api_key_id or KALSHI_API_KEY_ID
        key_path = private_key_path or KALSHI_PRIVATE_KEY_PATH

        if not self.api_key_id:
            raise KalshiAuthError(
                "KALSHI_API_KEY_ID is not set. "
                "Add it to config/secrets.env."
            )

        with open(key_path, "rb") as f:
            self.private_key = serialization.load_pem_private_key(
                f.read(), password=None
            )

        self.session = requests.Session()
        log.info("KalshiClient initialized (key_id=%s)", self.api_key_id[:8] + "...")

    # ─────────────────────────────────────────────────────────────────
    # Auth helpers
    # ─────────────────────────────────────────────────────────────────

    def _sign(self, method: str, path: str) -> dict:
        """
        Build the three Kalshi auth headers.

        Header format:
            KALSHI-ACCESS-KEY:       your API key ID
            KALSHI-ACCESS-TIMESTAMP: current unix ms as string
            KALSHI-ACCESS-SIGNATURE: base64(RSA-SHA256(timestamp + METHOD + path))
        """
        timestamp_ms = str(int(time.time() * 1000))
        message = (timestamp_ms + method.upper() + path).encode("utf-8")

        signature = self.private_key.sign(
            message,
            PSS(mgf=MGF1(hashes.SHA256()), salt_length=PSS.MAX_LENGTH),
            hashes.SHA256(),
        )

        return {
            "KALSHI-ACCESS-KEY":       self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
            "Content-Type":            "application/json",
        }

    # ─────────────────────────────────────────────────────────────────
    # HTTP layer with retry / backoff
    # ─────────────────────────────────────────────────────────────────

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        body: Optional[dict] = None,
    ) -> dict:
        """
        Make an authenticated request. Retries on 429 and 5xx with
        exponential backoff. Raises on 4xx (except 429).
        """
        url      = KALSHI_BASE_URL + path
        # Kalshi signs the full URL path (e.g. /trade-api/v2/portfolio/balance)
        full_path = urlparse(KALSHI_BASE_URL).path + path
        headers  = self._sign(method, full_path)
        backoff  = self.BASE_BACKOFF

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                resp = self.session.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=body,
                    timeout=10,
                )
            except requests.exceptions.RequestException as exc:
                log.warning("Request error (attempt %d/%d): %s", attempt, self.MAX_RETRIES, exc)
                if attempt == self.MAX_RETRIES:
                    raise
                time.sleep(min(backoff, self.MAX_BACKOFF))
                backoff *= 2
                headers = self._sign(method, full_path)  # timestamp must be fresh
                continue

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code == 429:
                wait = min(backoff, self.MAX_BACKOFF)
                log.warning("Rate limited. Waiting %.1fs (attempt %d/%d)", wait, attempt, self.MAX_RETRIES)
                time.sleep(wait)
                backoff *= 2
                headers = self._sign(method, full_path)
                continue

            if resp.status_code in (500, 502, 503, 504):
                wait = min(backoff, self.MAX_BACKOFF)
                log.warning("Server error %d. Waiting %.1fs (attempt %d/%d)",
                            resp.status_code, wait, attempt, self.MAX_RETRIES)
                time.sleep(wait)
                backoff *= 2
                headers = self._sign(method, full_path)
                continue

            if resp.status_code == 401:
                raise KalshiAuthError(
                    f"Authentication failed: {resp.text}. "
                    f"Check your API key and private key."
                )

            raise KalshiAPIError(resp.status_code, resp.text)

        raise KalshiAPIError(0, f"Max retries ({self.MAX_RETRIES}) exceeded for {method} {path}")

    # ─────────────────────────────────────────────────────────────────
    # Markets
    # ─────────────────────────────────────────────────────────────────

    def get_markets(
        self,
        status: str = "open",
        limit: int = 200,
        cursor: Optional[str] = None,
        series_ticker: Optional[str] = None,
    ) -> dict:
        """
        Fetch a page of markets.

        Args:
            status: "open" | "closed" | "settled"
            limit: page size, max 200
            cursor: pagination cursor from a previous response
            series_ticker: filter to one series (e.g. "PRES-2024")

        Returns:
            {"markets": [...], "cursor": "..."}

        Market fields you care about for arb:
            ticker, title, category, status,
            yes_ask, yes_bid, no_ask, no_bid  (all in cents, 1–99)
            volume, open_interest, close_time
        """
        params: dict = {"status": status, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        if series_ticker:
            params["series_ticker"] = series_ticker

        return self._request("GET", "/markets", params=params)

    def get_all_open_markets(
        self,
        series_ticker: Optional[str] = None,
        max_pages: int = 10,
        ticker_prefix_exclude: Optional[list] = None,
    ) -> list:
        """
        Fetch open markets by following pagination cursors.

        Args:
            series_ticker:          filter to one series (e.g. "KXCPI")
            max_pages:              safety cap — stop after this many pages
                                    (200 markets/page, default 10 = 2000 markets max).
                                    Kalshi has thousands of low-value MVE sports
                                    markets; cap prevents runaway pagination.
            ticker_prefix_exclude:  skip markets whose ticker starts with any of
                                    these prefixes (e.g. ["KXMVE"] to drop
                                    multi-variant esports/sports event markets).

        Returns a flat list of market dicts.
        """
        markets = []
        cursor  = None
        pages   = 0
        exclude = [p.upper() for p in (ticker_prefix_exclude or [])]

        while pages < max_pages:
            resp   = self.get_markets(cursor=cursor, series_ticker=series_ticker)
            page   = resp.get("markets", [])

            # Apply ticker prefix filter
            if exclude:
                page = [m for m in page
                        if not any(m.get("ticker", "").upper().startswith(pfx)
                                   for pfx in exclude)]

            markets.extend(page)
            pages += 1

            cursor = resp.get("cursor")
            if not cursor or not resp.get("markets"):
                break

            log.debug("Fetched %d markets so far (page %d)...", len(markets), pages)

        log.info("get_all_open_markets: fetched %d total markets", len(markets))
        return markets

    def get_market(self, ticker: str) -> dict:
        """
        Fetch a single market by ticker.

        Returns the full market dict including current yes_ask, no_ask,
        status, result (if resolved), and close_time.
        """
        return self._request("GET", f"/markets/{ticker}")

    # ─────────────────────────────────────────────────────────────────
    # Order book
    # ─────────────────────────────────────────────────────────────────

    def get_orderbook(self, ticker: str, depth: int = 10) -> dict:
        """
        Fetch the order book for a market.

        Args:
            ticker: e.g. "PRES-2024-DEM"
            depth:  number of price levels to return per side (max 200)

        Returns normalized dict:
            {
                "yes": {
                    "asks": [{"price": 0.45, "quantity": 200}, ...],
                    "bids": [{"price": 0.44, "quantity": 500}, ...],
                },
                "no": {
                    "asks": [{"price": 0.56, "quantity": 150}, ...],
                    "bids": [{"price": 0.55, "quantity": 300}, ...],
                },
                "timestamp": 1714000000.0,
            }

        Prices are converted from cents (int) to float (0.0–1.0).
        Quantity is number of contracts.
        """
        raw = self._request("GET", f"/markets/{ticker}/orderbook", params={"depth": depth})
        book = raw.get("orderbook", raw)
        return self._normalize_orderbook(book)

    def _normalize_orderbook(self, raw: dict) -> dict:
        """
        Convert the raw Kalshi orderbook from cents (int) to float probabilities.

        Raw format:
            {"yes": [[price_cents, quantity], ...], "no": [[price_cents, quantity], ...]}
        or
            {"yes": {"asks": [[price, qty], ...], "bids": [...]}, "no": {...}}

        Output format (unified with Polymarket normalizer):
            {"yes": {"asks": [...], "bids": [...]}, "no": {...}, "timestamp": float}
        """
        result: dict = {"timestamp": time.time()}

        for side in ("yes", "no"):
            raw_side = raw.get(side, {})

            # Handle flat list format [[price_cents, qty], ...]
            if isinstance(raw_side, list):
                result[side] = {
                    "asks": [
                        {"price": level[0] / 100.0, "quantity": level[1]}
                        for level in sorted(raw_side, key=lambda x: x[0])
                        if level[1] > 0
                    ],
                    "bids": [],
                }
                continue

            # Handle dict format {"asks": [...], "bids": [...]}
            result[side] = {
                "asks": [
                    {"price": level[0] / 100.0, "quantity": level[1]}
                    for level in sorted(raw_side.get("asks", []), key=lambda x: x[0])
                    if level[1] > 0
                ],
                "bids": [
                    {"price": level[0] / 100.0, "quantity": level[1]}
                    for level in sorted(raw_side.get("bids", []), key=lambda x: x[0], reverse=True)
                    if level[1] > 0
                ],
            }

        return result

    def get_best_prices(self, ticker: str) -> dict:
        """
        Convenience method: return only the best ask/bid for YES and NO.

        Returns:
            {
                "yes_ask": 0.45,  "yes_ask_qty": 200,
                "yes_bid": 0.44,  "yes_bid_qty": 500,
                "no_ask":  0.56,  "no_ask_qty":  150,
                "no_bid":  0.55,  "no_bid_qty":  300,
                "timestamp": 1714000000.0,
            }
        Returns None values if a side has no liquidity.
        """
        book = self.get_orderbook(ticker, depth=1)
        result = {"timestamp": book["timestamp"]}

        for side in ("yes", "no"):
            asks = book[side]["asks"]
            bids = book[side]["bids"]
            result[f"{side}_ask"]     = asks[0]["price"]    if asks else None
            result[f"{side}_ask_qty"] = asks[0]["quantity"] if asks else 0
            result[f"{side}_bid"]     = bids[0]["price"]    if bids else None
            result[f"{side}_bid_qty"] = bids[0]["quantity"] if bids else 0

        return result

    # ─────────────────────────────────────────────────────────────────
    # Orders
    # ─────────────────────────────────────────────────────────────────

    def place_order(
        self,
        ticker: str,
        side: str,
        count: int,
        price_cents: int,
        order_type: str = "limit",
        expiry_secs: int = 30,
    ) -> dict:
        """
        Place a buy order on Kalshi.

        Args:
            ticker:      market ticker, e.g. "PRES-2024-DEM"
            side:        "yes" or "no"
            count:       number of contracts (integer)
            price_cents: limit price in cents (1–99). For YES orders this
                         is the YES price; the NO price is set to (100 - price_cents).
            order_type:  "limit" (default) or "market"
            expiry_secs: seconds until the order auto-cancels if unfilled

        Returns:
            The full order response dict including order_id and status.

        Raises:
            KalshiAPIError on rejection (e.g. insufficient funds, market closed).
        """
        if side not in ("yes", "no"):
            raise ValueError(f"side must be 'yes' or 'no', got '{side}'")
        if not (1 <= price_cents <= 99):
            raise ValueError(f"price_cents must be 1–99, got {price_cents}")
        if count < 1:
            raise ValueError(f"count must be >= 1, got {count}")

        yes_price = price_cents if side == "yes" else (100 - price_cents)
        no_price  = price_cents if side == "no"  else (100 - price_cents)

        body = {
            "action":        "buy",
            "type":          order_type,
            "ticker":        ticker,
            "side":          side,
            "count":         count,
            "yes_price":     yes_price,
            "no_price":      no_price,
            "expiration_ts": int(time.time()) + expiry_secs,
        }

        log.info(
            "Placing %s order: %s %s x%d @ %dc",
            order_type, ticker, side.upper(), count, price_cents
        )
        return self._request("POST", "/portfolio/orders", body=body)

    def cancel_order(self, order_id: str) -> dict:
        """
        Cancel an open order by its order_id.

        Returns the cancellation response or raises KalshiAPIError
        if the order is already filled or doesn't exist.
        """
        log.info("Cancelling order %s", order_id)
        return self._request("DELETE", f"/portfolio/orders/{order_id}")

    def get_order(self, order_id: str) -> dict:
        """
        Fetch the current status of an order.

        Useful for checking fill status after placement.
        Key fields: status ("resting"|"filled"|"canceled"), contracts_filled.
        """
        return self._request("GET", f"/portfolio/orders/{order_id}")

    def get_open_orders(self) -> list:
        """Fetch all currently open (resting) orders."""
        resp = self._request("GET", "/portfolio/orders", params={"status": "resting"})
        return resp.get("orders", [])

    # ─────────────────────────────────────────────────────────────────
    # Portfolio / Positions
    # ─────────────────────────────────────────────────────────────────

    def get_positions(self) -> list:
        """
        Fetch all current open positions.

        Each position includes:
            ticker, market_title, side, quantity,
            market_exposure (dollars at risk), realized_pnl, unrealized_pnl
        """
        resp = self._request("GET", "/portfolio/positions")
        return resp.get("market_positions", [])

    def get_balance(self) -> dict:
        """
        Fetch portfolio balance.

        Returns:
            {
                "balance":             12345,   # cents — your available cash
                "payout":              0,        # cents — pending payouts
                "fees_paid":           123,      # cents — total fees paid
                "total_deposited":     50000,    # cents
                "total_withdrawn":     0,
                "total_traded_volume": 8900,     # cents
            }

        Note: divide by 100 to get USD.
        """
        resp = self._request("GET", "/portfolio/balance")
        # Normalize to dollars
        return {
            k: v / 100.0 if isinstance(v, (int, float)) else v
            for k, v in resp.items()
        }

    def get_fills(self, ticker: Optional[str] = None) -> list:
        """
        Fetch recent order fills (executed trades).

        Args:
            ticker: optional filter to one market

        Returns list of fill dicts with price, count, side, created_time.
        """
        params = {}
        if ticker:
            params["ticker"] = ticker
        resp = self._request("GET", "/portfolio/fills", params=params)
        return resp.get("fills", [])

    # ─────────────────────────────────────────────────────────────────
    # Resolution helpers
    # ─────────────────────────────────────────────────────────────────

    def is_market_resolved(self, ticker: str) -> tuple:
        """
        Check if a market has resolved.

        Kalshi uses two terminal statuses:
          - "finalized" — outcome determined, ready for payout (most common)
          - "settled"   — payout completed
        Both indicate the market has a final result.

        Returns:
            (resolved: bool, result: str | None)
            result is "yes" or "no" if resolved, None otherwise.
        """
        mkt = self.get_market(ticker)
        market = mkt.get("market", mkt)
        status = (market.get("status") or "").lower()
        result = (market.get("result") or "").lower() or None
        # Also treat empty-string result as unresolved even if status is terminal
        is_resolved = status in ("settled", "finalized") and result in ("yes", "no")
        return is_resolved, result

    def get_settlement_value(self, ticker: str, side: str, contracts: int) -> float:
        """
        Calculate the USD payout for a settled position.

        Args:
            ticker:    market ticker
            side:      "yes" or "no" (the side you hold)
            contracts: number of contracts you hold

        Returns:
            USD payout (either contracts * $1.00 or $0.00)
        """
        resolved, result = self.is_market_resolved(ticker)
        if not resolved:
            return 0.0
        return float(contracts) if result == side else 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Quick smoke test — run directly to verify auth is working
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO)

    print("Initializing KalshiClient...")
    client = KalshiClient()

    print("\n--- Balance ---")
    balance = client.get_balance()
    print(json.dumps(balance, indent=2))

    print("\n--- First 3 open markets ---")
    resp = client.get_markets(limit=3)
    for mkt in resp.get("markets", []):
        print(f"  {mkt['ticker']}: YES ask={mkt.get('yes_ask')}c  NO ask={mkt.get('no_ask')}c  closes={mkt.get('close_time','?')[:10]}")

    if resp.get("markets"):
        ticker = resp["markets"][0]["ticker"]
        print(f"\n--- Order book for {ticker} ---")
        book = client.get_orderbook(ticker, depth=3)
        print("YES asks:", book["yes"]["asks"][:3])
        print("NO asks: ", book["no"]["asks"][:3])
        print("Timestamp age: {:.1f}s ago".format(time.time() - book["timestamp"]))
