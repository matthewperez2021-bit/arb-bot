"""
strategies.py — Versioned strategy registry for the arb bot.

Each Strategy bundles all tunable parameters that affect which opportunities
get traded and how they're sized. Versions are append-only — never edit a
past version, only add new ones. The registry itself is the changelog.

To switch strategies: change ACTIVE_STRATEGY at the bottom of this file.
To run two side-by-side: pass --strategies v1,v2 to sports_paper_test.py.
"""

from dataclasses import dataclass, field, asdict
from typing import FrozenSet, Dict


@dataclass(frozen=True)
class Strategy:
    """Immutable bundle of tunable parameters for one strategy version."""
    name: str                                # "v1", "v2", ...
    created_at: str                          # ISO date "2026-04-29"
    notes: str                               # one-line description of the change

    # ── Edge / sizing ────────────────────────────────────────────────────────
    min_net_edge: float                      # e.g. 0.015 = 1.5% min net edge to trade
    max_per_trade_usd: float                 # max $ per single trade
    max_total_deployed_usd: float            # max $ across all open positions
    kelly_fraction: float                    # 0.5 = half-Kelly

    # ── Quality filters ──────────────────────────────────────────────────────
    min_books: int                           # min sportsbooks needed for fair_prob
    max_legs: int                            # max parlay legs (99 = no cap)
    max_trusted_edge_pct: float              # discard "too good to be true" (100 = no cap)
    excluded_sports: FrozenSet[str]          # e.g. frozenset({"basketball_nba"})
    allowed_sides: FrozenSet[str]            # {"yes","no"} or {"no"} only

    def as_dict(self) -> dict:
        """Serialize for logging or display. frozensets become sorted lists."""
        d = asdict(self)
        d["excluded_sports"] = sorted(self.excluded_sports)
        d["allowed_sides"]   = sorted(self.allowed_sides)
        return d


# ─────────────────────────────────────────────────────────────────────────────
# REGISTRY — append-only. Never edit or remove past versions.
# ─────────────────────────────────────────────────────────────────────────────

STRATEGIES: Dict[str, Strategy] = {
    "v1": Strategy(
        name="v1",
        created_at="2026-04-28",
        notes=(
            "Baseline: original settings with no sport/leg/edge filters. "
            "First 59 settled trades produced 20% win rate, +$249 P&L, +15.6% ROI."
        ),
        min_net_edge=0.015,
        max_per_trade_usd=50.0,
        max_total_deployed_usd=2000.0,
        kelly_fraction=0.5,
        min_books=2,
        max_legs=99,
        max_trusted_edge_pct=100.0,
        excluded_sports=frozenset(),
        allowed_sides=frozenset({"yes", "no"}),
    ),
    "v2": Strategy(
        name="v2",
        created_at="2026-05-04",                # redefined before any v2 trades placed
        notes=(
            "Quality filters tightened from v1 calibration analysis (90 settled trades): "
            "min edge 4%, max edge 8% (12%+ bucket showed -33% ROI / CalibF 0.56 — "
            "apparent edges above 8% are model error, not real opportunities), "
            "cap parlays at 3 legs (correlation gets unreliable past 3), "
            "exclude NBA/NHL (CalibF 0.68/0.57, ROI -47%/-46% in v1 data), "
            "min 2 books for fair_prob. Edge-accuracy work in same commit: "
            "same-game correlation uplift + totals leg pricing."
        ),
        min_net_edge=0.04,
        max_per_trade_usd=50.0,
        max_total_deployed_usd=2000.0,
        kelly_fraction=0.5,
        min_books=2,
        max_legs=3,
        max_trusted_edge_pct=8.0,                 # tightened from 12 -> 8
        excluded_sports=frozenset({                # re-added based on calibration data
            "basketball_nba",
            "icehockey_nhl",
        }),
        allowed_sides=frozenset({"yes", "no"}),
    ),
    "v3": Strategy(
        name="v3",
        created_at="2026-04-30",
        notes=(
            "Ultra-aggressive: full Kelly, $100 max/trade, 0.5% min edge, "
            "no sport/leg/side/edge-cap filters, single-book pricing accepted."
        ),
        min_net_edge=0.005,                  # 0.5% — capture nearly every mispricing
        max_per_trade_usd=100.0,             # double v1/v2 — bigger bets on each edge
        max_total_deployed_usd=2000.0,
        kelly_fraction=1.0,                  # full Kelly — maximum theoretical growth
        min_books=1,                         # accept single-book fair_prob estimates
        max_legs=99,                         # no parlay-leg cap
        max_trusted_edge_pct=100.0,          # no "too good to be true" filter
        excluded_sports=frozenset(),         # trade every sport
        allowed_sides=frozenset({"yes", "no"}),
    ),
    "v4": Strategy(
        name="v4",
        created_at="2026-05-01",
        notes=(
            "High-volume small-stakes: $20 max/trade, 0.3% min edge, "
            "no filters — chase every micro-edge for steady singles."
        ),
        min_net_edge=0.003,                  # 0.3% — accept tiny edges
        max_per_trade_usd=20.0,              # hard cap per ticket
        max_total_deployed_usd=2000.0,
        kelly_fraction=0.5,                  # half-Kelly (cap binds first anyway)
        min_books=2,                         # keep at least some quality
        max_legs=99,                         # no parlay-leg cap
        max_trusted_edge_pct=100.0,          # no "too good to be true" filter
        excluded_sports=frozenset(),         # trade every sport
        allowed_sides=frozenset({"yes", "no"}),
    ),
    # ── Add v5, v6, ... here. Do NOT edit past versions. ─────────────────────
}


# Single source of truth for which strategy the scheduler / paper test runs.
# Override on the command line with --strategy <name>.
ACTIVE_STRATEGY: str = "v2"


def get(name: str | None = None) -> Strategy:
    """Look up a strategy by name. None → ACTIVE_STRATEGY."""
    key = name or ACTIVE_STRATEGY
    if key not in STRATEGIES:
        available = ", ".join(STRATEGIES.keys())
        raise KeyError(f"Unknown strategy '{key}'. Available: {available}")
    return STRATEGIES[key]


def all_versions() -> list:
    """Return every registered Strategy in insertion order."""
    return list(STRATEGIES.values())
