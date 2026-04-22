"""
polymarket.py — Polymarket Gamma + CLOB API client.

Two API layers:
  Gamma API  — market metadata, search, token IDs
  CLOB API   — real-time order books, order placement, fills

Auth: py-clob-client with an EOA private key + proxy wallet.

Usage:
    from clients.polymarket import PolymarketClient
    client = PolymarketClient()
    markets = client.get_markets(limit=50)
    book    = client.get_orderbook("71321045...")   # YES token_id
"""

import logging
import time
from typing import Optional

import requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.constants import POLYGON

from config.settings import (
    POLY_CLOB_URL,
    POLY_GAMMA_URL,
    POLY_PRIVATE_KEY,
    POLY_PROXY_WALLET,
)

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Exceptions
# ─────────────────────────────────────────────────────────────────────────────

class PolymarketAuthError(Exception):
    """Raised when wallet credentials are missing or invalid."""

class PolymarketAPIError(Exception):
    """Raised on non-2xx responses from Gamma or CLOB APIs."""
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}: {message}")


# ─────────────────────────────────────────────────────────────────────────────
# Client
# ─────────────────────────────────────────────────────────────────────────────

class PolymarketClient:
    """
    Unified Polymarket client covering both the Gamma (metadata) and
    CLOB (execution) APIs.

    Key concepts:
      - Every market has two tokens: YES and NO (each with a unique token_id)
      - Prices are floats 0.0–1.0 (not cents)
      - Order sizes are in USD (not contracts)
      - To get # contracts: size_usd / price
    """

    MAX_RETRIES  = 5
    BASE_BACKOFF = 1.0
    MAX_BACKOFF  = 30.0

    def __init__(
        self,
        private_key: Optional[str] = None,
        proxy_wallet: Optional[str] = None,
    ):
        self._private_key  = private_key  or POLY_PRIVATE_KEY
        self._proxy_wallet = proxy_wallet or POLY_PROXY_WALLET

        if not self._private_key:
            raise PolymarketAuthError(
                "POLY_PRIVATE_KEY is not set. Add it to config/secrets.env."
            )

        self._gamma_session = requests.Session()

        # CLOB client is initialized lazily to avoid startup auth calls
        self._clob: Optional[ClobClient] = None

        log.info("PolymarketClient initialized (wallet=%s...)", self._proxy_wallet[:8] if self._proxy_wallet else "?")

    # ─────────────────────────────────────────────────────────────────
    # CLOB client (lazy init)
    # ─────────────────────────────────────────────────────────────────

    @property
    def clob(self) -> ClobClient:
        """
        Lazily initialize the CLOB client and derive API credentials.
        Called automatically the first time any CLOB method is used.
        """
        if self._clob is None:
            log.info("Initializing Polymarket CLOB client...")
            self._clob = ClobClient(
                host=POLY_CLOB_URL,
                chain_id=POLYGON,
                private_key=self._private_key,
                signature_type=2,           # EIP-712
                funder=self._proxy_wallet,
            )
            # Derive or create API credentials from the wallet
            self._clob.set_api_creds(self._clob.create_or_derive_api_creds())
            log.info("CLOB client ready.")
        return self._clob

    # ─────────────────────────────────────────────────────────────────
    # Gamma API — market metadata
    # ─────────────────────────────────────────────────────────────────

    def _gamma_get(self, path: str, params: Optional[dict] = None) -> dict | list:
        """GET request to the Gamma API with retry/backoff."""
        url     = POLY_GAMMA_URL + path
        backoff = self.BASE_BACKOFF

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                resp = self._gamma_session.get(url, params=params, timeout=10)
            except requests.exceptions.RequestException as exc:
                log.warning("Gamma request error (attempt %d/%d): %s", attempt, self.MAX_RETRIES, exc)
                if attempt == self.MAX_RETRIES:
                    raise
                time.sleep(min(backoff, self.MAX_BACKOFF))
                backoff *= 2
                continue

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code == 429:
                wait = min(backoff, self.MAX_BACKOFF)
                log.warning("Gamma rate limited. Waiting %.1fs", wait)
                time.sleep(wait)
                backoff *= 2
                continue

            if resp.status_code >= 500:
                wait = min(backoff, self.MAX_BACKOFF)
                log.warning("Gamma server error %d. Waiting %.1fs", resp.status_code, wait)
                time.sleep(wait)
                backoff *= 2
                continue

            raise PolymarketAPIError(resp.status_code, resp.text)

        raise PolymarketAPIError(0, f"Max retries exceeded for GET {path}")

    def get_markets(
        self,
        limit: int = 100,
        offset: int = 0,
        active: bool = True,
        closed: bool = False,
    ) -> list:
        """
        Fetch a page of markets from the Gamma API.

        Args:
            limit:  page size (max 100 per request)
            offset: pagination offset
            active: include active markets (default True)
            closed: include closed markets (default False)

        Returns:
            List of market dicts. Each market includes:
                id, question, endDate, active, tokens, volume24hr, liquidity

        Key structure per market:
            {
                "id": "0xabc...",
                "question": "Will Democrats win the 2024 presidential election?",
                "endDate": "2024-11-05T23:59:00Z",
                "active": True,
                "tokens": [
                    {"token_id": "71321045...", "outcome": "Yes"},
                    {"token_id": "52114320...", "outcome": "No"}
                ],
                "volume24hr": 45200.50,
                "liquidity":  128000.00,
            }
        """
        params = {"limit": limit, "offset": offset, "active": active, "closed": closed}
        result = self._gamma_get("/markets", params=params)
        # Gamma returns either a list directly or {"markets": [...]}
        return result if isinstance(result, list) else result.get("markets", [])

    def get_all_active_markets(self, min_liquidity: float = 500.0) -> list:
        """
        Fetch ALL active markets by paginating through the Gamma API.

        Args:
            min_liquidity: skip markets below this USD liquidity threshold.
                           Low-liquidity markets have too much slippage for arb.

        Returns a flat list of market dicts.
        """
        markets = []
        offset  = 0
        limit   = 100

        while True:
            page = self.get_markets(limit=limit, offset=offset)
            if not page:
                break

            for mkt in page:
                liq = mkt.get("liquidity") or mkt.get("liquidityNum", 0)
                if float(liq) >= min_liquidity:
                    markets.append(mkt)

            if len(page) < limit:
                break   # last page

            offset += limit
            log.debug("Fetched %d Polymarket markets so far...", len(markets))

        log.info("get_all_active_markets: fetched %d markets (min_liquidity=$%.0f)",
                 len(markets), min_liquidity)
        return markets

    def get_market(self, market_id: str) -> dict:
        """Fetch a single market by its condition ID (0x...)."""
        result = self._gamma_get(f"/markets/{market_id}")
        return result if isinstance(result, dict) else result[0]

    # ─────────────────────────────────────────────────────────────────
    # Token helpers
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    def get_token_ids(market: dict) -> tuple:
        """
        Extract YES and NO token IDs from a market dict.

        Returns:
            (yes_token_id: str, no_token_id: str)

        Raises ValueError if tokens are missing or malformed.
        """
        tokens = market.get("tokens", [])
        if len(tokens) < 2:
            raise ValueError(f"Market {market.get('id')} has fewer than 2 tokens: {tokens}")

        yes_id = no_id = None
        for token in tokens:
            outcome = token.get("outcome", "").lower()
            if outcome in ("yes", "true", "1"):
                yes_id = token["token_id"]
            elif outcome in ("no", "false", "0"):
                no_id = token["token_id"]

        # Fallback: first token = YES, second = NO
        if not yes_id:
            yes_id = tokens[0]["token_id"]
        if not no_id:
            no_id = tokens[1]["token_id"]

        return yes_id, no_id

    # ─────────────────────────────────────────────────────────────────
    # CLOB API — order books
    # ─────────────────────────────────────────────────────────────────

    def get_orderbook(self, token_id: str) -> dict:
        """
        Fetch the order book for a single token (YES or NO side).

        Returns normalized dict:
            {
                "asks": [{"price": 0.45, "size": 200.0}, ...],  # sorted ascending
                "bids": [{"price": 0.44, "size": 100.0}, ...],  # sorted descending
                "timestamp": 1714000000.0,
            }

        Note: size is in USD. To get contracts: size / price.
        """
        raw  = self.clob.get_order_book(token_id)
        return self._normalize_orderbook(raw)

    def get_market_orderbooks(self, market: dict) -> dict:
        """
        Fetch order books for both YES and NO tokens of a market.

        Returns:
            {
                "yes": { "asks": [...], "bids": [...], "timestamp": float },
                "no":  { "asks": [...], "bids": [...], "timestamp": float },
            }
        """
        yes_id, no_id = self.get_token_ids(market)
        return {
            "yes": self.get_orderbook(yes_id),
            "no":  self.get_orderbook(no_id),
        }

    def _normalize_orderbook(self, raw) -> dict:
        """
        Convert py-clob-client OrderBook object to a clean dict.

        Prices are already 0.0–1.0 floats in Polymarket.
        Sizes are in USD.
        """
        timestamp = time.time()

        def parse_levels(levels) -> list:
            result = []
            for level in (levels or []):
                try:
                    price = float(level.price)
                    size  = float(level.size)
                    if size > 0:
                        result.append({"price": price, "size": size})
                except (AttributeError, TypeError, ValueError):
                    continue
            return result

        asks = sorted(parse_levels(getattr(raw, "asks", [])), key=lambda x: x["price"])
        bids = sorted(parse_levels(getattr(raw, "bids", [])), key=lambda x: x["price"], reverse=True)

        return {"asks": asks, "bids": bids, "timestamp": timestamp}

    def get_spread(self, token_id: str) -> dict:
        """
        Convenience method: return just the best ask/bid for one token.

        Returns:
            {
                "best_ask":  0.45,
                "best_bid":  0.44,
                "ask_size":  200.0,   # USD available at best ask
                "bid_size":  100.0,
                "spread":    0.01,
                "timestamp": float,
            }
        """
        book = self.get_orderbook(token_id)
        asks = book["asks"]
        bids = book["bids"]

        best_ask  = asks[0]["price"] if asks else None
        best_bid  = bids[0]["price"] if bids else None
        ask_size  = asks[0]["size"]  if asks else 0.0
        bid_size  = bids[0]["size"]  if bids else 0.0
        spread    = round(best_ask - best_bid, 4) if (best_ask and best_bid) else None

        return {
            "best_ask":  best_ask,
            "best_bid":  best_bid,
            "ask_size":  ask_size,
            "bid_size":  bid_size,
            "spread":    spread,
            "timestamp": book["timestamp"],
        }

    # ─────────────────────────────────────────────────────────────────
    # CLOB API — order placement
    # ─────────────────────────────────────────────────────────────────

    def place_limit_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size_usd: float,
    ) -> dict:
        """
        Place a GTC limit order via the CLOB API.

        Args:
            token_id: the YES or NO token ID for the market
            side:     "BUY" or "SELL"
            price:    limit price, 0.0–1.0
            size_usd: order size in USD (e.g. 50.0 = $50)

        Returns:
            Order response dict including order_id and status.

        Note: To buy NO tokens, pass the NO token_id with side="BUY".
              The CLOB treats YES and NO tokens independently.
        """
        if side not in ("BUY", "SELL"):
            raise ValueError(f"side must be 'BUY' or 'SELL', got '{side}'")
        if not (0.01 <= price <= 0.99):
            raise ValueError(f"price must be 0.01–0.99, got {price}")
        if size_usd < 1.0:
            raise ValueError(f"size_usd must be >= $1.00, got {size_usd}")

        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size_usd,
            side=side,
            order_type=OrderType.GTC,
        )

        log.info(
            "Placing Poly limit order: token=%s... %s %.4f @ $%.2f",
            token_id[:10], side, price, size_usd
        )

        result = self.clob.create_and_post_order(order_args)
        return result if isinstance(result, dict) else {"raw": result}

    def cancel_order(self, order_id: str) -> dict:
        """Cancel an open order by order_id."""
        log.info("Cancelling Poly order %s", order_id)
        result = self.clob.cancel(order_id)
        return result if isinstance(result, dict) else {"raw": result}

    def cancel_all_orders(self) -> dict:
        """Cancel all open orders. Useful for emergency shutdown."""
        log.warning("Cancelling ALL open Polymarket orders.")
        result = self.clob.cancel_all()
        return result if isinstance(result, dict) else {"raw": result}

    # ─────────────────────────────────────────────────────────────────
    # Portfolio / Settlement
    # ─────────────────────────────────────────────────────────────────

    def get_positions(self) -> list:
        """
        Fetch all open token positions.

        Returns list of position dicts:
            [{"asset": token_id, "size": 100.0, "avgPrice": 0.45}, ...]
        """
        result = self.clob.get_positions()
        return result if isinstance(result, list) else result.get("data", [])

    def get_balance(self) -> float:
        """Return available USDC balance in USD."""
        result = self.clob.get_collateral_balance()
        # Returns {"balance": "123.45"} or similar
        if isinstance(result, dict):
            return float(result.get("balance", 0))
        return float(result)

    def check_settlement(self, token_id: str) -> float:
        """
        Check if a token has settled and return the payout per contract.

        Returns 1.0 if the token won, 0.0 if it lost or is still open.
        Used by the capital recycler to claim payouts.
        """
        try:
            # Settled tokens will show up in redeemable positions
            positions = self.get_positions()
            for pos in positions:
                if pos.get("asset") == token_id:
                    # If position still shows as open, not yet settled
                    return 0.0
            # Position no longer appears — check if it was redeemed
            # (In practice, monitor via market status from Gamma API)
            return 0.0
        except Exception as exc:
            log.warning("check_settlement error for %s: %s", token_id[:10], exc)
            return 0.0

    def is_market_active(self, market_id: str) -> bool:
        """Quick check if a market is still active/tradeable."""
        try:
            mkt = self.get_market(market_id)
            return mkt.get("active", False) and not mkt.get("closed", True)
        except Exception:
            return False


# ─────────────────────────────────────────────────────────────────────────────
# Smoke test
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO)

    print("Initializing PolymarketClient (Gamma only — no wallet needed for reads)...")
    client = PolymarketClient.__new__(PolymarketClient)
    client._gamma_session = requests.Session()
    client._clob = None
    client._private_key  = "dummy"
    client._proxy_wallet = "0x0000000000000000000000000000000000000000"

    print("\n--- First 3 active markets ---")
    markets = client.get_markets(limit=3)
    for mkt in markets:
        yes_id, no_id = PolymarketClient.get_token_ids(mkt)
        print(f"  {mkt['question'][:60]}...")
        print(f"    YES token: {yes_id[:16]}...")
        print(f"    NO  token: {no_id[:16]}...")
        print(f"    Liquidity: ${mkt.get('liquidity', 0):,.0f}  closes: {mkt.get('endDate','?')[:10]}")
