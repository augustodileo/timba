"""Tests for health endpoint and log file handler."""

import logging
import time

from timba.health import HealthState, LogFileHandler, sanitize_log_line, start_health_server


class TestHealthState:
    def test_initial_state(self):
        h = HealthState()
        d = h.to_dict()
        assert d["status"] == "stale"  # no tick yet
        assert d["errors"] == 0

    def test_after_tick(self):
        h = HealthState()
        h.update(last_tick=time.time(), feed_healthy=True)
        d = h.to_dict()
        assert d["status"] == "ok"
        assert d["feed_healthy"] is True
        assert "portfolio" not in d  # business data belongs in /api/status

    def test_stale_after_no_tick(self):
        h = HealthState()
        h.last_tick = time.time() - 20  # 20s ago
        d = h.to_dict()
        assert d["status"] == "stale"


class TestSanitizeLogLine:
    def test_removes_wallet_address(self):
        line = "Connected to 0xDeaDbeefdEAdbeefdEadbEEFdeadbeEFdEaDbeeF"
        assert "[REDACTED]" in sanitize_log_line(line)

    def test_removes_balance(self):
        line = "USDC balance: $124.50"
        assert "[REDACTED]" in sanitize_log_line(line)

    def test_removes_ip(self):
        line = "Geoblock: ip=10.0.0.1"
        assert "[REDACTED]" in sanitize_log_line(line)

    def test_removes_private_key_ref(self):
        line = "POLYMARKET_PRIVATE_KEY not set"
        assert "[REDACTED]" in sanitize_log_line(line)

    def test_preserves_normal_log(self):
        line = "SNIPE BTC 5m | UP @$0.92 | confidence=85%"
        assert sanitize_log_line(line) == line


class TestLogFileHandler:
    def test_writes_sanitized_logs(self, tmp_path):
        log_file = tmp_path / "bot.log"
        handler = LogFileHandler(log_file, max_lines=10)
        handler.setFormatter(logging.Formatter("%(message)s"))

        logger = logging.getLogger("test_health_log")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

        logger.info("SNIPE BTC 5m | UP @$0.92")
        logger.info("Connected to 0xDeaDbeefdEAdbeefdEadbEEFdeadbeEFdEaDbeeF")

        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 2
        assert "SNIPE BTC" in lines[0]
        assert "[REDACTED]" in lines[1]

        logger.removeHandler(handler)

    def test_max_lines(self, tmp_path):
        log_file = tmp_path / "bot.log"
        handler = LogFileHandler(log_file, max_lines=5)
        handler.setFormatter(logging.Formatter("%(message)s"))

        logger = logging.getLogger("test_health_max")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

        for i in range(10):
            logger.info("Line %d", i)

        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 5
        assert "Line 9" in lines[-1]

        logger.removeHandler(handler)


class TestHealthServer:
    def test_health_endpoint(self):
        import httpx
        health = HealthState()
        health.last_tick = time.time()
        health.mode = "paper"

        server = start_health_server(health, port=18080)
        try:
            resp = httpx.get("http://localhost:18080/health", timeout=5)
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert "errors" in data
        finally:
            server.shutdown()

    def test_404_on_unknown_path(self):
        import httpx
        health = HealthState()
        server = start_health_server(health, port=18081)
        try:
            resp = httpx.get("http://localhost:18081/unknown", timeout=5)
            assert resp.status_code == 404
        finally:
            server.shutdown()


class TestHealthStateUpdate:
    """Test HealthState.update() with kwargs (lines 55-58)."""

    def test_update_sets_multiple_fields(self):
        h = HealthState()
        h.update(last_tick=123.0, feed_healthy=False, errors=5)
        assert h.last_tick == 123.0
        assert h.feed_healthy is False
        assert h.errors == 5

    def test_update_sets_custom_field(self):
        h = HealthState()
        h.update(custom_field="hello")
        assert h.custom_field == "hello"


class TestHealthStateIsReady:
    """Test HealthState.is_ready() method (lines 55-58)."""

    def test_not_ready_when_never_ticked(self):
        h = HealthState()
        assert h.is_ready() is False

    def test_ready_when_recent_tick_and_healthy(self):
        h = HealthState()
        h.update(last_tick=time.time(), feed_healthy=True)
        assert h.is_ready() is True

    def test_not_ready_when_stale_tick(self):
        h = HealthState()
        h.update(last_tick=time.time() - 20, feed_healthy=True)
        assert h.is_ready() is False

    def test_not_ready_when_feed_unhealthy(self):
        h = HealthState()
        h.update(last_tick=time.time(), feed_healthy=False)
        assert h.is_ready() is False


class TestReadyEndpoint:
    """Test /ready endpoint (lines 84-89)."""

    def test_ready_returns_200_when_ready(self):
        import httpx
        health = HealthState()
        health.update(last_tick=time.time(), feed_healthy=True)
        server = start_health_server(health, port=18082)
        try:
            resp = httpx.get("http://localhost:18082/ready", timeout=5)
            assert resp.status_code == 200
            data = resp.json()
            assert data["ready"] is True
        finally:
            server.shutdown()

    def test_ready_returns_503_when_not_ready(self):
        import httpx
        health = HealthState()
        # Never ticked — not ready
        server = start_health_server(health, port=18083)
        try:
            resp = httpx.get("http://localhost:18083/ready", timeout=5)
            assert resp.status_code == 503
            data = resp.json()
            assert data["ready"] is False
        finally:
            server.shutdown()


class TestLogFileHandlerEmitException:
    """Test LogFileHandler emit exception path (lines 120-121)."""

    def test_emit_exception_is_swallowed(self, tmp_path):
        log_file = tmp_path / "bot.log"
        handler = LogFileHandler(log_file, max_lines=10)
        # Set a formatter that will cause an exception
        handler.setFormatter(logging.Formatter("%(message)s"))

        # Make path.write_text raise an exception
        from unittest.mock import patch
        with patch.object(handler, 'path') as mock_path:
            mock_path.write_text.side_effect = OSError("disk full")
            record = logging.LogRecord(
                name="test", level=logging.INFO, pathname="", lineno=0,
                msg="test message", args=(), exc_info=None,
            )
            # Should not raise — exception is swallowed
            handler.emit(record)
