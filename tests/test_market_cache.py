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
