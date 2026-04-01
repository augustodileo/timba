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
        h.last_tick = time.time()
        h.feed_healthy = True
        h.total_wins = 5
        h.total_pnl = 10.5
        d = h.to_dict()
        assert d["status"] == "ok"
        assert d["total_wins"] == 5
        assert d["total_pnl"] == 10.5
        assert d["feed_healthy"] is True

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
