"""General-purpose market data recorder — owned by the trader.

Ticks are the shared source of truth for market data. Recorded once per market
per second by the trader for ALL markets in 'on' or 'paper' mode. Contains only
raw observable market data — no strategy-specific computed values or window context.

Strategies join their own computed data (EVs, etc.) to ticks via tick_id,
writing to per-strategy subdirectories.

Storage: SQLite (bot.db) via the db module.
"""

import itertools
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_tick_counter = itertools.count(1)
_ev_counter = itertools.count(1)


def _next_tick_id() -> int:
    return next(_tick_counter)


def _next_ev_id() -> int:
    return next(_ev_counter)


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
    strategy: str,
    data: dict,
    slug: str = "",
    tick_id: int = 0,
) -> int:
    """Write strategy-specific computed data to SQLite.

    Returns ev_id for cross-referencing with trades.
    """
    from timba import db

    ev_id = _next_ev_id()
    data["id"] = ev_id
    data["tick_id"] = tick_id
    if slug:
        data["slug"] = slug

    try:
        db.insert_ev(data, strategy)
    except Exception:
        logger.debug("EV write failed", exc_info=True)

    return ev_id
