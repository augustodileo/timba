"""Tests for maintenance scheduler."""

from unittest.mock import MagicMock, patch

from timba.scheduler import MaintenanceScheduler, get_close_minutes, get_next_safe_window, is_safe_window


class TestCloseMinutes:
    def test_5m_only(self):
        mins = get_close_minutes(["5m"])
        assert mins == {0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}

    def test_15m_only(self):
        mins = get_close_minutes(["15m"])
        assert mins == {0, 15, 30, 45}

    def test_1h_only(self):
        mins = get_close_minutes(["1h"])
        assert mins == {0}

    def test_combined(self):
        mins = get_close_minutes(["5m", "15m", "1h"])
        assert 0 in mins
        assert 5 in mins
        assert 15 in mins
        assert len(mins) == 12  # 5m covers all


class TestIsSafeWindow:
    @patch("timba.scheduler.time")
    def test_safe_at_midpoint(self, mock_time):
        # At :02:30 — 2.5 min after :00, 2.5 min before :05
        mock_time.time.return_value = 150.0  # 2:30 into the hour
        assert is_safe_window(["5m"], buffer_sec=120) is True

    @patch("timba.scheduler.time")
    def test_unsafe_near_close(self, mock_time):
        # At :04:00 — 1 min before :05 close
        mock_time.time.return_value = 240.0  # 4:00 into the hour
        assert is_safe_window(["5m"], buffer_sec=120) is False

    @patch("timba.scheduler.time")
    def test_unsafe_just_after_close(self, mock_time):
        # At :05:30 — 30s after :05 close
        mock_time.time.return_value = 330.0  # 5:30 into the hour
        assert is_safe_window(["5m"], buffer_sec=120) is False


class TestMaintenanceScheduler:
    def test_describe(self):
        s = MaintenanceScheduler(["5m", "15m"])
        desc = s.describe()
        assert ":00" in desc
        assert ":05" in desc
        assert "buffer" in desc

    @patch("timba.db.get_pending_redemption", return_value=0.0)
    def test_sync_balance(self, mock_pending):
        s = MaintenanceScheduler(["5m"])
        clob = MagicMock()
        clob.get_usdc_balance.return_value = 100.0
        state = MagicMock()
        state.cash = 95.0
        state.portfolio = 95.0
        state.pending_redemption = 0.0
        s._sync_balance(clob, state)
        assert state.cash == 100.0

    def test_sync_balance_failure(self):
        s = MaintenanceScheduler(["5m"])
        clob = MagicMock()
        clob.get_usdc_balance.side_effect = OSError("timeout")
        state = MagicMock()
        # Should not crash
        s._sync_balance(clob, state)

    @patch("timba.scheduler.is_safe_window", return_value=False)
    def test_should_run_returns_false_when_unsafe(self, mock_safe):
        s = MaintenanceScheduler(["5m"])
        s._last_balance_sync = 0  # overdue
        assert s.should_run_maintenance() is False

    @patch("timba.scheduler.is_safe_window", return_value=True)
    @patch("timba.scheduler.time")
    def test_should_run_returns_true_when_balance_due(self, mock_time, mock_safe):
        mock_time.time.return_value = 1000.0
        s = MaintenanceScheduler(["5m"])
        s._last_balance_sync = 0  # 1000s ago, well past 300s interval
        assert s.should_run_maintenance() is True

    @patch("timba.scheduler.is_safe_window", return_value=True)
    @patch("timba.scheduler.time")
    def test_should_run_returns_false_when_not_due(self, mock_time, mock_safe):
        mock_time.time.return_value = 1000.0
        s = MaintenanceScheduler(["5m"])
        s._last_balance_sync = 999.0   # 1s ago
        s._last_redeem_scan = 999.0
        assert s.should_run_maintenance() is False

    @patch("timba.scheduler.is_safe_window", return_value=False)
    def test_run_skips_when_unsafe(self, mock_safe):
        s = MaintenanceScheduler(["5m"])
        clob = MagicMock()
        state = MagicMock()
        s._last_balance_sync = 0
        s.run(clob, state)
        clob.get_usdc_balance.assert_not_called()

    @patch("timba.db.get_pending_redemption", return_value=0.0)
    @patch("timba.scheduler.is_safe_window", return_value=True)
    @patch("timba.scheduler.time")
    def test_run_syncs_balance_when_due(self, mock_time, mock_safe, mock_pending):
        mock_time.time.return_value = 1000.0
        s = MaintenanceScheduler(["5m"])
        s._last_balance_sync = 0
        clob = MagicMock()
        clob.get_usdc_balance.return_value = 50.0
        state = MagicMock()
        state.cash = 50.0
        state.portfolio = 50.0
        state.pending_redemption = 0.0

        s.run(clob, state)

        clob.get_usdc_balance.assert_called_once()
        assert s._last_balance_sync == 1000.0

    @patch("timba.scheduler.is_safe_window", return_value=True)
    @patch("timba.scheduler.time")
    def test_run_calls_redeem_when_due(self, mock_time, mock_safe):
        mock_time.time.return_value = 1000.0
        s = MaintenanceScheduler(["5m"])
        s._last_balance_sync = 999.0  # not due
        s._last_redeem_scan = 0       # due
        clob = MagicMock()
        clob.get_usdc_balance.return_value = 50.0
        state = MagicMock()
        state.cash = 50.0
        relay = MagicMock()
        redeem_fn = MagicMock()

        s.run(clob, state, relay_client=relay, redeem_fn=redeem_fn)

        redeem_fn.assert_called_once()
        assert s._last_redeem_scan == 1000.0


class TestGetNextSafeWindow:
    @patch("timba.scheduler.time")
    def test_returns_zero_when_already_safe(self, mock_time):
        # 2:30 into the hour — safe for 5m (buffer=120s)
        mock_time.time.return_value = 150.0
        offset = get_next_safe_window(["5m"], buffer_sec=120)
        assert offset == 0

    @patch("timba.scheduler.time")
    def test_returns_positive_offset_when_unsafe(self, mock_time):
        # 4:50 into the hour — 10s before :05 close, unsafe
        mock_time.time.return_value = 290.0
        offset = get_next_safe_window(["5m"], buffer_sec=120)
        assert offset > 0
        assert offset < 300

    @patch("timba.scheduler.time")
    def test_returns_fallback_when_no_safe_window(self, mock_time):
        # With 1h interval and buffer=1800s (30min), every second within
        # the 300s scan window is unsafe (close at :00, +-30min covers all)
        mock_time.time.return_value = 150.0  # 2:30 into the hour
        offset = get_next_safe_window(["1h"], buffer_sec=1800)
        assert offset == 300


class TestShouldRotateDb:
    @patch("timba.db.db_size_mb")
    @patch("timba.scheduler.time")
    def test_skips_when_too_recent(self, mock_time, mock_size):
        mock_time.time.return_value = 1000.0
        s = MaintenanceScheduler(["5m"])
        s._last_rotation_check = 1000.0  # just checked
        result = s.should_rotate_db()
        assert result is None
        mock_size.assert_not_called()

    @patch("timba.db.DB_MAX_SIZE_MB", 500)
    @patch("timba.db.db_size_mb", return_value=600)
    @patch("timba.scheduler.time")
    def test_returns_size_reason(self, mock_time, mock_size):
        mock_time.time.return_value = 1000.0
        s = MaintenanceScheduler(["5m"])
        s._last_rotation_check = 0  # bypass interval
        result = s.should_rotate_db()
        assert result is not None
        assert "size" in result

    @patch("timba.db.DB_MAX_SIZE_MB", 500)
    @patch("timba.db.db_size_mb", return_value=10)
    @patch("timba.scheduler.time")
    def test_returns_daily_reason(self, mock_time, mock_size):
        mock_time.time.return_value = 1000.0
        s = MaintenanceScheduler(["5m"])
        s._last_rotation_check = 0
        s._last_rotation_date = "2025-01-01"
        result = s.should_rotate_db()
        assert result is not None
        assert "daily" in result

    @patch("timba.db.DB_MAX_SIZE_MB", 500)
    @patch("timba.db.db_size_mb", return_value=0.5)
    @patch("timba.scheduler.time")
    def test_no_rotation_when_small_db(self, mock_time, mock_size):
        mock_time.time.return_value = 1000.0
        s = MaintenanceScheduler(["5m"])
        s._last_rotation_check = 0
        s._last_rotation_date = "2025-01-01"  # different date
        result = s.should_rotate_db()
        assert result is None

    @patch("timba.db.DB_MAX_SIZE_MB", 500)
    @patch("timba.db.db_size_mb", return_value=10)
    @patch("timba.scheduler.time")
    def test_no_rotation_when_same_day(self, mock_time, mock_size):
        from datetime import datetime, timezone
        mock_time.time.return_value = 1000.0
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        s = MaintenanceScheduler(["5m"])
        s._last_rotation_check = 0
        s._last_rotation_date = today
        result = s.should_rotate_db()
        assert result is None
