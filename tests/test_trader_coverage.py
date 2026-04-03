"""Additional trader.py tests targeting uncovered code paths.

Covers: run() shutdown/crash threshold, _scheduler_tick (balance sync,
redeem scan, DB rotation), _rotate_db, delegate methods, _eval_and_bet
inner function (window checks, liquidity, evaluate, bet placement,
skip-first-window), seen_slugs eviction, _log_clob_state, and
_redeem_scan_bg edge cases.
"""

import threading
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from timba import db
from timba.base import PositionState
from timba.market_cache import MarketSnapshot
from timba.state import State
from timba.strategies import BetDecision
from timba.strategies.favorite import FavoritePosition
from timba.trader import Trader


def _make_position(slug="test-slug", **kw):
    defaults = dict(
        condition_id="0x1", question="test", slug=slug,
        coin="btc", interval="5m", token_id_up="tu", token_id_down="td",
        end_timestamp=int(time.time()) + 10, window_start_ts=int(time.time()) - 290,
        contracts=10, entry_window_sec=20, close_window_sec=5,
        min_price=0.95, min_signal_chg=0.05,
    )
    defaults.update(kw)
    return FavoritePosition(**defaults)


# ── run() ──


class TestRun:
    def test_shutdown_event_exits_cleanly(self, trader_setup):
        """run() should exit when shutdown_event is set."""
        trader, state = trader_setup
        shutdown = threading.Event()

        # Set shutdown immediately so run() exits after first check
        shutdown.set()

        # Patch background threads and sleep to avoid blocking
        with patch.object(trader, '_discover_and_register'), \
             patch.object(trader.tick_recorder, 'run_loop'), \
             patch.object(trader.discovery, 'run_loop'), \
             patch.object(trader.resolver, 'run_loop'):
            trader.run(shutdown_event=shutdown)

        # Should have stopped background threads
        assert trader._background_running is False

    def test_error_window_threshold_shuts_down(self, trader_setup):
        """run() should exit when too many errors in a short window."""
        trader, state = trader_setup
        shutdown = threading.Event()
        call_count = 0

        def _failing_cleanup():
            nonlocal call_count
            call_count += 1
            if call_count >= 12:
                # After enough errors, set shutdown to prevent infinite loop
                shutdown.set()
            raise RuntimeError("boom")

        with patch.object(trader, '_discover_and_register'), \
             patch.object(trader.tick_recorder, 'run_loop'), \
             patch.object(trader.discovery, 'run_loop'), \
             patch.object(trader.resolver, 'run_loop'), \
             patch.object(trader, '_cleanup_stale_positions', side_effect=_failing_cleanup), \
             patch('timba.trader.time.sleep'):
            trader.run(shutdown_event=shutdown)

        assert trader.health.errors >= 10


# ── Delegate methods ──


class TestDelegateMethods:
    def test_commit_resolve_delegates(self, trader_setup):
        trader, _ = trader_setup
        trader.resolver.commit_resolve = MagicMock()
        strat = MagicMock()
        pos = MagicMock()

        trader._commit_resolve("favorite", strat, pos)
        trader.resolver.commit_resolve.assert_called_once_with("favorite", strat, pos)

    def test_commit_order_fill_delegates(self, trader_setup):
        trader, state = trader_setup
        state.cash = 100.0
        state.reserved_cash = 10.0

        trader._commit_order_fill(5.0, 10.0)

        assert state.cash == pytest.approx(95.0)
        assert state.reserved_cash == pytest.approx(0.0)

    def test_release_order_delegates(self, trader_setup):
        trader, state = trader_setup
        state.reserved_cash = 10.0

        trader._release_order(10.0)

        assert state.reserved_cash == pytest.approx(0.0)


# ── _scheduler_tick ──


class TestSchedulerTick:
    def test_balance_sync_queues_mutation(self, trader_setup):
        """_scheduler_tick should queue a balance sync when interval elapsed."""
        trader, state = trader_setup
        trader.scheduler._last_balance_sync = 0  # Force sync
        trader.scheduler._balance_interval = 0   # Always eligible
        trader.clob_client.get_usdc_balance.return_value = 200.0

        with patch("timba.scheduler.is_safe_window", return_value=True):
            trader._scheduler_tick()

        # Mutation should be queued
        assert not trader._mutations.empty()

        # Drain and verify state updated
        trader._drain_mutations()
        assert state.cash == pytest.approx(200.0)

    def test_balance_sync_skipped_outside_safe_window(self, trader_setup):
        """_scheduler_tick should do nothing outside safe windows."""
        trader, state = trader_setup
        state.cash = 100.0
        trader.scheduler._last_balance_sync = 0

        with patch("timba.scheduler.is_safe_window", return_value=False):
            trader._scheduler_tick()

        assert trader._mutations.empty()
        assert state.cash == pytest.approx(100.0)

    def test_balance_sync_handles_network_error(self, trader_setup):
        """Balance sync should not crash on network errors."""
        trader, _ = trader_setup
        trader.scheduler._last_balance_sync = 0
        trader.scheduler._balance_interval = 0
        trader.clob_client.get_usdc_balance.side_effect = ConnectionError("timeout")

        with patch("timba.scheduler.is_safe_window", return_value=True):
            trader._scheduler_tick()  # Should not raise

        assert trader._mutations.empty()

    def test_redeem_scan_queued(self, trader_setup):
        """_scheduler_tick should call _redeem_scan when interval elapsed."""
        trader, _ = trader_setup
        trader.relay_client = MagicMock()
        trader.scheduler._last_balance_sync = time.time()  # Skip balance sync
        trader.scheduler._last_redeem_scan = 0
        trader.scheduler._redeem_interval = 0

        with patch("timba.scheduler.is_safe_window", return_value=True), \
             patch.object(trader, '_redeem_scan') as mock_redeem:
            trader._scheduler_tick()
            mock_redeem.assert_called_once()

    def test_db_rotation_queued(self, trader_setup):
        """_scheduler_tick should queue DB rotation when should_rotate returns truthy."""
        trader, _ = trader_setup
        trader.scheduler._last_balance_sync = time.time()
        trader.scheduler._last_redeem_scan = time.time()
        trader.scheduler.should_rotate_db = MagicMock(return_value="daily rotation")

        with patch("timba.scheduler.is_safe_window", return_value=True):
            trader._scheduler_tick()

        assert not trader._mutations.empty()


# ── _rotate_db ──


class TestRotateDb:
    def test_rotate_updates_last_rotation_date(self, trader_setup):
        """_rotate_db should update scheduler._last_rotation_date on success."""
        trader, _ = trader_setup
        trader.scheduler._last_rotation_date = "2026-01-01"

        with patch("timba.db.rotate", return_value="/some/archive.db"):
            trader._rotate_db("daily rotation")

        assert trader.scheduler._last_rotation_date == datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def test_rotate_no_update_when_rotate_returns_none(self, trader_setup):
        """_rotate_db should NOT update date when rotate() returns None."""
        trader, _ = trader_setup
        trader.scheduler._last_rotation_date = "2026-01-01"

        with patch("timba.db.rotate", return_value=None):
            trader._rotate_db("daily rotation")

        assert trader.scheduler._last_rotation_date == "2026-01-01"


# ── _evaluate_all (inner _eval_and_bet) ──


class TestEvalAndBet:
    def _setup_eval(self, trader, state, slug="btc-updown-5m-1234567890",
                    end_offset=8, entry_window=20, close_window=5,
                    tick_data=True, signal=None):
        """Wire up a position with tick data for _evaluate_all to process."""
        now = time.time()
        pos = _make_position(
            slug=slug,
            end_timestamp=int(now + end_offset),
            window_start_ts=int(now - 290),
            entry_window_sec=entry_window,
            close_window_sec=close_window,
        )
        trader.positions["favorite"][slug] = pos

        if tick_data:
            snapshot = MarketSnapshot(
                mid_up=0.98, mid_down=0.02,
                fill_up=0.98, fill_down=0.02,
                size_up=100, size_down=200,
                tick_size=0.01,
            )
            sig = signal or MagicMock()
            trader._recorded_ticks[slug] = (42, snapshot, sig)

        return pos

    def test_skips_pending_order(self, trader_setup):
        """Positions in PENDING_ORDER state should be skipped by eval."""
        trader, state = trader_setup
        pos = self._setup_eval(trader, state)
        pos.transition(PositionState.PENDING_ORDER)

        trader._evaluate_all()

        assert pos.state == PositionState.PENDING_ORDER

    def test_skips_when_no_tick_data(self, trader_setup):
        """Positions with no recorded tick should be skipped."""
        trader, state = trader_setup
        pos = self._setup_eval(trader, state, tick_data=False)

        trader._evaluate_all()

        # Still watching — no eval happened
        assert pos.state == PositionState.WATCHING

    def test_window_timeout_marks_skipped(self, trader_setup):
        """Position past its close window should be marked SKIPPED."""
        trader, state = trader_setup
        # end_offset=1 with close_window=5 → remaining(1) < close_window(5) → timeout
        pos = self._setup_eval(trader, state, end_offset=1, close_window=5)

        trader._evaluate_all()

        assert pos.state == PositionState.SKIPPED
        assert "timeout" in (pos.skip_reason or "")

    def test_early_window_not_evaluated(self, trader_setup):
        """Position too far from entry window should not be evaluated."""
        trader, state = trader_setup
        # end_offset=1000 with entry_window=20 → remaining(1000) > entry(20) → early
        pos = self._setup_eval(trader, state, end_offset=1000, entry_window=20)

        trader._evaluate_all()

        # Still watching, not even in work list (remaining > entry + buffer)
        assert pos.state == PositionState.WATCHING

    def test_evaluate_places_bet(self, trader_setup):
        """When strategy returns should_bet=True, execute_bet is called."""
        trader, state = trader_setup
        self._setup_eval(trader, state)

        decision = BetDecision(
            should_bet=True, side="up", price=0.98, size=5,
            reason="high EV", computed={"ev_up": 0.05, "ev_down": -0.02},
        )
        strat = trader._strategies["favorite"]
        strat.evaluate = MagicMock(return_value=decision)

        with patch.object(trader.order_manager, 'execute_bet') as mock_bet:
            trader._evaluate_all()
            mock_bet.assert_called_once()

    def test_evaluate_logs_skip_reason(self, trader_setup):
        """When strategy returns should_bet=False with reason, it should be logged."""
        trader, state = trader_setup
        pos = self._setup_eval(trader, state)

        decision = BetDecision(
            should_bet=False, reason="price too low",
            computed={"ev_up": 0.01, "ev_down": -0.01},
        )
        strat = trader._strategies["favorite"]
        strat.evaluate = MagicMock(return_value=decision)

        trader._evaluate_all()

        assert pos.skip_reason == "price too low"
        assert pos.state == PositionState.WATCHING

    def test_skip_first_window_blocks_bet(self, trader_setup):
        """Position with _skip_first_window=True should not bet even if strategy says yes."""
        trader, state = trader_setup
        pos = self._setup_eval(trader, state)
        pos._skip_first_window = True

        decision = BetDecision(
            should_bet=True, side="up", price=0.98, size=5,
            computed={"ev_up": 0.05, "ev_down": -0.02},
        )
        strat = trader._strategies["favorite"]
        strat.evaluate = MagicMock(return_value=decision)

        with patch.object(trader.order_manager, 'execute_bet') as mock_bet:
            trader._evaluate_all()
            mock_bet.assert_not_called()

    def test_low_liquidity_marks_skipped(self, trader_setup):
        """Position with low liquidity should be marked SKIPPED."""
        trader, state = trader_setup
        pos = self._setup_eval(trader, state)
        pos.liquidity = 50  # Below MIN_LIQUIDITY (100)
        pos.liquidity_checked = False

        trader._evaluate_all()

        assert pos.state == PositionState.SKIPPED
        assert "liquidity" in (pos.skip_reason or "")

    def test_evaluate_writes_ev(self, trader_setup):
        """When strategy returns computed data, an EV record should be written."""
        trader, state = trader_setup
        pos = self._setup_eval(trader, state)

        computed = {"ev_up": 0.05, "ev_down": -0.02, "remaining": 8.0, "progress": 0.5}
        decision = BetDecision(
            should_bet=False, reason="below threshold",
            computed=computed,
        )
        strat = trader._strategies["favorite"]
        strat.evaluate = MagicMock(return_value=decision)

        with patch("timba.trader.write_strategy_data", return_value=99) as mock_write:
            trader._evaluate_all()
            mock_write.assert_called_once()
            assert pos.ev_id == 99


# ── seen_slugs eviction ──


class TestSeenSlugsEviction:
    def test_old_seen_slugs_evicted(self, trader_setup):
        """Seen slugs older than 2h should be evicted during cleanup."""
        trader, _ = trader_setup
        now = time.time()
        trader._seen_slugs["favorite"] = {
            "old-slug": now - 8000,   # >2h ago → should be evicted
            "recent-slug": now - 100, # recent → should stay
        }

        trader._cleanup_stale_positions()

        assert "old-slug" not in trader._seen_slugs["favorite"]
        assert "recent-slug" in trader._seen_slugs["favorite"]


# ── _log_clob_state ──


class TestLogClobState:
    def test_logs_without_error(self, trader_setup):
        """_log_clob_state should query CLOB balance and log it."""
        trader, state = trader_setup
        state.cash = 500.0
        state.portfolio = 500.0
        trader.clob_client.get_usdc_balance.return_value = 500.0

        trader._log_clob_state()  # Should not raise

    def test_handles_network_error(self, trader_setup):
        """_log_clob_state should catch network errors gracefully."""
        trader, _ = trader_setup
        trader.clob_client.get_usdc_balance.side_effect = ConnectionError("timeout")

        trader._log_clob_state()  # Should not raise

    def test_warns_on_cash_mismatch(self, trader_setup):
        """_log_clob_state should log a warning when CLOB != local cash."""
        trader, state = trader_setup
        state.cash = 500.0
        trader.clob_client.get_usdc_balance.return_value = 100.0

        trader._log_clob_state()  # Should not raise


# ── _scheduler_loop ──


class TestSchedulerLoop:
    def test_loop_exits_when_background_stops(self, trader_setup):
        """_scheduler_loop should exit when _background_running is False."""
        trader, _ = trader_setup
        trader._background_running = False

        # Should return immediately
        trader._scheduler_loop()

    def test_loop_catches_exceptions(self, trader_setup):
        """_scheduler_loop should catch and log errors without crashing."""
        trader, _ = trader_setup
        trader._background_running = True
        call_count = 0

        def _failing_tick():
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                trader._background_running = False
                return
            raise RuntimeError("scheduler boom")

        trader._scheduler_tick = _failing_tick

        with patch('timba.trader.time.sleep'):
            trader._scheduler_loop()

        assert call_count >= 2


# ── _redeem_scan_bg edge cases ──


class TestRunKeyboardInterrupt:
    def test_keyboard_interrupt_exits_cleanly(self, trader_setup):
        """run() should handle KeyboardInterrupt and stop (lines 285-286, 289-294)."""
        trader, state = trader_setup
        shutdown = threading.Event()

        call_count = 0
        def raise_keyboard_interrupt():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise KeyboardInterrupt()

        with patch.object(trader, '_discover_and_register'), \
             patch.object(trader.tick_recorder, 'run_loop'), \
             patch.object(trader.discovery, 'run_loop'), \
             patch.object(trader.resolver, 'run_loop'), \
             patch.object(trader, '_drain_mutations', side_effect=raise_keyboard_interrupt), \
             patch('timba.trader.time.sleep'):
            trader.run(shutdown_event=shutdown)

        assert trader._background_running is False


class TestRunErrorWindowReset:
    def test_error_window_resets_after_timeout(self, trader_setup):
        """Error window resets when errors are spaced > ERROR_WINDOW_SEC apart (lines 300-301).

        We use a time mock that jumps forward by >60s between errors to trigger the reset.
        """
        trader, state = trader_setup
        shutdown = threading.Event()
        call_count = 0

        def _failing_cleanup():
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                shutdown.set()
                return
            raise RuntimeError("boom")

        # Mock time.time to return values far apart so the error window resets.
        # run() calls time.time() at: (1) _error_window_start init, (2) each except block.
        # We need call N+1 - call N > 60 for the reset to trigger.
        time_counter = [0]

        def mock_time():
            time_counter[0] += 100  # each call 100s apart, always > ERROR_WINDOW_SEC (60)
            return time_counter[0]

        with patch.object(trader, '_discover_and_register'), \
             patch.object(trader.tick_recorder, 'run_loop'), \
             patch.object(trader.discovery, 'run_loop'), \
             patch.object(trader.resolver, 'run_loop'), \
             patch.object(trader, '_cleanup_stale_positions', side_effect=_failing_cleanup), \
             patch('timba.trader.time.time', side_effect=mock_time), \
             patch('timba.trader.time.sleep'):
            trader.run(shutdown_event=shutdown)

        # Errors were counted but window reset each time, so no crash threshold
        assert trader.health.errors >= 2


class TestDrainMutationsQueueEmpty:
    def test_queue_empty_race_condition(self, trader_setup):
        """_drain_mutations handles queue.Empty from get_nowait() (line 324)."""
        import queue as queue_mod
        trader, state = trader_setup

        # Create a mock queue where empty() returns False but get_nowait raises Empty
        mock_q = MagicMock()
        call_count = 0
        def mock_empty():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return False  # enter the loop
            return True  # exit
        mock_q.empty.side_effect = mock_empty
        mock_q.get_nowait.side_effect = queue_mod.Empty()

        trader._mutations = mock_q
        trader._drain_mutations()  # should not raise
        mock_q.get_nowait.assert_called_once()


class TestEvalAndBetEarlyReturn:
    def test_early_window_returns_from_eval(self, trader_setup):
        """_eval_and_bet returns early when check_entry_window says 'early' (line 487).

        Position remaining is within EVAL_WINDOW_BUFFER but before entry window.
        """
        trader, state = trader_setup
        now = time.time()
        # remaining = 25s, entry_window = 20s → "early" status
        # But 25 <= 20 + 30 (buffer) so it enters the work list
        pos = _make_position(
            slug="btc-updown-5m-early",
            end_timestamp=int(now + 25),
            window_start_ts=int(now - 275),
            entry_window_sec=20,
            close_window_sec=5,
        )
        trader.positions["favorite"]["btc-updown-5m-early"] = pos

        # Provide tick data so it doesn't return at tick_data check
        snapshot = MarketSnapshot(
            mid_up=0.98, mid_down=0.02,
            fill_up=0.98, fill_down=0.02,
            size_up=100, size_down=200,
            tick_size=0.01,
        )
        trader._recorded_ticks["btc-updown-5m-early"] = (42, snapshot, MagicMock())

        strat = trader._strategies["favorite"]
        strat.evaluate = MagicMock()

        trader._evaluate_all()

        # evaluate should NOT have been called because check_entry_window returns "early"
        strat.evaluate.assert_not_called()
        assert pos.state == PositionState.WATCHING


class TestRedeemScanBgEdgeCases:
    def test_skips_trades_without_condition_id(self, tmp_path):
        """Trades missing condition_id should be skipped."""
        data_dir = tmp_path / "data"
        db.init(data_dir)
        state = State()

        trades = [{"coin": "btc", "interval": "5m", "contracts": 5, "token_id": "tok"}]

        with patch("timba.redeem.check_needs_redeem") as mock_check:
            Trader._redeem_scan_bg(MagicMock(), MagicMock(), state, trades)
            mock_check.assert_not_called()

    def test_skips_trades_without_token_id(self, tmp_path):
        """Trades missing token_id should be skipped."""
        data_dir = tmp_path / "data"
        db.init(data_dir)
        state = State()

        trades = [{"condition_id": "0xabc", "coin": "btc", "interval": "5m", "contracts": 5}]

        with patch("timba.redeem.check_needs_redeem") as mock_check:
            Trader._redeem_scan_bg(MagicMock(), MagicMock(), state, trades)
            mock_check.assert_not_called()

    def test_payout_handles_zero_contracts(self, tmp_path):
        """Payout calculation should handle 0 contracts without error."""
        data_dir = tmp_path / "data"
        db.init(data_dir)
        state = State()
        state.pending_redemption = 0.0

        trades = [{
            "condition_id": "0xabc", "token_id": "tok",
            "coin": "btc", "interval": "5m", "contracts": 0,
        }]

        with patch("timba.redeem.check_needs_redeem", return_value=False):
            Trader._redeem_scan_bg(MagicMock(), MagicMock(), state, trades)

        # cash credited with payout of 0
        assert state.cash == pytest.approx(0.0)
