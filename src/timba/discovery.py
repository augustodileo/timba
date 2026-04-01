"""Market discovery: find new UpDown markets and register positions.

Runs as a background thread, polling the Gamma API every few minutes.
Discovered markets are registered into the shared positions dict and
tracked in MarketCache for continuous CLOB polling.
"""

import logging
import time

from timba import market as market_mod
from timba.base import MarketPosition
from timba.config import INTERVAL_SECS, Config
from timba.market import MarketSeries, UpDownMarket
from timba.market_cache import MarketCache
from timba.strategies import Strategy

logger = logging.getLogger(__name__)

class DiscoveryWorker:
    """Background thread that discovers new markets and registers positions."""

    def __init__(
        self,
        config: Config,
        positions: dict[str, dict[str, MarketPosition]],
        seen_slugs: dict[str, set[str]],
        strategies: dict[str, Strategy],
        strategy_configs: dict[str, tuple],
        market_cache: MarketCache,
        data_dir: str,
    ):
        self._config = config
        self._positions = positions
        self._seen_slugs = seen_slugs
        self._strategies = strategies
        self._strategy_configs = strategy_configs
        self._market_cache = market_cache
        self._data_dir = data_dir

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def discover_and_register(self):
        """Discover markets and register positions for all enabled strategies."""
        series_list = self._build_series_list()

        # Collect all slugs we already know about to skip redundant HTTP calls
        known_slugs = set()
        for name in self._strategies:
            known_slugs.update(self._positions[name].keys())
            known_slugs.update(self._seen_slugs[name])

        markets = market_mod.discover_active_markets(series_list, known_slugs=known_slugs)

        now = time.time()
        for mkt in markets:
            remaining = mkt.end_timestamp - now
            if remaining < 3:
                continue

            # Skip markets that haven't started yet
            interval_sec = INTERVAL_SECS.get(mkt.interval, 300)
            if now < mkt.end_timestamp - interval_sec:
                continue

            # Register market in cache for continuous polling
            self._market_cache.track(
                mkt.slug, mkt.token_id_up, mkt.token_id_down,
            )

            for name, strat in self._strategies.items():
                self._register_for_strategy(name, strat, mkt)

        total_pos = sum(len(p) for p in self._positions.values())
        logger.debug("Discovery | %d markets | %d positions", len(markets), total_pos)

    def run_loop(self, is_running):
        """Background thread entry point. Call from threading.Thread(target=...)."""
        logger.info("Discovery thread started")
        while is_running():
            try:
                self.discover_and_register()
            except Exception:
                logger.debug("Discovery error", exc_info=True)
            from timba.constants import DISCOVERY_INTERVAL_SEC
            for _ in range(DISCOVERY_INTERVAL_SEC):
                if not is_running():
                    return
                time.sleep(1)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_series_list(self) -> list[MarketSeries]:
        """Build discovery list from union of all enabled strategies' markets."""
        series = []
        for coin, interval in self._config.get_discovery_markets():
            interval_sec = INTERVAL_SECS.get(interval, 300)
            series.append(MarketSeries(coin, interval, interval_sec))
        return series

    def _register_for_strategy(
        self, name: str, strat: Strategy, mkt: UpDownMarket,
    ):
        """Register a market for one strategy if configured and not seen."""
        positions = self._positions[name]
        seen = self._seen_slugs[name]
        slug = mkt.slug

        if slug in positions or slug in seen:
            return

        gcfg, markets_list = self._strategy_configs[name]

        market_cfg = None
        for m in markets_list:
            if m.get("coin") == mkt.coin and m.get("interval") == mkt.interval:
                market_cfg = m
                break
        if market_cfg is None:
            return

        market_mode = market_cfg.get("mode", "live")
        if market_mode == "off":
            return

        pos = strat.create_position(mkt, market_cfg, gcfg)
        if pos is None:
            return

        pos.data_dir = self._data_dir

        # If registered mid-window, skip betting this window
        remaining = pos.time_remaining()
        if remaining < pos.entry_window_sec and remaining > pos.close_window_sec:
            pos._skip_first_window = True
            logger.debug(
                "Register %s/%s mid-window (%.0fs remaining) — skip first bet",
                name, slug, remaining,
            )
        else:
            pos._skip_first_window = False

        positions[slug] = pos

        logger.debug(
            "Register %s/%s | %s %s %s",
            name, slug, mkt.coin.upper(), mkt.interval, market_mode,
        )
