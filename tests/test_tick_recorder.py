"""Tests for the tick recorder background thread."""

import time
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

from timba.tick_recorder import TickRecorder


@dataclass
class FakeSignal:
    direction: str = "up"
    change_pct: float = 0.15
    seconds_trending: float = 30.0
    reversed_recently: bool = False
    confidence: float = 0.7
    price_open: float = 66000.0
    price_now: float = 66100.0


@dataclass
class FakeSnapshot:
    mid_up: float = 0.95
    mid_down: float = 0.05
    fill_up: float = 0.96
    fill_down: float = 0.06
    tick_size: float = 0.01
    updated_at: float = 1.0


def _make_position(slug="btc-updown-5m-1234", coin="btc", interval="5m", terminal=False):
    pos = MagicMock()
    pos.slug = slug
    pos.coin = coin
    pos.interval = interval
    pos.window_start_ts = int(time.time()) - 60
    pos.state = MagicMock()
    pos.state.is_terminal = terminal
    return pos


class TestTickRecorder:
    def test_init(self):
        recorder = TickRecorder(
            positions={"fav": {}},
            strategies={"fav": "fav"},
            recorded_ticks={},
            feed=MagicMock(),
            market_cache=MagicMock(),
        )
        assert recorder._positions == {"fav": {}}

    def test_record_tick_stores_snapshot(self):
        """_record_tick writes to recorded_ticks dict when signal and snapshot available."""
        pos = _make_position()
        recorded_ticks = {}

        feed = MagicMock()
        feed.get_direction.return_value = FakeSignal()

        cache = MagicMock()
        cache.get.return_value = FakeSnapshot()

        recorder = TickRecorder(
            positions={"fav": {pos.slug: pos}},
            strategies={"fav": "fav"},
            recorded_ticks=recorded_ticks,
            feed=feed,
            market_cache=cache,
        )

        with patch("timba.tick_recorder.record_tick", return_value=42):
            recorder._record_tick(pos)

        assert pos.slug in recorded_ticks
        tick_id, snapshot, signal = recorded_ticks[pos.slug]
        assert tick_id == 42
        assert snapshot.mid_up == 0.95
        assert signal.direction == "up"

    def test_record_tick_skips_no_signal(self):
        """_record_tick does nothing when feed returns None signal."""
        pos = _make_position()
        recorded_ticks = {}

        feed = MagicMock()
        feed.get_direction.return_value = None

        cache = MagicMock()

        recorder = TickRecorder(
            positions={"fav": {pos.slug: pos}},
            strategies={"fav": "fav"},
            recorded_ticks=recorded_ticks,
            feed=feed,
            market_cache=cache,
        )

        with patch("timba.tick_recorder.record_tick") as mock_record:
            recorder._record_tick(pos)
            mock_record.assert_not_called()

        assert pos.slug not in recorded_ticks

    def test_record_tick_skips_no_snapshot(self):
        """_record_tick does nothing when cache returns None snapshot."""
        pos = _make_position()
        recorded_ticks = {}

        feed = MagicMock()
        feed.get_direction.return_value = FakeSignal()

        cache = MagicMock()
        cache.get.return_value = None

        recorder = TickRecorder(
            positions={"fav": {pos.slug: pos}},
            strategies={"fav": "fav"},
            recorded_ticks=recorded_ticks,
            feed=feed,
            market_cache=cache,
        )

        with patch("timba.tick_recorder.record_tick") as mock_record:
            recorder._record_tick(pos)
            mock_record.assert_not_called()

    def test_record_tick_skips_stale_snapshot(self):
        """_record_tick does nothing when snapshot.updated_at is 0."""
        pos = _make_position()
        recorded_ticks = {}

        feed = MagicMock()
        feed.get_direction.return_value = FakeSignal()

        cache = MagicMock()
        stale_snapshot = FakeSnapshot(updated_at=0)
        cache.get.return_value = stale_snapshot

        recorder = TickRecorder(
            positions={"fav": {pos.slug: pos}},
            strategies={"fav": "fav"},
            recorded_ticks=recorded_ticks,
            feed=feed,
            market_cache=cache,
        )

        with patch("timba.tick_recorder.record_tick") as mock_record:
            recorder._record_tick(pos)
            mock_record.assert_not_called()

    def test_run_loop_skips_terminal_positions(self):
        """run_loop only records ticks for non-terminal positions."""
        active_pos = _make_position(slug="active-slug", terminal=False)
        terminal_pos = _make_position(slug="done-slug", terminal=True)
        recorded_ticks = {}

        feed = MagicMock()
        feed.is_healthy.return_value = True
        feed.get_direction.return_value = FakeSignal()

        cache = MagicMock()
        cache.get.return_value = FakeSnapshot()

        recorder = TickRecorder(
            positions={"fav": {"active-slug": active_pos, "done-slug": terminal_pos}},
            strategies={"fav": "fav"},
            recorded_ticks=recorded_ticks,
            feed=feed,
            market_cache=cache,
        )

        # Run one iteration then stop
        call_count = 0
        def is_running():
            nonlocal call_count
            call_count += 1
            return call_count <= 2  # Let the outer loop run once, then stop

        with patch("timba.tick_recorder.record_tick", return_value=1):
            with patch("timba.tick_recorder.time.sleep"):
                recorder.run_loop(is_running)

        # Only the active position should have been recorded
        assert "active-slug" in recorded_ticks
        assert "done-slug" not in recorded_ticks

    def test_run_loop_skips_unhealthy_feed(self):
        """run_loop waits when feed is unhealthy."""
        pos = _make_position()
        recorded_ticks = {}

        feed = MagicMock()
        feed.is_healthy.return_value = False

        cache = MagicMock()

        recorder = TickRecorder(
            positions={"fav": {pos.slug: pos}},
            strategies={"fav": "fav"},
            recorded_ticks=recorded_ticks,
            feed=feed,
            market_cache=cache,
        )

        call_count = 0
        def is_running():
            nonlocal call_count
            call_count += 1
            return call_count <= 2

        with patch("timba.tick_recorder.record_tick") as mock_record:
            with patch("timba.tick_recorder.time.sleep"):
                recorder.run_loop(is_running)
            mock_record.assert_not_called()

    def test_run_loop_skips_no_feed(self):
        """run_loop waits when feed is None."""
        pos = _make_position()
        recorded_ticks = {}

        recorder = TickRecorder(
            positions={"fav": {pos.slug: pos}},
            strategies={"fav": "fav"},
            recorded_ticks=recorded_ticks,
            feed=None,
            market_cache=MagicMock(),
        )

        call_count = 0
        def is_running():
            nonlocal call_count
            call_count += 1
            return call_count <= 2

        with patch("timba.tick_recorder.record_tick") as mock_record:
            with patch("timba.tick_recorder.time.sleep"):
                recorder.run_loop(is_running)
            mock_record.assert_not_called()

    def test_run_loop_stops_mid_slug_iteration(self):
        """run_loop breaks out of inner slug loop when is_running() returns False (line 58)."""
        # Create many positions so the inner loop has work to do
        positions = {}
        for i in range(10):
            slug = f"slug-{i}"
            positions[slug] = _make_position(slug=slug, terminal=False)

        recorded_ticks = {}

        feed = MagicMock()
        feed.is_healthy.return_value = True
        feed.get_direction.return_value = FakeSignal()

        cache = MagicMock()
        cache.get.return_value = FakeSnapshot()

        recorder = TickRecorder(
            positions={"fav": positions},
            strategies={"fav": "fav"},
            recorded_ticks=recorded_ticks,
            feed=feed,
            market_cache=cache,
        )

        # is_running returns True initially, then False after recording a few ticks
        record_count = 0
        def counting_record_tick(**kwargs):
            nonlocal record_count
            record_count += 1
            return record_count

        call_count = 0
        def is_running():
            nonlocal call_count
            call_count += 1
            # Let the outer loop start (call 1 = True), then stop mid-iteration
            # is_running is called once at outer while, plus once per slug in inner loop
            return call_count <= 3  # stops after processing a few slugs

        with patch("timba.tick_recorder.record_tick", side_effect=counting_record_tick):
            with patch("timba.tick_recorder.time.sleep"):
                recorder.run_loop(is_running)

        # Should have recorded fewer than all 10 positions due to early break
        assert record_count < 10

    def test_run_loop_handles_exception(self):
        """run_loop catches exceptions and continues."""
        recorded_ticks = {}

        feed = MagicMock()
        feed.is_healthy.return_value = True
        feed.get_direction.side_effect = RuntimeError("boom")

        # Positions dict that raises on iteration
        bad_positions = MagicMock()
        bad_positions.__getitem__ = MagicMock(side_effect=RuntimeError("boom"))

        recorder = TickRecorder(
            positions=bad_positions,
            strategies={"fav": "fav"},
            recorded_ticks=recorded_ticks,
            feed=feed,
            market_cache=MagicMock(),
        )

        call_count = 0
        def is_running():
            nonlocal call_count
            call_count += 1
            return call_count <= 2

        # Should not raise
        with patch("timba.tick_recorder.time.sleep"):
            recorder.run_loop(is_running)
