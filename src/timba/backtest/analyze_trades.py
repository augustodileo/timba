"""Trade analysis: PnL breakdowns per coin, interval, price bucket, signal patterns."""

import json
import sys
from collections import defaultdict
from pathlib import Path

from timba.backtest.common import load_trades


def _pnl_for(trade: dict) -> float:
    """Calculate PnL from a trade record."""
    pnl = trade.get("pnl")
    if pnl is not None and pnl != 0:
        return pnl
    contracts = trade.get("contracts", 5)
    buy = trade.get("buy_price", 0)
    if not buy:
        return 0.0
    if trade["type"].endswith("_win") or trade["type"] == "win":
        return (1.0 - buy) * contracts
    elif trade["type"].endswith("_loss") or trade["type"] == "loss":
        return -buy * contracts
    return 0.0


def _wr(wins: int, total: int) -> str:
    return f"{wins/total:.1%}" if total > 0 else "  n/a"


def _group_stats(trades: list[dict]) -> dict:
    wins = sum(1 for t in trades if t["type"].endswith("_win") or t["type"] == "win")
    losses = len(trades) - wins
    pnl = sum(_pnl_for(t) for t in trades)
    return {"count": len(trades), "wins": wins, "losses": losses, "pnl": pnl}


def _load_evs_by_id(data_dir: Path, strategy: str) -> dict[int, dict]:
    """Load EVs keyed by ev.id from SQLite DBs."""
    import sqlite3
    evs = {}
    db_files = sorted(data_dir.glob("bot_*.db"))
    current = data_dir / "bot.db"
    if current.exists():
        db_files.append(current)
    for db_file in db_files:
        try:
            conn = sqlite3.connect(str(db_file), timeout=5)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM evs WHERE strategy = ?", (strategy,)
            ).fetchall()
            for r in rows:
                d = dict(r)
                # Unpack extras JSON
                extras_raw = d.pop("extras", None)
                if extras_raw:
                    try:
                        d.update(json.loads(extras_raw))
                    except (json.JSONDecodeError, TypeError):
                        pass
                evs[d["id"]] = d
            conn.close()
        except Exception:
            continue
    return evs


def _load_ticks_by_id(data_dir: Path, tick_ids: set[int] | None = None) -> dict[int, dict]:
    """Load ticks keyed by tick.id from SQLite DBs.

    If tick_ids is provided, only loads those specific ticks (much faster).
    """
    import sqlite3
    ticks = {}
    db_files = sorted(data_dir.glob("bot_*.db"))
    current = data_dir / "bot.db"
    if current.exists():
        db_files.append(current)
    for db_file in db_files:
        try:
            conn = sqlite3.connect(str(db_file), timeout=5)
            conn.row_factory = sqlite3.Row
            if tick_ids is not None:
                # Batch query for specific tick IDs
                ids_in_db = tick_ids - set(ticks.keys())
                if not ids_in_db:
                    conn.close()
                    continue
                # SQLite IN clause with parameter binding
                batch = list(ids_in_db)
                for i in range(0, len(batch), 500):
                    chunk = batch[i:i+500]
                    placeholders = ",".join("?" * len(chunk))
                    rows = conn.execute(
                        f"SELECT * FROM ticks WHERE id IN ({placeholders})", chunk
                    ).fetchall()
                    for r in rows:
                        d = dict(r)
                        d["signal_rev"] = bool(d.get("signal_rev"))
                        ticks[d["id"]] = d
            else:
                rows = conn.execute("SELECT * FROM ticks").fetchall()
                for r in rows:
                    d = dict(r)
                    d["signal_rev"] = bool(d.get("signal_rev"))
                    ticks[d["id"]] = d
            conn.close()
        except Exception:
            continue
    return ticks


def _enrich_trades(trades: list[dict], evs: dict, ticks: dict) -> list[dict]:
    """Join trade → ev → tick and merge signal fields into each trade."""
    enriched = []
    for t in trades:
        ev = evs.get(t.get("ev_id"), {})
        tick = ticks.get(ev.get("tick_id"), {})
        t["_signal_dir"] = tick.get("signal_dir", "")
        t["_signal_chg"] = tick.get("signal_chg", 0.0)
        t["_signal_trend_sec"] = tick.get("signal_trend_sec", 0.0)
        t["_signal_rev"] = tick.get("signal_rev", False)
        t["_mid_up"] = tick.get("mid_up", 0.0)
        t["_mid_down"] = tick.get("mid_down", 0.0)
        t["_fill_up"] = tick.get("fill_up", 0.0)
        t["_fill_down"] = tick.get("fill_down", 0.0)
        t["_remaining"] = ev.get("remaining", 0.0)
        t["_progress"] = ev.get("progress", 0.0)
        t["_ev_up"] = ev.get("ev_up", 0.0)
        t["_ev_down"] = ev.get("ev_down", 0.0)
        t["_p_up"] = ev.get("p_up", 0.0)
        t["_p_down"] = ev.get("p_down", 0.0)
        t["_has_signal"] = bool(tick)
        enriched.append(t)
    return enriched


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    if n % 2 == 0:
        return (s[n // 2 - 1] + s[n // 2]) / 2
    return s[n // 2]


def _signal_stats(trades: list[dict]) -> dict:
    """Compute signal statistics for a group of trades."""
    chgs = [abs(t["_signal_chg"]) for t in trades if t["_has_signal"]]
    trends = [t["_signal_trend_sec"] for t in trades if t["_has_signal"]]
    remaining = [t["_remaining"] for t in trades if t["_remaining"] > 0]
    progress = [t["_progress"] for t in trades if t["_has_signal"]]
    mids = [t.get("midpoint") or t.get("buy_price", 0) for t in trades]
    pnls = [_pnl_for(t) for t in trades]
    total_signal = sum(1 for t in trades if t["_has_signal"])
    rev_count = sum(1 for t in trades if t["_signal_rev"])
    agree_count = sum(1 for t in trades if t["_has_signal"]
                      and t.get("side") == t["_signal_dir"])
    disagree_count = sum(1 for t in trades if t["_has_signal"]
                         and t.get("side") and t["_signal_dir"] != "flat"
                         and t.get("side") != t["_signal_dir"])
    flat_count = sum(1 for t in trades if t["_has_signal"]
                     and t["_signal_dir"] == "flat")
    return {
        "chg_avg": _avg(chgs), "chg_med": _median(chgs),
        "chg_min": min(chgs) if chgs else 0, "chg_max": max(chgs) if chgs else 0,
        "trend_avg": _avg(trends), "trend_med": _median(trends),
        "trend_min": min(trends) if trends else 0, "trend_max": max(trends) if trends else 0,
        "remaining_avg": _avg(remaining),
        "remaining_min": min(remaining) if remaining else 0,
        "remaining_max": max(remaining) if remaining else 0,
        "progress_avg": _avg(progress),
        "midpoint_avg": _avg(mids), "midpoint_med": _median(mids),
        "midpoint_min": min(mids) if mids else 0, "midpoint_max": max(mids) if mids else 0,
        "pnl_min": min(pnls) if pnls else 0, "pnl_max": max(pnls) if pnls else 0,
        "rev_count": rev_count,
        "rev_pct": rev_count / max(1, total_signal) * 100,
        "agree_count": agree_count,
        "agree_pct": agree_count / max(1, total_signal) * 100,
        "disagree_count": disagree_count,
        "disagree_pct": disagree_count / max(1, total_signal) * 100,
        "flat_count": flat_count,
        "n": total_signal,
    }


def analyze_main(data_dir: Path, strategy: str | None = None) -> None:
    """Print trade analysis with rich tables."""
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    console = Console()
    trades = load_trades(data_dir, strategy=strategy)

    if not trades:
        console.print("[red]No trade files found.[/]")
        sys.exit(1)

    # Filter to resolved trades (not watching/skipped without resolution)
    bets = [t for t in trades if t.get("type") in ("win", "loss", "paper_win", "paper_loss")]
    skips = [t for t in trades if t.get("type", "").startswith("skip_")]

    strat_label = strategy.upper() if strategy else "ALL"
    console.print(f"\n[bold]Trade Analysis: {strat_label}[/] — {len(trades)} total, {len(bets)} bets, {len(skips)} skips\n")

    if not bets and not skips:
        console.print("[yellow]No trades to analyze.[/]")
        return

    if not bets:
        console.print("[yellow]No bet trades — only skips.[/]")

    if not bets:
        # Skip to pass analysis
        if skips:
            skip_wins = [t for t in skips if t["type"] == "skip_win"]
            skip_losses = [t for t in skips if t["type"] == "skip_loss"]
            console.print(Panel(
                f"Total skipped: {len(skips)}\n"
                f"Would have won: {len(skip_wins)}\n"
                f"Would have lost: {len(skip_losses)}",
                title="Skip Analysis",
                border_style="yellow",
            ))
        return

    # ── Price distribution ──
    price_table = Table(title="Price Distribution", border_style="cyan")
    price_table.add_column("Price Range", width=12)
    price_table.add_column("Wins", justify="right", width=5)
    price_table.add_column("Losses", justify="right", width=6)
    price_table.add_column("Total", justify="right", width=5)
    price_table.add_column("WR", justify="right", width=6)
    price_table.add_column("PnL", justify="right", width=10)
    price_table.add_column("Avg PnL", justify="right", width=8)

    buckets = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0})
    for t in bets:
        price = t.get("buy_price", 0)
        bucket = min(int(price * 10), 9)
        label = f"${bucket/10:.1f}-${(bucket+1)/10:.1f}"
        won = t["type"].endswith("_win") or t["type"] == "win"
        pnl = _pnl_for(t)
        if won:
            buckets[label]["wins"] += 1
        else:
            buckets[label]["losses"] += 1
        buckets[label]["pnl"] += pnl

    tw, tl, tp = 0, 0, 0.0
    for i in range(10):
        label = f"${i/10:.1f}-${(i+1)/10:.1f}"
        b = buckets[label]
        total = b["wins"] + b["losses"]
        if total == 0:
            continue
        tw += b["wins"]
        tl += b["losses"]
        tp += b["pnl"]
        wr = b["wins"] / total * 100
        avg = b["pnl"] / total
        color = "green" if b["pnl"] >= 0 else "red"
        price_table.add_row(
            label, str(b["wins"]), str(b["losses"]), str(total),
            f"{wr:.0f}%", f"[{color}]${b['pnl']:+.3f}[/]", f"[{color}]${avg:+.3f}[/]",
        )

    price_table.add_section()
    wr = tw / max(1, tw + tl) * 100
    avg = tp / max(1, tw + tl)
    color = "green" if tp >= 0 else "red"
    price_table.add_row(
        "[bold]TOTAL[/]", f"[bold]{tw}[/]", f"[bold]{tl}[/]", f"[bold]{tw+tl}[/]",
        f"[bold]{wr:.0f}%[/]", f"[bold {color}]${tp:+.3f}[/]", f"[bold {color}]${avg:+.3f}[/]",
    )
    console.print(price_table)
    console.print()

    # ── Per coin ──
    coin_table = Table(title="Per Coin", border_style="cyan")
    coin_table.add_column("Coin", width=6, style="bold")
    coin_table.add_column("Trades", justify="right", width=6)
    coin_table.add_column("W", justify="right", width=4)
    coin_table.add_column("L", justify="right", width=4)
    coin_table.add_column("WR", justify="right", width=6)
    coin_table.add_column("PnL", justify="right", width=10)

    by_coin = defaultdict(list)
    for t in bets:
        by_coin[t.get("coin", "?")].append(t)

    for coin in sorted(by_coin):
        s = _group_stats(by_coin[coin])
        color = "green" if s["pnl"] >= 0 else "red"
        coin_table.add_row(
            coin.upper(), str(s["count"]), str(s["wins"]), str(s["losses"]),
            _wr(s["wins"], s["count"]), f"[{color}]${s['pnl']:+.3f}[/]",
        )
    console.print(coin_table)
    console.print()

    # ── Per interval ──
    iv_table = Table(title="Per Interval", border_style="cyan")
    iv_table.add_column("Int", width=6, style="bold")
    iv_table.add_column("Trades", justify="right", width=6)
    iv_table.add_column("W", justify="right", width=4)
    iv_table.add_column("L", justify="right", width=4)
    iv_table.add_column("WR", justify="right", width=6)
    iv_table.add_column("PnL", justify="right", width=10)

    by_iv = defaultdict(list)
    for t in bets:
        by_iv[t.get("interval", "?")].append(t)

    for iv in sorted(by_iv):
        s = _group_stats(by_iv[iv])
        color = "green" if s["pnl"] >= 0 else "red"
        iv_table.add_row(
            iv, str(s["count"]), str(s["wins"]), str(s["losses"]),
            _wr(s["wins"], s["count"]), f"[{color}]${s['pnl']:+.3f}[/]",
        )
    console.print(iv_table)
    console.print()

    # ── Per coin + interval ──
    combo_table = Table(title="Per Coin × Interval", border_style="cyan")
    combo_table.add_column("Coin", width=6, style="bold")
    combo_table.add_column("Int", width=4)
    combo_table.add_column("Trades", justify="right", width=6)
    combo_table.add_column("W", justify="right", width=4)
    combo_table.add_column("L", justify="right", width=4)
    combo_table.add_column("WR", justify="right", width=6)
    combo_table.add_column("PnL", justify="right", width=10)
    combo_table.add_column("$/trade", justify="right", width=8)

    by_combo = defaultdict(list)
    for t in bets:
        by_combo[(t.get("coin", "?"), t.get("interval", "?"))].append(t)

    for (coin, iv) in sorted(by_combo):
        s = _group_stats(by_combo[(coin, iv)])
        ppt = s["pnl"] / s["count"] if s["count"] else 0
        color = "green" if s["pnl"] >= 0 else "red"
        combo_table.add_row(
            coin.upper(), iv, str(s["count"]), str(s["wins"]), str(s["losses"]),
            _wr(s["wins"], s["count"]), f"[{color}]${s['pnl']:+.3f}[/]", f"[{color}]${ppt:+.3f}[/]",
        )
    console.print(combo_table)
    console.print()

    # ── Signal pattern analysis (join trade → ev → tick) ──
    if strategy:
        evs = _load_evs_by_id(data_dir, strategy)
        # Only load the ticks referenced by EVs (not all 5M+ ticks)
        needed_tick_ids = {ev.get("tick_id") for ev in evs.values() if ev.get("tick_id")}
        ticks_by_id = _load_ticks_by_id(data_dir, tick_ids=needed_tick_ids)
        all_enriched = _enrich_trades(trades, evs, ticks_by_id)
        signal_count = sum(1 for t in all_enriched if t["_has_signal"])

        if signal_count > 0:
            bet_wins = [t for t in all_enriched if t["type"] in ("win", "paper_win") and t["_has_signal"]]
            bet_losses = [t for t in all_enriched if t["type"] in ("loss", "paper_loss") and t["_has_signal"]]
            skip_wins = [t for t in all_enriched if t["type"] == "skip_win" and t["_has_signal"]]
            skip_losses = [t for t in all_enriched if t["type"] == "skip_loss" and t["_has_signal"]]
            [t for t in all_enriched if t["type"].startswith("fail_") and t["_has_signal"]]

            fail_wins = [t for t in all_enriched if t["type"] == "fail_win" and t["_has_signal"]]
            fail_losses = [t for t in all_enriched if t["type"] == "fail_loss" and t["_has_signal"]]

            groups = [
                ("Bet Wins", bet_wins),
                ("Bet Losses", bet_losses),
                ("Fail Wins", fail_wins),
                ("Fail Losses", fail_losses),
                ("Skip Wins", skip_wins),
                ("Skip Losses", skip_losses),
            ]

            # ── Market state at decision ──
            mkt_table = Table(title="Market & PnL at Decision", border_style="magenta")
            mkt_table.add_column("Group", width=11, style="bold", no_wrap=True)
            mkt_table.add_column("N", justify="right", width=4)
            mkt_table.add_column("Mid avg", justify="right", width=7)
            mkt_table.add_column("Mid min", justify="right", width=7)
            mkt_table.add_column("Mid max", justify="right", width=7)
            mkt_table.add_column("Remain", justify="right", width=6)
            mkt_table.add_column("Rem min", justify="right", width=7)
            mkt_table.add_column("Rem max", justify="right", width=7)
            mkt_table.add_column("PnL min", justify="right", width=8)
            mkt_table.add_column("PnL max", justify="right", width=8)

            for label, group in groups:
                if not group:
                    mkt_table.add_row(label, "0", *["—"] * 8)
                    continue
                s = _signal_stats(group)
                mkt_table.add_row(
                    label, str(s["n"]),
                    f"${s['midpoint_avg']:.3f}",
                    f"${s['midpoint_min']:.3f}", f"${s['midpoint_max']:.3f}",
                    f"{s['remaining_avg']:.1f}s",
                    f"{s['remaining_min']:.1f}s", f"{s['remaining_max']:.1f}s",
                    f"${s['pnl_min']:.2f}", f"${s['pnl_max']:.2f}",
                )

            console.print(mkt_table)
            console.print()

            # ── Signal at decision ──
            sig_table = Table(title="Coinbase Signal at Decision", border_style="magenta")
            sig_table.add_column("Group", width=11, style="bold", no_wrap=True)
            sig_table.add_column("N", justify="right", width=4)
            sig_table.add_column("Chg avg", justify="right", width=8)
            sig_table.add_column("Chg min", justify="right", width=8)
            sig_table.add_column("Chg max", justify="right", width=8)
            sig_table.add_column("Trend avg", justify="right", width=9)
            sig_table.add_column("Trend min", justify="right", width=9)
            sig_table.add_column("Trend max", justify="right", width=9)
            sig_table.add_column("Reversed", justify="right", width=8)
            sig_table.add_column("Agree", justify="right", width=8)
            sig_table.add_column("Disagree", justify="right", width=8)

            for label, group in groups:
                if not group:
                    sig_table.add_row(label, "0", *["—"] * 9)
                    continue
                s = _signal_stats(group)
                sig_table.add_row(
                    label, str(s["n"]),
                    f"{s['chg_avg']:.4f}%",
                    f"{s['chg_min']:.4f}%", f"{s['chg_max']:.4f}%",
                    f"{s['trend_avg']:.0f}s",
                    f"{s['trend_min']:.0f}s", f"{s['trend_max']:.0f}s",
                    f"{s['rev_count']}/{s['n']} ({s['rev_pct']:.0f}%)",
                    f"{s['agree_count']}/{s['n']} ({s['agree_pct']:.0f}%)",
                    f"{s['disagree_count']}/{s['n']} ({s['disagree_pct']:.0f}%)",
                )

            console.print(sig_table)
            console.print()

            # ── Glossary ──
            console.print(Panel(
                "[bold]Chg[/]: absolute Coinbase price change (%) from market open to decision time\n"
                "[bold]Trend[/]: seconds the Coinbase price has been moving in the same direction\n"
                "[bold]Reversed[/]: signal flipped direction within the last 30s before decision\n"
                "[bold]Agree[/]: Coinbase direction matches the CLOB favorite side we bet on\n"
                "[bold]Disagree[/]: Coinbase says the opposite direction of our bet\n"
                "[bold]Mid[/]: CLOB midpoint of the side we bet on (higher = more certain)\n"
                "[bold]Remain[/]: seconds left until market close when we placed the bet\n"
                "[bold]PnL[/]: profit/loss per trade (win: $1-price, loss: -$price per contract)",
                title="Column Glossary",
                border_style="dim",
            ))

    # ── Skip analysis ──
    if skips:
        skip_wins = [t for t in skips if t["type"] == "skip_win"]
        skip_losses = [t for t in skips if t["type"] == "skip_loss"]

        console.print(Panel(
            f"Total skipped: {len(skips)}\n"
            f"Would have won: {len(skip_wins)}\n"
            f"Would have lost: {len(skip_losses)}",
            title="Skip Analysis",
            border_style="yellow",
        ))

    # ── Loss deep-dive: EVs + ticks timeline for each loss ──
    bet_losses = [t for t in bets if t["type"] in ("loss", "paper_loss")]
    if bet_losses:
        console.print(Panel(
            f"{len(bet_losses)} losing trades — full EV + tick timeline below",
            title="Loss Deep Dive",
            border_style="red bold",
        ))

        _print_loss_timelines(console, data_dir, bet_losses, strategy)

    console.print()


def _print_loss_timelines(console: object, data_dir: Path, losses: list[dict], strategy: str) -> None:
    """For each loss, load all EVs for that slug and their ticks, print timeline."""
    import sqlite3

    from rich.table import Table

    db_files = sorted(data_dir.glob("bot_*.db"))
    current = data_dir / "bot.db"
    if current.exists():
        db_files.append(current)

    for trade in losses:
        slug = trade["slug"]
        side = trade.get("side", "?")
        buy_price = trade.get("buy_price", 0)
        pnl = _pnl_for(trade)
        sniped = (trade.get("sniped_at") or "")[:19]

        console.print(f"\n[bold red]{'─'*70}[/]")
        console.print(
            f"[bold]{trade['coin'].upper()} {trade['interval']}[/] | "
            f"BET [bold]{side.upper()}[/] @${buy_price:.4f} | "
            f"pnl=[red]${pnl:.3f}[/] | {sniped}"
        )

        # Load all EVs for this slug across all DBs
        all_evs = []
        for db_file in db_files:
            try:
                conn = sqlite3.connect(str(db_file), timeout=5)
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM evs WHERE slug = ? AND strategy = ? ORDER BY id",
                    (slug, strategy),
                ).fetchall()
                all_evs.extend([dict(r) for r in rows])
                conn.close()
            except Exception:
                continue

        # Load all ticks for this slug
        all_ticks = {}
        for db_file in db_files:
            try:
                conn = sqlite3.connect(str(db_file), timeout=5)
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM ticks WHERE slug = ? ORDER BY ts", (slug,),
                ).fetchall()
                for r in rows:
                    d = dict(r)
                    all_ticks[d["id"]] = d
                conn.close()
            except Exception:
                continue

        if not all_evs:
            console.print("  [dim]No EVs found for this slug[/]")
            continue

        console.print(f"  [dim]{len(all_evs)} EVs, {len(all_ticks)} ticks[/]\n")

        # Print EV timeline with tick data
        tbl = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
        tbl.add_column("EV#", style="dim", width=6)
        tbl.add_column("Time", width=8)
        tbl.add_column("Remain", justify="right", width=6)
        tbl.add_column("Prog", justify="right", width=5)
        tbl.add_column("mid_up", justify="right", width=7)
        tbl.add_column("mid_dn", justify="right", width=7)
        tbl.add_column("ev_up", justify="right", width=7)
        tbl.add_column("ev_dn", justify="right", width=7)
        tbl.add_column("signal", width=6)
        tbl.add_column("chg%", justify="right", width=7)
        tbl.add_column("trend", justify="right", width=5)

        for ev in all_evs:
            tick = all_ticks.get(ev.get("tick_id"), {})
            ts = tick.get("ts", "")
            ts_short = ts[-12:-4] if len(ts) > 12 else ts

            remaining = ev.get("remaining") or 0
            progress = ev.get("progress") or 0
            ev_up = ev.get("ev_up") or 0
            ev_down = ev.get("ev_down") or 0
            mid_up = tick.get("mid_up", 0)
            mid_down = tick.get("mid_down", 0)
            sig_dir = tick.get("signal_dir", "")
            sig_chg = tick.get("signal_chg", 0)
            sig_trend = tick.get("signal_trend_sec", 0)

            ev_up_style = "green" if ev_up > 0 else ("red" if ev_up < 0 else "")
            ev_dn_style = "green" if ev_down > 0 else ("red" if ev_down < 0 else "")

            tbl.add_row(
                str(ev["id"]),
                ts_short,
                f"{remaining:.1f}s",
                f"{progress:.0%}",
                f"{mid_up:.3f}",
                f"{mid_down:.3f}",
                f"[{ev_up_style}]{ev_up:+.4f}[/]" if ev_up_style else f"{ev_up:+.4f}",
                f"[{ev_dn_style}]{ev_down:+.4f}[/]" if ev_dn_style else f"{ev_down:+.4f}",
                sig_dir,
                f"{sig_chg:+.4f}",
                f"{sig_trend:.0f}s",
            )

        console.print(tbl)

        # Show post-bet ticks (the reversal)
        sorted_ticks = sorted(all_ticks.values(), key=lambda t: t["ts"])
        if sorted_ticks and all_evs:
            last_ev_tick_id = all_evs[-1].get("tick_id", 0)
            after_ev = False
            post_ticks = []
            for t in sorted_ticks:
                if t["id"] == last_ev_tick_id:
                    after_ev = True
                    continue
                if after_ev:
                    post_ticks.append(t)

            if post_ticks:
                sample = post_ticks[:3] + post_ticks[-3:] if len(post_ticks) > 6 else post_ticks
                console.print(f"\n  [dim]Post-bet ticks ({len(post_ticks)} total):[/]")
                for t in sample:
                    ts_short = t["ts"][-12:-4] if len(t["ts"]) > 12 else t["ts"]
                    winner = "UP" if t["mid_up"] >= 0.5 else "DOWN"
                    style = "green" if winner == side.upper() else "red"
                    console.print(
                        f"    {ts_short} | mid_up={t['mid_up']:.3f} mid_down={t['mid_down']:.3f} "
                        f"| signal={t['signal_dir']:5s} {t['signal_chg']:+.4f}% "
                        f"| → [{style}]{winner}[/]"
                    )

            last = sorted_ticks[-1]
            final_winner = "UP" if last["mid_up"] >= 0.5 else "DOWN"
            console.print(
                f"\n  [bold]Resolution:[/] mid_up={last['mid_up']:.3f} → "
                f"[{'green' if final_winner == side.upper() else 'red bold'}]{final_winner}[/] "
                f"(we bet {side.upper()})"
            )
