"""Tests for startup reconciliation."""

from unittest.mock import MagicMock

import pytest

from timba.reconcile import reconcile_startup
from timba.state import State


class TestReconcileStartup:
    def test_no_open_orders(self, tmp_data_dir):
        state = State()
        state.init_portfolio(100.0)

        clob = MagicMock()
        clob.get_orders.return_value = []
        clob.get_usdc_balance.return_value = 100.0

        summary = reconcile_startup(clob, state)

        assert summary["open_orders_found"] == 0
        assert not summary["orders_cancelled"]
        assert summary["clob_balance"] == 100.0
        assert summary["cash_delta"] == pytest.approx(0.0)
        clob.cancel_all.assert_not_called()

    def test_cancels_found_orders(self, tmp_data_dir):
        state = State()
        state.init_portfolio(100.0)

        mock_order = MagicMock()
        mock_order.order_id = "order-123"
        mock_order.price = 0.95
        mock_order.size = 10
        mock_order.size_matched = 0
        mock_order.side = "BUY"
        mock_order.original_size = 10

        clob = MagicMock()
        clob.get_orders.return_value = [mock_order]
        clob.get_usdc_balance.return_value = 100.0

        summary = reconcile_startup(clob, state)

        assert summary["open_orders_found"] == 1
        assert summary["orders_cancelled"]
        clob.cancel_all.assert_called_once()

    def test_fixes_cash_mismatch(self, tmp_data_dir):
        """Crash scenario: cash was deducted locally but order was never placed."""
        state = State()
        state.init_portfolio(100.0)
        state.deduct_cash(5.0)  # simulates pre-crash deduction
        assert state.cash == pytest.approx(95.0)

        clob = MagicMock()
        clob.get_orders.return_value = []
        clob.get_usdc_balance.return_value = 100.0  # CLOB still has full balance

        summary = reconcile_startup(clob, state)

        assert summary["cash_delta"] == pytest.approx(5.0)
        assert state.cash == pytest.approx(100.0)  # corrected to CLOB balance

    def test_resets_reserved_cash(self, tmp_data_dir):
        state = State()
        state.init_portfolio(100.0)
        state.reserved_cash = 15.0  # leftover from crash

        clob = MagicMock()
        clob.get_orders.return_value = []
        clob.get_usdc_balance.return_value = 100.0

        reconcile_startup(clob, state)

        assert state.reserved_cash == 0.0

    def test_survives_get_orders_error(self, tmp_data_dir):
        """CLOB errors should not crash the bot."""
        state = State()
        state.init_portfolio(100.0)

        clob = MagicMock()
        clob.get_orders.side_effect = Exception("network error")
        clob.get_usdc_balance.return_value = 100.0

        summary = reconcile_startup(clob, state)
        assert summary["open_orders_found"] == 0
        assert state.cash == pytest.approx(100.0)

    def test_survives_balance_error(self, tmp_data_dir):
        """If balance query fails, keep local cash."""
        state = State()
        state.init_portfolio(100.0)

        clob = MagicMock()
        clob.get_orders.return_value = []
        clob.get_usdc_balance.side_effect = Exception("network error")

        summary = reconcile_startup(clob, state)
        assert state.cash == pytest.approx(100.0)  # unchanged
        assert summary["clob_balance"] == 0.0

    def test_corrected_cash_updated_in_memory(self, tmp_data_dir):
        """Cash correction should update state in memory."""
        state = State()
        state.init_portfolio(100.0)
        state.deduct_cash(10.0)

        clob = MagicMock()
        clob.get_orders.return_value = []
        clob.get_usdc_balance.return_value = 100.0

        reconcile_startup(clob, state)

        assert state.cash == pytest.approx(100.0)

    def test_cancel_all_failure_survives(self, tmp_data_dir):
        """If cancel_all raises, reconciliation should continue without crashing."""
        state = State()
        state.init_portfolio(100.0)

        mock_order = MagicMock()
        mock_order.order_id = "order-456"
        mock_order.price = 0.90
        mock_order.size = 5
        mock_order.size_matched = 0
        mock_order.side = "BUY"
        mock_order.original_size = 5

        clob = MagicMock()
        clob.get_orders.return_value = [mock_order]
        clob.cancel_all.side_effect = Exception("cancel failed")
        clob.get_usdc_balance.return_value = 100.0

        summary = reconcile_startup(clob, state)

        assert summary["open_orders_found"] == 1
        assert summary["orders_cancelled"] is False  # cancel failed
        clob.cancel_all.assert_called_once()
        # Cash should still be reconciled despite cancel failure
        assert state.cash == pytest.approx(100.0)
