# Arb-Bot Project Context

> Paste this into a new Claude chat to get me up to speed instantly.

---

## What This Algorithm Does (One Paragraph)

This is an **automated sports arbitrage bot** that exploits pricing inefficiencies between Kalshi (a CFTC-regulated prediction market) and major sportsbooks (DraftKings, FanDuel, BetMGM, Caesars, etc.). It scans Kalshi's KXMVE multi-leg sports parlay markets, prices each leg using the devigged consensus from sportsbook odds (via The Odds API), multiplies the leg probabilities to compute the parlay's "fair" probability, and looks for cases where Kalshi's price is meaningfully below the sportsbook-implied fair value (after the 7% Kalshi taker fee). It then half-Kelly sizes a single-leg trade on Kalshi only — sportsbook accounts get banned within months for arb behavior, so we treat sportsbooks as a pricing signal, not a trading venue. Currently runs in **paper mode** (no real money), logging every trade to SQLite with per-strategy versioning so we can iterate filters without losing comparability.

**Edge thesis:** Kalshi's KXMVE crowd is slower than sharp sportsbook lines. When Kalshi quotes a parlay at 50¢ but sportsbooks imply only 8¢ fair value, that's a +38% net edge to buy NO.

---

## Current State Snapshot (2026-05-01)

| Metric | Value |
|---|---|
| **Bankroll** | $2,249.03 (+12.5% from $2,000 starting) |
| **Settled trades** | 59 (12W / 47L = 20% win rate) |
| **Net P&L (realized)** | +$249.03 |
| **Open positions** | 11 (~$382 deployed) |
| **Active strategy** | v1 (baseline) |
| **A/B test running** | v1 + v2 (when scheduler is up) |
| **Mode** | PAPER (no real $ at risk) |

**Big finding so far:** Returns are wildly uneven by sport — MLB +221% ROI, MLS +7%, NBA -100%, NHL -49%. v2 was created to filter NBA/NHL out and cap "too good to be true" edges.

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
| `scripts/seed_backtest_data.py` | Seeds `data/historical_odds/` with per-bookmaker data for backtesting. |
| `clients/kalshi.py` | Kalshi API client (auth, orderbook, market lookup, `is_market_resolved`). |
| `clients/odds_api.py` | Odds API client (h2h + player props, devig logic, prop cache). |
| `clients/odds_harvester_client.py` | OddsHarvester wrapper — scrapes OddsPortal, devigs decimal odds, builds team prob cache. |
| `detection/odds_arb_scanner.py` | Core: parses KXMVE titles, prices legs, computes net edge. Accepts dual-source odds. |
| `detection/kxmve_parser.py` | Parses parlay titles into structured legs (team_win, player_over, total_over). |
| `data/arb_positions.db` | SQLite DB. Tables: `sports_paper_trades`, `bankroll`. |
| `data/harvester_cache.json` | OddsHarvester team prob cache (auto-written, 2h TTL). |
| `data/historical_odds/` | Per-bookmaker historical match data seeded by `seed_backtest_data.py`. |

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
```

---

## Strategy Versions

**Rules:** Append-only. Never edit a past version. The registry (`config/strategies.py`) is the changelog.

### v1 — Baseline (created 2026-04-28, ACTIVE)
Original config, no filters.
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
Performance: 59 trades, 20% WR, +$249 (+15.6% ROI). Big drag from NBA (-100%) and NHL (-49%).

### v2 — Filtered (created 2026-04-29, DEFINED)
Filter pass derived from v1's slice analysis.
```
min_net_edge          = 4.0%
max_per_trade_usd     = $50
max_total_deployed    = $2,000
kelly_fraction        = 0.5
min_books             = 2
max_legs              = 3 (parlays past 3 had bad correlation)
max_trusted_edge_pct  = 12% (above this is model error)
excluded_sports       = basketball_nba, icehockey_nhl
allowed_sides         = yes, no
```
Status: 0 trades placed yet — first scan only had NBA/NHL fully-priced opps, all correctly filtered.

### Switching / reverting
Edit one line in `config/strategies.py`:
```python
ACTIVE_STRATEGY = "v1"   # or "v2"
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
              - player_over → devigged Over/Under from prop cache
              - total_over → SKIPPED (no data source yet, would create partials)

4. COMBINE → Multiply leg probs → parlay fair_prob
              (Treats legs as independent — known limitation; correlation discount on roadmap)

5. EDGE   →  net_edge = fair_prob − (kalshi_ask × 1.07 fee)
              Side selection: trade YES if net_edge_yes > 0, NO if net_edge_no > 0

6. FILTER →  Strategy-level rejects (excluded_sports, max_legs, edge ceiling, side)
              Plus: drop partial-coverage opps (legs_priced < legs_total)

7. SIZE   →  Half-Kelly: f* = (win_prob - cost) / (1 - cost), then × 0.5
              Capped at max_per_trade_usd
              Bounded by remaining session budget (max_total_deployed - already_open)

8. LOG    →  INSERT into sports_paper_trades with strategy_version tag

9. RESOLVE → resolve_trades.py polls Kalshi later. When status='finalized'
              or 'settled' AND result is yes/no, marks WON/LOST and
              updates bankroll table.
```

---

## Important Decisions / Non-Bugs

These are intentional behaviors — don't "fix" them:

- **"Strategy filters dropped X"** in v2 output is the goal, not a bug. v2 is supposed to skip NBA/NHL.
- **Partial-coverage opportunities** (e.g. `legs=2/3`) are shown as warnings only and never traded. One unmatched team would inflate the edge artificially.
- **Kalshi resolution status:** Both `finalized` AND `settled` count as resolved. (Original code only checked `settled` — that was a bug, fixed.)
- **A/B mode capital:** Each strategy gets the FULL `--capital`, not a split. Per-strategy `max_total_deployed_usd` caps individual exposure.
- **`STARTING_CAPITAL_USD = 2000`** — bumped from 1k to cover existing open positions when the cross-session capital cap was added.
- **NO-side trades pricing:** For NO bets, win_prob = (1 - fair_prob), not fair_prob. (NO trades win when the parlay does NOT hit.)
- **KXMVE order books are always empty** — we synthesize a fake `NormalizedMarketBook` from `yes_ask_dollars` / `no_ask_dollars` on the market data itself.
- **Player name normalization** uses NFKD Unicode strip (handles "Vučević" matching "Vucevic") + last-name fallback.

---

## OddsHarvester Integration (added 2026-05-01)

Supplemental sportsbook data source via OddsPortal scraping (Playwright-based).
Runs on a 2h batch cycle — never blocks the 45s scan loop.

**To activate:**
```powershell
pip install oddsharvester playwright
python -m playwright install chromium
# Then in config/settings.py: ODDS_HARVESTER_ENABLED = True
```

**How it fits in:**
- `OddsHarvesterClient.fetch_upcoming()` scrapes per-bookmaker decimal odds for 6 sports
- Devigs them and stores `{team_norm: {prob, n_books, sport}}` in `data/harvester_cache.json`
- `OddsArbScanner._price_team_leg()` checks harvester as fallback when Odds API has no line,
  or uses harvester if it has more bookmakers contributing
- `seed_backtest_data.py` uses the `scrape_historic` mode to fill `data/historical_odds/`

**Sport mapping:** mlb→baseball, nba→basketball, nhl→ice-hockey, nfl→american-football,
mls→football, tennis_atp→tennis. MMA not covered by OddsPortal.

**Known issue:** `CommandEnum.UPCOMING_MATCHES` required (plain string "scrape_upcoming" fails).
Already fixed in `odds_harvester_client.py`.

---

## Open Roadmap Items (not yet built)

- Same-game leg correlation discount (currently treats parlay legs as independent)
- Game total (Over/Under) leg pricing — currently those markets are skipped
- Per-sport Kelly fraction (smaller for noisier sports)
- Time-to-close filter (only trade markets closing within 24h — sportsbook lines sharpest then)
- Live trading mode (currently paper only)
- More sportsbook coverage for player props (currently 1-2 books per prop)
- Wire `harvester_cache` into `sports_paper_test.py` scan call (reads cache from disk automatically once enabled)

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

> I'm working on an automated sports arbitrage bot at `C:\Users\mpere\Documents\Claude\Projects\Arb Project\arb-bot`. It paper-trades Kalshi KXMVE multi-leg sports markets vs sportsbook consensus prices from The Odds API, with versioned strategies (currently v1 baseline + v2 filtered). Bankroll is $2k, currently +12.5%. Read `CONTEXT.md` in the project root for full state. I want to [DESCRIBE TASK].
