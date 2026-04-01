"""Tests for OrderManager (orders.py): cash lock, order fill, release."""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from timba.base import PositionState
from timba.strategies.favorite import FavoritePosition


class TestCashLock:
    """Verify atomic check-and-reserve prevents concurrent double-spend."""

    def _make_live_position(self, slug="test-slug", cost_price=0.95, contracts=10):
        """Create a live-mode position that will trigger a cash check."""
        return FavoritePosition(
            condition_id="0x1", question="test", slug=slug,
            coin="btc", interval="5m", token_id_up="tu", token_id_down="td",
            end_timestamp=int(time.time()) + 10, window_start_ts=int(time.time()) - 290,
            contracts=contracts, entry_window_sec=30, close_window_sec=2,
            min_price=0.95, min_signal_chg=0.05,
            market_mode="live",
        )

    def test_sequential_reserve_respects_prior(self, trader_setup):
        """Second reserve sees the first one's deduction."""
        trader, state = trader_setup
        state.cash = 100.0
        state.reserved_cash = 0.0

        pos_a = self._make_live_position("slug-a")
        self._make_live_position("slug-b")

        decision = MagicMock()
        decision.side = "up"
        decision.price = 0.95
        decision.size = 10
        decision.reason = ""
        decision.should_bet = True
        decision.computed = {}

        # First bet: cost = 0.95 * 10 = $9.50, reserves it
        with patch.object(trader.order_manager, '_handle_order'):
            trader._execute_bet("favorite", MagicMock(), pos_a, decision)
        assert state.reserved_cash == pytest.approx(9.50, abs=0.01)

        # Second bet sees reduced available_cash
        assert state.available_cash == pytest.approx(90.50, abs=0.01)

    def test_concurrent_reserves_no_double_spend(self, trader_setup):
        """Two threads competing for cash > available should allow at most one."""
        trader, state = trader_setup
        state.cash = 100.0
        state.reserved_cash = 0.0

        results = {"reserved": 0, "skipped": 0}
        barrier = threading.Barrier(2)

        def try_reserve(cost):
            barrier.wait()
            with trader._cash_lock:
                if state.available_cash < cost:
                    results["skipped"] += 1
                    return
                state.reserved_cash += cost
                results["reserved"] += 1

        # Both want $60 but only $100 available — at most one should succeed
        t1 = threading.Thread(target=try_reserve, args=(60,))
        t2 = threading.Thread(target=try_reserve, args=(60,))
        t1.start(); t2.start()
        t1.join(); t2.join()

        assert results["reserved"] <= 1 or state.reserved_cash <= 100.0

    def test_release_frees_cash_for_next_reserve(self, trader_setup):
        """After release_order, the cash is available again."""
        trader, state = trader_setup
        state.cash = 100.0
        state.reserved_cash = 50.0

        assert state.available_cash == pytest.approx(50.0)

        trader.order_manager.release_order(50.0)
        assert state.reserved_cash == 0.0
        assert state.available_cash == pytest.approx(100.0)

    def test_commit_order_fill_deducts_and_releases(self, trader_setup):
        """Fill deducts actual cost and releases the reservation."""
        trader, state = trader_setup
        state.cash = 100.0
        state.reserved_cash = 60.0

        trader.order_manager.commit_order_fill(actual_cost=55.0, reserved_cost=60.0)

        assert state.reserved_cash == 0.0
        assert state.cash == pytest.approx(45.0)


class TestHandleOrderPaperFill:
    """Test _handle_order for paper mode — instant fill at rounded price."""

    def _make_paper_position(self):
        return FavoritePosition(
            condition_id="0x1", question="test", slug="btc-updown-5m-100",
            coin="btc", interval="5m", token_id_up="tu", token_id_down="td",
            end_timestamp=int(time.time()) + 10, window_start_ts=int(time.time()) - 290,
            contracts=5, entry_window_sec=30, close_window_sec=2,
            min_price=0.95, min_signal_chg=0.05,
            market_mode="paper",
        )

    def _make_decision(self, price=0.98, size=5):
        d = MagicMock()
        d.side = "up"
        d.price = price
        d.size = size
        d.reason = "test"
        d.should_bet = True
        d.computed = {}
        return d

    def test_paper_fills_at_rounded_price(self, trader_setup):
        """Paper order fills instantly at the ceil-rounded price."""
        trader, state = trader_setup
        pos = self._make_paper_position()
        pos.transition(PositionState.PENDING_ORDER)
        decision = self._make_decision(price=0.975)

        # Mock _wait_for_tick to return tick_size=0.01, max_price=0.99
        with patch.object(trader.order_manager, '_wait_for_tick', return_value=(0.01, 0.99)):
            strat = MagicMock()
            trader.order_manager._handle_order("favorite", strat, pos, decision, 0.975, 0)

        assert pos.state == PositionState.PAPER
        # ceil(0.975 / 0.01) * 0.01 = 0.98
        assert pos.buy_price == pytest.approx(0.98, abs=0.001)
        strat.on_bet.assert_called_once()

    def test_paper_unfillable_price_transitions_to_failed(self, trader_setup):
        """Paper order with price that rounds above max_price transitions to FAILED."""
        trader, state = trader_setup
        pos = self._make_paper_position()
        pos.transition(PositionState.PENDING_ORDER)
        decision = self._make_decision(price=0.995)

        # tick=0.01, max=0.99 → ceil(0.995/0.01)*0.01 = 1.00 > 0.99
        with patch.object(trader.order_manager, '_wait_for_tick', return_value=(0.01, 0.99)):
            strat = MagicMock()
            trader.order_manager._handle_order("favorite", strat, pos, decision, 0.995, 0)

        assert pos.state == PositionState.FAILED
        assert "unfillable" in pos.skip_reason
        strat.on_bet.assert_not_called()

    def test_paper_exact_max_price_fills(self, trader_setup):
        """Price that rounds exactly to max_price should fill."""
        trader, state = trader_setup
        pos = self._make_paper_position()
        pos.transition(PositionState.PENDING_ORDER)
        decision = self._make_decision(price=0.99)

        with patch.object(trader.order_manager, '_wait_for_tick', return_value=(0.01, 0.99)):
            strat = MagicMock()
            trader.order_manager._handle_order("favorite", strat, pos, decision, 0.99, 0)

        assert pos.state == PositionState.PAPER
        assert pos.buy_price == pytest.approx(0.99)

    def test_paper_with_small_tick_fills_at_finer_precision(self, trader_setup):
        """With tick_size=0.001, ceil rounds to finer precision."""
        trader, state = trader_setup
        pos = self._make_paper_position()
        pos.transition(PositionState.PENDING_ORDER)
        decision = self._make_decision(price=0.9825)

        # tick=0.001, max=0.999
        with patch.object(trader.order_manager, '_wait_for_tick', return_value=(0.001, 0.999)):
            strat = MagicMock()
            trader.order_manager._handle_order("favorite", strat, pos, decision, 0.9825, 0)

        assert pos.state == PositionState.PAPER
        # ceil(0.9825 / 0.001) * 0.001 = 0.983
        assert pos.buy_price == pytest.approx(0.983, abs=0.001)


class TestHandleOrderLive:
    """Test _handle_order for live mode — CLOB placement."""

    def _make_live_position(self):
        return FavoritePosition(
            condition_id="0x1", question="test", slug="btc-updown-5m-100",
            coin="btc", interval="5m", token_id_up="tu", token_id_down="td",
            end_timestamp=int(time.time()) + 10, window_start_ts=int(time.time()) - 290,
            contracts=5, entry_window_sec=30, close_window_sec=2,
            min_price=0.95, min_signal_chg=0.05,
            market_mode="live",
        )

    def _make_decision(self, price=0.98, size=5):
        d = MagicMock()
        d.side = "up"
        d.price = price
        d.size = size
        d.reason = "test"
        d.should_bet = True
        d.computed = {}
        return d

    def test_live_successful_fill_queues_commit(self, trader_setup):
        """Live order that fills successfully queues commit_order_fill."""
        trader, state = trader_setup
        pos = self._make_live_position()
        pos.transition(PositionState.PENDING_ORDER)
        decision = self._make_decision(price=0.98)

        with patch.object(trader.order_manager, '_wait_for_tick', return_value=(0.01, 0.99)), \
             patch.object(trader.order_manager, '_create_clob_client') as mock_clob, \
             patch('timba.orders.place_order', return_value=True):
            mock_clob.return_value = MagicMock()
            strat = MagicMock()
            trader.order_manager._handle_order("favorite", strat, pos, decision, 0.98, 4.90)

        assert pos.state == PositionState.SNIPED
        strat.on_bet.assert_called_once()
        # A mutation should be queued
        assert not trader._mutations.empty()

    def test_live_failed_fill_queues_release(self, trader_setup):
        """Live order that fails to fill queues release_order."""
        trader, state = trader_setup
        pos = self._make_live_position()
        pos.transition(PositionState.PENDING_ORDER)
        decision = self._make_decision(price=0.98)

        with patch.object(trader.order_manager, '_wait_for_tick', return_value=(0.01, 0.99)), \
             patch.object(trader.order_manager, '_create_clob_client') as mock_clob, \
             patch('timba.orders.place_order', return_value=False):
            mock_clob.return_value = MagicMock()
            strat = MagicMock()
            trader.order_manager._handle_order("favorite", strat, pos, decision, 0.98, 4.90)

        assert pos.state == PositionState.SKIPPED
        strat.on_bet.assert_not_called()
        assert not trader._mutations.empty()

    def test_live_unfillable_releases_cash(self, trader_setup):
        """Live unfillable order queues release_order."""
        trader, state = trader_setup
        pos = self._make_live_position()
        pos.transition(PositionState.PENDING_ORDER)
        decision = self._make_decision(price=0.995)

        with patch.object(trader.order_manager, '_wait_for_tick', return_value=(0.01, 0.99)):
            strat = MagicMock()
            trader.order_manager._handle_order("favorite", strat, pos, decision, 0.995, 4.975)

        assert pos.state == PositionState.FAILED
        # Cash release mutation should be queued
        assert not trader._mutations.empty()

    def test_handle_order_exception_skips_and_releases(self, trader_setup):
        """Exception during _handle_order transitions to SKIPPED and releases cash."""
        trader, state = trader_setup
        pos = self._make_live_position()
        pos.transition(PositionState.PENDING_ORDER)
        decision = self._make_decision(price=0.98)

        with patch.object(trader.order_manager, '_wait_for_tick', side_effect=Exception("network error")):
            strat = MagicMock()
            trader.order_manager._handle_order("favorite", strat, pos, decision, 0.98, 4.90)

        assert pos.state == PositionState.SKIPPED
        assert not trader._mutations.empty()


class TestExecuteBet:
    """Test execute_bet: cash check, logging, thread spawn."""

    def _make_position(self, mode="paper"):
        return FavoritePosition(
            condition_id="0x1", question="test", slug="btc-updown-5m-100",
            coin="btc", interval="5m", token_id_up="tu", token_id_down="td",
            end_timestamp=int(time.time()) + 10, window_start_ts=int(time.time()) - 290,
            contracts=5, entry_window_sec=30, close_window_sec=2,
            min_price=0.95, min_signal_chg=0.05,
            market_mode=mode,
        )

    def _make_decision(self, price=0.98, size=5):
        d = MagicMock()
        d.side = "up"
        d.price = price
        d.size = size
        d.reason = "test"
        d.should_bet = True
        d.computed = {}
        return d

    def test_paper_bet_skips_cash_check(self, trader_setup):
        """Paper bets don't check or reserve cash."""
        trader, state = trader_setup
        state.cash = 0.0  # No cash
        state.reserved_cash = 0.0
        pos = self._make_position(mode="paper")
        decision = self._make_decision()

        with patch.object(trader.order_manager, '_handle_order'):
            trader.order_manager.execute_bet("favorite", MagicMock(), pos, decision)

        # Should transition to PENDING_ORDER regardless of cash
        assert pos.state == PositionState.PENDING_ORDER
        assert state.reserved_cash == 0.0

    def test_live_bet_insufficient_cash_skips(self, trader_setup):
        """Live bet with insufficient cash transitions to SKIPPED."""
        trader, state = trader_setup
        state.cash = 1.0  # Only $1
        state.reserved_cash = 0.0
        pos = self._make_position(mode="live")
        decision = self._make_decision(price=0.98, size=10)  # Cost = $9.80

        with patch.object(trader.order_manager, '_handle_order'):
            trader.order_manager.execute_bet("favorite", MagicMock(), pos, decision)

        assert pos.state == PositionState.SKIPPED
        assert "insufficient funds" in pos.skip_reason


class TestWaitForTick:
    """Test _wait_for_tick polling logic."""

    def test_returns_immediately_when_price_fits(self, trader_setup):
        """When price fits within tick, returns immediately without polling."""
        trader, state = trader_setup
        snapshot = MagicMock()
        snapshot.tick_size = 0.01
        trader.market_cache.get = MagicMock(return_value=snapshot)

        tick, max_price = trader.order_manager._wait_for_tick("test-slug", 0.95, timeout=1)

        assert tick == 0.01
        assert max_price == pytest.approx(0.99)

    def test_returns_default_tick_when_no_snapshot(self, trader_setup):
        """When market_cache has no data, uses default tick=0.01."""
        trader, state = trader_setup
        trader.market_cache.get = MagicMock(return_value=None)

        tick, max_price = trader.order_manager._wait_for_tick("test-slug", 0.95, timeout=0.1)

        assert tick == 0.01
        assert max_price == pytest.approx(0.99)
