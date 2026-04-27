"""
normalizer.py — Unified order book and market schema for the arb bot.

Both Kalshi and Polymarket use different formats, units, and field names.
This module converts both into a single NormalizedMarket and NormalizedBook
schema that every downstream module (detector, executor, tracker) uses.

Key conversions:
  Kalshi  prices: cents (int 1-99)    → float 0.01-0.99
  Poly    prices: already float       → no change
  Kalshi  size:   contracts (int)     → kept as contracts
  Poly    size:   USD (float)         → converted to contracts (size / price)
"""

import time
from dataclasses import dataclass, field
from typing import Optional

from config.settings import MAX_BOOK_AGE_SECS


# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PriceLevel:
    """A single price level in an order book."""
    price: float        # 0.0–1.0 probability
    quantity: float     # number of contracts available at this price


@dataclass
class NormalizedBook:
    """
    Unified order book for one side (YES or NO) of a binary market.

    Asks are sorted ascending (cheapest first — what you pay to buy).
    Bids are sorted descending (highest first — what you receive to sell).
    """
    asks: list[PriceLevel] = field(default_factory=list)
    bids: list[PriceLevel] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask_qty(self) -> float:
        return self.asks[0].quantity if self.asks else 0.0

    @property
    def best_bid_qty(self) -> float:
        return self.bids[0].quantity if self.bids else 0.0

    @property
    def spread(self) -> Optional[float]:
        if self.best_ask is not None and self.best_bid is not None:
            return round(self.best_ask - self.best_bid, 4)
        return None

    @property
    def is_fresh(self) -> bool:
        """Returns True if the book data is within MAX_BOOK_AGE_SECS."""
        return (time.time() - self.timestamp) < MAX_BOOK_AGE_SECS

    def has_liquidity(self, min_contracts: float = 1.0) -> bool:
        """Returns True if there is at least min_contracts available on the ask side."""
        return bool(self.asks) and self.best_ask_qty >= min_contracts


@dataclass
class NormalizedMarketBook:
    """
    Combined YES + NO order books for one market on one platform.
    This is what the arb detector consumes.
    """
    yes: NormalizedBook
    no:  NormalizedBook

    @property
    def is_fresh(self) -> bool:
        return self.yes.is_fresh and self.no.is_fresh

    @property
    def has_liquidity(self) -> bool:
        return self.yes.has_liquidity() and self.no.has_liquidity()


@dataclass
class NormalizedMarket:
    """
    Unified market metadata, independent of platform format.
    Created from either a Kalshi market dict or a Polymarket market dict.
    """
    platform:    str        # "kalshi" or "polymarket"
    market_id:   str        # ticker (Kalshi) or condition ID (Polymarket)
    title:       str        # human-readable question
    close_time:  str        # ISO 8601 UTC string
    yes_token:   str        # YES token/ticker identifier
    no_token:    str        # NO token/ticker identifier
    category:    str = ""
    volume_24h:  float = 0.0
    liquidity:   float = 0.0
    fetched_at:  float = field(default_factory=time.time)
    extra:       dict = field(default_factory=dict)   # platform-specific raw fields


# ─────────────────────────────────────────────────────────────────────────────
# Kalshi normalizers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_price(val) -> Optional[float]:
    """Parse a Kalshi price string like '0.4600' → 0.46, or None if missing/zero."""
    try:
        f = float(val)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def normalize_kalshi_market(raw: dict) -> NormalizedMarket:
    """
    Convert a raw Kalshi market dict → NormalizedMarket.

    Input shape:
        {
            "ticker":     "PRES-2024-DEM",
            "title":      "Will the Democrat win the 2024 presidential election?",
            "category":   "Politics",
            "close_time": "2024-11-05T23:59:00Z",
            "yes_ask":    45,    # cents
            "no_ask":     56,
            "volume":     125000,
            ...
        }
    """
    ticker = raw["ticker"]
    return NormalizedMarket(
        platform=   "kalshi",
        market_id=  ticker,
        title=      raw.get("title", ""),
        close_time= raw.get("close_time", ""),
        yes_token=  ticker,     # Kalshi uses ticker for both sides; side specified at order time
        no_token=   ticker,
        category=   raw.get("category", ""),
        volume_24h= float(raw.get("volume", 0)),
        liquidity=  float(raw.get("open_interest", 0)),
        extra={
            "floor_strike": raw.get("floor_strike"),
            "cap_strike":   raw.get("cap_strike"),
            "strike_type":  raw.get("strike_type"),
            "subtitle":     raw.get("subtitle", ""),
            "rules_primary": raw.get("rules_primary", ""),
            "yes_ask": _parse_price(raw.get("yes_ask_dollars")),
            "no_ask":  _parse_price(raw.get("no_ask_dollars")),
            "yes_bid": _parse_price(raw.get("yes_bid_dollars")),
            "no_bid":  _parse_price(raw.get("no_bid_dollars")),
        },
    )


def normalize_kalshi_book(raw: dict) -> NormalizedMarketBook:
    """
    Convert a KalshiClient.get_orderbook() result → NormalizedMarketBook.

    Input shape (already normalized by KalshiClient):
        {
            "yes": {"asks": [{"price": 0.45, "quantity": 200}], "bids": [...]},
            "no":  {"asks": [{"price": 0.56, "quantity": 150}], "bids": [...]},
            "timestamp": 1714000000.0,
        }
    """
    ts = raw.get("timestamp", time.time())

    def parse_side(side_data: dict) -> NormalizedBook:
        asks = [
            PriceLevel(price=lvl["price"], quantity=float(lvl["quantity"]))
            for lvl in side_data.get("asks", [])
            if lvl["quantity"] > 0
        ]
        bids = [
            PriceLevel(price=lvl["price"], quantity=float(lvl["quantity"]))
            for lvl in side_data.get("bids", [])
            if lvl["quantity"] > 0
        ]
        return NormalizedBook(asks=asks, bids=bids, timestamp=ts)

    return NormalizedMarketBook(
        yes=parse_side(raw.get("yes", {})),
        no= parse_side(raw.get("no",  {})),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Polymarket normalizers
# ─────────────────────────────────────────────────────────────────────────────

def normalize_poly_market(raw: dict) -> NormalizedMarket:
    """
    Convert a raw Polymarket Gamma API market dict → NormalizedMarket.

    Input shape:
        {
            "id":       "0xabc...",
            "question": "Will Democrats win the 2024 presidential election?",
            "endDate":  "2024-11-05T23:59:00Z",
            "active":   True,
            "tokens":   [
                {"token_id": "71321045...", "outcome": "Yes"},
                {"token_id": "52114320...", "outcome": "No"}
            ],
            "volume24hr": 45200.50,
            "liquidity":  128000.00,
        }
    """
    tokens  = raw.get("tokens", [])
    yes_id  = no_id = ""
    for tok in tokens:
        outcome = tok.get("outcome", "").lower()
        if outcome in ("yes", "true"):
            yes_id = tok["token_id"]
        elif outcome in ("no", "false"):
            no_id = tok["token_id"]

    # Fallback ordering if outcome labels are missing
    if not yes_id and len(tokens) >= 1:
        yes_id = tokens[0]["token_id"]
    if not no_id and len(tokens) >= 2:
        no_id = tokens[1]["token_id"]

    return NormalizedMarket(
        platform=   "polymarket",
        market_id=  raw.get("id", ""),
        title=      raw.get("question", ""),
        close_time= raw.get("endDate", ""),
        yes_token=  yes_id,
        no_token=   no_id,
        category=   raw.get("category", ""),
        volume_24h= float(raw.get("volume24hr") or 0),
        liquidity=  float(raw.get("liquidity")  or 0),
    )


def normalize_poly_book(raw: dict, price_per_contract: float = 1.0) -> NormalizedMarketBook:
    """
    Convert a PolymarketClient.get_market_orderbooks() result → NormalizedMarketBook.

    Polymarket CLOB sizes are in USD. We convert to contracts:
        contracts = size_usd / price

    Input shape:
        {
            "yes": {"asks": [{"price": 0.45, "size": 90.0}], "bids": [...], "timestamp": ...},
            "no":  {"asks": [{"price": 0.56, "size": 84.0}], "bids": [...], "timestamp": ...},
        }
    """
    def parse_side(side_data: dict) -> NormalizedBook:
        ts = side_data.get("timestamp", time.time())

        asks = []
        for lvl in side_data.get("asks", []):
            price = float(lvl["price"])
            size_usd = float(lvl["size"])
            if price > 0 and size_usd > 0:
                contracts = size_usd / price  # convert USD → contracts
                asks.append(PriceLevel(price=price, quantity=contracts))

        bids = []
        for lvl in side_data.get("bids", []):
            price = float(lvl["price"])
            size_usd = float(lvl["size"])
            if price > 0 and size_usd > 0:
                contracts = size_usd / price
                bids.append(PriceLevel(price=price, quantity=contracts))

        return NormalizedBook(asks=asks, bids=bids, timestamp=ts)

    return NormalizedMarketBook(
        yes=parse_side(raw.get("yes", {})),
        no= parse_side(raw.get("no",  {})),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────────────────────────

def validate_book_pair(kalshi_book: NormalizedMarketBook,
                       poly_book:   NormalizedMarketBook) -> tuple[bool, str]:
    """
    Check that both books are usable before attempting arb detection.

    Returns (valid: bool, reason: str).
    """
    if not kalshi_book.is_fresh:
        return False, "Kalshi order book is stale (> {}s old)".format(MAX_BOOK_AGE_SECS)
    if not poly_book.is_fresh:
        return False, "Polymarket order book is stale (> {}s old)".format(MAX_BOOK_AGE_SECS)
    if not kalshi_book.has_liquidity:
        return False, "Kalshi order book has no liquidity"
    if not poly_book.has_liquidity:
        return False, "Polymarket order book has no liquidity"
    return True, "OK"


# ─────────────────────────────────────────────────────────────────────────────
# Quick self-test
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Simulate a Kalshi book (prices in cents already converted by KalshiClient)
    fake_kalshi_raw = {
        "yes": {
            "asks": [{"price": 0.45, "quantity": 200}, {"price": 0.46, "quantity": 800}],
            "bids": [{"price": 0.44, "quantity": 500}],
        },
        "no": {
            "asks": [{"price": 0.56, "quantity": 150}, {"price": 0.57, "quantity": 600}],
            "bids": [{"price": 0.55, "quantity": 300}],
        },
        "timestamp": time.time(),
    }

    # Simulate a Polymarket book (sizes in USD)
    fake_poly_raw = {
        "yes": {
            "asks": [{"price": 0.47, "size": 94.0}, {"price": 0.48, "size": 192.0}],
            "bids": [{"price": 0.46, "size": 46.0}],
            "timestamp": time.time(),
        },
        "no": {
            "asks": [{"price": 0.54, "size": 81.0}, {"price": 0.55, "size": 220.0}],
            "bids": [{"price": 0.53, "size": 53.0}],
            "timestamp": time.time(),
        },
    }

    kalshi_book = normalize_kalshi_book(fake_kalshi_raw)
    poly_book   = normalize_poly_book(fake_poly_raw)

    print("=== Kalshi Book ===")
    print(f"  YES best ask: {kalshi_book.yes.best_ask:.2f}  qty: {kalshi_book.yes.best_ask_qty}")
    print(f"  NO  best ask: {kalshi_book.no.best_ask:.2f}  qty: {kalshi_book.no.best_ask_qty}")
    print(f"  Fresh: {kalshi_book.is_fresh}  Liquid: {kalshi_book.has_liquidity}")

    print("\n=== Polymarket Book ===")
    print(f"  YES best ask: {poly_book.yes.best_ask:.2f}  qty: {poly_book.yes.best_ask_qty:.1f} contracts")
    print(f"  NO  best ask: {poly_book.no.best_ask:.2f}  qty: {poly_book.no.best_ask_qty:.1f} contracts")

    print("\n=== Validation ===")
    valid, reason = validate_book_pair(kalshi_book, poly_book)
    print(f"  Valid: {valid}  Reason: {reason}")

    # Arb check: K-YES + P-NO = 0.45 + 0.54 = 0.99 → only 1% gross, below 1.5% threshold
    # K-NO  + P-YES = 0.56 + 0.47 = 1.03 → no arb (costs > $1.00)
    print(f"\n=== Arb Check ===")
    print(f"  K-YES + P-NO  = {kalshi_book.yes.best_ask + poly_book.no.best_ask:.2f}  (arb if < 1.00)")
    print(f"  K-NO  + P-YES = {kalshi_book.no.best_ask + poly_book.yes.best_ask:.2f}  (arb if < 1.00)")
