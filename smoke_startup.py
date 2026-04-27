"""Quick startup smoke test — runs one scan cycle then exits."""
import sys, os, logging
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s - %(message)s")

from clients.kalshi import KalshiClient
from clients.predictit import PredictItClient
from clients.normalizer import normalize_kalshi_market, normalize_kalshi_book
from matching.matcher import MarketMatcher
from matching.llm_verifier import LLMVerifier
from detection.arb_detector import ArbDetector
from execution.executor import ArbExecutor
from risk.risk_manager import RiskManager
from tracking.position_tracker import PositionTracker
from config.preflight import run_preflight
from config.settings import (
    KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH,
    ANTHROPIC_API_KEY, ODDS_API_KEY,
    KALSHI_ECONOMIC_SERIES, KALSHI_SERIES_MAX_PAGES,
)
from scripts.paper_trade import _predictit_to_normalized_markets, _fetch_predictit_book

print("\n=== Preflight ===")
pre = run_preflight(
    kalshi_api_key_id=KALSHI_API_KEY_ID,
    kalshi_private_key_path=KALSHI_PRIVATE_KEY_PATH,
    predictit_enabled=True,
    odds_api_key=ODDS_API_KEY,
    anthropic_api_key=ANTHROPIC_API_KEY,
)
for w in pre.warnings:
    print("  WARN:", w)
assert pre.ok, "Preflight failed: " + str(pre.errors)
print("  OK")

print("\n=== Component init ===")
kalshi   = KalshiClient()
pi       = PredictItClient()
matcher  = MarketMatcher()
verifier = LLMVerifier()
detector = ArbDetector(live_mode=False, second_platform="predictit")
tracker  = PositionTracker()
risk     = RiskManager(bankroll=1000.0, live_mode=False)
executor = ArbExecutor(kalshi_client=kalshi, poly_client=None,
                       position_tracker=tracker, dry_run=True)
print("  second_platform =", detector.second_platform,
      "| min_edge = {:.0f}%".format(detector.min_edge * 100))

print("\n=== Market fetch ===")
# Fetch Kalshi via series_ticker to bypass KXMVE pagination flood.
# The plain paginated endpoint returns 4 000+ KXMVE sports markets that
# fill all pages before any economic/political market appears.
seen: set  = set()
kalshi_raw: list = []
for series in KALSHI_ECONOMIC_SERIES:
    try:
        batch = kalshi.get_all_open_markets(series_ticker=series, max_pages=KALSHI_SERIES_MAX_PAGES)
        for m in batch:
            t = m.get("ticker", "")
            if t and t not in seen:
                seen.add(t)
                kalshi_raw.append(m)
        print("  Kalshi {:7s}: {:3d} markets".format(series, len(batch)))
    except Exception as e:
        print("  Kalshi {} FAILED: {}".format(series, e))
# 1-page general scan — picks up political markets once Kalshi lists them
try:
    general = kalshi.get_all_open_markets(max_pages=1)
    added = 0
    for m in general:
        t = m.get("ticker", "")
        if t and not t.upper().startswith("KXMVE") and t not in seen:
            seen.add(t)
            kalshi_raw.append(m)
            added += 1
    if added:
        print("  Kalshi general (non-MVE): {} markets".format(added))
except Exception as e:
    print("  Kalshi general FAILED:", e)

kalshi_norm = [normalize_kalshi_market(m) for m in kalshi_raw]
pi_markets  = _predictit_to_normalized_markets(pi)
print("  ---------------------------------------------")
print("  Kalshi total (econ series):   {}".format(len(kalshi_norm)))
print("  PredictIt binary markets:     {}".format(len(pi_markets)))

print("\n=== Matcher ===")
candidates = matcher.find_matches(kalshi_norm, pi_markets, threshold=0.55)
print("  Candidates: {}".format(len(candidates)))
if candidates:
    print("  Top matches:")
    for c in candidates[:5]:
        print("    [{:.2f}] K: {} | PI: {}".format(
            c.score, c.kalshi.title[:48], c.poly.title[:48]))
else:
    print("  (No candidates — expected until Kalshi opens 2026 midterm/governance markets.)")
    print("  Kalshi currently: economic series only (CPI/Fed/GDP/BTC).")
    print("  PredictIt currently: political markets only.")
    print("  Overlap will appear once KXSEN / KXHOUSE / KXPRES series open.")

if candidates:
    print("\n=== Order book fetch (first candidate) ===")
    c = candidates[0]
    try:
        k_book = normalize_kalshi_book(kalshi.get_orderbook(c.kalshi.market_id))
        pi_book = _fetch_predictit_book(pi, c.poly)
        print("  Kalshi YES ask: {}  NO ask: {}".format(
            k_book.yes.best_ask, k_book.no.best_ask))
        if pi_book:
            print("  PI     YES ask: {}  NO ask: {}".format(
                pi_book.yes.best_ask, pi_book.no.best_ask))
        opp = detector.analyze(c.kalshi, c.poly, k_book, pi_book, match_score=c.score) if pi_book else None
        if opp:
            print("  ARB FOUND: {:.2f}% net edge | {} contracts".format(
                opp.net_profit_pct * 100, opp.max_contracts))
        else:
            print("  No arb (expected for efficient markets)")
    except Exception as e:
        print("  Book fetch error:", e)

tracker.close()
print("\n=== STARTUP: READY ===")
print("Run paper trade with:")
print("  python scripts/paper_trade.py --hours 24 --capital 1000")
