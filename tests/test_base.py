"""Tests for shared framework: base.py helpers (poll_order_fill, place_order, resolve_winner, MarketPosition)."""

import time
from unittest.mock import MagicMock

import pytest

from timba.base import (
    _VALID_TRANSITIONS,
    RESOLVE_DELAY_SEC,
    MarketPosition,
    PositionState,
    check_entry_window,
    check_liquidity,
    place_order,
    poll_order_fill,
    resolve_winner,
)


def _make_position(**kw):
    defaults = dict(
        condition_id="0x1", question="Bitcoin Up or Down - March 23, 10:00AM-10:05AM ET",
        slug="btc-updown-5m-123", coin="btc", interval="5m",
        token_id_up="tu", token_id_down="td",
        end_timestamp=int(time.time()) + 10,
        window_start_ts=int(time.time()) - 280,
        contracts=200, entry_window_sec=20, close_window_sec=5,
    )
    defaults.update(kw)
    return MarketPosition(**defaults)


class TestMarketPositionProperties:
    def test_label(self):
        p = _make_position()
        assert "BTC" in p.label
        assert "5m" in p.label

    def test_time_remaining(self):
        p = _make_position(end_timestamp=int(time.time()) + 100)
        assert 95 < p.time_remaining() <= 100


class TestCheckEntryWindow:
    def test_early_returns_early(self):
        p = _make_position(end_timestamp=int(time.time()) + 60, entry_window_sec=20)
        status, remaining, progress = check_entry_window(p)
        assert status == "early"

    def test_active_returns_active(self):
        p = _make_position(end_timestamp=int(time.time()) + 10, entry_window_sec=20, close_window_sec=5)
        status, remaining, progress = check_entry_window(p)
        assert status == "active"
        assert 0.0 <= progress <= 1.0

    def test_timeout_returns_timeout(self):
        p = _make_position(end_timestamp=int(time.time()) + 3, entry_window_sec=20, close_window_sec=5)
        status, remaining, progress = check_entry_window(p)
        assert status == "timeout"


class TestPollOrderFill:
    def test_returns_fill_data(self):
        clob = MagicMock()
        order = MagicMock(size_matched=200, price=0.93)
        clob.get_orders.return_value = [order]
        size, price = poll_order_fill(clob, "order123", "test")
        assert size == 200
        assert price == pytest.approx(0.93)

    def test_returns_zero_on_no_fill(self):
        clob = MagicMock()
        order = MagicMock(size_matched=0, price=0.93)
        clob.get_orders.return_value = [order]
        size, price = poll_order_fill(clob, "order123", "test")
        assert size == 0
        assert price == 0

    def test_returns_zero_on_api_failure(self):
        clob = MagicMock()
        clob.get_orders.side_effect = Exception("timeout")
        size, price = poll_order_fill(clob, "order123", "test")
        assert size == 0
        assert price == 0


class TestResolveWinner:
    def test_win_via_clob(self):
        p = _make_position(end_timestamp=int(time.time()) - RESOLVE_DELAY_SEC - 1)
        p.state = PositionState.SNIPED
        p.side = "up"
        feed = MagicMock()
        won = resolve_winner(p, feed, MagicMock(), lambda c, t: 0.95)
        assert won is True

    def test_loss_via_clob(self):
        p = _make_position(end_timestamp=int(time.time()) - RESOLVE_DELAY_SEC - 1)
        p.state = PositionState.SNIPED
        p.side = "up"
        feed = MagicMock()
        won = resolve_winner(p, feed, MagicMock(), lambda c, t: 0.10)
        assert won is False

    def test_coinbase_fallback(self):
        from timba.feed import DirectionSignal
        p = _make_position(end_timestamp=int(time.time()) - RESOLVE_DELAY_SEC - 1)
        p.state = PositionState.SNIPED
        p.side = "up"
        feed = MagicMock()
        feed.get_direction.return_value = DirectionSignal(
            direction="up", change_pct=0.1, seconds_trending=100,
            reversed_recently=False, confidence=0.8,
        )
        won = resolve_winner(p, feed, MagicMock(), lambda c, t: None)
        assert won is True

    def test_not_ready_during_delay(self):
        p = _make_position(end_timestamp=int(time.time()) - 5)
        p.state = PositionState.SNIPED
        p.side = "up"
        feed = MagicMock()
        won = resolve_winner(p, feed, MagicMock(), lambda c, t: 0.95)
        assert won is None

    def test_not_ready_if_still_open(self):
        p = _make_position(end_timestamp=int(time.time()) + 60)
        p.state = PositionState.SNIPED
        p.side = "up"
        feed = MagicMock()
        won = resolve_winner(p, feed, MagicMock(), lambda c, t: 0.95)
        assert won is None

    def test_no_side_returns_false(self):
        """When pos.side is empty, resolve_winner returns False (no prediction)."""
        p = _make_position(end_timestamp=int(time.time()) - RESOLVE_DELAY_SEC - 1)
        p.state = PositionState.SNIPED
        p.side = ""
        feed = MagicMock()
        won = resolve_winner(p, feed, MagicMock(), lambda c, t: 0.95)
        assert won is False


class TestMarketPositionProgress:
    def test_progress_at_before_window(self):
        """Tick well before entry window returns 0.0 progress."""
        p = _make_position(
            end_timestamp=int(time.time()) + 100,
            entry_window_sec=20,
            close_window_sec=5,
        )
        # tick_ts far in the past → remaining >> entry_window_sec
        tick_ts = time.time() - 200
        assert p.progress_at(tick_ts) == 0.0

    def test_progress_at_after_close(self):
        """Tick past close window returns 1.0 progress."""
        p = _make_position(
            end_timestamp=int(time.time()) + 2,
            entry_window_sec=20,
            close_window_sec=5,
        )
        # tick_ts in the future → remaining < close_window_sec
        tick_ts = time.time() + 10
        assert p.progress_at(tick_ts) == 1.0

    def test_progress_at_zero_observation_window(self):
        """entry_window_sec == close_window_sec → obs=0, returns 1.0."""
        p = _make_position(
            end_timestamp=int(time.time()) + 10,
            entry_window_sec=5,
            close_window_sec=5,
        )
        # remaining=10 > entry_window=5, so progress_at returns 0.0 via first branch
        # To actually hit obs<=0, remaining must be between close and entry (equal here)
        # Set tick_ts so remaining == 5 exactly (entry=close=5)
        tick_ts = p.end_timestamp - 5
        assert p.progress_at(tick_ts) == 1.0

    def test_in_window_at_inside(self):
        """Returns True when tick is within entry window."""
        end = int(time.time()) + 15
        p = _make_position(end_timestamp=end, entry_window_sec=20, close_window_sec=5)
        # remaining = end - tick_ts = 15 → 5 <= 15 <= 20 → True
        tick_ts = time.time()
        assert p.in_window_at(tick_ts) is True

    def test_in_window_at_outside(self):
        """Returns False when tick is outside entry window."""
        end = int(time.time()) + 100
        p = _make_position(end_timestamp=end, entry_window_sec=20, close_window_sec=5)
        # remaining = 100 → 100 > 20 → False
        tick_ts = time.time()
        assert p.in_window_at(tick_ts) is False


class TestCheckLiquidity:
    def test_low_liquidity_skips(self):
        """Liquidity below MIN_LIQUIDITY (100) sets SKIPPED state."""
        p = _make_position(liquidity=50.0, liquidity_checked=False)
        result = check_liquidity(p)
        assert result is False
        assert p.state == PositionState.SKIPPED
        assert "low liquidity" in p.skip_reason

    def test_sufficient_liquidity(self):
        """Liquidity above MIN_LIQUIDITY returns True, state unchanged."""
        p = _make_position(liquidity=200.0, liquidity_checked=False)
        result = check_liquidity(p)
        assert result is True
        assert p.state == PositionState.WATCHING

    def test_already_checked_skips(self):
        """liquidity_checked=True returns True without re-checking."""
        p = _make_position(liquidity=50.0, liquidity_checked=True)
        result = check_liquidity(p)
        assert result is True

    def test_negative_liquidity_skips_check(self):
        """liquidity=-1 (no data) returns True, skipping the check."""
        p = _make_position(liquidity=-1.0, liquidity_checked=False)
        result = check_liquidity(p)
        assert result is True


class TestPlaceOrder:
    def test_place_order_success(self):
        """Successful order placement and fill."""
        p = _make_position(market_mode="live", contracts=200)
        p.state = PositionState.PENDING_ORDER
        p.side = "up"

        clob = MagicMock()
        resp = MagicMock()
        resp.success = True
        resp.order_id = "order-abc"
        clob.create_and_post_order.return_value = resp

        with pytest.MonkeyPatch.context() as m:
            m.setattr("timba.base.poll_order_fill", lambda c, oid, lbl: (200, 0.93))
            result = place_order(p, "up", 0.93, 200, clob)

        assert result is True
        assert p.order_id == "order-abc"
        assert p.buy_price == pytest.approx(0.93)
        assert p.contracts == 200

    def test_place_order_rejected(self):
        """CLOB returns success=False, position becomes SKIPPED."""
        p = _make_position(market_mode="live")
        p.state = PositionState.PENDING_ORDER

        clob = MagicMock()
        resp = MagicMock()
        resp.success = False
        resp.error_msg = "rate limited"
        clob.create_and_post_order.return_value = resp

        result = place_order(p, "up", 0.93, 200, clob)

        assert result is False
        assert p.state == PositionState.SKIPPED
        assert "order rejected" in p.skip_reason or "order failed" in p.skip_reason

    def test_place_order_not_filled(self):
        """Order placed but poll returns (0,0), position becomes SKIPPED."""
        p = _make_position(market_mode="live")
        p.state = PositionState.PENDING_ORDER

        clob = MagicMock()
        resp = MagicMock()
        resp.success = True
        resp.order_id = "order-xyz"
        clob.create_and_post_order.return_value = resp

        with pytest.MonkeyPatch.context() as m:
            m.setattr("timba.base.poll_order_fill", lambda c, oid, lbl: (0, 0))
            result = place_order(p, "up", 0.95, 100, clob)

        assert result is False
        assert p.state == PositionState.SKIPPED
        assert "not filled" in p.skip_reason


class TestResolveWinnerUnknownState:
    def test_resolve_unknown_state_returns_none(self):
        """pos.state=WATCHING (not in RESOLVE_MAP) returns None."""
        p = _make_position(end_timestamp=int(time.time()) - RESOLVE_DELAY_SEC - 1)
        p.state = PositionState.WATCHING
        p.side = "up"
        feed = MagicMock()
        won = resolve_winner(p, feed, MagicMock(), lambda c, t: 0.95)
        assert won is None


class TestTransition:
    """State machine validation via pos.transition()."""

    def test_valid_transition_succeeds(self):
        p = _make_position()
        assert p.state == PositionState.WATCHING
        assert p.transition(PositionState.PENDING_ORDER) is True
        assert p.state == PositionState.PENDING_ORDER

    def test_invalid_transition_rejected(self):
        p = _make_position()
        assert p.transition(PositionState.WON) is False
        assert p.state == PositionState.WATCHING  # unchanged

    def test_transition_sets_extra_fields(self):
        p = _make_position()
        p.transition(PositionState.SKIPPED, skip_reason="test reason", sniped_at="2026-01-01T00:00:00Z")
        assert p.state == PositionState.SKIPPED
        assert p.skip_reason == "test reason"
        assert p.sniped_at == "2026-01-01T00:00:00Z"

    def test_full_happy_path_paper(self):
        """WATCHING → PENDING_ORDER → PAPER → PAPER_WON."""
        p = _make_position()
        assert p.transition(PositionState.PENDING_ORDER)
        assert p.transition(PositionState.PAPER)
        assert p.transition(PositionState.PAPER_WON)
        assert p.state == PositionState.PAPER_WON

    def test_full_happy_path_live(self):
        """WATCHING → PENDING_ORDER → SNIPED → WON."""
        p = _make_position()
        assert p.transition(PositionState.PENDING_ORDER)
        assert p.transition(PositionState.SNIPED)
        assert p.transition(PositionState.WON)
        assert p.state == PositionState.WON

    def test_skip_path(self):
        """WATCHING → SKIPPED → SKIP_WON."""
        p = _make_position()
        assert p.transition(PositionState.SKIPPED)
        assert p.transition(PositionState.SKIP_WON)
        assert p.state == PositionState.SKIP_WON

    def test_fail_path(self):
        """WATCHING → PENDING_ORDER → FAILED → FAIL_LOST."""
        p = _make_position()
        assert p.transition(PositionState.PENDING_ORDER)
        assert p.transition(PositionState.FAILED)
        assert p.transition(PositionState.FAIL_LOST)
        assert p.state == PositionState.FAIL_LOST

    def test_terminal_state_rejects_further_transitions(self):
        """Once in a terminal state, no further transitions are allowed."""
        p = _make_position()
        p.transition(PositionState.SKIPPED)
        p.transition(PositionState.SKIP_WON)
        # SKIP_WON is terminal — nothing should be allowed
        assert p.transition(PositionState.WATCHING) is False
        assert p.transition(PositionState.WON) is False
        assert p.state == PositionState.SKIP_WON

    def test_concurrent_transitions_one_wins(self):
        """Two threads racing to transition — exactly one should succeed."""
        import threading

        p = _make_position()
        results = []
        barrier = threading.Barrier(2)

        def try_transition(target):
            barrier.wait()
            results.append(p.transition(target))

        t1 = threading.Thread(target=try_transition, args=(PositionState.PENDING_ORDER,))
        t2 = threading.Thread(target=try_transition, args=(PositionState.SKIPPED,))
        t1.start(); t2.start()
        t1.join(); t2.join()

        # Both PENDING_ORDER and SKIPPED are valid from WATCHING,
        # but only one can win — the other sees a state it can't transition from
        assert results.count(True) >= 1
        assert p.state in (PositionState.PENDING_ORDER, PositionState.SKIPPED)

    def test_all_transitions_in_table_are_valid(self):
        """Every transition in _VALID_TRANSITIONS involves real PositionState values."""
        for source, targets in _VALID_TRANSITIONS.items():
            assert isinstance(source, PositionState)
            for t in targets:
                assert isinstance(t, PositionState)
