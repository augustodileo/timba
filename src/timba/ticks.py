"""General-purpose market data recorder — owned by the trader.

Ticks are the shared source of truth for market data. Recorded once per market
per second by the trader for ALL markets in 'on' or 'paper' mode. Contains only
raw observable market data — no strategy-specific computed values or window context.

Strategies join their own computed data (EVs, etc.) to ticks via tick_id,
writing to per-strategy subdirectories.

Storage: SQLite (bot.db) via the db module.
Backtest still writes JSONL — pass data_dir to write_strategy_data() to use JSONL.
"""

import itertools
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_tick_counter = itertools.count(1)
_ev_counter = itertools.count(1)


def _next_tick_id() -> int:
    return next(_tick_counter)


def _next_ev_id() -> int:
    return next(_ev_counter)


def _max_id_in_jsonl(path: Path) -> int:
    """Read the last few lines of a JSONL file to find the max 'id' field.

    Legacy helper — used by backtest JSONL loading and migration scripts.
    """
    if not path.exists():
        return 0
    max_id = 0
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - 65536))
            tail = f.read().decode("utf-8", errors="replace")
        for line in tail.strip().split("\n"):
            try:
                record = json.loads(line)
                max_id = max(max_id, record.get("id", 0))
            except (json.JSONDecodeError, ValueError):
                continue
    except OSError:
        pass
    return max_id


def init_ids() -> None:
    """Seed tick/ev counters from SQLite to ensure globally unique IDs."""
    global _tick_counter, _ev_counter

    from timba import db

    tick_max = db.max_id("ticks")
    ev_max = db.max_id("evs")

    _tick_counter = itertools.count(tick_max + 1)
    _ev_counter = itertools.count(ev_max + 1)

    if tick_max > 0 or ev_max > 0:
        logger.info("Seeded IDs: tick=%d, ev=%d", tick_max + 1, ev_max + 1)


def record_tick(
    slug: str,
    coin: str,
    interval: str,
    mid_up: float,
    mid_down: float,
    fill_up: float,
    fill_down: float,
    signal_dir: str,
    signal_chg: float,
    signal_trend_sec: float,
    signal_rev: bool,
    price_open: float = 0.0,
    price_now: float = 0.0,
) -> int:
    """Write one tick of raw market data to SQLite.

    Returns the tick_id for cross-referencing.
    """
    from timba import db

    tick_id = _next_tick_id()
    ts = datetime.now(timezone.utc).isoformat()

    tick = {
        "id": tick_id,
        "ts": ts,
        "slug": slug,
        "coin": coin,
        "interval": interval,
        "mid_up": mid_up,
        "mid_down": mid_down,
        "fill_up": fill_up,
        "fill_down": fill_down,
        "signal_dir": signal_dir,
        "signal_chg": round(signal_chg, 6),
        "signal_trend_sec": round(signal_trend_sec, 1),
        "signal_rev": signal_rev,
        "price_open": price_open,
        "price_now": price_now,
    }

    try:
        db.insert_tick(tick)
    except Exception:
        logger.debug("Tick write failed", exc_info=True)

    return tick_id


def write_strategy_data(
    data_dir: str | Path | None,
    strategy: str,
    filename_prefix: str,
    data: dict,
    slug: str = "",
    tick_id: int = 0,
) -> int:
    """Write strategy-specific computed data.

    data_dir=None → SQLite (live bot, db must be initialized).
    data_dir=Path → JSONL file in data_dir/strategy/ (backtest).

    Returns ev_id for cross-referencing with trades.
    """
    ev_id = _next_ev_id()
    data["id"] = ev_id
    data["tick_id"] = tick_id
    if slug:
        data["slug"] = slug

    if data_dir is not None:
        # Backtest path: write JSONL
        date_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        try:
            d = Path(data_dir) / strategy
            d.mkdir(parents=True, exist_ok=True)
            with open(d / f"{filename_prefix}_{date_str}.jsonl", "a") as f:
                f.write(json.dumps(data) + "\n")
        except OSError:
            pass
    else:
        # Live path: write SQLite
        from timba import db
        try:
            db.insert_ev(data, strategy)
        except Exception:
            logger.debug("EV write failed", exc_info=True)

    return ev_id
