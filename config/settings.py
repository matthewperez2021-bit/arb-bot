"""
settings.py — Central configuration for the arb bot.
All tunable constants live here. Never hardcode these elsewhere.
"""

import os
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "secrets.env"), override=True)

# ─────────────────────────────────────────────────────────────────────
# PLATFORM CREDENTIALS (loaded from secrets.env)
# ─────────────────────────────────────────────────────────────────────
KALSHI_API_KEY_ID       = os.getenv("KALSHI_API_KEY_ID", "")
KALSHI_PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", "config/kalshi_private.pem")

# Polymarket — geo-blocked for US users. Kept for reference; replaced by PredictIt.
POLY_PRIVATE_KEY        = os.getenv("POLY_PRIVATE_KEY", "")
POLY_PROXY_WALLET       = os.getenv("POLY_PROXY_WALLET", "")

# PredictIt — CFTC no-action letter, US-legal, political markets
# No API key needed for reads. Account required for trading (manual execution).
PREDICTIT_ACCOUNT_EMAIL = os.getenv("PREDICTIT_ACCOUNT_EMAIL", "")
PREDICTIT_ACCOUNT_PASS  = os.getenv("PREDICTIT_ACCOUNT_PASS", "")

# The Odds API — aggregates DraftKings, FanDuel, BetMGM, Caesars etc.
# Free tier: 500 req/month. Get key at: https://the-odds-api.com
ODDS_API_KEY            = os.getenv("ODDS_API_KEY", "")

TELEGRAM_BOT_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID        = os.getenv("TELEGRAM_CHAT_ID", "")

ANTHROPIC_API_KEY       = os.getenv("ANTHROPIC_API_KEY", "")

# ─────────────────────────────────────────────────────────────────────
# PLATFORM API ENDPOINTS
# ─────────────────────────────────────────────────────────────────────
KALSHI_BASE_URL     = "https://api.elections.kalshi.com/trade-api/v2"
POLY_GAMMA_URL      = "https://gamma-api.polymarket.com"      # blocked in US
POLY_CLOB_URL       = "https://clob.polymarket.com"           # blocked in US
PREDICTIT_BASE_URL  = "https://www.predictit.org/api"
ODDS_API_BASE_URL   = "https://api.the-odds-api.com"

# ─────────────────────────────────────────────────────────────────────
# FEE STRUCTURE  (update if platforms change their fee schedule)
# ─────────────────────────────────────────────────────────────────────
KALSHI_TAKER_FEE    = 0.07      # 7% of contract price on taker orders
POLY_TAKER_FEE      = 0.02      # 2% on taker orders (blocked in US)

# PredictIt fee structure — much higher than Kalshi/Poly
# 10% fee on profits per contract + 10% withdrawal fee
# Effective round-trip cost: ~18–20%. Require large gross edges.
PREDICTIT_PROFIT_FEE    = 0.10  # 10% of winnings per contract
PREDICTIT_WITHDRAWAL_FEE = 0.10 # 10% on cash withdrawals
PREDICTIT_MIN_NET_EDGE  = 0.15  # 15% minimum net edge for PI arb (vs 2% for Poly)

# The Odds API — sportsbook signal source (not a trading venue)
ODDS_API_MIN_BOOKS      = 3     # minimum books for consensus to be reliable
ODDS_API_MIN_EDGE       = 0.04  # 4% minimum edge vs sportsbook consensus

# Sports to actively scan.
# Each entry = 1 Odds API credit per refresh.
#
# Quota math (The Odds API):
#   Free tier  (500 /mo):  1-2 sports at 6-hr refresh
#   Starter    ($9.99/mo, 5 000/mo):  7 sports at 2-hr refresh = 3 024/mo ✓
#   Standard   ($19.99/mo, 15 000/mo): all sports at 45-min refresh
#
# Get a key / upgrade at: https://the-odds-api.com
ODDS_API_ACTIVE_SPORTS: list = [
    "mlb",          # Baseball         — Apr–Oct
    "nba",          # Basketball       — Oct–Jun (playoffs Apr–Jun)
    "nhl",          # Hockey           — Oct–Jun (playoffs Apr–Jun)
    "nfl",          # American football — Sep–Feb (futures year-round)
    "mma",          # UFC / MMA        — year-round
    "mls",          # MLS Soccer       — Feb–Nov
    "tennis_atp",   # ATP Tennis       — year-round
]

# How often to re-fetch sportsbook odds (seconds).
# Starter plan (5 000/mo): 7 sports * 12 refreshes/day * 30 days = 2 520/mo
ODDS_API_REFRESH_SECS: int   = 7200      # 2 hours

# How many pages of KXMVE sports markets to fetch per scan cycle.
# 1 page = 200 markets; 3 pages = up to 600 multi-leg Kalshi contracts.
KALSHI_KXMVE_MAX_PAGES: int  = 3

# ─────────────────────────────────────────────────────────────────────
# ARB DETECTION THRESHOLDS
# ─────────────────────────────────────────────────────────────────────
MIN_NET_EDGE_PAPER  = 0.015     # 1.5% — minimum net profit in paper mode (Phase 3)
MIN_NET_EDGE_LIVE   = 0.020     # 2.0% — stricter threshold for live trading (Phase 4)
MAX_SLIPPAGE_PCT    = 0.005     # 0.5% — reject trade if slippage exceeds this
MAX_BOOK_AGE_SECS   = 5         # Reject order book data older than 5 seconds
MAX_DURATION_DAYS   = 14        # Only trade markets resolving within 14 days

# ─────────────────────────────────────────────────────────────────────
# KALSHI MARKET SERIES
# ─────────────────────────────────────────────────────────────────────
# Economic/financial series that Kalshi always has open.
# Fetched explicitly via series_ticker= to bypass the KXMVE sports
# pagination flood (4000+ markets that fill all pages before political ones).
#
# Political/governance series are NOT listed here because Kalshi
# currently has no open political markets (2026 midterm cycle not yet
# started). They will be added back once Kalshi lists them:
#   "KXPRES", "KXSEN", "KXHOUSE", "KXGOV"  ← add when they open
KALSHI_ECONOMIC_SERIES: list = ["KXCPI", "KXFED", "KXGDP", "KXBTC"]

# Max pages to fetch per series_ticker query (200 markets/page).
# Each series has at most a few hundred markets, so 5 pages is plenty.
KALSHI_SERIES_MAX_PAGES: int  = 5

# Max pages for the supplemental general scan (catches any political
# markets once Kalshi lists them). Keep small — KXMVE fills all pages.
KALSHI_GENERAL_MAX_PAGES: int = 1

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
STARTING_CAPITAL_USD        = 2000.0    # Starting bankroll — update as you scale

# ── DEPRECATED: tunables below are now per-strategy in config/strategies.py ──
# Kept here for backwards compatibility / non-paper-test scripts.
# To change paper-test behavior, add a new strategy version and switch
# ACTIVE_STRATEGY in config/strategies.py.
MAX_SINGLE_POSITION_USD     = 50.0      # Max $ per single arb trade  (use Strategy.max_per_trade_usd)
MAX_TOTAL_DEPLOYED_USD      = 2000.0    # Max $ across ALL open positions  (use Strategy.max_total_deployed_usd)
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

# ─────────────────────────────────────────────────────────────────────
# ODDS HARVESTER  (supplemental sportsbook data via OddsPortal)
# Set ODDS_HARVESTER_ENABLED=True after running: pip install oddsharvester
# then: playwright install chromium
# ─────────────────────────────────────────────────────────────────────
ODDS_HARVESTER_ENABLED        = True    # requires: pip install oddsharvester && python -m playwright install chromium
ODDS_HARVESTER_REFRESH_SECS   = 7200   # re-scrape every 2 hours (batch, slow)
ODDS_HARVESTER_CACHE_PATH     = "data/harvester_cache.json"
ODDS_HARVESTER_HISTORICAL_DIR = "data/historical_odds/"

# Mapping from arb-bot sport keys → OddsHarvester sport name
# MMA is not supported by OddsHarvester (no OddsPortal coverage)
ODDS_HARVESTER_SPORT_MAP: dict = {
    "mlb":          "baseball",
    "nba":          "basketball",
    "nhl":          "ice-hockey",
    "nfl":          "american-football",
    "mls":          "football",      # OddsHarvester uses "football" for soccer
    "tennis_atp":   "tennis",
    "tennis_wta":   "tennis",
}

# Sharp book weights for OddsHarvester devig.
# Pinnacle and sharp European books close closest to true probability.
# Keys are lowercase substrings matched against bookmaker_name.
SHARP_BOOK_WEIGHTS: dict = {
    "pinnacle":  2.0,   # sharpest book globally — weight double
    "bet365":    1.5,   # sharp European reference book
    "default":   1.0,   # recreational books (DraftKings, FanDuel, etc.)
}

# ─────────────────────────────────────────────────────────────────────
# CALIBRATION OVERRIDES
# Populated from calibration_report.py when a bucket shows systematic
# over/under-estimation. Applied as a multiplier on Kelly fraction.
#
# Format: {"sport__bucket": calibration_factor}
# e.g. {"mlb__4-6%": 0.72} means our 4-6% MLB edge estimates are
# 28% too optimistic → Kelly sized at 72% of the calculated value.
#
# Leave empty until you have 20+ settled trades per bucket.
# ─────────────────────────────────────────────────────────────────────
CALIBRATION_OVERRIDES: dict = {
    # "mlb__0-2%":   1.00,   # example — fill from calibration_report.py
}

# ─────────────────────────────────────────────────────────────────────
# SAME-GAME CORRELATION UPLIFT
# When multiple parlay legs come from the same game, the independence
# assumption (multiplying probabilities) UNDER-estimates fair_prob
# because positively-correlated outcomes are more likely to all hit.
#
# Formula: fair_prob = independent_product * uplift
# Applied per same-game leg group, then groups multiplied across.
#
# Empirical estimates from same-game parlay correlation studies:
# ─────────────────────────────────────────────────────────────────────
SAME_GAME_CORRELATION_UPLIFT: dict = {
    "basketball_nba": 1.18,      # team_win + player_pts strongly correlated
    "icehockey_nhl":  1.20,      # team_win + player_goals very correlated
    "americanfootball_nfl": 1.15,
    "baseball_mlb":   1.05,      # weakest — pitcher props ≈ independent of team result
    "soccer_usa_mls": 1.12,
    "tennis_atp_us_open": 1.00,  # 1 player per match — no same-game multi-leg
    "default":        1.10,
}
