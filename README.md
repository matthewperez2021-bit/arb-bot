# Polymarket x Kalshi Arbitrage Bot

Binary event arbitrage system scanning for pricing inefficiencies between
Polymarket (decentralized) and Kalshi (CFTC-regulated).

## Reference
- `polymarket_kalshi_arb_context.md.docx` — Full technical reference
- `arb_system_roadmap.docx` — Phased build roadmap

## Quick Start

```bash
# 1) Create & activate a virtualenv (recommended)
python -m venv .venv

# Windows PowerShell:
.\.venv\Scripts\Activate.ps1

# macOS/Linux:
source .venv/bin/activate

# 2) Install dependencies
python -m pip install -U pip
python -m pip install -r requirements.txt

# 3) Copy and fill in your credentials (do NOT commit secrets)
cp config/secrets.env.example config/secrets.env

# 4) Run tests
python -m pytest -q

# 5) Paper trade (Phase 3+)
python scripts/paper_trade.py --hours 2

# 6) Live bot (Phase 5)
python bot.py --live
```

## Build Order
Follow the roadmap phases strictly — do not deploy real capital before Phase 4.

| Phase | Focus | Gate |
|-------|-------|------|
| 1 | API clients + data layer | Live order books working |
| 2 | Market matching + LLM verify | 95% match accuracy on test set |
| 3 | Arb detection + paper trade | 5+ opps/day, 90%+ paper win rate |
| 4 | Live execution + risk controls | 100 live trades, 90%+ win rate |
| 5 | Full automation + scale | 14 days unattended |

## Legal Note
Kalshi is fully legal in all 50 US states (CFTC-regulated).
Polymarket is legally ambiguous for US users. Understand the risk before trading.
