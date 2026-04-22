"""
settings.py — Central configuration for the arb bot.
All tunable constants live here. Never hardcode these elsewhere.
"""

import os
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "secrets.env"))

# ─────────────────────────────────────────────────────────────────────
# PLATFORM CREDENTIALS (loaded from secrets.env)
# ─────────────────────────────────────────────────────────────────────
KALSHI_API_KEY_ID       = os.getenv("KALSHI_API_KEY_ID", "")
KALSHI_PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", "config/kalshi_private.pem")

POLY_PRIVATE_KEY        = os.getenv("POLY_PRIVATE_KEY", "")
POLY_PROXY_WALLET       = os.getenv("POLY_PROXY_WALLET", "")

TELEGRAM_BOT_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID        = os.getenv("TELEGRAM_CHAT_ID", "")

ANTHROPIC_API_KEY       = os.getenv("ANTHROPIC_API_KEY", "")

# ─────────────────────────────────────────────────────────────────────
# PLATFORM API ENDPOINTS
# ─────────────────────────────────────────────────────────────────────
KALSHI_BASE_URL     = "https://api.elections.kalshi.com/trade-api/v2"
POLY_GAMMA_URL      = "https://gamma-api.polymarket.com"
POLY_CLOB_URL       = "https://clob.polymarket.com"

# ─────────────────────────────────────────────────────────────────────
# FEE STRUCTURE  (update if platforms change their fee schedule)
# ─────────────────────────────────────────────────────────────────────
KALSHI_TAKER_FEE    = 0.07      # 7% of contract price on taker orders
POLY_TAKER_FEE      = 0.02      # 2% on taker orders

# ─────────────────────────────────────────────────────────────────────
# ARB DETECTION THRESHOLDS
# ─────────────────────────────────────────────────────────────────────
MIN_NET_EDGE_PAPER  = 0.015     # 1.5% — minimum net profit in paper mode (Phase 3)
MIN_NET_EDGE_LIVE   = 0.020     # 2.0% — stricter threshold for live trading (Phase 4)
MAX_SLIPPAGE_PCT    = 0.005     # 0.5% — reject trade if slippage exceeds this
MAX_BOOK_AGE_SECS   = 5         # Reject order book data older than 5 seconds
MAX_DURATION_DAYS   = 14        # Only trade markets resolving within 14 days

# ─────────────────────────────────────────────────────────────────────
# MARKET MATCHING THRESHOLDS
# ─────────────────────────────────────────────────────────────────────
MATCH_CANDIDATE_THRESHOLD   = 0.55  # Min combined similarity score to enter candidate list
MATCH_TRADE_THRESHOLD       = 0.70  # Min match score to be eligible for trading
MATCH_LIVE_THRESHOLD        = 0.75  # Stricter threshold for live trading
LLM_CONFIDENCE_THRESHOLD    = 0.80  # Min Claude confidence to accept a match
MATCH_CACHE_TTL_SECS        = 3600  # 1 hour — how long to cache verified matches
DATE_TOLERANCE_DAYS         = 3     # +/- days for close date compatibility check

# ─────────────────────────────────────────────────────────────────────
# RISK MANAGEMENT
# ─────────────────────────────────────────────────────────────────────
STARTING_CAPITAL_USD        = 1000.0    # Starting bankroll — update as you scale

# Phase 4 initial limits (conservative)
MAX_SINGLE_POSITION_USD     = 50.0      # Max $ per single arb trade
MAX_TOTAL_DEPLOYED_USD      = 200.0     # Max $ across all open positions simultaneously
MAX_NAKED_CONTRACTS         = 3         # Max unhedged contracts before forced close
NAKED_EXPOSURE_TIMEOUT_SECS = 60        # Auto-close naked leg after this many seconds
KELLY_FRACTION              = 0.5       # Half-Kelly sizing

# Phase 5 scaled limits (update after 100 live trades with >=90% win rate)
# MAX_SINGLE_POSITION_USD   = 200.0
# MAX_TOTAL_DEPLOYED_USD    = 2000.0

# ─────────────────────────────────────────────────────────────────────
# EXECUTION
# ─────────────────────────────────────────────────────────────────────
SCAN_INTERVAL_SECS          = 45        # How often to scan all matched pairs
ORDER_EXPIRY_SECS           = 30        # Kalshi order expiration_ts offset
SLIPPAGE_TOLERANCE          = 0.005     # Accept fills up to 0.5% above detected price
LATE_FILL_PREMIUM           = 0.05      # Pay up to 5% more to fill a missing naked leg

# ─────────────────────────────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────────────────────────────
SQLITE_DB_PATH      = "data/arb_positions.db"
POSTGRES_URL        = os.getenv("POSTGRES_URL", "")    # Set in secrets.env for Phase 5

# ─────────────────────────────────────────────────────────────────────
# PATHS
# ─────────────────────────────────────────────────────────────────────
MATCH_CACHE_PATH    = "data/match_cache.json"
SNAPSHOT_DIR        = "data/snapshots/"
LABELED_PAIRS_PATH  = "tests/labeled_pairs.json"

# ─────────────────────────────────────────────────────────────────────
# LLM
# ─────────────────────────────────────────────────────────────────────
LLM_MODEL           = "claude-opus-4-6"     # Model used for market verification
LLM_MAX_TOKENS      = 300
