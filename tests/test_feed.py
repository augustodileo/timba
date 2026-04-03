import time

from timba.feed import COIN_TO_PAIR, DirectionSignal, PriceFeed


class TestPriceFeedOffline:
    """Test feed logic without hitting real APIs."""

    def test_coin_to_pair_mapping(self):
        assert COIN_TO_PAIR["btc"] == "BTC-USD"
        assert COIN_TO_PAIR["eth"] == "ETH-USD"
        assert COIN_TO_PAIR["sol"] == "SOL-USD"
        assert COIN_TO_PAIR["doge"] == "DOGE-USD"

    def test_get_price_none_when_no_data(self):
        f = PriceFeed(coins=["btc"], poll_interval=999)
        assert f.get_price("btc") is None

    def test_get_direction_none_when_no_data(self):
        f = PriceFeed(coins=["btc"], poll_interval=999)
        assert f.get_direction("btc", int(time.time()) - 60) is None

    def test_get_direction_with_injected_data(self):
        f = PriceFeed(coins=["btc"], poll_interval=999)
        now = time.time()
        # Inject fake price history: price went from 100 to 101 (up)
        f._prices["btc"] = 101.0
        f._history["btc"] = [
            (now - 60, 100.0),
            (now - 30, 100.5),
            (now - 10, 101.0),
        ]
        signal = f.get_direction("btc", int(now - 60))
        assert signal is not None
        assert signal.direction == "up"
        assert signal.change_pct > 0

    def test_get_direction_down(self):
        f = PriceFeed(coins=["btc"], poll_interval=999)
        now = time.time()
        f._prices["btc"] = 99.0
        f._history["btc"] = [
            (now - 60, 100.0),
            (now - 30, 99.5),
            (now - 10, 99.0),
        ]
        signal = f.get_direction("btc", int(now - 60))
        assert signal.direction == "down"
        assert signal.change_pct < 0

    def test_get_direction_flat(self):
        f = PriceFeed(coins=["btc"], poll_interval=999)
        now = time.time()
        f._prices["btc"] = 100.001
        f._history["btc"] = [
            (now - 60, 100.0),
            (now - 10, 100.001),
        ]
        signal = f.get_direction("btc", int(now - 60))
        assert signal.direction == "flat"

    def test_reversed_recently_detected(self):
        f = PriceFeed(coins=["btc"], poll_interval=999)
        now = time.time()
        f._prices["btc"] = 100.5
        # Price went below open, then above — reversal in last 30s
        f._history["btc"] = [
            (now - 60, 100.0),
            (now - 40, 101.0),
            (now - 20, 99.5),  # below open
            (now - 10, 100.5),  # back above
        ]
        signal = f.get_direction("btc", int(now - 60))
        assert signal.reversed_recently is True

    def test_confidence_higher_for_big_moves(self):
        f = PriceFeed(coins=["btc"], poll_interval=999)
        now = time.time()

        # Small move
        f._prices["btc"] = 100.05
        f._history["btc"] = [(now - 60, 100.0), (now - 10, 100.05)]
        small = f.get_direction("btc", int(now - 60))

        # Big move
        f._prices["btc"] = 100.5
        f._history["btc"] = [(now - 60, 100.0), (now - 10, 100.5)]
        big = f.get_direction("btc", int(now - 60))

        assert big.confidence > small.confidence


class TestDirectionSignal:
    def test_dataclass_fields(self):
        s = DirectionSignal(direction="up", change_pct=0.1,
                           seconds_trending=100, reversed_recently=False,
                           confidence=0.8)
        assert s.direction == "up"
        assert s.confidence == 0.8

    def test_default_price_fields(self):
        """DirectionSignal default price_open and price_now are 0.0 (line 46-47)."""
        s = DirectionSignal(direction="up", change_pct=0.5,
                           seconds_trending=50, reversed_recently=False,
                           confidence=0.6)
        assert s.price_open == 0.0
        assert s.price_now == 0.0

    def test_custom_price_fields(self):
        """DirectionSignal with explicit price_open and price_now."""
        s = DirectionSignal(direction="down", change_pct=-0.5,
                           seconds_trending=50, reversed_recently=True,
                           confidence=0.3, price_open=100.0, price_now=99.5)
        assert s.price_open == 100.0
        assert s.price_now == 99.5


class TestPriceFeedStartStop:
    """Test start/stop lifecycle, is_healthy, and polling paths."""

    def test_start_when_already_running_is_noop(self):
        """start() when _running is True should return immediately (line 67)."""
        f = PriceFeed(coins=["btc"], poll_interval=999)
        f._running = True
        original_thread = f._thread
        f.start()
        # Should not create a new thread
        assert f._thread is original_thread

    def test_stop_joins_thread(self):
        """stop() sets _running=False and joins thread (lines 85-87)."""
        f = PriceFeed(coins=["btc"], poll_interval=999)
        f._running = True
        from unittest.mock import MagicMock
        mock_thread = MagicMock()
        f._thread = mock_thread
        f.stop()
        assert f._running is False
        mock_thread.join.assert_called_once_with(timeout=5)

    def test_stop_when_no_thread(self):
        """stop() when no thread is a no-op (lines 85-87)."""
        f = PriceFeed(coins=["btc"], poll_interval=999)
        f._running = True
        f.stop()
        assert f._running is False

    def test_is_healthy_true_when_never_polled(self):
        """is_healthy returns True if _last_success==0 (line 92-93)."""
        f = PriceFeed(coins=["btc"], poll_interval=999)
        assert f.is_healthy() is True

    def test_is_healthy_true_when_recent_success(self):
        """is_healthy returns True when last success is recent (line 94)."""
        f = PriceFeed(coins=["btc"], poll_interval=999)
        f._last_success = time.time()
        assert f.is_healthy() is True

    def test_is_healthy_false_when_stale(self):
        """is_healthy returns False when stale beyond threshold (line 94)."""
        f = PriceFeed(coins=["btc"], poll_interval=999, stale_threshold=5)
        f._last_success = time.time() - 10
        assert f.is_healthy() is False


class TestPriceFeedHistory:
    """Test history edge cases in get_direction."""

    def test_direction_uses_earliest_when_no_window_start_data(self):
        """When all history is BEFORE window_start, falls back to earliest (lines 125-126)."""
        f = PriceFeed(coins=["btc"], poll_interval=999)
        now = time.time()
        f._prices["btc"] = 105.0
        # All history timestamps are BEFORE window_start_ts
        # So the loop at line 118 won't find any ts >= window_start_ts
        f._history["btc"] = [
            (now - 300, 100.0),
            (now - 200, 102.0),
        ]
        # Window start is AFTER all history entries
        signal = f.get_direction("btc", int(now - 50))
        assert signal is not None
        # Should fallback to history[0][1] = 100.0
        assert signal.price_open == 100.0

    def test_direction_returns_signal_with_prices(self):
        """DirectionSignal includes price_open and price_now (lines 179, 183)."""
        f = PriceFeed(coins=["btc"], poll_interval=999)
        now = time.time()
        f._prices["btc"] = 100.15
        f._history["btc"] = [
            (now - 60, 100.0),
            (now - 10, 100.15),
        ]
        signal = f.get_direction("btc", int(now - 60))
        assert signal is not None
        assert signal.price_open == 100.0
        assert signal.price_now == 100.15

    def test_direction_medium_change_confidence(self):
        """Medium change_pct (0.1-0.3 range) adds 0.25 confidence (line 181)."""
        f = PriceFeed(coins=["btc"], poll_interval=999)
        now = time.time()
        # ~0.15% change with 300s window → medium confidence factor
        f._prices["btc"] = 100.15
        f._history["btc"] = [
            (now - 300, 100.0),
            (now - 1, 100.15),
        ]
        signal = f.get_direction("btc", int(now - 300))
        assert signal is not None
        assert signal.confidence > 0

    def test_direction_small_change_confidence(self):
        """Small change_pct (0.05-0.1 range) adds 0.1 confidence (line 183)."""
        f = PriceFeed(coins=["btc"], poll_interval=999)
        now = time.time()
        # ~0.07% change with 300s window (scale=1), between 0.05 and 0.1
        f._prices["btc"] = 100.07
        f._history["btc"] = [
            (now - 300, 100.0),
            (now - 1, 100.07),
        ]
        signal = f.get_direction("btc", int(now - 300))
        assert signal is not None
        assert signal.direction == "up"
        assert signal.confidence > 0


class TestPriceFeedBackfillAndPoll:
    """Test backfill and poll error paths."""

    def test_backfill_skips_unknown_coin(self):
        """_backfill_history skips coins not in COIN_TO_PAIR (line 233)."""
        f = PriceFeed(coins=["unknown_coin"], poll_interval=999)
        # Should not raise
        f._backfill_history()
        assert f._history.get("unknown_coin") is None

    def test_backfill_handles_request_error(self):
        """_backfill_history handles RequestException (lines 254-255)."""
        from unittest.mock import patch

        import requests as req
        f = PriceFeed(coins=["btc"], poll_interval=999)
        with patch("timba.feed.requests.get", side_effect=req.RequestException("network error")):
            # Should not raise
            f._backfill_history()
        # History should be empty
        assert f._history.get("btc") is None or len(f._history.get("btc", [])) == 0

    def test_poll_loop_skips_unknown_pair(self):
        """_poll_loop skips coins not in COIN_TO_PAIR (line 266)."""
        from unittest.mock import patch
        f = PriceFeed(coins=["nonexistent"], poll_interval=999)
        f._running = True

        # Run one iteration of the poll loop then stop
        call_count = 0
        def mock_sleep(duration):
            nonlocal call_count
            call_count += 1
            f._running = False  # stop after first iteration

        with patch("timba.feed.time.sleep", side_effect=mock_sleep):
            f._poll_loop()

        # No prices should be set for unknown coin
        assert f.get_price("nonexistent") is None

    def test_poll_loop_updates_history_and_last_success(self):
        """_poll_loop appends to history and sets _last_success (lines 278, 287)."""
        from unittest.mock import MagicMock, patch
        f = PriceFeed(coins=["btc"], poll_interval=999)
        f._running = True
        # Do NOT pre-populate _history so line 278 (coin not in self._history) is taken

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"data": {"amount": "50000.0"}}
        mock_resp.raise_for_status = MagicMock()

        call_count = 0
        def mock_sleep(duration):
            nonlocal call_count
            call_count += 1
            f._running = False

        mock_session = MagicMock()
        mock_session.get.return_value = mock_resp

        with patch("timba.feed.requests.Session", return_value=mock_session), \
             patch("timba.feed.time.sleep", side_effect=mock_sleep):
            f._poll_loop()

        assert f.get_price("btc") == 50000.0
        assert len(f._history["btc"]) == 1
        assert f._last_success > 0

    def test_poll_loop_handles_request_exception(self):
        """_poll_loop handles exceptions per coin gracefully (lines 287-288)."""
        from unittest.mock import MagicMock, patch

        import requests as req
        f = PriceFeed(coins=["btc"], poll_interval=999)
        f._running = True

        mock_session = MagicMock()
        mock_session.get.side_effect = req.RequestException("timeout")

        def mock_sleep(duration):
            f._running = False

        with patch("timba.feed.requests.Session", return_value=mock_session), \
             patch("timba.feed.time.sleep", side_effect=mock_sleep):
            f._poll_loop()

        # Should not crash, no price set
        assert f.get_price("btc") is None

    def test_start_waits_for_prices(self):
        """start() waits up to 5s for _prices to be populated (line 75)."""
        from unittest.mock import MagicMock, patch
        f = PriceFeed(coins=["btc"], poll_interval=999)

        # Mock _backfill_history to set a price immediately
        def mock_backfill():
            f._prices["btc"] = 50000.0

        sleep_calls = []
        def track_sleep(duration):
            sleep_calls.append(duration)

        with patch.object(f, '_backfill_history', side_effect=mock_backfill), \
             patch.object(f, '_poll_loop'), \
             patch("timba.feed.time.sleep", side_effect=track_sleep), \
             patch("threading.Thread") as mock_thread_cls:
            mock_thread = MagicMock()
            mock_thread_cls.return_value = mock_thread
            f.start()

        assert f._running is True
        # Should not have called sleep(0.5) because _prices was set immediately
        assert all(d == 0.5 for d in sleep_calls)

    def test_start_sleeps_waiting_for_prices(self):
        """start() sleeps when _prices is empty, up to 10 times (line 75)."""
        from unittest.mock import MagicMock, patch
        f = PriceFeed(coins=["btc"], poll_interval=999)

        sleep_calls = []
        def track_sleep(duration):
            sleep_calls.append(duration)
            # Simulate prices appearing after 2 sleeps
            if len(sleep_calls) >= 2:
                f._prices["btc"] = 50000.0

        with patch.object(f, '_backfill_history'), \
             patch.object(f, '_poll_loop'), \
             patch("timba.feed.time.sleep", side_effect=track_sleep), \
             patch("threading.Thread") as mock_thread_cls:
            mock_thread = MagicMock()
            mock_thread_cls.return_value = mock_thread
            f.start()

        assert f._running is True
        assert len(sleep_calls) == 2
        assert all(d == 0.5 for d in sleep_calls)

    def test_backfill_successful(self):
        """_backfill_history processes candles successfully (lines 240-253)."""
        from unittest.mock import MagicMock, patch
        f = PriceFeed(coins=["btc"], poll_interval=999)

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        # Candles: [timestamp, low, high, open, close, volume]
        mock_resp.json.return_value = [
            [1000, 99.0, 101.0, 100.0, 100.5, 1000],
            [1060, 100.0, 102.0, 100.5, 101.0, 1200],
        ]

        with patch("timba.feed.requests.get", return_value=mock_resp):
            f._backfill_history()

        assert "btc" in f._history
        assert len(f._history["btc"]) == 2
        # Most recent candle's close price should be set
        assert f._prices["btc"] == 101.0
        # History should be sorted by timestamp
        assert f._history["btc"][0][0] == 1000.0
        assert f._history["btc"][1][0] == 1060.0
