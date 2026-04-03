"""Main trading loop: strategy-agnostic orchestrator.

The trader does NOT know strategy logic. It:
  1. Discovers markets (from union of all strategies' configured coins/intervals)
  2. Records one tick per market per second (raw market data)
  3. Passes tick data to each strategy's evaluate() → gets back True/False
  4. Places orders for strategies that say True
  5. Resolves outcomes using shared CLOB/Coinbase logic
  6. Records trades and triggers redemption

Strategies are loaded from timba.strategies/ by config key name.

Component architecture:
  - OrderManager (orders.py): cash check, tick wait, CLOB placement
  - DiscoveryWorker (discovery.py): Gamma API polling, position registration
  - ResolutionWorker (resolution.py): CLOB resolution polling, trade recording
  - TickRecorder (tick_recorder.py): continuous market data capture
  - Trader (this file): orchestrator — wires components, runs main eval loop
"""

import logging
import queue
import threading
import time
from datetime import datetime, timezone

import requests
from polymarket_apis import PolymarketClobClient

from timba.base import (
    MarketPosition,
    PositionState,
    check_entry_window,
    check_liquidity,
)
from timba.config import Config
from timba.discovery import DiscoveryWorker
from timba.feed import PriceFeed
from timba.health import HealthState
from timba.market_cache import MarketCache
from timba.orders import OrderManager
from timba.redeem import create_relay_client, redeem_position
from timba.resolution import ResolutionWorker
from timba.scheduler import MaintenanceScheduler
from timba.state import State
from timba.strategies import (
    BetDecision,
    Strategy,
    TickData,
    load_strategies,
)
from timba.strategies import (
    get as get_strategy,
)
from timba.tick_recorder import TickRecorder
from timba.ticks import write_strategy_data

logger = logging.getLogger(__name__)

# Main loop timing
MAIN_LOOP_SEC = 1
SCHEDULER_POLL_SEC = 5

# Evaluation
EVAL_WINDOW_BUFFER_SEC = 30
MAX_EVAL_WORKERS = 10


class LockedDict:
    """Thread-safe dict wrapper for cross-thread key access (get/set/pop)."""

    def __init__(self) -> None:
        self._data: dict = {}
        self._lock = threading.Lock()

    def get(self, key: str, default: object = None) -> object:
        with self._lock:
            return self._data.get(key, default)

    def __setitem__(self, key: str, value: object) -> None:
        with self._lock:
            self._data[key] = value

    def pop(self, key: str, *args: object) -> object:
        with self._lock:
            return self._data.pop(key, *args)


class Trader:
    def __init__(self, config: Config, state: State, data_dir: str = "data") -> None:
        self.config = config
        self.state = state
        self.data_dir = str(data_dir)
        self._mutations = queue.Queue()

        # Connect to CLOB
        if not config.polymarket.private_key:
            raise ValueError("POLYMARKET_PRIVATE_KEY is required")
        if not config.polymarket.funder:
            raise ValueError("POLYMARKET_FUNDER is required")
        self.clob_client = PolymarketClobClient(
            private_key=config.polymarket.private_key,
            address=config.polymarket.funder,
            signature_type=config.polymarket.signature_type,
        )
        creds = self.clob_client.create_or_derive_api_creds()
        self.clob_client.set_api_creds(creds)
        self._api_creds = creds

        # Relayer for auto-redemption
        if config.polymarket.relayer_api_key:
            self.relay_client = create_relay_client(
                config.polymarket.private_key,
                config.polymarket.relayer_api_key,
                config.polymarket.relayer_api_key_address,
            )
        else:
            self.relay_client = None
            logger.warning("RELAYER_API_KEY not set — winning positions must be redeemed manually")

        # Reconciliation already happened in cli.py before Trader creation
        self._log_clob_state()

        # ── Load strategies ──
        load_strategies()
        self._strategies: dict[str, Strategy] = {}
        self._strategy_configs: dict[str, tuple] = {}
        self._init_strategies()

        # ── Positions per strategy ──
        self.positions: dict[str, dict[str, MarketPosition]] = {
            name: {} for name in self._strategies
        }
        # slug → timestamp (TTL eviction, cleaned every cycle)
        self._seen_slugs: dict[str, dict[str, float]] = {
            name: {} for name in self._strategies
        }

        self.health = HealthState()

        # Maintenance scheduler
        intervals = config.get_all_intervals()
        self.scheduler = MaintenanceScheduler(intervals)
        self.scheduler.seed_rotation_date()

        # Price feed
        self.feed: PriceFeed | None = None
        if config.needs_feed():
            coins = config.get_all_coins()
            self.feed = PriceFeed(coins=coins, poll_interval=1.0)
            self.feed.start()

        # Market data cache
        self.market_cache = MarketCache(self.clob_client, max_workers=config.max_workers)
        self.market_cache.start()

        # Recorded ticks: tick recorder writes, eval reads, resolution pops
        self._recorded_ticks = LockedDict()

        # ── Components ──
        self.order_manager = OrderManager(
            config=config,
            state=state,
            mutations=self._mutations,
            market_cache=self.market_cache,
            api_creds=self._api_creds,
        )

        self.discovery = DiscoveryWorker(
            config=config,
            positions=self.positions,
            seen_slugs=self._seen_slugs,
            strategies=self._strategies,
            strategy_configs=self._strategy_configs,
            market_cache=self.market_cache,
            mutations=self._mutations,
            data_dir=self.data_dir,
        )

        self.resolver = ResolutionWorker(
            positions=self.positions,
            seen_slugs=self._seen_slugs,
            strategies=self._strategies,
            mutations=self._mutations,
            feed=self.feed,
            clob_client=self.clob_client,
            market_cache=self.market_cache,
            state=self.state,
            recorded_ticks=self._recorded_ticks,
        )

        self.tick_recorder = TickRecorder(
            positions=self.positions,
            strategies=self._strategies,
            recorded_ticks=self._recorded_ticks,
            feed=self.feed,
            market_cache=self.market_cache,
        )

    def _init_strategies(self) -> None:
        """Auto-discover enabled strategies by matching config keys to registered strategies."""
        for name, scfg in self.config.strategies.items():
            if not scfg.enabled or not scfg.markets:
                continue
            strat = get_strategy(name)
            if strat is None:
                logger.warning("Strategy '%s' enabled in config but no strategies/%s.py found", name, name)
                continue
            self._strategies[name] = strat
            self._strategy_configs[name] = (scfg, scfg.markets)
            logger.info("Strategy '%s' loaded with %d market(s)", name, len(scfg.markets))

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self, shutdown_event: threading.Event | None = None) -> None:
        """Main loop: evaluate → bet → apply state mutations.

        Background threads do all network I/O and queue mutations.
        The main loop is the ONLY writer to in-memory state.

        Args:
            shutdown_event: Optional threading.Event. When set, the loop
                exits gracefully (used by the API server's /api/stop endpoint).
        """
        logger.info(
            "Starting trader | strategies=%s",
            "+".join(self._strategies.keys()) or "none",
        )
        logger.info("Scheduler: %s", self.scheduler.describe())

        # Discover markets immediately (blocking, just once at startup)
        self._discover_and_register()

        # Background threads
        self._background_running = True

        tick_thread = threading.Thread(
            target=self.tick_recorder.run_loop,
            args=(lambda: self._background_running,),
            daemon=True, name="tick-recorder",
        )
        tick_thread.start()

        discovery_thread = threading.Thread(
            target=self.discovery.run_loop,
            args=(lambda: self._background_running,),
            daemon=True, name="discovery",
        )
        discovery_thread.start()

        resolve_thread = threading.Thread(
            target=self.resolver.run_loop,
            args=(lambda: self._background_running,),
            daemon=True, name="resolver",
        )
        resolve_thread.start()

        scheduler_thread = threading.Thread(
            target=self._scheduler_loop, daemon=True, name="scheduler",
        )
        scheduler_thread.start()

        # Main loop: evaluate + bet + drain mutation queue
        # Time-windowed crash threshold: 10 errors in 60s → shut down
        _error_window_start = time.time()
        _error_window_count = 0
        MAX_ERRORS_IN_WINDOW = 10
        ERROR_WINDOW_SEC = 60

        while True:
            # Check for shutdown signal from API or external caller
            if shutdown_event and shutdown_event.is_set():
                logger.info("Shutdown event received, stopping")
                self._background_running = False
                if self.feed:
                    self.feed.stop()
                self.market_cache.stop()
                break

            try:
                self._drain_mutations()
                self._cleanup_stale_positions()
                self._evaluate_all()
                self._update_health()

            except KeyboardInterrupt:
                logger.info("Shutting down")
                self._background_running = False
                if self.feed:
                    self.feed.stop()
                self.market_cache.stop()
                break
            except Exception:
                logger.exception("Error in trading loop")
                self.health.errors += 1
                now = time.time()
                if now - _error_window_start > ERROR_WINDOW_SEC:
                    _error_window_start = now
                    _error_window_count = 1
                else:
                    _error_window_count += 1
                if _error_window_count >= MAX_ERRORS_IN_WINDOW:
                    logger.critical(
                        "Too many errors (%d in %.0fs), shutting down",
                        _error_window_count, now - _error_window_start,
                    )
                    self._background_running = False
                    if self.feed:
                        self.feed.stop()
                    self.market_cache.stop()
                    break

            time.sleep(MAIN_LOOP_SEC)

    def _drain_mutations(self) -> None:
        """Apply all queued state mutations. Only called from main loop."""
        while not self._mutations.empty():
            try:
                mutation = self._mutations.get_nowait()
                mutation()
            except queue.Empty:
                break
            except Exception:
                logger.exception("Error applying state mutation")

    # ------------------------------------------------------------------
    # Backward-compat delegates (tests + internal references)
    # ------------------------------------------------------------------

    def _discover_and_register(self) -> None:
        self.discovery.discover_and_register()

    def _execute_bet(self, name: str, strat: Strategy, pos: MarketPosition, decision: BetDecision) -> None:
        self.order_manager.execute_bet(name, strat, pos, decision)

    def _commit_resolve(self, name: str, strat: Strategy, pos: MarketPosition) -> None:
        self.resolver.commit_resolve(name, strat, pos)

    def _commit_order_fill(self, actual_cost: float, reserved_cost: float) -> None:
        self.order_manager.commit_order_fill(actual_cost, reserved_cost)

    def _release_order(self, reserved_cost: float) -> None:
        self.order_manager.release_order(reserved_cost)

    # ------------------------------------------------------------------
    # Scheduler (background thread — HTTP only, queues state writes)
    # ------------------------------------------------------------------

    def _scheduler_loop(self) -> None:
        """Background thread: maintenance tasks. Queues state writes."""
        logger.info("Scheduler thread started")
        while self._background_running:
            try:
                self._scheduler_tick()
            except Exception:
                logger.debug("Scheduler error", exc_info=True)
            time.sleep(SCHEDULER_POLL_SEC)

    def _scheduler_tick(self) -> None:
        """Run scheduler maintenance, queue any state mutations."""
        from timba.scheduler import is_safe_window
        if not is_safe_window(self.scheduler.intervals, self.scheduler.buffer_sec):
            return

        now = time.time()

        # Balance sync
        if (now - self.scheduler._last_balance_sync) >= self.scheduler._balance_interval:
            self.scheduler._last_balance_sync = now
            try:
                usdc = self.clob_client.get_usdc_balance()
                self._mutations.put(lambda u=usdc: self._apply_balance_sync(u))
            except Exception:
                logger.debug("Balance sync failed, will retry next window")

        # Redeem scan
        if (now - self.scheduler._last_redeem_scan) >= self.scheduler._redeem_interval:
            self.scheduler._last_redeem_scan = now
            if self.relay_client:
                self._redeem_scan()

        # DB rotation check
        rotation_reason = self.scheduler.should_rotate_db()
        if rotation_reason:
            self._mutations.put(lambda r=rotation_reason: self._rotate_db(r))

    def _apply_balance_sync(self, usdc: float) -> None:
        """Apply balance sync to state. Only called from main loop."""
        old_cash, old_portfolio = self.state.apply_balance_sync(usdc)
        if abs(usdc - old_cash) > 0.10 or abs(self.state.portfolio - old_portfolio) > 0.10:
            logger.info("Balance sync: cash=$%.2f portfolio=$%.2f pending=$%.2f",
                        self.state.cash, self.state.portfolio, self.state.pending_redemption)

    def _rotate_db(self, reason: str) -> None:
        """Rotate the database. Only called from main loop via _drain_mutations."""
        from timba import db
        archive = db.rotate(reason)
        if archive:
            self.scheduler._last_rotation_date = (
                datetime.now(timezone.utc).strftime("%Y-%m-%d")
            )

    # ------------------------------------------------------------------
    # Stale position cleanup (main loop)
    # ------------------------------------------------------------------

    def _cleanup_stale_positions(self) -> None:
        """Remove positions whose market ended long ago without resolution.
        Also evicts old entries from _seen_slugs (2h TTL) and _recorded_ticks.
        """
        now = time.time()
        cutoff = now - 300

        for name in self._strategies:
            for slug, pos in list(self.positions[name].items()):
                if (pos.end_timestamp and pos.end_timestamp < cutoff
                        and not pos.state.is_terminal):
                    logger.debug("Cleanup stale %s/%s (ended %ds ago, state=%s)",
                                 name, slug, int(now - pos.end_timestamp), pos.state.value)

                    if pos.state == PositionState.WATCHING and not pos.ev_id:
                        # Never evaluated — just drop it, no trade to record
                        del self.positions[name][slug]
                        self._seen_slugs[name][slug] = now
                        if not any(slug in self.positions[n] for n in self.positions):
                            self.market_cache.untrack(slug)
                            self._recorded_ticks.pop(slug, None)
                    else:
                        # Was evaluated or bet on — let resolution handle it
                        pos.transition(
                            PositionState.SKIPPED,
                            skip_reason=pos.skip_reason or "stale cleanup",
                            sniped_at=pos.sniped_at or datetime.now(timezone.utc).isoformat(),
                        )

        # Evict old seen_slugs (2h TTL — markets older than this won't reappear in discovery).
        # Mutate in-place so discovery thread's references stay valid.
        seen_cutoff = now - 7200
        for name in self._seen_slugs:
            stale = [s for s, t in self._seen_slugs[name].items() if t <= seen_cutoff]
            for s in stale:
                del self._seen_slugs[name][s]

    # ------------------------------------------------------------------
    # Strategy evaluation (main loop)
    # ------------------------------------------------------------------

    def _evaluate_all(self) -> None:
        """Evaluate all strategies in parallel using ThreadPoolExecutor."""
        if not self.feed or not self.feed.is_healthy():
            if self.feed:
                logger.warning("Feed unhealthy — skipping eval")
            return

        work = []
        for name in self._strategies:
            for slug, pos in list(self.positions[name].items()):
                if (not pos.state.is_terminal
                        and pos.time_remaining() <= pos.entry_window_sec + EVAL_WINDOW_BUFFER_SEC):
                    work.append((name, slug, pos))

        if not work:
            return

        tick_cache = {}
        for name, slug, pos in work:
            if slug not in tick_cache:
                tick_cache[slug] = self._build_tick_data(pos)

        from concurrent.futures import ThreadPoolExecutor

        def _eval_and_bet(item: tuple[str, str, MarketPosition]) -> None:
            name, slug, pos = item

            # Order in flight — skip entirely, thread is handling it
            if pos.state == PositionState.PENDING_ORDER:
                return

            tick_data = tick_cache.get(slug)
            if tick_data is None:
                return

            window_status, remaining, progress = check_entry_window(pos)
            if window_status == "early":
                return
            if window_status == "timeout":
                if pos.state == PositionState.WATCHING:
                    pos.transition(
                        PositionState.SKIPPED,
                        sniped_at=datetime.now(timezone.utc).isoformat(),
                        skip_reason=pos.skip_reason or "window timeout",
                    )
                return

            if not check_liquidity(pos):
                return

            strat = self._strategies[name]
            decision = strat.evaluate(pos, tick_data)
            pos.skip_reason = decision.reason

            if decision.computed:
                ev_id = write_strategy_data(
                    name, decision.computed,
                    slug=slug, tick_id=tick_data.tick_id,
                )
                pos.ev_id = ev_id

                c = decision.computed
                logger.info(
                    "EV #%d ← tick #%d | %s %s %s | remaining=%.1fs progress=%.0f%% | ev_up=%.3f ev_down=%.3f",
                    ev_id, tick_data.tick_id, pos.coin.upper(), pos.interval, name,
                    c.get("remaining", 0), c.get("progress", 0) * 100,
                    c.get("ev_up", 0), c.get("ev_down", 0),
                )

            if decision.should_bet and getattr(pos, '_skip_first_window', False):
                decision = BetDecision(
                    should_bet=False,
                    reason="skip first window (started mid-market)",
                    computed=decision.computed,
                )

            if not decision.should_bet and pos.skip_reason:
                logger.info("SKIP ev #%d | %s %s %s | %s",
                            pos.ev_id or 0, pos.coin.upper(), pos.interval, name, pos.skip_reason)

            if decision.should_bet and pos.state == PositionState.WATCHING:
                self.order_manager.execute_bet(name, strat, pos, decision)

        with ThreadPoolExecutor(max_workers=MAX_EVAL_WORKERS) as pool:
            list(pool.map(_eval_and_bet, work))

    def _build_tick_data(self, pos: MarketPosition) -> TickData | None:
        """Build TickData from the last recorded tick for this slug."""
        recorded = self._recorded_ticks.get(pos.slug)
        if recorded is None:
            return None

        tick_id, snapshot, signal = recorded
        return TickData(
            tick_id=tick_id, ts=time.time(), signal=signal,
            mid_up=snapshot.mid_up, mid_down=snapshot.mid_down,
            fill_up=snapshot.fill_up, fill_down=snapshot.fill_down,
            size_up=snapshot.size_up, size_down=snapshot.size_down,
            tick_size=snapshot.tick_size,
        )

    # ------------------------------------------------------------------
    # Health, redemption
    # ------------------------------------------------------------------

    def _update_health(self) -> None:
        self.health.update(
            last_tick=time.time(),
            feed_healthy=self.feed.is_healthy() if self.feed else False,
        )

    def _redeem_scan(self) -> None:
        from timba import db
        db.flush()
        unredeemed = db.get_unredeemed_wins()
        if not unredeemed:
            return
        logger.info("Redeem scan: %d unredeemed wins", len(unredeemed))
        t = threading.Thread(
            target=self._redeem_scan_bg,
            args=(self.relay_client, self.clob_client, self.state, unredeemed),
            daemon=True,
        )
        t.start()

    @staticmethod
    def _redeem_scan_bg(relay_client: object, clob_client: object, state: State, trades: list[dict]) -> None:
        from timba import db
        from timba.redeem import check_needs_redeem
        for trade in trades:
            cid = trade.get("condition_id", "")
            tid = trade.get("token_id", "")
            coin = trade.get("coin", "?").upper()
            interval = trade.get("interval", "?")
            payout = (trade.get("contracts", 0) or 0) * 1.0
            if not cid or not tid:
                continue
            if not check_needs_redeem(clob_client, tid):
                logger.info("REDEEM already | %s %s | condition=%s | payout=$%.2f",
                            coin, interval, cid[:16], payout)
                db.mark_trade_redeemed(cid)
                state.credit_redemption(payout)
                continue
            success = redeem_position(relay_client, cid)
            if success:
                logger.info("REDEEM ok | %s %s | condition=%s | payout=$%.2f",
                            coin, interval, cid[:16], payout)
                db.mark_trade_redeemed(cid)
                state.credit_redemption(payout)
            else:
                logger.warning("REDEEM failed | %s %s | condition=%s | will retry",
                               coin, interval, cid[:16])

    def _log_clob_state(self) -> None:
        try:
            usdc = self.clob_client.get_usdc_balance()
            logger.info("CLOB state | USDC=$%.2f | portfolio=$%.2f | cash=$%.2f",
                        usdc, self.state.portfolio, self.state.cash)
            if abs(usdc - self.state.cash) > 10:
                logger.warning("Cash mismatch: CLOB=$%.2f vs local=$%.2f", usdc, self.state.cash)
        except (requests.RequestException, OSError, ValueError, Exception):
            logger.exception("Failed to query CLOB state on startup")
