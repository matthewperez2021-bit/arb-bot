"""
_display.py — Shared rich-based rendering helpers for the sports bot.

Centralises console output so sports_paper_test.py and resolve_trades.py
produce a consistent, color-coded look. Falls back to plain text if rich
is unavailable (so the bot still runs on minimal installs).
"""
from __future__ import annotations

import os
import sys
from typing import Dict, List, Optional

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.text import Text
    from rich import box
    _HAS_RICH = True
except ImportError:
    _HAS_RICH = False


# Force UTF-8 + color on Windows console
if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_console: Optional["Console"] = Console(force_terminal=True, legacy_windows=False) if _HAS_RICH else None


def console():
    return _console


def _color_money(value: float) -> str:
    """Return a rich markup string colored green/red by sign."""
    if not _HAS_RICH:
        return f"{'+'if value >= 0 else ''}${value:,.2f}"
    color = "green" if value > 0 else ("red" if value < 0 else "white")
    sign  = "+" if value >= 0 else ""
    return f"[{color}]{sign}${value:,.2f}[/{color}]"


def _color_pct(value: float) -> str:
    if not _HAS_RICH:
        return f"{'+'if value >= 0 else ''}{value:.1f}%"
    color = "green" if value > 0 else ("red" if value < 0 else "white")
    sign  = "+" if value >= 0 else ""
    return f"[{color}]{sign}{value:.1f}%[/{color}]"


def print_strategy_header(strategy_name: str, bankroll: float, max_per_trade: float,
                          notes: str, mode: str = "PAPER") -> None:
    """Replaces the old '+============+' ASCII box at the top of each run."""
    if not _HAS_RICH:
        print()
        print("  +" + "=" * 64 + "+")
        print(f"  |  STRATEGY: {strategy_name:<6}  Bankroll: ${bankroll:,.2f}  "
              f"Max/Trade: ${max_per_trade:.0f}  MODE: {mode}  |")
        print(f"  |  {notes[:60]:<62}|")
        print("  +" + "=" * 64 + "+")
        print()
        return

    style = {"v1": "cyan", "v2": "magenta", "v3": "yellow", "v4": "green"}.get(strategy_name, "white")
    title = Text(f"STRATEGY {strategy_name.upper()}", style=f"bold {style}")
    body  = Text.assemble(
        ("Bankroll: ", "dim"),
        (f"${bankroll:,.2f}", "bold green" if bankroll >= 0 else "bold red"),
        ("    Max/Trade: ", "dim"),
        (f"${max_per_trade:.0f}", "white"),
        ("    Mode: ", "dim"),
        (mode, "yellow" if mode == "PAPER" else "bold red"),
        ("\n", ""),
        (notes[:80], "italic dim"),
    )
    _console.print(Panel(body, title=title, border_style=style, padding=(0, 1)))


def print_portfolio_summary(strategy_name: str, bankroll: float,
                             total_deployed: float, total_expected: float,
                             total_contracts: int, n_trades: int,
                             avg_edge: float, avg_books: float) -> None:
    """Replace the 'PORTFOLIO SUMMARY' block at the end of run_paper_test."""
    utilization  = (total_deployed / bankroll * 100) if bankroll > 0 else 0
    expected_roi = (total_expected / total_deployed * 100) if total_deployed > 0 else 0
    cap_roi      = (total_expected / bankroll * 100) if bankroll > 0 else 0

    if not _HAS_RICH:
        print(f"\n  -- PORTFOLIO SUMMARY ({strategy_name}) --")
        print(f"    Bankroll: ${bankroll:,.2f}  |  Deployed: ${total_deployed:,.2f} "
              f"({utilization:.1f}%)  |  Trades: {n_trades}  |  ExpProfit: ${total_expected:.2f}")
        return

    t = Table(title=f"Portfolio Summary — {strategy_name}", show_header=False,
              box=box.ROUNDED, border_style="cyan", padding=(0, 1))
    t.add_column("Metric", style="dim")
    t.add_column("Value")

    t.add_row("Bankroll",         f"[bold]${bankroll:,.2f}[/bold]")
    t.add_row("Capital deployed", f"${total_deployed:,.2f}  ({utilization:.1f}%)")
    t.add_row("Capital idle",     f"${bankroll - total_deployed:,.2f}  ({100-utilization:.1f}%)")
    t.add_row("Trades placed",    f"{n_trades}  ({total_contracts:,} contracts)")
    t.add_row("Average edge",     f"{avg_edge*100:.2f}%  across {avg_books:.1f} books/leg")
    t.add_row("Expected profit",  _color_money(total_expected))
    t.add_row("Return on deployed", _color_pct(expected_roi))
    t.add_row("Return on capital",  _color_pct(cap_roi))
    _console.print(t)


def print_resolve_summary(starting: float, current: float,
                          total_trades: int, wins: int, losses: int,
                          last_updated: str,
                          per_strategy_rows: Optional[List[Dict]] = None) -> None:
    """Render the 'Bankroll' block at the end of resolve_trades.py."""
    pnl     = current - starting
    pnl_pct = (pnl / starting * 100) if starting else 0
    win_rate = (wins / total_trades * 100) if total_trades else 0

    if not _HAS_RICH:
        print(f"\n  -- Bankroll --------------------------------------------------")
        print(f"  Starting:       ${starting:>10,.2f}")
        print(f"  Current:        ${current:>10,.2f}  "
              f"({'+'if pnl>=0 else ''}{pnl_pct:.1f}%)")
        print(f"  Realised P&L:   {'+'if pnl>=0 else ''}${pnl:>9.2f}")
        print(f"  Settled trades: {total_trades:>10}  "
              f"({wins} won / {losses} lost  |  {win_rate:.0f}% win rate)")
        return

    t = Table(title="Bankroll Status", show_header=False, box=box.ROUNDED,
              border_style="cyan", padding=(0, 1))
    t.add_column("Metric", style="dim")
    t.add_column("Value")
    t.add_row("Starting",  f"${starting:,.2f}")
    t.add_row("Current",   Text.from_markup(
        f"[bold]${current:,.2f}[/bold]  ({_color_pct(pnl_pct)})"))
    t.add_row("Realised P&L", _color_money(pnl))
    wr_color = "green" if win_rate >= 50 else ("yellow" if win_rate >= 30 else "red")
    t.add_row("Settled trades",
              f"{total_trades}  ({wins}W / {losses}L  |  "
              f"[{wr_color}]{win_rate:.0f}% win rate[/{wr_color}])")
    t.add_row("Last updated", last_updated[:19])
    _console.print(t)

    if per_strategy_rows:
        st = Table(title="Per-Strategy Breakdown", box=box.SIMPLE_HEAVY,
                   border_style="dim", header_style="bold cyan")
        st.add_column("Strategy", style="bold")
        st.add_column("Current",   justify="right")
        st.add_column("P&L",       justify="right")
        st.add_column("Trades",    justify="right")
        st.add_column("W/L",       justify="right")
        st.add_column("Win Rate",  justify="right")
        for r in per_strategy_rows:
            s_pnl = r["current_capital"] - r["starting_capital"]
            s_wr  = (r["wins"] / r["total_trades"] * 100) if r["total_trades"] else 0
            wrc   = "green" if s_wr >= 50 else ("yellow" if s_wr >= 30 else "red")
            color = {"v1": "cyan", "v2": "magenta", "v3": "yellow", "v4": "green"}.get(r["strategy_name"], "white")
            st.add_row(
                f"[{color}]{r['strategy_name']}[/{color}]",
                f"${r['current_capital']:,.2f}",
                _color_money(s_pnl),
                f"{r['total_trades']}",
                f"{r['wins']}W / {r['losses']}L",
                f"[{wrc}]{s_wr:.0f}%[/{wrc}]",
            )
        _console.print(st)


def print_scan_dashboard(scan_label: str, mode: str, main_current: float,
                          main_starting: float, main_settled: int,
                          main_wins: int, main_losses: int,
                          per_strategy: List[Dict]) -> None:
    """
    End-of-scan unified dashboard. Always prints — used as the
    grep target by sports_scheduler.py for console output.

    per_strategy items must contain:
        name, bankroll, open_deployed, trades_this_scan,
        deployed_this_scan, expected_this_scan, realized_pnl, win_rate
    """
    pnl     = main_current - main_starting
    pnl_pct = (pnl / main_starting * 100) if main_starting else 0
    main_wr = (main_wins / max(main_wins + main_losses, 1)) * 100

    if not _HAS_RICH:
        # Plain-text fallback (also helps the scheduler's grep)
        print()
        print("  +" + "=" * 70 + "+")
        print(f"  |  SCAN DASHBOARD  -  {scan_label:<46}|")
        print(f"  |  Mode: {mode}  |  Main bankroll: ${main_current:,.2f}  "
              f"({'+'if pnl>=0 else ''}{pnl_pct:.1f}%)  |  "
              f"Settled: {main_settled} ({main_wr:.0f}% WR)" + " " * 5 + "|")
        print("  +" + "=" * 70 + "+")
        for s in per_strategy:
            settled_n = s.get("settled_count", 0)
            wr_str = f"{s['win_rate']:.0f}% WR ({settled_n})" if settled_n else "no trades"
            print(f"  | {s['name']:<4}  Bankroll ${s['bankroll']:>8.2f}  "
                  f"Open ${s['open_deployed']:>6.2f}  "
                  f"+{s['trades_this_scan']} trd  "
                  f"Exp ${s['expected_this_scan']:>+7.4f}  "
                  f"Realized ${s['realized_pnl']:>+7.2f}  {wr_str:<14}|")
        print("  +" + "=" * 70 + "+")
        return

    # Header panel
    pnl_text = Text.assemble(
        ("Main bankroll: ", "dim"),
        (f"${main_current:,.2f}", "bold"),
        (" (", ""),
        Text.from_markup(_color_pct(pnl_pct)),
        (")    ", ""),
        ("Settled: ", "dim"),
        (f"{main_settled}", "bold"),
        ("  ", ""),
        (f"({main_wr:.0f}% WR)",
         "green" if main_wr >= 50 else ("yellow" if main_wr >= 30 else "red")),
        ("    Mode: ", "dim"),
        (mode, "yellow" if mode == "PAPER" else "bold red"),
    )
    _console.print(Panel(pnl_text, title=f"[bold cyan]SCAN DASHBOARD[/bold cyan]  •  {scan_label}",
                         border_style="cyan", padding=(0, 1)))

    # Per-strategy table
    t = Table(box=box.SIMPLE_HEAVY, border_style="dim",
              header_style="bold white on grey23", padding=(0, 1))
    t.add_column("Strategy",  style="bold")
    t.add_column("Bankroll",  justify="right")
    t.add_column("Open",      justify="right")
    t.add_column("New Trades", justify="right")
    t.add_column("Exp P&L",   justify="right")
    t.add_column("Realized",  justify="right")
    t.add_column("Win Rate",  justify="right")
    for s in per_strategy:
        color = {"v1": "cyan", "v2": "magenta", "v3": "yellow", "v4": "green"}.get(s["name"], "white")
        wr        = s["win_rate"]
        settled_n = s.get("settled_count", 0)
        # Distinguish "no settled trades yet" (—) from "0% win rate" (red 0%)
        if settled_n == 0:
            wr_txt = "[dim]—[/dim]"
        else:
            wrc = ("green"  if wr >= 50
                   else "yellow" if wr >= 30
                   else "red")
            wr_txt = f"[{wrc}]{wr:.0f}%[/{wrc}]  [dim]({settled_n})[/dim]"
        new_t = s["trades_this_scan"]
        new_text = (f"[bold green]+{new_t}[/bold green]" if new_t > 0
                    else "[dim]0[/dim]")
        t.add_row(
            f"[{color}]{s['name']}[/{color}]",
            f"${s['bankroll']:,.2f}",
            f"${s['open_deployed']:,.2f}",
            new_text,
            _color_money(s["expected_this_scan"]),
            _color_money(s["realized_pnl"]),
            wr_txt,
        )
    _console.print(t)
