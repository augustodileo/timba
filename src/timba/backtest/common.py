"""Shared helpers for backtest: data loading, validation, config lookup."""

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from timba.config import INTERVAL_SECS
from timba.feed import DirectionSignal
from timba.market import UpDownMarket
from timba.strategies import TickData

# ── Tick validation ──────────────────────────────────────────────────

# Ticks are raw market data — no remaining/progress (those are in strategy EVs)
TICK_REQUIRED_FIELDS = [
    "slug", "coin", "interval",
    "mid_up", "mid_down", "fill_up", "fill_down",
    "signal_dir", "signal_chg", "signal_trend_sec", "signal_rev",
]


def validate_tick(tick: dict) -> str | None:
    """Return skip reason if tick is missing required data, else None."""
    for f in TICK_REQUIRED_FIELDS:
        if f not in tick:
            return f"missing {f}"
    return None


# ── Trade validation ─────────────────────────────────────────────────

TRADE_REQUIRED_FIELDS = [
    "type", "slug", "side", "buy_price",
]


def validate_trade(trade: dict) -> str | None:
    """Return skip reason if trade is missing required data, else None."""
    for f in TRADE_REQUIRED_FIELDS:
        if f not in trade:
            return f"missing {f}"

    if not trade["side"]:
        return "empty side"
    if trade["buy_price"] <= 0 or trade["buy_price"] >= 1.0:
        return f"buy_price out of range ({trade['buy_price']})"

    return None


# ── Data loading: ticks ──────────────────────────────────────────────

def load_ticks_from_files(files: list[Path]):
    """Load ticks from JSONL files, grouped by slug, sorted by timestamp."""
    by_slug: dict[str, list[dict]] = defaultdict(list)
    skip_count = 0
    for f in files:
        with open(f) as fh:
            for line in fh:
                try:
                    tick = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if validate_tick(tick):
                    skip_count += 1
                    continue
                by_slug[tick["slug"]].append(tick)
    for slug in by_slug:
        by_slug[slug].sort(key=lambda t: t.get("ts", ""))
    return dict(by_slug), skip_count


def load_ticks(data_dir: Path):
    """Load ticks from data_dir/ticks_*.jsonl (excluding backtest outputs)."""
    files = sorted(f for f in data_dir.glob("ticks_*.jsonl")
                   if ".backtest." not in f.name)
    return load_ticks_from_files(files)


def load_ticks_from_file(path: Path):
    """Load ticks from a single file, grouped by slug."""
    return load_ticks_from_files([path])


# ── Data loading: EVs ────────────────────────────────────────────────

def load_evs(data_dir: Path, strategy: str = "favorite"):
    """Load EVs from {strategy}/evs_*.jsonl, keyed by tick_id."""
    evs = {}
    strat_dir = data_dir / strategy
    if not strat_dir.exists():
        return evs
    files = sorted(f for f in strat_dir.glob("evs_*.jsonl")
                   if ".backtest." not in f.name)
    for f in files:
        with open(f) as fh:
            for line in fh:
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                tid = ev.get("tick_id")
                if tid is not None:
                    evs[tid] = ev
    return evs


def load_evs_from_file(path: Path) -> dict[int, dict]:
    """Load EVs from a single file, keyed by tick_id."""
    evs = {}
    with open(path) as fh:
        for line in fh:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            tid = ev.get("tick_id")
            if tid is not None:
                evs[tid] = ev
    return evs


def load_ticks_with_evs(data_dir: Path, strategy: str = "favorite",
                        evs_file: Path | None = None):
    """Load ticks joined with EVs. Returns ticks grouped by slug with EV fields merged.

    If evs_file is provided, uses that for EVs (e.g., backtest output).
    Otherwise loads live evs from {strategy}/ subdir.
    Falls back to reading EVs embedded in ticks (old format compat).
    """
    ticks_by_slug, skip_count = load_ticks(data_dir)

    if evs_file:
        evs = load_evs_from_file(evs_file)
    else:
        evs = load_evs(data_dir, strategy=strategy)

    if evs:
        # Merge EVs into ticks by tick id (tick.id == ev.tick_id)
        for slug, ticks in ticks_by_slug.items():
            for tick in ticks:
                tid = tick.get("id")
                ev = evs.get(tid, {}) if tid is not None else {}
                tick["ev_up"] = ev.get("ev_up", tick.get("ev_up", 0.0))
                tick["ev_down"] = ev.get("ev_down", tick.get("ev_down", 0.0))
                tick["p_up"] = ev.get("p_up", tick.get("p_up", 0.0))
                tick["p_down"] = ev.get("p_down", tick.get("p_down", 0.0))
                # Also merge remaining/progress from EVs (strategy-specific)
                if "remaining" in ev:
                    tick["remaining"] = ev["remaining"]
                if "progress" in ev:
                    tick["progress"] = ev["progress"]
    # else: old format — EVs already embedded in ticks, nothing to merge

    return ticks_by_slug, skip_count


# ── Data loading: trades ─────────────────────────────────────────────

def load_trades(data_dir: Path, strategy: str | None = None) -> list[dict]:
    """Load all trades from current + rotated SQLite DBs, with JSONL fallback."""
    import sqlite3

    # Scan all DBs: rotated (bot_*.db) + current (bot.db)
    db_files = sorted(data_dir.glob("bot_*.db"))
    current = data_dir / "bot.db"
    if current.exists():
        db_files.append(current)

    trades = []
    for db_file in db_files:
        try:
            conn = sqlite3.connect(str(db_file), timeout=5)
            conn.row_factory = sqlite3.Row
            if strategy:
                rows = conn.execute(
                    "SELECT * FROM trades WHERE strategy = ? ORDER BY id", (strategy,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM trades ORDER BY id").fetchall()
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

    # JSONL fallback
    trades = []
    if strategy:
        dirs = [data_dir / strategy]
    else:
        dirs = [d for d in data_dir.iterdir() if d.is_dir() and not d.name.startswith(".")]

    for d in dirs:
        if not d.exists():
            continue
        for f in sorted(d.glob("trades_*.jsonl")):
            with open(f) as fh:
                for line in fh:
                    try:
                        trades.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    return trades


def load_trade_outcomes(data_dir: Path, strategy: str | None = None) -> dict[str, dict]:
    """Load trades, return dict mapping slug -> trade record."""
    outcomes = {}
    for trade in load_trades(data_dir, strategy=strategy):
        slug = trade.get("slug")
        if slug:
            outcomes[slug] = trade
    return outcomes


# ── Formula helpers ──────────────────────────────────────────────────

def run_formula(trade: dict, agree_boost: float, disagree_penalty: float,
                change_cap_pct: float, trend_cap_sec: float):
    """Estimate P(win) from trade snapshot, return (p_win, ev).

    Uses the midpoint as the market-implied probability (favorite strategy
    does not have a separate formula -- midpoint IS the probability).
    Legacy parameters (agree_boost, etc.) are accepted but ignored.
    """
    p_win = trade.get("midpoint", 0.5)
    ev = p_win - trade["buy_price"]
    return p_win, ev


# ── Backtest helpers ─────────────────────────────────────────────────

def mock_market_from_tick(tick: dict) -> UpDownMarket:
    """Build a mock UpDownMarket from a tick dict for position creation."""
    slug = tick["slug"]
    coin = tick["coin"]
    interval = tick["interval"]
    iv_sec = INTERVAL_SECS.get(interval, 300)
    try:
        slug_ts = int(slug.rsplit("-", 1)[1])
        end_ts = slug_ts + iv_sec
    except (ValueError, IndexError):
        end_ts = 0
    return UpDownMarket(
        condition_id="backtest", question="backtest",
        slug=slug, coin=coin, interval=interval,
        token_id_up="tu", token_id_down="td",
        end_timestamp=end_ts,
        gamma_price_up=0.5, gamma_price_down=0.5,
    )


def tick_data_from_dict(tick: dict) -> TickData | None:
    """Build a TickData from a raw tick dict. Returns None if timestamp invalid."""
    try:
        tick_ts = datetime.fromisoformat(tick["ts"]).timestamp()
    except Exception:
        return None
    return TickData(
        tick_id=tick.get("id", 0),
        ts=tick_ts,
        signal=DirectionSignal(
            direction=tick.get("signal_dir", "flat"),
            change_pct=tick.get("signal_chg", 0),
            seconds_trending=tick.get("signal_trend_sec", 0),
            reversed_recently=tick.get("signal_rev", False),
            confidence=0.0,
        ),
        mid_up=tick["mid_up"], mid_down=tick["mid_down"],
        fill_up=tick["fill_up"], fill_down=tick["fill_down"],
        size_up=200, size_down=200,
    )


def resolve_from_ticks(ticks: list[dict]) -> str | None:
    """Determine the winning side from the last tick's midpoints.

    Same logic as base.resolve_winner: mid >= 0.5 → that side won.
    Returns 'up', 'down', or None if can't determine.
    """
    if not ticks:
        return None
    last = ticks[-1]
    mid_up = last.get("mid_up", 0)
    mid_down = last.get("mid_down", 0)
    if mid_up >= 0.5 and mid_down < 0.5:
        return "up"
    if mid_down >= 0.5 and mid_up < 0.5:
        return "down"
    return None


# ── Output file naming ───────────────────────────────────────────────

def next_output_path(original: Path) -> Path:
    """Generate unique output path: {stem}.backtest[_N].jsonl"""
    stem = original.stem
    parent = original.parent
    base = parent / f"{stem}.backtest.jsonl"
    if not base.exists():
        return base
    i = 1
    while True:
        candidate = parent / f"{stem}.backtest_{i}.jsonl"
        if not candidate.exists():
            return candidate
        i += 1
