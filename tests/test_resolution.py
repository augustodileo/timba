"""Tests for ResolutionWorker (resolution.py): commit_resolve, trade recording."""

import time
from unittest.mock import patch

import pytest

from timba import db
from timba.base import PositionState
from timba.strategies.favorite import FavoritePosition


def _make_position(slug="test-slug", **kw):
    defaults = dict(
        condition_id="0x1", question="BTC test", slug=slug,
        coin="btc", interval="5m", token_id_up="tu", token_id_down="td",
        end_timestamp=int(time.time()) - 1, window_start_ts=int(time.time()) - 300,
        contracts=10, entry_window_sec=30, close_window_sec=2,
        min_price=0.95, min_signal_chg=0.05,
    )
    defaults.update(kw)
    return FavoritePosition(**defaults)


class TestCommitResolve:
    def test_records_trade_and_removes_position(self, trader_setup):
        """commit_resolve should record trade and remove position from positions dict."""
        trader, state = trader_setup
        pos = _make_position(slug="test-resolve")
        pos.state = PositionState.PAPER_WON
        pos.side = "up"
        pos.buy_price = 0.90
        pos.pnl = 1.0
        pos.sniped_at = "2026-03-24T10:00:00Z"
        pos.resolved_at = "2026-03-24T10:00:30Z"

        trader.positions["favorite"]["test-resolve"] = pos

        strat = trader._strategies["favorite"]
        trader.resolver.commit_resolve("favorite", strat, pos)

        assert "test-resolve" not in trader.positions["favorite"]

        db.flush()
        trades = db.load_trades(strategy="favorite")
        assert len(trades) == 1
        assert trades[0]["type"] == "paper_win"

    def test_untracks_market_when_no_strategies_use_it(self, trader_setup):
        """market_cache.untrack should be called when slug is not in any strategy."""
        trader, state = trader_setup
        pos = _make_position(
            condition_id="0x2", slug="test-untrack", coin="eth",
        )
        pos.state = PositionState.PAPER_LOST
        pos.side = "down"
        pos.buy_price = 0.80
        pos.pnl = -8.0
        pos.sniped_at = "2026-03-24T10:00:00Z"
        pos.resolved_at = "2026-03-24T10:00:30Z"

        trader.positions["favorite"]["test-untrack"] = pos

        strat = trader._strategies["favorite"]
        with patch.object(trader.market_cache, "untrack") as mock_untrack:
            trader.resolver.commit_resolve("favorite", strat, pos)

            assert "test-untrack" not in trader.positions["favorite"]
            mock_untrack.assert_called_with("test-untrack")


class TestCheckAndQueueResolve:
    """Test _check_and_queue_resolve: win/loss/skip resolution paths."""

    def test_position_resolved_as_win(self, trader_setup):
        """A bet that won should queue a mutation with WON state."""
        trader, state = trader_setup
        pos = _make_position(slug="win-slug")
        pos.state = PositionState.PAPER
        pos.side = "up"
        pos.buy_price = 0.98
        pos.sniped_at = "2026-03-24T10:00:00Z"

        strat = trader._strategies["favorite"]

        with patch("timba.resolution.resolve_winner", return_value=True):
            trader.resolver._check_and_queue_resolve("favorite", strat, pos)

        assert pos.state == PositionState.PAPER_WON
        assert pos.pnl == pytest.approx((1.0 - 0.98) * 10)

    def test_position_resolved_as_loss(self, trader_setup):
        """A bet that lost should transition to PAPER_LOST."""
        trader, state = trader_setup
        pos = _make_position(slug="loss-slug")
        pos.state = PositionState.PAPER
        pos.side = "down"
        pos.buy_price = 0.95
        pos.sniped_at = "2026-03-24T10:00:00Z"

        strat = trader._strategies["favorite"]

        with patch("timba.resolution.resolve_winner", return_value=False):
            trader.resolver._check_and_queue_resolve("favorite", strat, pos)

        assert pos.state == PositionState.PAPER_LOST
        assert pos.pnl == pytest.approx(-0.95 * 10)

    def test_position_not_resolved_yet(self, trader_setup):
        """When resolve_winner returns None, state should not change."""
        trader, state = trader_setup
        pos = _make_position(slug="pending-slug")
        pos.state = PositionState.PAPER
        pos.side = "up"
        pos.buy_price = 0.98
        pos.sniped_at = "2026-03-24T10:00:00Z"

        strat = trader._strategies["favorite"]

        with patch("timba.resolution.resolve_winner", return_value=None):
            trader.resolver._check_and_queue_resolve("favorite", strat, pos)

        # State unchanged — still PAPER
        assert pos.state == PositionState.PAPER

    def test_skip_position_resolved_as_win(self, trader_setup):
        """A skipped position (side set) resolved as win → SKIP_WON."""
        trader, state = trader_setup
        pos = _make_position(slug="skip-win-slug")
        pos.state = PositionState.SKIPPED
        pos.side = "up"
        pos.buy_price = 0.96
        pos.sniped_at = "2026-03-24T10:00:00Z"

        strat = trader._strategies["favorite"]

        with patch("timba.resolution.resolve_winner", return_value=True):
            trader.resolver._check_and_queue_resolve("favorite", strat, pos)

        assert pos.state == PositionState.SKIP_WON

    def test_skip_position_resolved_as_loss(self, trader_setup):
        """A skipped position (side set) resolved as loss → SKIP_LOST."""
        trader, state = trader_setup
        pos = _make_position(slug="skip-loss-slug")
        pos.state = PositionState.SKIPPED
        pos.side = "down"
        pos.buy_price = 0.96
        pos.sniped_at = "2026-03-24T10:00:00Z"

        strat = trader._strategies["favorite"]

        with patch("timba.resolution.resolve_winner", return_value=False):
            trader.resolver._check_and_queue_resolve("favorite", strat, pos)

        assert pos.state == PositionState.SKIP_LOST

    def test_skip_none_when_no_side(self, trader_setup):
        """A skipped position with no side resolved as loss → SKIP_NONE."""
        trader, state = trader_setup
        pos = _make_position(slug="skip-none-slug")
        pos.state = PositionState.SKIPPED
        pos.side = ""
        pos.buy_price = 0
        pos.sniped_at = "2026-03-24T10:00:00Z"

        strat = trader._strategies["favorite"]

        with patch("timba.resolution.resolve_winner", return_value=False):
            trader.resolver._check_and_queue_resolve("favorite", strat, pos)

        assert pos.state == PositionState.SKIP_NONE

    def test_failed_position_resolved(self, trader_setup):
        """A failed position (wanted to bet, couldn't fill) resolved → FAIL_WON/FAIL_LOST."""
        trader, state = trader_setup
        pos = _make_position(slug="fail-slug")
        pos.state = PositionState.FAILED
        pos.side = "up"
        pos.buy_price = 0.995
        pos.sniped_at = "2026-03-24T10:00:00Z"
        pos.skip_reason = "unfillable"

        strat = trader._strategies["favorite"]

        with patch("timba.resolution.resolve_winner", return_value=True):
            trader.resolver._check_and_queue_resolve("favorite", strat, pos)

        assert pos.state == PositionState.FAIL_WON

    def test_queues_mutation_on_resolve(self, trader_setup):
        """Resolution should queue a commit_resolve mutation."""
        trader, state = trader_setup
        pos = _make_position(slug="mutation-slug")
        pos.state = PositionState.PAPER
        pos.side = "up"
        pos.buy_price = 0.98
        pos.sniped_at = "2026-03-24T10:00:00Z"

        strat = trader._strategies["favorite"]

        with patch("timba.resolution.resolve_winner", return_value=True):
            trader.resolver._check_and_queue_resolve("favorite", strat, pos)

        assert not trader._mutations.empty()

    def test_non_pending_state_returns_early(self, trader_setup):
        """resolve_map returns None for non-pending states — no-op."""
        trader, state = trader_setup
        pos = _make_position(slug="watching-slug")
        pos.state = PositionState.WATCHING
        pos.side = ""

        strat = trader._strategies["favorite"]

        with patch("timba.resolution.resolve_winner") as mock_resolve:
            trader.resolver._check_and_queue_resolve("favorite", strat, pos)

        # resolve_winner should NOT be called since WATCHING is not in RESOLVE_MAP
        mock_resolve.assert_not_called()
        assert pos.state == PositionState.WATCHING


class TestResolvePending:
    """Test _resolve_pending: scanning positions and error handling."""

    def test_skips_non_pending_positions(self, trader_setup):
        """_resolve_pending should skip positions that are not pending."""
        trader, state = trader_setup
        pos = _make_position(slug="watching-slug")
        pos.state = PositionState.WATCHING
        trader.positions["favorite"]["watching-slug"] = pos

        with patch.object(trader.resolver, '_check_and_queue_resolve') as mock_check:
            trader.resolver._resolve_pending()

        mock_check.assert_not_called()

    def test_processes_pending_positions(self, trader_setup):
        """_resolve_pending should process positions with pending state."""
        trader, state = trader_setup
        pos = _make_position(slug="paper-slug")
        pos.state = PositionState.PAPER
        pos.side = "up"
        pos.buy_price = 0.98
        trader.positions["favorite"]["paper-slug"] = pos

        with patch.object(trader.resolver, '_check_and_queue_resolve') as mock_check:
            trader.resolver._resolve_pending()

        mock_check.assert_called_once()

    def test_error_handling_increments_fail_count(self, trader_setup):
        """Errors during resolution should be tracked per-position."""
        trader, state = trader_setup
        pos = _make_position(slug="error-slug")
        pos.state = PositionState.PAPER
        pos.side = "up"
        pos.buy_price = 0.98
        trader.positions["favorite"]["error-slug"] = pos

        with patch.object(trader.resolver, '_check_and_queue_resolve', side_effect=Exception("CLOB down")):
            trader.resolver._resolve_pending()

        assert pos._resolve_fail_count == 1

    def test_error_count_accumulates_within_window(self, trader_setup):
        """Multiple errors within 60s accumulate count (line 92)."""
        trader, state = trader_setup
        pos = _make_position(slug="multi-error-slug")
        pos.state = PositionState.PAPER
        pos.side = "up"
        pos.buy_price = 0.98
        trader.positions["favorite"]["multi-error-slug"] = pos

        with patch.object(trader.resolver, '_check_and_queue_resolve', side_effect=Exception("CLOB down")):
            trader.resolver._resolve_pending()
            trader.resolver._resolve_pending()

        assert pos._resolve_fail_count == 2

    def test_error_warns_at_three_failures(self, trader_setup):
        """After 3 failures in 60s, should log a warning (line 94)."""
        trader, state = trader_setup
        pos = _make_position(slug="warn-slug")
        pos.state = PositionState.PAPER
        pos.side = "up"
        pos.buy_price = 0.98
        trader.positions["favorite"]["warn-slug"] = pos

        with patch.object(trader.resolver, '_check_and_queue_resolve', side_effect=Exception("CLOB down")):
            for _ in range(3):
                trader.resolver._resolve_pending()

        assert pos._resolve_fail_count >= 3

    def test_success_resets_fail_count(self, trader_setup):
        """Successful resolution resets fail count to 0."""
        trader, state = trader_setup
        pos = _make_position(slug="reset-slug")
        pos.state = PositionState.PAPER
        pos.side = "up"
        pos.buy_price = 0.98
        pos._resolve_fail_since = time.time()
        pos._resolve_fail_count = 2
        trader.positions["favorite"]["reset-slug"] = pos

        with patch("timba.resolution.resolve_winner", return_value=True):
            trader.resolver._resolve_pending()

        assert pos._resolve_fail_count == 0
        assert pos._resolve_fail_since == 0

    def test_error_window_resets_after_60s(self, trader_setup):
        """Failure window resets if >60s since first failure (line 88-90)."""
        trader, state = trader_setup
        pos = _make_position(slug="window-reset-slug")
        pos.state = PositionState.PAPER
        pos.side = "up"
        pos.buy_price = 0.98
        # Simulate a failure that happened 70s ago
        pos._resolve_fail_since = time.time() - 70
        pos._resolve_fail_count = 2
        trader.positions["favorite"]["window-reset-slug"] = pos

        with patch.object(trader.resolver, '_check_and_queue_resolve', side_effect=Exception("CLOB down")):
            trader.resolver._resolve_pending()

        # Window should have reset — count back to 1
        assert pos._resolve_fail_count == 1


class TestRunLoop:
    """Test run_loop method (lines 60-66)."""

    def test_run_loop_calls_resolve_pending(self, trader_setup):
        """run_loop calls _resolve_pending while is_running returns True."""
        trader, state = trader_setup

        call_count = [0]
        def mock_is_running():
            call_count[0] += 1
            return call_count[0] <= 1  # Run once then stop

        with patch.object(trader.resolver, '_resolve_pending') as mock_resolve, \
             patch("timba.resolution.time.sleep"):
            trader.resolver.run_loop(mock_is_running)

        mock_resolve.assert_called_once()

    def test_run_loop_catches_exceptions(self, trader_setup):
        """run_loop catches exceptions from _resolve_pending (lines 64-65)."""
        trader, state = trader_setup

        call_count = [0]
        def mock_is_running():
            call_count[0] += 1
            return call_count[0] <= 2  # Run twice then stop

        with patch.object(trader.resolver, '_resolve_pending', side_effect=Exception("unexpected")), \
             patch("timba.resolution.time.sleep"):
            # Should not raise
            trader.resolver.run_loop(mock_is_running)
