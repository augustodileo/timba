"""Tick recorder: continuously capture market data for all tracked positions.

Runs as a background thread, writing ticks to SQLite and storing snapshots
in the shared recorded_ticks dict for the eval loop to read.
"""

import logging
import time

from timba.base import MarketPosition
from timba.feed import PriceFeed
from timba.market_cache import MarketCache
from timba.ticks import record_tick

logger = logging.getLogger(__name__)

TICK_RECORDER_SEC = 0.5


class TickRecorder:
    """Background thread that records market ticks."""

    def __init__(
        self,
        positions: dict[str, dict[str, MarketPosition]],
        strategies: dict[str, str],
        recorded_ticks,
        feed: PriceFeed | None,
        market_cache: MarketCache,
    ):
        self._positions = positions
        self._strategies = strategies
        self._recorded_ticks = recorded_ticks
        self._feed = feed
        self._market_cache = market_cache

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_loop(self, is_running):
        """Background thread entry point."""
        logger.info("Tick recorder started")
        while is_running():
            try:
                if not self._feed or not self._feed.is_healthy():
                    time.sleep(0.5)
                    continue

                active_slugs: dict[str, MarketPosition] = {}
                for name in self._strategies:
                    for slug, pos in list(self._positions[name].items()):
                        if slug not in active_slugs and not pos.state.is_terminal:
                            active_slugs[slug] = pos

                for slug, pos in active_slugs.items():
                    if not is_running():
                        break
                    self._record_tick(pos)

            except Exception:
                logger.debug("Tick recorder error", exc_info=True)

            time.sleep(TICK_RECORDER_SEC)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _record_tick(self, pos: MarketPosition):
        """Record one tick for a market from cache. Stores snapshot for eval loop."""
        signal = self._feed.get_direction(pos.coin, pos.window_start_ts)
        if signal is None:
            return

        snapshot = self._market_cache.get(pos.slug)
        if snapshot is None or snapshot.updated_at == 0:
            return

        tick_id = record_tick(
            slug=pos.slug, coin=pos.coin, interval=pos.interval,
            mid_up=snapshot.mid_up, mid_down=snapshot.mid_down,
            fill_up=snapshot.fill_up, fill_down=snapshot.fill_down,
            signal_dir=signal.direction, signal_chg=signal.change_pct,
            signal_trend_sec=signal.seconds_trending,
            signal_rev=signal.reversed_recently,
            price_open=signal.price_open, price_now=signal.price_now,
        )

        logger.debug(
            "TICK #%d | %s %s %s | mid=%.3f/%.3f fill=%.4f/%.4f | signal: %s %.4f%% %ds%s",
            tick_id, pos.coin.upper(), pos.interval, pos.slug.rsplit("-", 1)[-1],
            snapshot.mid_up, snapshot.mid_down, snapshot.fill_up, snapshot.fill_down,
            signal.direction, signal.change_pct, signal.seconds_trending,
            " REV" if signal.reversed_recently else "",
        )

        # Store for eval loop — exact same data that was written to disk
        self._recorded_ticks[pos.slug] = (tick_id, snapshot, signal)
