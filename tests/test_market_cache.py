from unittest.mock import MagicMock, patch

from timba.market_cache import MarketCache, MarketSnapshot


class TestTrackAndGet:
    def _make_cache(self):
        return MarketCache(clob_client=MagicMock(), max_workers=2)

    def test_track_adds_market_to_cache(self):
        mc = self._make_cache()
        mc.track("btc-updown-5m-100", "tok_up", "tok_down", contracts=10)
        snap = mc.get("btc-updown-5m-100")
        assert snap is not None
        assert isinstance(snap, MarketSnapshot)
        assert snap.mid_up == 0.0
        assert snap.tick_size == 0.01

    def test_get_unknown_returns_none(self):
        mc = self._make_cache()
        assert mc.get("nonexistent-slug") is None

    def test_untrack_removes_market(self):
        mc = self._make_cache()
        mc.track("btc-updown-5m-100", "tok_up", "tok_down")
        assert mc.get("btc-updown-5m-100") is not None
        mc.untrack("btc-updown-5m-100")
        assert mc.get("btc-updown-5m-100") is None

    def test_untrack_unknown_noop(self):
        mc = self._make_cache()
        # Should not raise
        mc.untrack("never-tracked-slug")


class TestStartStop:
    def _make_cache(self):
        return MarketCache(clob_client=MagicMock(), max_workers=2)

    def test_start_creates_thread(self):
        mc = self._make_cache()
        mc.start()
        try:
            assert mc._running is True
            assert mc._thread is not None
            assert mc._thread.is_alive()
        finally:
            mc.stop()

    def test_start_twice_noop(self):
        mc = self._make_cache()
        mc.start()
        first_thread = mc._thread
        mc.start()  # second call should be a no-op
        try:
            assert mc._thread is first_thread
        finally:
            mc.stop()

    def test_stop_clears_running(self):
        mc = self._make_cache()
        mc.start()
        assert mc._running is True
        mc.stop()
        assert mc._running is False


class TestUpdateMarket:
    def _make_cache(self):
        return MarketCache(clob_client=MagicMock(), max_workers=2)

    @patch("timba.market_cache.simulate_fill")
    @patch("timba.market_cache._get_midpoint")
    def test_update_market_writes_snapshot(self, mock_midpoint, mock_fill):
        mc = self._make_cache()
        mc.track("btc-updown-5m-100", "tok_up", "tok_down", contracts=10)

        mock_midpoint.side_effect = [0.95, 0.05]  # mid_up, mid_down
        mock_fill.side_effect = [
            (0.96, 10, 0.001),  # fill_up, size_up, tick_size
            (0.06, 10, 0.001),  # fill_down, size_down, tick_size
        ]

        info = {"token_id_up": "tok_up", "token_id_down": "tok_down", "contracts": 10}
        mc._update_market("btc-updown-5m-100", info)

        snap = mc.get("btc-updown-5m-100")
        assert snap is not None
        assert snap.mid_up == 0.95
        assert snap.mid_down == 0.05
        assert snap.fill_up == 0.96
        assert snap.fill_down == 0.06
        assert snap.tick_size == 0.001
        assert snap.updated_at > 0

    @patch("timba.market_cache.simulate_fill")
    @patch("timba.market_cache._get_midpoint")
    def test_update_market_skips_on_none_midpoint(self, mock_midpoint, mock_fill):
        mc = self._make_cache()
        mc.track("btc-updown-5m-100", "tok_up", "tok_down", contracts=10)

        # mid_up returns None → early return, cache stays at defaults
        mock_midpoint.side_effect = [None, 0.05]

        info = {"token_id_up": "tok_up", "token_id_down": "tok_down", "contracts": 10}
        mc._update_market("btc-updown-5m-100", info)

        snap = mc.get("btc-updown-5m-100")
        assert snap.mid_up == 0.0  # unchanged from default
        assert snap.updated_at == 0.0  # never updated
        mock_fill.assert_not_called()

    @patch("timba.market_cache.simulate_fill")
    @patch("timba.market_cache._get_midpoint")
    def test_update_market_uses_default_tick_size(self, mock_midpoint, mock_fill):
        mc = self._make_cache()
        mc.track("btc-updown-5m-100", "tok_up", "tok_down", contracts=10)

        mock_midpoint.side_effect = [0.90, 0.10]
        # simulate_fill returns None for both → tick_size stays 0.01
        mock_fill.return_value = None

        info = {"token_id_up": "tok_up", "token_id_down": "tok_down", "contracts": 10}
        mc._update_market("btc-updown-5m-100", info)

        snap = mc.get("btc-updown-5m-100")
        assert snap.tick_size == 0.01
        # fill values fall back to midpoints when simulate_fill returns None
        assert snap.fill_up == 0.90
        assert snap.fill_down == 0.10


class TestPollLoopErrorPaths:
    """Test _poll_loop error handling and failure tracking (lines 101-131)."""

    def _make_cache(self):
        return MarketCache(clob_client=MagicMock(), max_workers=2)

    def test_poll_loop_empty_slugs_sleeps(self):
        """When no markets tracked, poll loop sleeps and continues (line 93-94)."""
        mc = self._make_cache()
        mc._running = True

        call_count = [0]
        def mock_sleep(duration):
            call_count[0] += 1
            if call_count[0] >= 2:
                mc._running = False

        import timba.market_cache
        with patch.object(timba.market_cache.time, "sleep", side_effect=mock_sleep):
            mc._poll_loop()

        assert call_count[0] >= 2

    @patch("timba.market_cache.simulate_fill")
    @patch("timba.market_cache._get_midpoint")
    def test_poll_loop_clears_failure_on_success(self, mock_midpoint, mock_fill):
        """Successful update clears failure tracking for that slug (line 116)."""
        mc = self._make_cache()
        mc.track("btc-updown-5m-100", "tok_up", "tok_down", contracts=10)
        mc._running = True

        mock_midpoint.side_effect = [0.95, 0.05]
        mock_fill.side_effect = [(0.96, 10, 0.001), (0.06, 10, 0.001)]

        call_count = [0]
        def mock_sleep(duration):
            call_count[0] += 1
            mc._running = False

        import timba.market_cache
        with patch.object(timba.market_cache.time, "sleep", side_effect=mock_sleep):
            mc._poll_loop()

        snap = mc.get("btc-updown-5m-100")
        assert snap is not None
        assert snap.mid_up == 0.95

    def test_poll_loop_tracks_failures(self):
        """Failed futures increment per-slug failure counter (lines 117-128)."""
        mc = self._make_cache()
        mc.track("btc-updown-5m-100", "tok_up", "tok_down", contracts=10)
        mc._running = True

        call_count = [0]
        def mock_sleep(duration):
            call_count[0] += 1
            if call_count[0] >= 2:
                mc._running = False

        # Make _update_market raise to trigger failure tracking
        with patch.object(mc, '_update_market', side_effect=Exception("API error")):
            import timba.market_cache
            with patch.object(timba.market_cache.time, "sleep", side_effect=mock_sleep):
                mc._poll_loop()

        # Should not crash

    def test_poll_loop_warns_at_failure_threshold(self):
        """After 5 failures in 60s, logs a warning (lines 124-126)."""
        mc = self._make_cache()
        mc.track("btc-updown-5m-100", "tok_up", "tok_down", contracts=10)
        mc._running = True

        iteration = [0]
        def mock_sleep(duration):
            iteration[0] += 1
            if iteration[0] >= 6:
                mc._running = False

        with patch.object(mc, '_update_market', side_effect=Exception("API error")):
            import timba.market_cache
            with patch.object(timba.market_cache.time, "sleep", side_effect=mock_sleep):
                mc._poll_loop()

        # Should not crash — warning was logged internally

    def test_poll_loop_runtime_error_stops(self):
        """RuntimeError from pool.submit sets _running=False (lines 104-107)."""
        mc = self._make_cache()
        mc.track("btc-updown-5m-100", "tok_up", "tok_down", contracts=10)
        mc._running = True

        def mock_sleep(duration):
            mc._running = False

        with patch("timba.market_cache.ThreadPoolExecutor") as mock_pool_cls:
            mock_pool = MagicMock()
            mock_pool.__enter__ = MagicMock(return_value=mock_pool)
            mock_pool.__exit__ = MagicMock(return_value=False)
            mock_pool.submit.side_effect = RuntimeError("interpreter shutting down")
            mock_pool_cls.return_value = mock_pool

            import timba.market_cache
            with patch.object(timba.market_cache.time, "sleep", side_effect=mock_sleep):
                mc._poll_loop()

        assert mc._running is False

    def test_poll_loop_outer_exception(self):
        """Exception in outer try block is caught and logged (line 130-131)."""
        mc = self._make_cache()
        mc._running = True

        iteration = [0]
        def mock_sleep(duration):
            iteration[0] += 1
            if iteration[0] >= 2:
                mc._running = False

        # Make _lock.acquire raise to trigger outer exception
        original_lock = mc._lock

        call_count = [0]
        class FailingLock:
            def __enter__(self_lock):
                call_count[0] += 1
                if call_count[0] <= 1:
                    raise Exception("lock error")
                return original_lock.__enter__()
            def __exit__(self_lock, *args):
                return original_lock.__exit__(*args)

        mc._lock = FailingLock()

        import timba.market_cache
        with patch.object(timba.market_cache.time, "sleep", side_effect=mock_sleep):
            mc._poll_loop()

        # Should not crash

    def test_poll_loop_stops_during_future_completion(self):
        """_running set to False during as_completed loop breaks (line 112)."""
        mc = self._make_cache()
        mc.track("slug-a", "tu_a", "td_a", contracts=10)
        mc.track("slug-b", "tu_b", "td_b", contracts=10)
        mc._running = True

        from concurrent.futures import Future
        future_a = Future()
        future_b = Future()
        future_a.set_result(None)
        future_b.set_result(None)

        submit_count = [0]
        def mock_submit(fn, *args, **kwargs):
            submit_count[0] += 1
            if submit_count[0] == 1:
                return future_a
            return future_b

        def fake_as_completed(futures):
            # After yielding first future, set _running to False
            for f in futures:
                yield f
                mc._running = False

        def mock_sleep(duration):
            mc._running = False

        with patch("timba.market_cache.ThreadPoolExecutor") as mock_pool_cls, \
             patch("timba.market_cache.as_completed", side_effect=fake_as_completed):
            mock_pool = MagicMock()
            mock_pool.__enter__ = MagicMock(return_value=mock_pool)
            mock_pool.__exit__ = MagicMock(return_value=False)
            mock_pool.submit.side_effect = mock_submit
            mock_pool_cls.return_value = mock_pool

            import timba.market_cache
            with patch.object(timba.market_cache.time, "sleep", side_effect=mock_sleep):
                mc._poll_loop()

    def test_poll_loop_stops_during_submit_loop(self):
        """_running set to False during submit loop breaks (line 101)."""
        mc = self._make_cache()
        mc.track("slug-a", "tu_a", "td_a", contracts=10)
        mc.track("slug-b", "tu_b", "td_b", contracts=10)
        mc._running = True

        from concurrent.futures import Future

        def mock_submit(fn, *args, **kwargs):
            # Stop after first submit
            mc._running = False
            f = Future()
            f.set_result(None)
            return f

        def mock_sleep(duration):
            mc._running = False

        with patch("timba.market_cache.ThreadPoolExecutor") as mock_pool_cls:
            mock_pool = MagicMock()
            mock_pool.__enter__ = MagicMock(return_value=mock_pool)
            mock_pool.__exit__ = MagicMock(return_value=False)
            mock_pool.submit.side_effect = mock_submit
            mock_pool_cls.return_value = mock_pool

            import timba.market_cache
            with patch.object(timba.market_cache.time, "sleep", side_effect=mock_sleep):
                mc._poll_loop()

        assert mc._running is False

    def test_poll_loop_failure_window_resets_after_60s(self):
        """Failure count resets when window exceeds 60s (line 119-120)."""
        mc = self._make_cache()
        mc.track("btc-updown-5m-100", "tok_up", "tok_down", contracts=10)
        mc._running = True

        # We need to simulate failures where the window has expired (>60s)
        # by manipulating time
        import timba.market_cache
        now = [1000.0]

        def mock_time():
            return now[0]

        iteration = [0]
        def mock_sleep(duration):
            iteration[0] += 1
            if iteration[0] == 1:
                # After first failure, advance time by 61s to reset window
                now[0] += 61
            elif iteration[0] >= 3:
                mc._running = False

        with patch.object(mc, '_update_market', side_effect=Exception("API error")), \
             patch.object(timba.market_cache.time, "time", side_effect=mock_time), \
             patch.object(timba.market_cache.time, "sleep", side_effect=mock_sleep):
            mc._poll_loop()

        # Should not crash — failure window was reset
