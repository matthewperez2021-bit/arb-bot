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
| `config/settings.py` | Global constants (API keys, fees, exchange URLs, thresholds). |
| `scripts/sports_paper_test.py` | The main scan + trade pipeline. Takes `--strategy` and `--strategies`. |
| `scripts/sports_scheduler.py` | Hourly background loop. Runs resolver then paper test. |
| `scripts/resolve_trades.py` | Checks Kalshi for settled markets, updates P&L + bankroll. |
| `scripts/view_trades.py` | Read-only viewer. Flags: `--status`, `--strategies`, `--session`, `--all` |
| `scripts/analyze_performance.py` | Slices settled trades by side/sport/edge/legs/etc. |
| `scripts/calibration_report.py` | Predicted vs actual win rate by edge bucket + sport, plus CLV summary. |
| `scripts/seed_odds_api_historical.py` | Seeds `data/historical_odds/{sport}/` with snapshots from The Odds API `/v4/historical` endpoints. |
| `scripts/seed_kalshi_historical.py` | Seeds `data/historical_kalshi/` with per-market candlestick price history from Kalshi. Modes: `--from-trades`, `--ticker`, `--series`. |
| `scripts/backtest.py` | Replays seeded snapshots → per-(snapshot, event) consensus timeline. Optional `--csv` export. |
| `clients/kalshi.py` | Kalshi API client (auth, orderbook, market lookup, `is_market_resolved`). |
| `clients/odds_api.py` | Odds API client (h2h + player props + **totals** + **historical**, devig logic, caches). |
| `detection/odds_arb_scanner.py` | Core: parses KXMVE titles, prices legs (incl. **totals**), applies same-game correlation uplift, computes net edge. |
| `detection/kxmve_parser.py` | Parses parlay titles into structured legs (team_win, player_over, total_over). |
| `data/arb_positions.db` | SQLite DB. Tables: `sports_paper_trades`, `bankroll`, `strategy_bankrolls`. |
| `data/historical_odds/{sport}/` | Per-sport historical snapshots fetched by `seed_odds_api_historical.py`. |
| `pyproject.toml` | pytest config: scopes test collection to `tests/` only. |

---

## Daily Commands (memorize these)

### Scan / trade
```powershell
python scripts/sports_paper_test.py                           # uses ACTIVE_STRATEGY
python scripts/sports_paper_test.py --strategy v2             # force a version
python scripts/sports_paper_test.py --strategies v1,v2        # A/B (each gets FULL bankroll)
python scripts/sports_scheduler.py --strategies v1,v2         # hourly background loop
```

### Historical seeding (Odds API paid tier)
```powershell
python scripts/seed_odds_api_historical.py --sport mlb --max-snapshots 5      # quick smoke
python scripts/seed_odds_api_historical.py --sport mlb,nba --markets h2h,spreads,totals
python scripts/seed_odds_api_historical.py --start 2026-05-06T00:00:00Z --end 2026-04-01T00:00:00Z
```
Walks the `previous_timestamp` chain backward. Writes snapshots to `data/historical_odds/{sport_key}/{iso}.json`. Quota-aware (`--quota-floor`). Skips files already on disk unless `--overwrite`.

### Kalshi historical seeding
```powershell
python scripts/seed_kalshi_historical.py --from-trades --limit 5    # 5 most recent settled trades
python scripts/seed_kalshi_historical.py --from-trades              # ALL settled trade tickers
python scripts/seed_kalshi_historical.py --ticker KXMVE...-... --start 2026-05-01T00:00:00Z --end 2026-05-06T00:00:00Z
python scripts/seed_kalshi_historical.py --series KXMVECROSSCATEGORY --limit 20
python scripts/seed_kalshi_historical.py --period 1                 # minute-resolution (3.5-day max window)
```
Writes per-ticker candle files to `data/historical_kalshi/{ticker}.json`. Default period is hourly. Skips files already on disk unless `--overwrite`. `--from-trades` mode auto-derives the window per ticker from `opened_at`/`resolved_at` (with `--pad-hours` padding).

### Backtest replay (offline)
Two modes — `--mode timeline` (default) and `--mode strategy-replay`.

```powershell
# timeline: consensus drift across snapshots
python scripts/backtest.py                                    # all sports
python scripts/backtest.py --sport mlb                        # one sport
python scripts/backtest.py --sport mlb,nba --csv              # combined CSV export

# strategy-replay: would v1/v2/v3/v4 each have placed each settled trade?
python scripts/backtest.py --mode strategy-replay             # all 4 strategies
python scripts/backtest.py --mode strategy-replay --enrich    # use Kalshi candles for entry price
python scripts/backtest.py --mode strategy-replay --strategies v2,v4 --verbose
```

**Strategy-replay output** (per strategy): trades placed, won, lost, total stake, P&L, ROI, win rate. With `--verbose`, also prints rejection-reason breakdown. With `--enrich`, attempts to read seeded Kalshi candles to derive an actual entry price near `opened_at`; falls back to the trade's recorded `kalshi_ask`. Reports candle-coverage % so you know how much of the analysis used real candles vs. recorded prices.

**Sample first-run finding (90 settled trades):** v2's tighter filters placed only 4 trades but won 3 — empirical confirmation the 4-8% edge band + NBA/NHL exclusion + 3-leg cap is doing real work, not just dropping volume.

**Limitations:**
- In-progress games' lines freeze; their snapshot consensus probs reflect score, not pre-game truth. Filter on `commence_time > snapshot_iso` for clean training data.
- Strategy-replay uses each trade's recorded `fair_prob` and `net_edge` — it doesn't yet re-derive `fair_prob` from a matching Odds API snapshot at `opened_at`. Adding that requires parsing each KXMVE title via `KXMVEParser` and pricing legs from the snapshot — same pipeline the live scanner runs. Worth doing once seeded snapshots overlap settled-trade timestamps.

### Automation (Windows Scheduled Tasks)
Both seeders run nightly via Windows Task Scheduler so historical data accumulates without manual intervention. Set up ONCE on the bot-hosting machine:

```powershell
git pull
.\scripts\setup_scheduled_tasks.ps1                          # registers both tasks
```

Tasks created:
| Name                    | Schedule       | Action                                                      | Cost         |
|-------------------------|----------------|-------------------------------------------------------------|--------------|
| `ArbBot-Seed-OddsAPI`   | Daily 02:00    | `python scripts/seed_odds_api_historical.py --max-snapshots 12` | ~120 quota/day (under 20K monthly) |
| `ArbBot-Seed-Kalshi`    | Daily 02:30    | `python scripts/seed_kalshi_historical.py --from-trades`        | Free (Kalshi reads not metered) |

Per-task stdout/stderr is appended to `data/scheduled_tasks/{TaskName}.log` for diagnostics.

`resolve_trades.py` is intentionally NOT scheduled separately — `sports_scheduler.py` already runs it at the top of every 20-minute scan.

**Verify registration:**
```powershell
Get-ScheduledTask -TaskName "ArbBot-*" | Select-Object TaskName, State, @{N="NextRun";E={(Get-ScheduledTaskInfo $_).NextRunTime}}
```

**Re-run safe (idempotent):** the script uses `-Force` so re-running with changed schedules just overwrites the old definitions.

**Remove later:**
```powershell
.\scripts\setup_scheduled_tasks.ps1 -Remove
```

**Switching machines:** the tasks live in Windows Task Scheduler, NOT in git. After moving the bot to a new machine, run the setup script again on the new one and `-Remove` on the old one. Otherwise both will fire and duplicate-fetch.

### Inspecting scheduler output
The bot writes `data/sports_scheduler.log` in UTF-8 with Unicode box-drawing borders from Rich. PowerShell defaults to Windows-1252 reading, which mangles them. Use the helper instead of raw `Get-Content`:

```powershell
.\scripts\view-log.ps1                            # last 100 lines, default filter
.\scripts\view-log.ps1 -Tail 300                  # last 300 lines
.\scripts\view-log.ps1 -Tail 200 -Match "v2|v4"   # custom regex
.\scripts\view-log.ps1 -All -Match ""             # full file, no filter
```

Default filter matches `STRATEGY|filters dropped|Trades placed|Run #` — the lines you care about for tracking what each strategy is doing per scan.

### Diagnostic queries
```powershell
# Trades placed since a given timestamp, grouped by strategy
python -c "import sqlite3; c=sqlite3.connect('data/arb_positions.db'); print('Since 2026-05-06 17:00:'); [print(' ',r) for r in c.execute(\"SELECT strategy_version, COUNT(*) FROM sports_paper_trades WHERE opened_at > '2026-05-06T17:00:00' GROUP BY strategy_version\").fetchall()]"
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

### Sync between machines (single-writer)
The trade DB (`data/arb_positions.db`) is tracked in git as of commit `22c9082` so trade history transfers across machines. **Only one machine writes at a time** — SQLite doesn't merge.

```powershell
# On the machine that will run the bot — pull latest before running
git pull

# After a scan/resolve session, push so the other machine stays current
git add data/arb_positions.db
git commit -m "trades: <short summary>"
git push
```

What's tracked: `data/arb_positions.db` only.
What's NOT tracked (regenerable): `*.log`, `historical_odds/`, `player_prop_cache.json`, `snapshots/`, `*.db-shm`, `*.db-wal`, `arb_positions.db.bak`.

To recover trade history on a fresh clone: `git pull` is enough — no import step.

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
              - team_win/team_spread → devigged sportsbook consensus (Odds API)
              - player_over → devigged Over/Under from prop cache
              - total_over → devigged Over/Under from totals_cache

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

### 3. Snapshot archiving (Odds API historical)
`scripts/seed_odds_api_historical.py` walks The Odds API's `previous_timestamp` chain backward and writes per-sport JSON snapshots to `data/historical_odds/{sport_key}/{iso}.json`. `backtest.py` (still a stub) will eventually replay these. Run on demand — not part of the live scan loop.

---

## Open Roadmap Items (not yet built)

- **Snapshot-based fair_prob enrichment in strategy-replay** — currently
  `--mode strategy-replay` uses each trade's recorded `fair_prob`. To re-derive
  it from the matching Odds API snapshot at `opened_at`, parse the trade's
  KXMVE title via `KXMVEParser`, price each team_win leg from the snapshot,
  and apply the same-game correlation uplift. This unlocks "what if our
  pricing model had been different?" analysis on top of the strategy comparison.
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
- **APIs used:** Kalshi (real account), The Odds API (paid tier with historical-snapshot access)
- **Repo:** https://github.com/matthewperez2021-bit/arb-bot (push to `main`)

---

## Quick Brief When Starting a New Chat

> I'm working on an automated sports arbitrage bot at `C:\Users\mpere\Documents\Claude\Projects\Arb Project\arb-bot`. It paper-trades Kalshi KXMVE multi-leg sports markets vs sportsbook consensus prices from The Odds API (live + historical snapshots). Active strategy is v2 (4-8% edge band, NBA/NHL excluded, half-Kelly). 90 settled trades on v1 showed MLB +88% / MLS -2% / NBA -47% / NHL -46% ROI, and edges above 8% are model error. Recent shipped work: same-game correlation uplift, totals leg pricing, CLV tracking, calibration_report.py, and Odds API historical seeder (replaced abandoned OddsHarvester scraper). Read `CONTEXT.md` for full state. I want to [DESCRIBE TASK].
