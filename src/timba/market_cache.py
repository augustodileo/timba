"""Background market data collector with parallel polling.

Continuously polls CLOB for midpoints and orderbook data for all tracked markets.
Uses ThreadPoolExecutor to poll multiple markets in parallel.
Strategies read from the cache (instant) instead of making API calls (blocking).
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from timba.clob_helpers import _get_midpoint, simulate_fill

logger = logging.getLogger(__name__)

@dataclass
class MarketSnapshot:
    """Latest market data for one token pair."""
    mid_up: float = 0.0
    mid_down: float = 0.0
    fill_up: float = 0.0
    fill_down: float = 0.0
    size_up: int = 200
    size_down: int = 200
    updated_at: float = 0.0  # unix timestamp
    tick_size: float = 0.01  # current CLOB tick size (from orderbook)


class MarketCache:
    """Background thread that keeps market data fresh using parallel polling."""

    def __init__(self, clob_client: object, max_workers: int) -> None:
        self._clob = clob_client
        self._max_workers = max_workers
        self._lock = threading.Lock()
        self._markets: dict[str, dict] = {}
        self._cache: dict[str, MarketSnapshot] = {}
        self._thread: threading.Thread | None = None
        self._running = False

    def track(self, slug: str, token_id_up: str, token_id_down: str, contracts: int = 200) -> None:
        """Register a market to track."""
        with self._lock:
            self._markets[slug] = {
                "token_id_up": token_id_up,
                "token_id_down": token_id_down,
                "contracts": contracts,
            }
            if slug not in self._cache:
                self._cache[slug] = MarketSnapshot()

    def untrack(self, slug: str) -> None:
        """Stop tracking a market."""
        with self._lock:
            self._markets.pop(slug, None)
            self._cache.pop(slug, None)

    def get(self, slug: str) -> MarketSnapshot | None:
        """Get latest snapshot for a market. Returns None if not tracked."""
        with self._lock:
            return self._cache.get(slug)

    def start(self) -> None:
        """Start the background polling thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        logger.info("MarketCache started (workers=%d)", self._max_workers)

    def stop(self) -> None:
        """Stop the background polling thread."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def _poll_loop(self) -> None:
        """Continuously poll all tracked markets in parallel."""
        # Time-windowed per-slug failure tracking: warn at 5 failures in 60s
        _failures: dict[str, tuple[float, int]] = {}  # slug → (window_start, count)
        _FAIL_THRESHOLD = 5
        _FAIL_WINDOW_SEC = 60

        while self._running:
            try:
                with self._lock:
                    slugs = list(self._markets.items())

                if not slugs:
                    time.sleep(0.5)
                    continue

                # Poll all markets in parallel
                with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
                    futures = {}
                    for slug, info in slugs:
                        if not self._running:
                            break
                        try:
                            futures[pool.submit(self._update_market, slug, info)] = slug
                        except RuntimeError:
                            # Interpreter shutting down — pool is closed
                            self._running = False
                            break

                    now = time.time()
                    for future in as_completed(futures):
                        if not self._running:
                            break
                        slug = futures[future]
                        try:
                            future.result()
                            _failures.pop(slug, None)
                        except Exception:
                            start, count = _failures.get(slug, (now, 0))
                            if now - start > _FAIL_WINDOW_SEC:
                                start, count = now, 1
                            else:
                                count += 1
                            _failures[slug] = (start, count)
                            if count == _FAIL_THRESHOLD:
                                logger.warning("Cache poll failing for %s (%d in %.0fs)",
                                               slug[:30], count, now - start, exc_info=True)
                            else:
                                logger.debug("Cache poll failed for %s", slug, exc_info=True)

            except Exception:
                logger.exception("MarketCache poll loop error")

            # Small pause between full cycles to avoid hammering the API
            if self._running:
                time.sleep(0.1)

    def _update_market(self, slug: str, info: dict) -> None:
        """Poll CLOB for one market and update cache."""
        token_up = info["token_id_up"]
        token_down = info["token_id_down"]
        contracts = info["contracts"]

        mid_up = _get_midpoint(self._clob, token_up)
        mid_down = _get_midpoint(self._clob, token_down)

        if mid_up is None or mid_down is None:
            return

        fill_up, size_up = mid_up, contracts
        fill_down, size_down = mid_down, contracts
        tick_size = 0.01  # default

        result = simulate_fill(self._clob, token_up, contracts)
        if result is not None:
            fill_up, size_up, tick_size = result
        result = simulate_fill(self._clob, token_down, contracts)
        if result is not None:
            fill_down, size_down, tick_size = result

        snapshot = MarketSnapshot(
            mid_up=mid_up, mid_down=mid_down,
            fill_up=fill_up, fill_down=fill_down,
            size_up=size_up, size_down=size_down,
            updated_at=time.time(),
            tick_size=tick_size,
        )

        with self._lock:
            self._cache[slug] = snapshot
