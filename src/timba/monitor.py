"""Terminal monitor — renders bot status using rich tables."""

import glob
import json
import os
import sys
from datetime import datetime, timezone

import yaml
from rich.columns import Columns
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

FULL_NAME_TO_COIN = {
    "bitcoin": "btc", "ethereum": "eth", "solana": "sol",
    "xrp": "xrp", "bnb": "bnb", "dogecoin": "doge", "hype": "hype",
}
IV_ORDER = {"5m": 0, "15m": 1, "1h": 2, "4h": 3}
RESERVED_KEYS = {"log_level", "portfolio", "polymarket"}


def parse_slug(slug):
    if "-updown-" in slug:
        parts = slug.split("-")
        return parts[0], parts[2]
    if "-up-or-down-" in slug:
        full = slug.split("-up-or-down-")[0]
        return FULL_NAME_TO_COIN.get(full, full), "1h"
    return "", ""


def calc_pnl(t):
    pnl = t.get("pnl", 0)
    if pnl:
        return pnl
    c = t.get("contracts", 5)
    fill = t.get("buy_price", 0)
    if not fill:
        return 0
    if t["type"].endswith("_win") or t["type"] == "win":
        return (1.0 - fill) * c
    elif t["type"].endswith("_loss") or t["type"] == "loss":
        return -fill * c
    return 0


def fmt_pnl(v):
    return f"[green]+${v:.3f}[/]" if v >= 0 else f"[red]-${abs(v):.3f}[/]"


def load_strategy_trades(data_dir, strategy):
    # Scan all SQLite DBs: rotated (bot_*.db) + current (bot.db)
    import sqlite3
    db_files = sorted(glob.glob(os.path.join(data_dir, "bot_*.db")))
    current = os.path.join(data_dir, "bot.db")
    if os.path.exists(current):
        db_files.append(current)

    trades = []
    for db_file in db_files:
        try:
            conn = sqlite3.connect(db_file, timeout=5)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM trades WHERE strategy = ? ORDER BY id",
                (strategy,),
            ).fetchall()
            conn.close()
            for r in rows:
                d = dict(r)
                d["redeemed"] = bool(d.get("redeemed"))
                extras_raw = d.pop("extras", None)
                if extras_raw:
                    try:
                        d.update(json.loads(extras_raw))
                    except (json.JSONDecodeError, TypeError):
                        pass
                trades.append(d)
        except Exception:
            continue

    if trades:
        trades.sort(key=lambda t: t.get("sniped_at") or "")
        return trades

    # Fallback to JSONL (pre-migration data or no bot.db)
    strat_dir = os.path.join(data_dir, strategy)
    if os.path.isdir(strat_dir):
        for tf in sorted(glob.glob(os.path.join(strat_dir, "trades_*.jsonl")))[-3:]:
            with open(tf) as fh:
                for line in fh:
                    try:
                        trades.append(json.loads(line))
                    except (json.JSONDecodeError, ValueError):
                        pass
    return trades


def fmt_time(ts_str):
    try:
        utc_dt = datetime.fromisoformat(ts_str)
        if utc_dt.tzinfo is None:
            utc_dt = utc_dt.replace(tzinfo=timezone.utc)
        return utc_dt.astimezone().strftime("%H:%M")
    except (ValueError, TypeError):
        return ts_str[11:16] if len(ts_str) > 16 else ""


def build_overview_and_trades(state, strategies, data_dir, bot_env, enabled_strategies=None):
    """Build overview panel with trades underneath, inside one panel."""
    code_ver = state.get("code_version", "?")
    portfolio = state.get("portfolio", 0)
    cash = state.get("cash", 0)
    pending = state.get("pending_redemption", 0)

    lines = []
    lines.append(f"Code: {code_ver}")
    lines.append("")
    pend = f"  Pend: ${pending:.2f}" if pending > 0 else ""
    lines.append(f"Portfolio: ${portfolio:.2f}")
    lines.append(f"Cash:      ${cash:.2f}{pend}")

    for sname, sdata in sorted(strategies.items()):
        # Only show strategies that are enabled in config
        if enabled_strategies and sname not in enabled_strategies:
            continue

        # Stats from trades (SQLite is source of truth)
        trades = load_strategy_trades(data_dir, sname)
        from collections import Counter
        type_counts = Counter(t.get("type", "") for t in trades)

        # Bets (filled)
        w = type_counts.get("win", 0)
        l = type_counts.get("loss", 0)
        pw = type_counts.get("paper_win", 0)
        pl = type_counts.get("paper_loss", 0)
        pnl = sum(calc_pnl(t) for t in trades if t.get("type") in ("win", "loss"))
        paper_pnl = sum(calc_pnl(t) for t in trades if t.get("type", "").startswith("paper"))

        # Fails (wanted to bet, couldn't fill — by market_mode)
        live_fail_w = sum(1 for t in trades if t.get("type") == "fail_win" and t.get("market_mode") == "live")
        live_fail_l = sum(1 for t in trades if t.get("type") == "fail_loss" and t.get("market_mode") == "live")

        paper_fail_w = sum(1 for t in trades if t.get("type") == "fail_win" and t.get("market_mode") == "paper")
        paper_fail_l = sum(1 for t in trades if t.get("type") == "fail_loss" and t.get("market_mode") == "paper")

        # Skips (below threshold — by market_mode)
        live_skip_w = sum(1 for t in trades if t.get("type") == "skip_win" and t.get("market_mode") == "live")
        live_skip_l = sum(1 for t in trades if t.get("type") == "skip_loss" and t.get("market_mode") == "live")
        paper_skip_w = sum(1 for t in trades if t.get("type") == "skip_win" and t.get("market_mode") == "paper")
        paper_skip_l = sum(1 for t in trades if t.get("type") == "skip_loss" and t.get("market_mode") == "paper")
        skip_s = sum(1 for t in trades if t.get("type") == "skip_none")

        # PnL per hour helper
        def _pnl_rate(trade_filter, total):
            filtered = [t for t in trades if trade_filter(t)]
            first = min((t.get("sniped_at", "") for t in filtered), default="")
            if not first:
                return ""
            from datetime import datetime, timezone
            try:
                elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(first)).total_seconds() / 60
                if elapsed > 1:
                    rate = total / (elapsed / 60)
                    c = "green" if rate >= 0 else "red"
                    return f" [{c}]${rate:+.2f}/h[/]"
            except (ValueError, TypeError):
                pass
            return ""

        lines.append("")
        lines.append(f"[bold]{sname.upper()}[/]")

        def _wl_line(label, wins, losses):
            total = wins + losses
            if total == 0:
                return None
            wr = wins / total * 100
            return f"  {label} {wins}W/{losses}L {wr:.0f}%"

        has_live = (w + l + live_fail_w + live_fail_l + live_skip_w + live_skip_l) > 0
        has_paper = (pw + pl + paper_fail_w + paper_fail_l + paper_skip_w + paper_skip_l + skip_s) > 0

        # Live section
        if has_live:
            lines.append("  [dim]── LIVE ──[/]")
            if w + l > 0:
                color = "green" if pnl >= 0 else "red"
                rate = _pnl_rate(lambda t: t.get("type") in ("win", "loss"), pnl)
                lines.append(f"  Bets:   {w}W/{l}L {w/max(1,w+l)*100:.0f}% [{color}]${pnl:+.3f}[/]{rate}")
            if live_fail_w + live_fail_l > 0:
                lines.append(_wl_line("Fails: ", live_fail_w, live_fail_l))
            if live_skip_w + live_skip_l > 0:
                lines.append(_wl_line("Skips: ", live_skip_w, live_skip_l))

        # Paper section
        if has_paper:
            lines.append("  [dim]── PAPER ──[/]")
            if pw + pl > 0:
                color = "green" if paper_pnl >= 0 else "red"
                rate = _pnl_rate(lambda t: t.get("type", "").startswith("paper"), paper_pnl)
                lines.append(f"  Bets:   {pw}W/{pl}L {pw/max(1,pw+pl)*100:.0f}% [{color}]${paper_pnl:+.3f}[/]{rate}")
            if paper_fail_w + paper_fail_l > 0:
                lines.append(_wl_line("Fails: ", paper_fail_w, paper_fail_l))
            if paper_skip_w + paper_skip_l + skip_s > 0:
                line = _wl_line("Skips: ", paper_skip_w, paper_skip_l)
                if line and skip_s > 0:
                    lines.append(f"{line} +{skip_s}S")
                elif line:
                    lines.append(line)
                elif skip_s > 0:
                    lines.append(f"  Skips:  {skip_s}S")

    # Trades section — split live and paper
    all_trades = []
    for sname in strategies:
        if enabled_strategies and sname not in enabled_strategies:
            continue
        for t in load_strategy_trades(data_dir, sname):
            t["_strategy"] = sname
            all_trades.append(t)

    def _fmt_trade(t):
        won = t["type"].endswith("_win") or t["type"] == "win"
        r = "[green]W[/]" if won else "[red]L[/]"
        _coin, _iv = parse_slug(t.get("slug", ""))
        side = t.get("side", "-")[0].upper() if t.get("side") else "-"
        fill = t.get("buy_price", 0)
        p = calc_pnl(t)
        sn = t.get("_strategy", "?")[:4]
        h = "H" if t.get("hedged") else " "
        ts = fmt_time(t.get("resolved_at") or t.get("sniped_at") or "")
        color = "green" if p >= 0 else "red"
        return f"{sn} {r} %-4s %3s %s $%.4f [{color}]%+.3f[/] %s %s" % (_coin.upper(), _iv, side, fill, p, h, ts)

    live_trades = [t for t in all_trades if t.get("type") in ("win", "loss")]
    live_trades.sort(key=lambda t: t.get("resolved_at") or t.get("sniped_at") or "", reverse=True)

    paper_trades = [t for t in all_trades if t.get("type") in ("paper_win", "paper_loss")]
    paper_trades.sort(key=lambda t: t.get("resolved_at") or t.get("sniped_at") or "", reverse=True)

    if live_trades:
        lines.append("")
        lines.append("[bold]─── LIVE ───[/]")
        for t in live_trades[:10]:
            lines.append(_fmt_trade(t))

    if paper_trades:
        lines.append("")
        lines.append("[bold]─── PAPER ───[/]")
        for t in paper_trades[:10]:
            lines.append(_fmt_trade(t))

    now = datetime.now().strftime("%d-%m-%Y %H:%M:%S")
    return Panel("\n".join(lines), title=f"[bold]MONITOR[/] [dim]({bot_env})[/]  {now}", border_style="blue")


def build_strategy_table(sname, scfg, data_dir):
    markets = scfg.get("markets", [])
    if not markets:
        return None

    trades = load_strategy_trades(data_dir, sname)

    has_live = any(m.get("mode", "live") == "live" for m in markets)
    has_paper = any(m.get("mode", "live") == "paper" for m in markets)

    # Build columns dynamically based on which modes exist
    table = Table(title=f"[bold]{sname.upper()}[/]", border_style="cyan", show_lines=False, pad_edge=False)
    table.add_column("COIN", style="bold", width=5)
    table.add_column("INT", width=4)
    table.add_column("MODE", width=5)
    if has_live:
        table.add_column("Live Bets", width=11)
        table.add_column("Live Fails", width=11)
        table.add_column("Live Skips", width=11)
        table.add_column("Live PnL", width=11, justify="right")
    if has_paper:
        table.add_column("Paper Bets", width=11)
        table.add_column("Paper Fails", width=11)
        table.add_column("Paper Skips", width=11)
        table.add_column("Paper PnL", width=11, justify="right")

    sorted_m = sorted(markets, key=lambda x: (x.get("coin", ""), IV_ORDER.get(x.get("interval", ""), 9)))
    prev_coin = None
    live_t = {"bw": 0, "bl": 0, "fw": 0, "fl": 0, "sw": 0, "sl": 0, "pnl": 0.0}
    paper_t = {"bw": 0, "bl": 0, "fw": 0, "fl": 0, "sw": 0, "sl": 0, "pnl": 0.0}

    # Number of columns: coin + int + mode + (4 per mode block)
    ncols = 3 + (4 if has_live else 0) + (4 if has_paper else 0)
    empty_row = [""] * ncols

    def _wl(w, l):
        return f"{w}W/{l}L" if w + l > 0 else "—"

    for m in sorted_m:
        coin = m.get("coin", "").upper()
        iv = m.get("interval", "")

        if prev_coin and prev_coin != coin:
            table.add_row(*empty_row)
        prev_coin = coin

        ct = [t for t in trades if parse_slug(t.get("slug", "")) == (m.get("coin", ""), iv)]

        raw_mode = m.get("mode", "live")
        is_paper = raw_mode == "paper"

        # Compute stats for this market
        if is_paper:
            bw = sum(1 for t in ct if t.get("type") == "paper_win")
            bl = sum(1 for t in ct if t.get("type") == "paper_loss")
            bpnl = sum(calc_pnl(t) for t in ct if t.get("type") in ("paper_win", "paper_loss"))
        else:
            bw = sum(1 for t in ct if t.get("type") == "win")
            bl = sum(1 for t in ct if t.get("type") == "loss")
            bpnl = sum(calc_pnl(t) for t in ct if t.get("type") in ("win", "loss"))

        fw = sum(1 for t in ct if t.get("type") == "fail_win")
        fl = sum(1 for t in ct if t.get("type") == "fail_loss")
        sw = sum(1 for t in ct if t.get("type") == "skip_win")
        sl = sum(1 for t in ct if t.get("type") == "skip_loss")
        ss = sum(1 for t in ct if t.get("type") == "skip_none")

        totals = paper_t if is_paper else live_t
        totals["bw"] += bw; totals["bl"] += bl
        totals["fw"] += fw; totals["fl"] += fl
        totals["sw"] += sw; totals["sl"] += sl
        totals["pnl"] += bpnl

        bets_s = _wl(bw, bl)
        fails_s = _wl(fw, fl)
        skips_s = _wl(sw, sl + ss) if sw + sl + ss > 0 else "—"
        pnl_s = fmt_pnl(bpnl) if bw + bl > 0 else "—"

        mode_str = m.get("mode", "live")
        mode_label = f"[green]{mode_str}[/]" if mode_str == "live" else f"[dim]{mode_str}[/]"

        # Build row: coin, interval, mode, then live block (if exists), then paper block (if exists)
        row = [coin, iv, mode_label]
        if has_live:
            if is_paper:
                row += ["—", "—", "—", "—"]
            else:
                row += [bets_s, fails_s, skips_s, pnl_s]
        if has_paper:
            if is_paper:
                row += [bets_s, fails_s, skips_s, pnl_s]
            else:
                row += ["—", "—", "—", "—"]
        table.add_row(*row)

    table.add_section()
    # Total row
    row = ["[bold]TOTAL[/]", "", ""]
    if has_live:
        row += [
            f"[bold]{_wl(live_t['bw'], live_t['bl'])}[/]",
            f"[bold]{_wl(live_t['fw'], live_t['fl'])}[/]",
            f"[bold]{_wl(live_t['sw'], live_t['sl'])}[/]",
            fmt_pnl(live_t["pnl"]),
        ]
    if has_paper:
        row += [
            f"[bold]{_wl(paper_t['bw'], paper_t['bl'])}[/]",
            f"[bold]{_wl(paper_t['fw'], paper_t['fl'])}[/]",
            f"[bold]{_wl(paper_t['sw'], paper_t['sl'])}[/]",
            fmt_pnl(paper_t["pnl"]),
        ]
    table.add_row(*row)

    return table


def _build_state_from_api(data_dir):
    """Try to get state from running bot's API, fall back to SQLite-only."""
    from timba.client import BotClient
    try:
        client = BotClient()
        if client.is_running():
            status = client.status()
            state = status.get("state", {})
            state.setdefault("code_version", status.get("version", "?"))
            return state
    except Exception:
        pass

    # Offline: build minimal state from SQLite
    import sqlite3
    state = {"portfolio": 0, "cash": 0, "pending_redemption": 0, "code_version": "offline"}
    db_file = os.path.join(data_dir, "bot.db")
    if os.path.exists(db_file):
        try:
            conn = sqlite3.connect(db_file, timeout=5)
            row = conn.execute(
                "SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE type IN ('win', 'loss')"
            ).fetchone()
            state["total_pnl"] = row[0] if row else 0
            conn.close()
        except Exception:
            pass
    return state


def run(data_dir, config_path, bot_env, check_mode=False):
    state = _build_state_from_api(data_dir)

    if check_mode:
        total_pnl = state.get("total_pnl", 0)
        print(f"Portfolio: ${state.get('portfolio', 0):.2f}  PnL: ${total_pnl:+.2f}")
        if state.get("cash", 0) <= 0 and state.get("portfolio", 0) <= 0:
            print("FAIL: No funds")
            sys.exit(1)
        print("OK")
        return

    config = {}
    if config_path and os.path.exists(config_path):
        with open(config_path) as fh:
            config = yaml.safe_load(fh) or {}

    console = Console()

    # Determine which strategies are enabled in config
    enabled = set()
    for sname in config:
        if sname in RESERVED_KEYS:
            continue
        scfg = config.get(sname)
        if isinstance(scfg, dict) and scfg.get("enabled"):
            enabled.add(sname)

    # Build strategies dict from enabled config keys (trades come from SQLite)
    strategies = {sname: {} for sname in enabled}

    # Left: overview + trades (only enabled strategies)
    overview = build_overview_and_trades(state, strategies, data_dir, bot_env, enabled_strategies=enabled or None)

    # Right: strategy tables
    strat_tables = []
    for sname in sorted(config.keys()):
        if sname in RESERVED_KEYS:
            continue
        scfg = config[sname]
        if not isinstance(scfg, dict) or not scfg.get("enabled"):
            continue
        t = build_strategy_table(sname, scfg, data_dir)
        if t:
            strat_tables.append(t)

    panels = [overview] + strat_tables
    console.print(Columns(panels, padding=(0, 1), expand=False))
