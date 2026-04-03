"""Position resolution: check CLOB for outcomes, record trades.

Runs as a background thread, polling pending positions every few seconds.
HTTP calls happen in the background thread; state mutations are queued
for the main loop via the mutation queue.
"""

import logging
import queue
import time
import typing

from timba.base import (
    RESOLVE_MAP,
    MarketPosition,
    PositionState,
    resolve_winner,
)
from timba.clob_helpers import _get_midpoint
from timba.feed import PriceFeed
from timba.market_cache import MarketCache
from timba.state import State
from timba.strategies import Strategy

logger = logging.getLogger(__name__)

RESOLUTION_POLL_SEC = 2


class ResolutionWorker:
    """Background thread that checks CLOB for resolved positions."""

    def __init__(
        self,
        positions: dict[str, dict[str, MarketPosition]],
        seen_slugs: dict[str, dict[str, float]],
        strategies: dict[str, Strategy],
        mutations: queue.Queue,
        feed: PriceFeed | None,
        clob_client: object,
        market_cache: MarketCache,
        state: State,
        recorded_ticks: object,
    ) -> None:
        self._positions = positions
        self._seen_slugs = seen_slugs
        self._strategies = strategies
        self._mutations = mutations
        self._feed = feed
        self._clob = clob_client
        self._market_cache = market_cache
        self._state = state
        self._recorded_ticks = recorded_ticks

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_loop(self, is_running: typing.Callable[[], bool]) -> None:
        """Background thread entry point."""
        logger.info("Resolution thread started")
        while is_running():
            try:
                self._resolve_pending()
            except Exception:
                logger.debug("Resolution error", exc_info=True)
            time.sleep(RESOLUTION_POLL_SEC)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _resolve_pending(self) -> None:
        """Scan pending positions, check CLOB (HTTP), queue mutations."""
        now = time.time()
        for name, strat in self._strategies.items():
            for slug, pos in list(self._positions[name].items()):
                if not pos.state.is_pending:
                    continue
                try:
                    self._check_and_queue_resolve(name, strat, pos)
                    # Reset failure window on success
                    pos._resolve_fail_since = 0
                    pos._resolve_fail_count = 0
                except Exception:
                    # Time-windowed failure escalation: warn if 3+ failures in 60s
                    first = getattr(pos, '_resolve_fail_since', 0)
                    count = getattr(pos, '_resolve_fail_count', 0)
                    if now - first > 60:
                        pos._resolve_fail_since = now
                        pos._resolve_fail_count = 1
                    else:
                        pos._resolve_fail_count = count + 1
                    if pos._resolve_fail_count >= 3:
                        logger.warning("Resolve failing for %s/%s (%d in %.0fs)",
                                       name, slug, pos._resolve_fail_count,
                                       now - pos._resolve_fail_since, exc_info=True)
                    else:
                        logger.debug("Resolve error %s/%s", name, slug, exc_info=True)

    def _check_and_queue_resolve(
        self, name: str, strat: Strategy, pos: MarketPosition,
    ) -> None:
        """Check resolution via CLOB (HTTP), queue the state mutation."""
        mapping = RESOLVE_MAP.get(pos.state)
        if mapping is None:
            return

        won = resolve_winner(pos, self._feed, self._clob, _get_midpoint)
        if won is None:
            return

        strat.resolve(pos, won)

        # Determine final state
        if not won and not pos.side and pos.state == PositionState.SKIPPED:
            final_state = PositionState.SKIP_NONE
        else:
            _, state_won, state_lost = mapping
            final_state = state_won if won else state_lost

        pos.transition(final_state)

        self._mutations.put(
            lambda n=name, p=pos, s=strat: self.commit_resolve(n, s, p)
        )

    def commit_resolve(self, name: str, strat: Strategy, pos: MarketPosition) -> None:
        """Apply resolution to state. Only called from main loop via _drain_mutations."""
        self._state.record_trade(pos, name, extra_fields=strat.extra_fields(pos))

        self._seen_slugs[name][pos.slug] = time.time()
        if pos.slug in self._positions[name]:
            del self._positions[name][pos.slug]

        slug_still_active = any(
            pos.slug in self._positions[n] for n in self._positions
        )
        if not slug_still_active:
            self._market_cache.untrack(pos.slug)
            self._recorded_ticks.pop(pos.slug, None)

        # Strategy.resolve() already logs the outcome (LIVE/PAPER/FAIL/SKIP WIN/LOSS)
