# Arb-Bot Project Context

> Paste this into a new Claude chat to get me up to speed instantly.

---

## What This Algorithm Does (One Paragraph)

This is an **automated sports arbitrage bot** that exploits pricing inefficiencies between Kalshi (a CFTC-regulated prediction market) and major sportsbooks (DraftKings, FanDuel, BetMGM, Caesars, etc.). It scans Kalshi's KXMVE multi-leg sports parlay markets, prices each leg using the devigged consensus from sportsbook odds (via The Odds API), multiplies the leg probabilities to compute the parlay's "fair" probability, and looks for cases where Kalshi's price is meaningfully below the sportsbook-implied fair value (after the 7% Kalshi taker fee). It then half-Kelly sizes a single-leg trade on Kalshi only — sportsbook accounts get banned within months for arb behavior, so we treat sportsbooks as a pricing signal, not a trading venue. Currently runs in **paper mode** (no real money), logging every trade to SQLite with per-strategy versioning so we can iterate filters without losing comparability.

**Edge thesis:** Kalshi's KXMVE crowd is slower than sharp sportsbook lines. When Kalshi quotes a parlay at 50¢ but sportsbooks imply only 8¢ fair value, that's a +38% net edge to buy NO.

---

## Current State Snapshot (2026-05-04)

| Metric | Value |
|---|---|
| **Bankroll** | $1,771.35 (-11.4% from $2,000 starting) |
| **Settled trades** | 90 (22W / 68L = 24% win rate) |
| **Net P&L (realized)** | -$228.65 |
| **Active strategy** | **v2** (just edited + activated; 0 trades yet) |
| **Strategies running** | v1 (-$58, 82 trades), v3 (-$170, 8 trades), v4 (0 trades) |
| **Mode** | PAPER (no real $ at risk) |

**Calibration findings (from 90 v1 trades):**
- **MLB +88% ROI, MLS -2%, NBA -47%, NHL -46%** — sport effect is strong and stable
- **Apparent edges above 8% are model error** — 12%+ bucket has CalibF 0.56, ROI -33%
- **Overall fair_prob is ~22% too optimistic** (24.4% actual WR vs 31.3% predicted) — half-Kelly is appropriate; full-Kelly (v3) is reckless
- v2 redefined 2026-05-04 to encode these findings (8% edge cap, NBA/NHL excluded)

---

## Project Location

```
C:\Users\mpere\Documents\Claude\Projects\Arb Project\arb-bot
```

Always `cd` here first in PowerShell before running any command.

---

## Key Files

| Path | What it does |
|---|---|
| `config/strategies.py` | **Strategy registry** — versioned dataclasses (v1, v2, ...). Edit `ACTIVE_STRATEGY` to switch. |
| `config/settings.py` | Global constants (API keys, fees, exchange URLs, OddsHarvester settings). |
| `scripts/sports_paper_test.py` | The main scan + trade pipeline. Takes `--strategy` and `--strategies`. |
| `scripts/sports_scheduler.py` | Hourly background loop. Runs resolver then paper test. Refreshes OddsHarvester cache every 2h. |
| `scripts/resolve_trades.py` | Checks Kalshi for settled markets, updates P&L + bankroll. |
| `scripts/view_trades.py` | Read-only viewer. Flags: `--status`, `--strategies`, `--session`, `--all` |
| `scripts/analyze_performance.py` | Slices settled trades by side/sport/edge/legs/etc. |
| `scripts/calibration_report.py` | **NEW** — Predicted vs actual win rate by edge bucket + sport, plus CLV summary. |
| `scripts/seed_backtest_data.py` | Seeds `data/historical_odds/` with per-bookmaker data for backtesting. |
| `clients/kalshi.py` | Kalshi API client (auth, orderbook, market lookup, `is_market_resolved`). |
| `clients/odds_api.py` | Odds API client (h2h + player props + **totals**, devig logic, caches). |
| `clients/odds_harvester_client.py` | OddsHarvester wrapper — scrapes OddsPortal, devigs decimal odds (Pinnacle-weighted), builds team prob cache, archives daily snapshots. |
| `detection/odds_arb_scanner.py` | Core: parses KXMVE titles, prices legs (incl. **totals**), applies same-game correlation uplift, computes net edge. |
| `detection/kxmve_parser.py` | Parses parlay titles into structured legs (team_win, player_over, total_over). |
| `data/arb_positions.db` | SQLite DB. Tables: `sports_paper_trades`, `bankroll`, `strategy_bankrolls`. |
| `data/harvester_cache.json` | OddsHarvester team prob cache (auto-written, 2h TTL). |
| `data/historical_odds/{date}/` | Daily snapshots — every harvester refresh writes timestamped JSON for backtesting. |
| `pyproject.toml` | pytest config: scopes test collection to `tests/` only. |

---

## Daily Commands (memorize these)

### Scan / trade
```powershell
python scripts/sports_paper_test.py                           # uses ACTIVE_STRATEGY
python scripts/sports_paper_test.py --strategy v2             # force a version
python scripts/sports_paper_test.py --strategies v1,v2        # A/B (each gets FULL bankroll)
python scripts/sports_scheduler.py --strategies v1,v2         # hourly background loop
python scripts/sports_scheduler.py --no-harvester             # skip OddsHarvester refresh
```

### Settle / resolve
```powershell
python scripts/resolve_trades.py                              # check Kalshi for settlements
python scripts/resolve_trades.py --dry-run                    # preview, no DB writes
python scripts/resolve_trades.py --verbose                    # show every market checked
```

### View
```powershell
python scripts/view_trades.py --status                        # compact dashboard (top of mind)
python scripts/view_trades.py --strategies                    # version log + live P&L per version
python scripts/view_trades.py --session latest                # detail of most recent scan
python scripts/view_trades.py --all                           # every trade, newest first
```

### Analyze
```powershell
python scripts/analyze_performance.py                         # all-strategies slice breakdown
python scripts/analyze_performance.py --version v2            # filter to one strategy

python scripts/calibration_report.py                          # predicted vs actual WR by edge + sport
python scripts/calibration_report.py --strategy v2            # filter to one strategy
python scripts/calibration_report.py --min-trades 5           # require min sample per bucket
python scripts/calibration_report.py --export csv             # write data/calibration_*.csv
```

---

## Strategy Versions

**Rules:** Append-only EXCEPT a strategy with 0 trades may be edited in place
(no comparability lost yet). Once a strategy has a settled trade, it's frozen.

### v1 — Baseline (created 2026-04-28)
Original config, no filters. Acts as the calibration dataset.
```
min_net_edge          = 1.5%
max_per_trade_usd     = $50
max_total_deployed    = $2,000
kelly_fraction        = 0.5
min_books             = 2
max_legs              = 99 (no cap)
max_trusted_edge_pct  = 100% (no cap)
excluded_sports       = (none)
allowed_sides         = yes, no
```
Performance (90 trades): 24% WR, -$58 P&L. By sport: MLB +88%, MLS -2%, NBA -47%, NHL -46%.
By edge bucket: 0-2% +36%, 2-4% -59%, 4-6% +37%, 6-8% -42%, 8-12% +48%, 12%+ -33%.
**Insight:** edges above 8% are model error; NBA/NHL fair_prob formula is broken.

### v2 — Calibration-driven (redefined 2026-05-04, **ACTIVE**)
Tightened in place from calibration_report findings (was 0 trades, so not a recorded baseline).
```
min_net_edge          = 4.0%
max_per_trade_usd     = $50
max_total_deployed    = $2,000
kelly_fraction        = 0.5
min_books             = 2
max_legs              = 3
max_trusted_edge_pct  = 8.0%   ← tightened from 12 (CalibF 0.56 above 12%)
excluded_sports       = basketball_nba, icehockey_nhl   ← re-added (CalibF 0.68/0.57)
allowed_sides         = yes, no
```
Effective trade window: MLB / MLS / tennis / NFL with 4-8% net edge, max 3 legs.
Status: 0 trades placed yet. **Together with same-game correlation uplift + totals leg
pricing (shipped same day), this is the new baseline to validate.**

### v3 — Ultra-aggressive (created 2026-04-30)
Full Kelly, $100/trade, 0.5% min edge, no filters. **8 trades, -$170**. Confirms full-Kelly
is reckless given the 22% fair_prob over-estimation. Don't activate unless calibration is
much tighter.

### v4 — High-volume small stakes (created 2026-05-01)
$20/trade cap, 0.3% min edge. 0 trades placed — was filtered out by the v1 sample's
larger-edge pattern. May be worth reactivating once v2 stabilizes.

### Switching / reverting
Edit one line in `config/strategies.py`:
```python
ACTIVE_STRATEGY = "v2"   # or "v1", "v3", "v4"
```
Or override per-run with `--strategy v1`.

---

## Algorithm Pipeline (How a Scan Actually Works)

```
1. FETCH  →  Kalshi: ~600 KXMVE markets (3 pages)
              Odds API: ~115 events × 7 sports + player props for ~15 events
              [Cached 30 min to save quota]

2. PARSE  →  KXMVEParser splits each title into legs:
              "yes Lakers, no Bucks +5.5, yes LeBron 25+ pts" → 3 leg objects

3. PRICE  →  For each leg:
              - team_win/team_spread → devigged sportsbook consensus
                (falls back to OddsHarvester cache when Odds API has no line)
              - player_over → devigged Over/Under from prop cache
              - total_over → devigged Over/Under from totals_cache  (NEW)

4. COMBINE → Group priced legs by event_id, multiply within groups, then:
              - For groups with 2+ legs from same game: apply per-sport
                correlation uplift (NBA 1.18, NHL 1.20, NFL 1.15, MLB 1.05)
              - Multiply across groups (different games are independent)
              This corrects the systematic UNDER-estimate of same-game parlay
              probability that made NO trades look artificially attractive.

5. EDGE   →  net_edge = fair_prob − (kalshi_ask × 1.07 fee)
              Side selection: trade YES if net_edge_yes > 0, NO if net_edge_no > 0

6. FILTER →  Strategy-level rejects (excluded_sports, max_legs, edge ceiling, side)
              Plus: drop partial-coverage opps (legs_priced < legs_total)

7. SIZE   →  Half-Kelly: f* = (win_prob - cost) / (1 - cost), then × 0.5
              Capped at max_per_trade_usd
              Bounded by remaining session budget (max_total_deployed - already_open)

8. LOG    →  INSERT into sports_paper_trades with strategy_version tag

9. RESOLVE → resolve_trades.py polls Kalshi later. When status='finalized'
              or 'settled' AND result is yes/no:
              - Marks WON/LOST, updates actual_profit + bankroll table
              - Captures kalshi_closing_ask / kalshi_closing_no_ask in same
                get_market() call
              - Computes clv = closing_ask - entry_ask (positive = market
                confirmed our direction; gold-standard edge proof over time)
```

---

## Important Decisions / Non-Bugs

These are intentional behaviors — don't "fix" them:

- **"Strategy filters dropped X"** in v2 output is the goal, not a bug. v2 is supposed to skip NBA/NHL.
- **Partial-coverage opportunities** (e.g. `legs=2/3`) are shown as warnings only and never traded. One unmatched team would inflate the edge artificially.
- **Kalshi resolution status:** Both `finalized` AND `settled` count as resolved.
- **A/B mode capital:** Each strategy gets the FULL `--capital`, not a split. Per-strategy `max_total_deployed_usd` caps individual exposure.
- **`STARTING_CAPITAL_USD = 2000`** — bumped from 1k to cover existing open positions when the cross-session capital cap was added.
- **NO-side trades pricing:** For NO bets, win_prob = (1 - fair_prob), not fair_prob. (NO trades win when the parlay does NOT hit.) **Calibration report respects this — flips the formula by side.**
- **KXMVE order books are always empty** — we synthesize a fake `NormalizedMarketBook` from `yes_ask_dollars` / `no_ask_dollars` on the market data itself.
- **Player name normalization** uses NFKD Unicode strip (handles "Vučević" matching "Vucevic") + last-name fallback.
- **Same-game correlation uplift INCREASES fair_prob** — same-game legs are positively correlated, so independence multiplication UNDER-estimates parlay probability. This is why uplift > 1.0 (NBA 1.18, etc.).
- **v2 was edited in place** — normally strategies are append-only, but v2 had 0 trades so editing is fine. Once a strategy has settled trades, it's frozen.
- **pytest scoped to `tests/`** — `scripts/sports_paper_test.py` ends in `_test.py` and was being auto-collected; `pyproject.toml` now restricts collection.

---

## OddsHarvester Integration (added 2026-05-01)

Supplemental sportsbook data source via OddsPortal scraping (Playwright-based).
Runs on a 2h batch cycle — never blocks the 45s scan loop.

**To activate:**
```powershell
pip install oddsharvester playwright
python -m playwright install chromium
# In config/settings.py: ODDS_HARVESTER_ENABLED = True (currently True as of 2026-05-04)
```

**How it fits in:**
- `OddsHarvesterClient.fetch_upcoming()` scrapes per-bookmaker decimal odds for 6 sports
- **Sharp book weighting** — Pinnacle 2x, Bet365 1.5x, recreational 1x in devig
- Stores `{team_norm: {prob, n_books, sport, bookmaker_breakdown}}` in `data/harvester_cache.json`
- **Daily snapshot archive** — every refresh also writes `data/historical_odds/{date}/{sport}_{HHMM}.json`
  (accumulates dataset for backtest.py)
- `OddsArbScanner._price_team_leg()` checks harvester as fallback when Odds API has no line,
  or uses harvester if it has more bookmakers contributing
- `seed_backtest_data.py` uses the `scrape_historic` mode for retroactive season data

**Sport mapping:** mlb→baseball, nba→basketball, nhl→ice-hockey, nfl→american-football,
mls→football, tennis_atp→tennis. MMA not covered by OddsPortal.

**Known issue:** `CommandEnum.UPCOMING_MATCHES` required (plain string "scrape_upcoming" fails).
Already fixed in `odds_harvester_client.py`.

---

## Historical Data Pipeline (added 2026-05-04)

Three-piece system to validate edge accuracy and improve calibration over time.

### 1. CLV (Closing Line Value) tracking
Added 3 columns to `sports_paper_trades` via idempotent ALTER TABLE:
- `kalshi_closing_ask`, `kalshi_closing_no_ask` — captured at moment of settlement
- `clv = closing_ask - entry_ask` for the traded side
- **Positive avg CLV across many trades = real edge, not luck.** Gold standard.

### 2. Calibration report (`scripts/calibration_report.py`)
Groups settled trades by edge bucket (0-2%, 2-4%, ..., 12%+) and sport. Computes
`calibration_factor = actual_win_rate / predicted_win_rate`. Factor < 0.80 flags
the bucket as systematically over-estimating edge → Kelly oversized → tighten.

**Bug fixed 2026-05-04:** report was averaging `fair_prob` directly. For NO trades,
predicted win prob is `(1 - fair_prob)`, not `fair_prob`. Calibration factors are
now meaningful for both sides.

### 3. Daily snapshot archiving
OddsHarvester refresh writes timestamped snapshots to `data/historical_odds/`,
splitting by sport. Backtest.py (still a stub) will eventually replay these.

---

## Open Roadmap Items (not yet built)

- **`scripts/backtest.py`** — implement from stub once 2-4 weeks of harvester
  snapshots have accumulated. Replay strategies against historical odds.
- **CALIBRATION_OVERRIDES** in settings.py — placeholder dict; populate from
  calibration_report.py findings to apply Kelly multipliers per sport/bucket.
- **Player→event mapping** — currently player_over legs don't get an event_id,
  so same-game correlation doesn't catch (team_win + player_pts) pairs. Need
  a player→roster lookup to thread event_id through `_price_player_leg`.
- **Per-sport Kelly fraction** (smaller for noisier sports — once calibration data shows it).
- **Time-to-close filter** (only trade markets closing within 24h).
- **Live trading mode** (currently paper only). Gate: 30+ v2 trades with
  positive avg CLV + calibration factors in 0.85-1.15 range.
- **More sportsbook coverage for player props** (currently 1-2 books per prop).

---

## Environment

- **OS:** Windows 11 (PowerShell 5.1 — no `&&`, use `;` or `if ($?) {}`)
- **Python:** 3.14 (per the user's installation)
- **Database:** SQLite at `data/arb_positions.db`
- **Logs:** `data/sports_scheduler.log`, `data/sports_paper_test.log`
- **APIs used:** Kalshi (real account), The Odds API (Starter tier, 5000 req/mo), OddsHarvester (free, OddsPortal scraping — disabled by default)
- **Repo:** https://github.com/matthewperez2021-bit/arb-bot (push to `main`)

---

## Quick Brief When Starting a New Chat

> I'm working on an automated sports arbitrage bot at `C:\Users\mpere\Documents\Claude\Projects\Arb Project\arb-bot`. It paper-trades Kalshi KXMVE multi-leg sports markets vs sportsbook consensus prices from The Odds API + OddsHarvester. Active strategy is v2 (4-8% edge band, NBA/NHL excluded, half-Kelly). 90 settled trades on v1 showed MLB +88% / MLS -2% / NBA -47% / NHL -46% ROI, and edges above 8% are model error. Just shipped same-game correlation uplift, totals leg pricing, CLV tracking, and calibration_report.py. Read `CONTEXT.md` for full state. I want to [DESCRIBE TASK].
