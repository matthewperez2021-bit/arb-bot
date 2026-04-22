"""
position_tracker.py — SQLite-backed trade log and P&L tracker.

Persists every executed trade to a local SQLite database. This is the source
of truth for:
  - What positions are currently open
  - How much capital is deployed
  - Historical P&L per trade and in aggregate
  - Win rate, average edge, drawdown metrics

Schema design:
  positions table  — one row per arb trade, tracks full lifecycle
  fills table      — one row per leg fill (for post-trade audit)
  sessions table   — one row per bot run (tracks session-level P&L)

All monetary values stored in USD. Kalshi cents are converted before insertion.

Reference: polymarket_kalshi_arb_context.md.docx § 9 — Position Management & P&L
"""

import sqlite3
import logging
import json
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple
from contextlib import contextmanager

from config.settings import SQLITE_DB_PATH

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────────────────────────────────

SCHEMA_SQL = """
-- Main positions table
CREATE TABLE IF NOT EXISTS positions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_at           TEXT NOT NULL,
    closed_at           TEXT,
    status              TEXT NOT NULL DEFAULT 'open',   -- open | closed | naked | error
    mode                TEXT NOT NULL DEFAULT 'paper',  -- paper | live

    -- Kalshi leg
    kalshi_ticker       TEXT NOT NULL,
    kalshi_title        TEXT,
    kalshi_side         TEXT NOT NULL,   -- yes | no
    kalshi_price        REAL NOT NULL,   -- 0.0–1.0
    kalshi_contracts    INTEGER NOT NULL,
    kalshi_order_id     TEXT,
    kalshi_fill_price   REAL,            -- actual average fill

    -- Polymarket leg
    poly_question       TEXT,
    poly_token_id       TEXT NOT NULL,
    poly_side           TEXT NOT NULL,   -- BUY
    poly_price          REAL NOT NULL,   -- 0.0–1.0
    poly_size_usd       REAL NOT NULL,   -- USDC spent
    poly_order_id       TEXT,
    poly_fill_price     REAL,            -- actual average fill

    -- Economics
    gross_cost          REAL NOT NULL,   -- kalshi_price*contracts + poly_size_usd
    expected_profit     REAL,            -- at time of entry
    actual_profit       REAL,            -- filled in at close
    actual_profit_pct   REAL,

    -- Metadata
    match_score         REAL,
    llm_verified        INTEGER,         -- 0/1
    edge_per_day        REAL,
    days_to_resolution  INTEGER,
    close_reason        TEXT,            -- resolved_yes | resolved_no | manual | error
    notes               TEXT
);

-- Individual leg fills for audit trail
CREATE TABLE IF NOT EXISTS fills (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id     INTEGER NOT NULL REFERENCES positions(id),
    platform        TEXT NOT NULL,       -- kalshi | polymarket
    order_id        TEXT,
    filled_at       TEXT NOT NULL,
    contracts       INTEGER NOT NULL,
    fill_price      REAL NOT NULL,
    cost_usd        REAL NOT NULL
);

-- Session tracking
CREATE TABLE IF NOT EXISTS sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    mode            TEXT NOT NULL DEFAULT 'paper',
    trades_executed INTEGER DEFAULT 0,
    gross_pnl       REAL DEFAULT 0.0,
    fees_paid       REAL DEFAULT 0.0,
    net_pnl         REAL DEFAULT 0.0
);

-- Indices for common queries
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_positions_ticker ON positions(kalshi_ticker);
CREATE INDEX IF NOT EXISTS idx_fills_position   ON fills(position_id);
"""


# ─────────────────────────────────────────────────────────────────────────────
# Tracker
# ─────────────────────────────────────────────────────────────────────────────

class PositionTracker:
    """
    SQLite-backed persistence layer for all trade activity.

    Thread safety: SQLite with check_same_thread=False and WAL mode.
    Each write uses a context manager to ensure commits.
    """

    def __init__(self, db_path: str = SQLITE_DB_PATH):
        import os
        os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else ".", exist_ok=True)
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row   # Return dict-like rows
        self._init_db()

    def _init_db(self):
        """Create tables and enable WAL for concurrent read performance."""
        self.conn.executescript(SCHEMA_SQL)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.commit()
        logger.info(f"PositionTracker initialized: {self.db_path}")

    @contextmanager
    def _tx(self):
        """Context manager for write transactions."""
        try:
            yield self.conn
            self.conn.commit()
        except Exception as e:
            self.conn.rollback()
            raise

    # ── Write operations ──────────────────────────────────────────────────────

    def log_position(
        self,
        opp,
        contracts: int,
        k_avg_price: Optional[float] = None,
        p_avg_price: Optional[float] = None,
        total_cost: Optional[float] = None,
        mode: str = "paper",
    ) -> int:
        """
        Insert a new position record. Returns the position ID.

        Args:
            opp:          ArbOpportunity from arb_detector.py
            contracts:    Number of contracts actually filled
            k_avg_price:  Actual Kalshi fill price (defaults to opp.kalshi_price)
            p_avg_price:  Actual Poly fill price (defaults to opp.poly_price)
            total_cost:   Total USD spent (defaults to computed from prices)
            mode:         "paper" | "live"
        """
        k_fill = k_avg_price if k_avg_price is not None else opp.kalshi_price
        p_fill = p_avg_price if p_avg_price is not None else opp.poly_price
        cost   = total_cost if total_cost is not None else (k_fill + p_fill) * contracts

        now = _utcnow()
        with self._tx():
            cursor = self.conn.execute("""
                INSERT INTO positions (
                    opened_at, status, mode,
                    kalshi_ticker, kalshi_title, kalshi_side,
                    kalshi_price, kalshi_contracts, kalshi_fill_price,
                    poly_token_id, poly_question, poly_side,
                    poly_price, poly_size_usd, poly_fill_price,
                    gross_cost, expected_profit,
                    match_score, llm_verified,
                    edge_per_day, days_to_resolution
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                now, "open", mode,
                opp.kalshi_ticker,
                getattr(opp, "kalshi_title", None),
                opp.kalshi_side,
                opp.kalshi_price, contracts, k_fill,
                opp.poly_token_id,
                getattr(opp, "poly_question", None),
                opp.poly_side,
                opp.poly_price,
                p_fill * contracts,   # poly_size_usd
                p_fill,
                cost,
                opp.net_profit_pct * cost,
                getattr(opp, "match_score", None),
                1 if getattr(opp, "llm_verified", False) else 0,
                getattr(opp, "edge_per_day", None),
                getattr(opp, "days_to_resolution", None),
            ))
            position_id = cursor.lastrowid

        logger.info(f"Logged position #{position_id}: {opp.kalshi_ticker} "
                    f"({contracts} contracts, ${cost:.2f}, mode={mode})")
        return position_id

    def log_fill(self, position_id: int, platform: str, order_id: Optional[str],
                 contracts: int, fill_price: float, cost_usd: float):
        """Log an individual leg fill for audit trail."""
        with self._tx():
            self.conn.execute("""
                INSERT INTO fills (position_id, platform, order_id, filled_at,
                                   contracts, fill_price, cost_usd)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (position_id, platform, order_id, _utcnow(),
                  contracts, fill_price, cost_usd))

    def close_position(
        self,
        position_id: int,
        actual_profit: float,
        close_reason: str = "resolved",
        notes: Optional[str] = None,
    ) -> bool:
        """
        Mark a position as closed and record the actual P&L.

        Args:
            position_id:   From log_position() return value
            actual_profit: Net USD profit (after fees, before tax).
                           Negative = loss. E.g. -0.50 for a $0.50 loss.
            close_reason:  "resolved_yes" | "resolved_no" | "manual" | "error"
            notes:         Optional freeform annotation

        Returns:
            True if found and updated, False if position_id not found.
        """
        row = self.conn.execute(
            "SELECT gross_cost FROM positions WHERE id=?", (position_id,)
        ).fetchone()
        if row is None:
            logger.warning(f"close_position: no position with id={position_id}")
            return False

        gross_cost = row["gross_cost"] or 1.0
        profit_pct = actual_profit / gross_cost if gross_cost else 0.0

        with self._tx():
            self.conn.execute("""
                UPDATE positions SET
                    closed_at         = ?,
                    status            = 'closed',
                    actual_profit     = ?,
                    actual_profit_pct = ?,
                    close_reason      = ?,
                    notes             = ?
                WHERE id = ?
            """, (_utcnow(), actual_profit, profit_pct,
                  close_reason, notes, position_id))

        logger.info(f"Closed position #{position_id}: "
                    f"P&L=${actual_profit:+.2f} ({profit_pct:+.2%}), "
                    f"reason={close_reason}")
        return True

    def mark_naked(self, position_id: int, notes: str = ""):
        """Flag a position as having naked exposure (requires attention)."""
        with self._tx():
            self.conn.execute(
                "UPDATE positions SET status='naked', notes=? WHERE id=?",
                (notes, position_id)
            )

    # ── Read operations ───────────────────────────────────────────────────────

    def get_position(self, position_id: int) -> Optional[Dict]:
        """Fetch a single position by ID."""
        row = self.conn.execute(
            "SELECT * FROM positions WHERE id=?", (position_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_open_positions(self) -> List[Dict]:
        """All currently open positions."""
        rows = self.conn.execute(
            "SELECT * FROM positions WHERE status='open' ORDER BY opened_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_deployed_usd(self) -> float:
        """Total USD currently deployed in open positions."""
        row = self.conn.execute(
            "SELECT COALESCE(SUM(gross_cost), 0) AS total FROM positions WHERE status='open'"
        ).fetchone()
        return row["total"] if row else 0.0

    def get_all_positions(self, mode: Optional[str] = None,
                          limit: int = 500) -> List[Dict]:
        """All positions, optionally filtered by mode."""
        if mode:
            rows = self.conn.execute(
                "SELECT * FROM positions WHERE mode=? ORDER BY opened_at DESC LIMIT ?",
                (mode, limit)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM positions ORDER BY opened_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_closed_positions(self, mode: Optional[str] = None) -> List[Dict]:
        """All closed positions for P&L analysis."""
        if mode:
            rows = self.conn.execute(
                "SELECT * FROM positions WHERE status='closed' AND mode=? "
                "ORDER BY closed_at DESC",
                (mode,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM positions WHERE status='closed' ORDER BY closed_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    # ── P&L summary ───────────────────────────────────────────────────────────

    def get_pnl_summary(self, mode: Optional[str] = None) -> Dict[str, Any]:
        """
        Aggregate P&L statistics across all closed positions.

        Returns a dict with:
            total_trades, wins, losses, win_rate,
            gross_pnl, avg_profit_per_trade, best_trade, worst_trade,
            avg_edge, avg_edge_per_day, avg_days_to_resolution,
            total_deployed, open_positions
        """
        mode_filter = "AND mode=?" if mode else ""
        params = (mode,) if mode else ()

        # Closed positions stats
        row = self.conn.execute(f"""
            SELECT
                COUNT(*)                              AS total_trades,
                SUM(CASE WHEN actual_profit > 0 THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN actual_profit <= 0 THEN 1 ELSE 0 END) AS losses,
                COALESCE(SUM(actual_profit), 0)       AS gross_pnl,
                COALESCE(AVG(actual_profit), 0)       AS avg_profit,
                COALESCE(MAX(actual_profit), 0)       AS best_trade,
                COALESCE(MIN(actual_profit), 0)       AS worst_trade,
                COALESCE(AVG(actual_profit_pct), 0)   AS avg_edge,
                COALESCE(AVG(edge_per_day), 0)        AS avg_epd,
                COALESCE(AVG(days_to_resolution), 0)  AS avg_days
            FROM positions
            WHERE status='closed' {mode_filter}
        """, params).fetchone()

        open_row = self.conn.execute(f"""
            SELECT
                COUNT(*)                         AS open_count,
                COALESCE(SUM(gross_cost), 0)     AS deployed_usd
            FROM positions
            WHERE status='open' {mode_filter}
        """, params).fetchone()

        total  = row["total_trades"] or 0
        wins   = row["wins"] or 0

        return {
            "total_trades":          total,
            "wins":                  wins,
            "losses":                row["losses"] or 0,
            "win_rate":              (wins / total) if total > 0 else 0.0,
            "gross_pnl":             round(row["gross_pnl"], 4),
            "avg_profit_per_trade":  round(row["avg_profit"], 4),
            "best_trade_usd":        round(row["best_trade"], 4),
            "worst_trade_usd":       round(row["worst_trade"], 4),
            "avg_edge_pct":          round(row["avg_edge"], 4),
            "avg_edge_per_day":      round(row["avg_epd"], 2),
            "avg_days_to_resolution": round(row["avg_days"], 1),
            "open_positions":        open_row["open_count"] or 0,
            "deployed_usd":          round(open_row["deployed_usd"], 2),
            "mode":                  mode or "all",
        }

    def get_daily_pnl(self, days: int = 30) -> List[Dict]:
        """
        Daily P&L aggregation for the last N days.

        Returns list of {date, trades, pnl, cumulative_pnl} dicts.
        """
        rows = self.conn.execute("""
            SELECT
                DATE(closed_at)               AS date,
                COUNT(*)                      AS trades,
                COALESCE(SUM(actual_profit), 0) AS daily_pnl
            FROM positions
            WHERE status='closed'
              AND closed_at >= DATE('now', ? || ' days')
            GROUP BY DATE(closed_at)
            ORDER BY date
        """, (f"-{days}",)).fetchall()

        cumulative = 0.0
        result = []
        for r in rows:
            cumulative += r["daily_pnl"]
            result.append({
                "date": r["date"],
                "trades": r["trades"],
                "pnl": round(r["daily_pnl"], 4),
                "cumulative_pnl": round(cumulative, 4),
            })
        return result

    def get_open_exposure_by_market(self) -> List[Dict]:
        """
        Summary of open positions grouped by kalshi_ticker.
        Useful for checking concentration risk.
        """
        rows = self.conn.execute("""
            SELECT
                kalshi_ticker,
                kalshi_title,
                COUNT(*)              AS num_positions,
                SUM(gross_cost)       AS total_deployed,
                SUM(kalshi_contracts) AS total_contracts,
                MIN(opened_at)        AS first_opened
            FROM positions
            WHERE status='open'
            GROUP BY kalshi_ticker
            ORDER BY total_deployed DESC
        """).fetchall()
        return [dict(r) for r in rows]

    # ── Session management ────────────────────────────────────────────────────

    def start_session(self, mode: str = "paper") -> int:
        """Record a new bot session. Returns session_id."""
        with self._tx():
            cur = self.conn.execute(
                "INSERT INTO sessions (started_at, mode) VALUES (?, ?)",
                (_utcnow(), mode)
            )
        return cur.lastrowid

    def end_session(self, session_id: int, trades: int,
                    gross_pnl: float, fees: float):
        """Close a session record."""
        with self._tx():
            self.conn.execute("""
                UPDATE sessions SET
                    ended_at       = ?,
                    trades_executed = ?,
                    gross_pnl      = ?,
                    fees_paid      = ?,
                    net_pnl        = ?
                WHERE id = ?
            """, (_utcnow(), trades, gross_pnl, fees,
                  gross_pnl - fees, session_id))

    # ── Housekeeping ──────────────────────────────────────────────────────────

    def prune_old_closed(self, keep_days: int = 90):
        """Remove closed positions older than keep_days (keeps DB compact)."""
        with self._tx():
            self.conn.execute("""
                DELETE FROM positions
                WHERE status='closed'
                  AND closed_at < DATE('now', ? || ' days')
            """, (f"-{keep_days}",))
        logger.info(f"Pruned positions older than {keep_days} days.")

    def export_csv(self, filepath: str, mode: Optional[str] = None):
        """Export all positions to CSV for spreadsheet analysis."""
        import csv
        positions = self.get_all_positions(mode=mode, limit=10000)
        if not positions:
            logger.info("No positions to export.")
            return
        with open(filepath, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=positions[0].keys())
            writer.writeheader()
            writer.writerows(positions)
        logger.info(f"Exported {len(positions)} positions to {filepath}")

    def close(self):
        """Close the SQLite connection cleanly."""
        self.conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _utcnow() -> str:
    """ISO-8601 UTC timestamp string."""
    return datetime.now(timezone.utc).isoformat()
