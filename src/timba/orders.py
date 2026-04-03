"""Order lifecycle: cash check, tick wait, CLOB placement, paper fill.

Owns the entire order flow from bet decision to fill confirmation.
The OrderManager receives shared state by reference and communicates
results back to the main loop via the mutation queue.
"""

import logging
import math
import queue
import threading
import time
from datetime import datetime, timezone

from polymarket_apis import PolymarketClobClient

from timba.base import MarketPosition, PositionState, place_order
from timba.config import Config
from timba.market_cache import MarketCache
from timba.state import State
from timba.strategies import BetDecision, Strategy

logger = logging.getLogger(__name__)

# Order handling constants
TICK_WAIT_TIMEOUT_SEC = 10
TICK_WAIT_POLL_SEC = 0.5


class OrderManager:
    """Manages order placement for both paper and live modes.

    Thread safety: all cash operations go through State methods which
    hold State._lock internally. This serializes eval pool workers
    and mutation drains without an external lock.
    """

    def __init__(
        self,
        config: Config,
        state: State,
        mutations: queue.Queue,
        market_cache: MarketCache,
        api_creds: object,
    ) -> None:
        self._config = config
        self._state = state
        self._mutations = mutations
        self._market_cache = market_cache
        self._api_creds = api_creds

    # ------------------------------------------------------------------
    # Public API (called from Trader._evaluate_all via pool workers)
    # ------------------------------------------------------------------

    def execute_bet(
        self, name: str, strat: Strategy, pos: MarketPosition, decision: BetDecision,
    ) -> None:
        """Check cash, log the bet, spawn an order thread.

        Called from the eval ThreadPoolExecutor — must be thread-safe
        with respect to cash operations.
        """
        order_price = round(decision.price, 4)
        cost = order_price * decision.size

        logger.info(
            "BET ev #%d | %s %s %s | %s @$%.4f | %d contracts | mode=%s",
            pos.ev_id or 0, pos.coin.upper(), pos.interval,
            decision.side.upper(), decision.side.upper(), order_price,
            decision.size, pos.market_mode,
        )

        # Cash check (live only — atomic check-and-reserve to prevent
        # two pool workers from double-spending the same cash)
        if pos.market_mode != "paper":
            if not self._state.try_reserve(cost):
                pos.transition(
                    PositionState.SKIPPED,
                    skip_reason=(
                        f"insufficient funds (need ${cost:.2f}, "
                        f"have ${self._state.available_cash:.2f})"
                    ),
                )
                return

        pos.transition(
            PositionState.PENDING_ORDER,
            side=decision.side,
            buy_price=decision.price,
            contracts=decision.size,
            sniped_at=datetime.now(timezone.utc).isoformat(),
        )

        t = threading.Thread(
            target=self._handle_order,
            args=(name, strat, pos, decision, order_price, cost),
            daemon=True,
            name=f"order-{pos.coin}-{pos.interval}",
        )
        t.start()

    # ------------------------------------------------------------------
    # Mutation callbacks (executed on the main thread via _drain_mutations)
    # ------------------------------------------------------------------

    def commit_order_fill(self, actual_cost: float, reserved_cost: float) -> None:
        """Order filled — deduct cash and release reservation."""
        self._state.commit_fill(actual_cost, reserved_cost)

    def release_order(self, reserved_cost: float) -> None:
        """Order expired/failed — release cash reservation."""
        self._state.release_reservation(reserved_cost)

    # ------------------------------------------------------------------
    # Background thread (one per order)
    # ------------------------------------------------------------------

    def _create_clob_client(self) -> PolymarketClobClient:
        """Create a fresh CLOB client for use in a background thread."""
        client = PolymarketClobClient(
            private_key=self._config.polymarket.private_key,
            address=self._config.polymarket.funder,
            signature_type=self._config.polymarket.signature_type,
        )
        client.set_api_creds(self._api_creds)
        return client

    def _handle_order(
        self, name: str, strat: Strategy, pos: MarketPosition,
        decision: BetDecision, order_price: float, reserved_cost: float,
    ) -> None:
        """Background thread: wait for tick, place order (or paper-fill), queue mutations.

        Same flow for paper and live — only the actual CLOB call differs.
        """
        is_paper = pos.market_mode == "paper"
        try:
            tick, max_price = self._wait_for_tick(
                pos.slug, order_price, timeout=TICK_WAIT_TIMEOUT_SEC,
            )

            tick_f = float(tick)
            rounded_price = math.ceil(order_price / tick_f) * tick_f
            decimals = len(str(tick).rstrip('0').split('.')[-1])
            rounded_price = round(rounded_price, decimals)

            if rounded_price > max_price:
                pos.transition(
                    PositionState.FAILED,
                    skip_reason=(
                        f"unfillable (${order_price:.4f} → ${rounded_price:.4f} "
                        f"> max ${max_price:.3f}, tick={tick})"
                    ),
                )
                if not is_paper:
                    self._mutations.put(lambda r=reserved_cost: self.release_order(r))
                logger.info(
                    "FAIL ev #%d | %s %s %s | %s",
                    pos.ev_id or 0, pos.coin.upper(), pos.interval,
                    pos.side.upper(), pos.skip_reason,
                )
                return

            order_price = rounded_price

            if is_paper:
                pos.transition(
                    PositionState.PAPER,
                    buy_price=order_price,
                    cost=order_price * decision.size,
                )
                strat.on_bet(pos, decision)
                logger.info(
                    "BET FILLED ev #%d | %s %s %s | %s @$%.4f | %d contracts | paper",
                    pos.ev_id or 0, pos.coin.upper(), pos.interval,
                    pos.side.upper(), pos.side.upper(), order_price, pos.contracts,
                )
            else:
                clob = self._create_clob_client()
                if place_order(pos, decision.side, order_price, decision.size,
                               clob):
                    pos.transition(PositionState.SNIPED)
                    actual_cost = pos.cost
                    self._mutations.put(
                        lambda c=actual_cost, r=reserved_cost: self.commit_order_fill(c, r)
                    )
                    strat.on_bet(pos, decision)
                    logger.info(
                        "BET FILLED ev #%d | %s %s %s | %s @$%.4f | %d contracts | order=%s",
                        pos.ev_id or 0, pos.coin.upper(), pos.interval,
                        pos.side.upper(), pos.side.upper(), pos.buy_price,
                        pos.contracts, pos.order_id,
                    )
                else:
                    pos.transition(PositionState.SKIPPED)
                    self._mutations.put(lambda r=reserved_cost: self.release_order(r))
                    logger.info(
                        "BET EXPIRED ev #%d | %s %s %s | %s",
                        pos.ev_id or 0, pos.coin.upper(), pos.interval,
                        pos.side.upper() if pos.side else "?", pos.skip_reason,
                    )
        except Exception:
            pos.transition(PositionState.SKIPPED)
            if not is_paper:
                self._mutations.put(lambda r=reserved_cost: self.release_order(r))
            logger.exception("ORDER ERROR %s", pos.label)

    def _wait_for_tick(
        self, slug: str, order_price: float, timeout: float = 10,
    ) -> tuple[float, float]:
        """Poll market_cache for a tick_size where ceil(price/tick)*tick <= max_price.

        Returns (tick_size, max_price).
        """
        deadline = time.time() + timeout
        first_check = True

        while True:
            snapshot = self._market_cache.get(slug)
            tick = snapshot.tick_size if snapshot else 0.01
            tick_f = float(tick)
            decimals = len(str(tick).rstrip('0').split('.')[-1])
            max_price = round(1.0 - tick_f, decimals)
            rounded = round(math.ceil(order_price / tick_f) * tick_f, decimals)

            if rounded <= max_price:
                if not first_check:
                    logger.info(
                        "TICK CHANGED %s | tick=%s max=$%.4f — order $%.4f now valid",
                        slug[:30], tick, max_price, order_price,
                    )
                return tick, max_price

            if first_check:
                logger.info(
                    "TICK WAIT %s | $%.4f > max $%.3f (tick=%s), polling %ds...",
                    slug[:30], order_price, max_price, tick, timeout,
                )
                first_check = False

            if time.time() >= deadline:
                return tick, max_price

            time.sleep(TICK_WAIT_POLL_SEC)
