"""
diagnose_kalshi.py — Kalshi API diagnostic script.

Checks dependencies, credentials, connectivity, signing, and API health.
Run from the repo root:
    python scripts/diagnose_kalshi.py
"""

import importlib
import os
import sys
import time
import traceback

# ── make repo root importable ────────────────────────────────────────────────
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PASS = "[PASS]"
FAIL = "[FAIL]"
WARN = "[WARN]"

results: list[tuple[str, str]] = []


def check(label: str, fn):
    try:
        msg = fn()
        results.append((PASS, f"{label}: {msg}"))
    except Exception as exc:
        results.append((FAIL, f"{label}: {exc}"))


# ── 1. Python version ────────────────────────────────────────────────────────
check(
    "Python version",
    lambda: f"{sys.version.split()[0]} (need >=3.10)"
    if sys.version_info >= (3, 10)
    else (_ for _ in ()).throw(RuntimeError(f"Python {sys.version.split()[0]} is too old; need >=3.10")),
)

# ── 2. Core dependencies ─────────────────────────────────────────────────────
for pkg, imp in [
    ("requests",     "requests"),
    ("cryptography", "cryptography.hazmat.primitives.hashes"),
    ("cffi",         "cffi"),
    ("dotenv",       "dotenv"),
]:
    def _import(imp=imp, pkg=pkg):
        importlib.import_module(imp)
        ver = importlib.import_module(pkg.split(".")[0] if "." not in pkg else pkg).__version__ if hasattr(
            importlib.import_module(pkg.split(".")[0] if "." not in pkg else pkg), "__version__"
        ) else "?"
        return f"installed ({ver})"
    check(f"Package: {pkg}", _import)

# ── 3. secrets.env exists ────────────────────────────────────────────────────
SECRETS_PATH = os.path.join(ROOT, "config", "secrets.env")

def _check_secrets_file():
    if not os.path.exists(SECRETS_PATH):
        raise FileNotFoundError(
            f"{SECRETS_PATH} does not exist. "
            "Copy config/secrets.env.example → config/secrets.env and fill in your credentials."
        )
    return "found"
check("config/secrets.env exists", _check_secrets_file)

# ── 4. Load settings (triggers dotenv) ──────────────────────────────────────
def _load_settings():
    import config.settings as s
    return "loaded"
check("config.settings importable", _load_settings)

# ── 5. KALSHI_API_KEY_ID set ─────────────────────────────────────────────────
def _check_api_key():
    import config.settings as s
    if not s.KALSHI_API_KEY_ID:
        raise ValueError(
            "KALSHI_API_KEY_ID is empty. Set it in config/secrets.env."
        )
    masked = s.KALSHI_API_KEY_ID[:6] + "..." + s.KALSHI_API_KEY_ID[-4:]
    return f"set ({masked})"
check("KALSHI_API_KEY_ID", _check_api_key)

# ── 6. Private key file exists and is readable ───────────────────────────────
def _check_pem():
    import config.settings as s
    pem_path = s.KALSHI_PRIVATE_KEY_PATH
    abs_path  = pem_path if os.path.isabs(pem_path) else os.path.join(ROOT, pem_path)
    if not os.path.exists(abs_path):
        raise FileNotFoundError(
            f"PEM file not found: {abs_path}. "
            "Download your private key from Kalshi and place it at "
            f"{pem_path} (relative to repo root)."
        )
    size = os.path.getsize(abs_path)
    return f"found ({size} bytes) at {abs_path}"
check("Kalshi private key (.pem)", _check_pem)

# ── 7. Private key loads correctly ───────────────────────────────────────────
def _load_pem():
    import config.settings as s
    from cryptography.hazmat.primitives import serialization
    pem_path = s.KALSHI_PRIVATE_KEY_PATH
    abs_path  = pem_path if os.path.isabs(pem_path) else os.path.join(ROOT, pem_path)
    with open(abs_path, "rb") as f:
        key = serialization.load_pem_private_key(f.read(), password=None)
    key_size = key.key_size  # type: ignore[attr-defined]
    return f"RSA-{key_size} loaded OK"
check("RSA private key parses", _load_pem)

# ── 8. RSA signing works ─────────────────────────────────────────────────────
def _test_signing():
    import base64
    import config.settings as s
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    pem_path = s.KALSHI_PRIVATE_KEY_PATH
    abs_path  = pem_path if os.path.isabs(pem_path) else os.path.join(ROOT, pem_path)
    with open(abs_path, "rb") as f:
        key = serialization.load_pem_private_key(f.read(), password=None)
    ts  = str(int(time.time() * 1000))
    msg = (ts + "GET" + "/markets").encode("utf-8")
    sig = key.sign(msg, padding.PKCS1v15(), hashes.SHA256())
    encoded = base64.b64encode(sig).decode()
    return f"signature generated ({len(encoded)} chars)"
check("RSA signing produces valid output", _test_signing)

# ── 9. Network: reach Kalshi API domain ──────────────────────────────────────
def _check_network():
    import requests
    import config.settings as s
    # unauthenticated ping — expect 401 or 403, not connection error
    url = s.KALSHI_BASE_URL + "/markets?limit=1&status=open"
    try:
        r = requests.get(url, timeout=8)
        if r.status_code in (200, 400, 401, 403):
            return f"reachable (HTTP {r.status_code})"
        raise RuntimeError(f"Unexpected HTTP {r.status_code}")
    except requests.exceptions.ConnectionError as e:
        raise ConnectionError(f"Cannot reach {s.KALSHI_BASE_URL}: {e}")
check("Kalshi API reachable (unauthenticated)", _check_network)

# ── 10. KalshiClient instantiation ──────────────────────────────────────────
def _init_client():
    from clients.kalshi import KalshiClient
    client = KalshiClient()
    return "OK"
check("KalshiClient() instantiates", _init_client)

# ── 11. Authenticated request: get_balance ───────────────────────────────────
def _get_balance():
    from clients.kalshi import KalshiClient
    client = KalshiClient()
    bal = client.get_balance()
    usd = bal.get("balance", 0)
    return f"balance = ${usd:.2f}"
check("get_balance() authenticated call", _get_balance)

# ── 12. Fetch open markets ───────────────────────────────────────────────────
def _get_markets():
    from clients.kalshi import KalshiClient
    client = KalshiClient()
    resp = client.get_markets(limit=3)
    markets = resp.get("markets", [])
    if not markets:
        raise RuntimeError("API returned 0 markets (account may lack permissions or no open markets)")
    tickers = [m["ticker"] for m in markets[:3]]
    return f"{len(markets)} returned, first: {tickers}"
check("get_markets() returns data", _get_markets)

# ── 13. Fetch order book ─────────────────────────────────────────────────────
def _get_orderbook():
    from clients.kalshi import KalshiClient
    client = KalshiClient()
    resp = client.get_markets(limit=1)
    markets = resp.get("markets", [])
    if not markets:
        raise RuntimeError("No open markets to fetch order book for")
    ticker = markets[0]["ticker"]
    book = client.get_orderbook(ticker, depth=3)
    yes_asks = book["yes"]["asks"]
    return f"ticker={ticker}, YES asks={yes_asks[:2]}"
check("get_orderbook() normalizes correctly", _get_orderbook)


# ── Print results ────────────────────────────────────────────────────────────

print()
print("=" * 64)
print("  Kalshi API Diagnostic Report")
print("=" * 64)

passes  = sum(1 for r in results if r[0] == PASS)
failures = sum(1 for r in results if r[0] == FAIL)
warnings = sum(1 for r in results if r[0] == WARN)

for status, msg in results:
    print(f"  {status}  {msg}")

print()
print(f"  {passes} passed  |  {failures} failed  |  {warnings} warnings")
print("=" * 64)

if failures > 0:
    print()
    print("Fix the [FAIL] items above, then re-run this script.")
    sys.exit(1)
else:
    print()
    print("All checks passed. Kalshi API is ready.")
    sys.exit(0)
