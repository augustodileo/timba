"""Tests for the bot API server."""

import json
import sqlite3
import threading
import time

import pytest

from timba.health import HealthState
from timba.server import start_api_server


class TestAPIServer:
    def test_health_endpoint(self):
        import urllib.request
        health = HealthState()
        health.last_tick = time.time()
        shutdown_event = threading.Event()

        server = start_api_server(health, None, shutdown_event, "test-v1", port=18090, bind="127.0.0.1")
        try:
            resp = urllib.request.urlopen("http://127.0.0.1:18090/api/health", timeout=5)
            data = json.loads(resp.read())
            assert data["status"] == "ok"
            assert "errors" in data
        finally:
            server.shutdown()

    def test_health_endpoint_legacy_path(self):
        import urllib.request
        health = HealthState()
        health.last_tick = time.time()
        shutdown_event = threading.Event()

        server = start_api_server(health, None, shutdown_event, "test-v1", port=18091, bind="127.0.0.1")
        try:
            resp = urllib.request.urlopen("http://127.0.0.1:18091/health", timeout=5)
            data = json.loads(resp.read())
            assert data["status"] == "ok"
        finally:
            server.shutdown()

    def test_status_endpoint(self, tmp_path):
        import urllib.request

        from timba import db as _db
        from timba.state import State
        _db.init(tmp_path / "data")
        health = HealthState()
        health.last_tick = time.time()
        shutdown_event = threading.Event()

        state = State()
        state.cash = 100.0
        state.portfolio = 200.0

        server = start_api_server(health, state, shutdown_event, "test-v2", port=18092, bind="127.0.0.1")
        try:
            resp = urllib.request.urlopen("http://127.0.0.1:18092/api/status", timeout=5)
            data = json.loads(resp.read())
            assert data["version"] == "test-v2"
            assert "health" in data
            assert data["state"]["cash"] == 100.0
        finally:
            server.shutdown()

    def test_status_exposes_strategies_from_config(self, tmp_path):
        import urllib.request

        from timba import db as _db
        from timba.config import Config, StrategyConfig
        from timba.state import State
        _db.init(tmp_path / "data")
        health = HealthState()
        health.last_tick = time.time()
        shutdown_event = threading.Event()

        state = State()
        config = Config()
        config.strategies["favorite"] = StrategyConfig({
            "enabled": True,
            "markets": [
                {"coin": "btc", "interval": "5m", "mode": "paper"},
                {"coin": "eth", "interval": "5m", "mode": "live"},
            ],
        })

        server = start_api_server(health, state, shutdown_event, "test", data_dir=tmp_path / "data", config=config, port=18103, bind="127.0.0.1")
        try:
            resp = urllib.request.urlopen("http://127.0.0.1:18103/api/status", timeout=5)
            data = json.loads(resp.read())
            assert "strategies" in data
            assert "favorite" in data["strategies"]
            markets = data["strategies"]["favorite"]["markets"]
            assert len(markets) == 2
            assert markets[0]["coin"] == "btc"
            assert markets[1]["mode"] == "live"
        finally:
            server.shutdown()

    def test_status_excludes_disabled_strategies(self, tmp_path):
        import urllib.request

        from timba import db as _db
        from timba.config import Config, StrategyConfig
        from timba.state import State
        _db.init(tmp_path / "data")
        health = HealthState()
        health.last_tick = time.time()
        shutdown_event = threading.Event()

        state = State()
        config = Config()
        config.strategies["favorite"] = StrategyConfig({"enabled": False, "markets": []})

        server = start_api_server(health, state, shutdown_event, "test", config=config, port=18104, bind="127.0.0.1")
        try:
            resp = urllib.request.urlopen("http://127.0.0.1:18104/api/status", timeout=5)
            data = json.loads(resp.read())
            assert data["strategies"] == {}
        finally:
            server.shutdown()

    def test_stop_endpoint(self):
        import urllib.request
        health = HealthState()
        shutdown_event = threading.Event()

        server = start_api_server(health, None, shutdown_event, "test-v1", port=18093, bind="127.0.0.1")
        try:
            req = urllib.request.Request("http://127.0.0.1:18093/api/stop", method="POST", data=b"")
            resp = urllib.request.urlopen(req, timeout=5)
            data = json.loads(resp.read())
            assert data["status"] == "stopping"
            assert shutdown_event.is_set()
        finally:
            server.shutdown()

    def test_404_on_unknown_path(self):
        import urllib.error
        import urllib.request
        health = HealthState()
        shutdown_event = threading.Event()

        server = start_api_server(health, None, shutdown_event, "test-v1", port=18094, bind="127.0.0.1")
        try:
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                urllib.request.urlopen("http://127.0.0.1:18094/unknown", timeout=5)
            assert exc_info.value.code == 404
        finally:
            server.shutdown()

    def test_404_on_unknown_post(self):
        import urllib.error
        import urllib.request
        health = HealthState()
        shutdown_event = threading.Event()

        server = start_api_server(health, None, shutdown_event, "test-v1", port=18095, bind="127.0.0.1")
        try:
            req = urllib.request.Request("http://127.0.0.1:18095/unknown", method="POST", data=b"")
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                urllib.request.urlopen(req, timeout=5)
            assert exc_info.value.code == 404
        finally:
            server.shutdown()

    def test_trades_endpoint(self, tmp_path):
        """GET /api/trades returns trades from SQLite."""
        import urllib.request

        # Create a test SQLite DB with the trades table
        db_path = tmp_path / "bot.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""CREATE TABLE trades (
            id INTEGER PRIMARY KEY,
            type TEXT NOT NULL,
            strategy TEXT NOT NULL,
            slug TEXT NOT NULL,
            condition_id TEXT,
            coin TEXT NOT NULL DEFAULT '',
            interval TEXT NOT NULL DEFAULT '',
            side TEXT,
            buy_price REAL,
            contracts INTEGER,
            pnl REAL,
            sniped_at TEXT,
            resolved_at TEXT,
            end_timestamp INTEGER,
            market_mode TEXT,
            skip_reason TEXT,
            ticks_evaluated INTEGER,
            ev_id INTEGER,
            token_id TEXT,
            redeemed INTEGER DEFAULT 0,
            order_id TEXT,
            min_price REAL,
            midpoint REAL,
            extras TEXT
        )""")
        conn.execute(
            "INSERT INTO trades (id, type, strategy, slug, coin, interval, side, buy_price, contracts, pnl, sniped_at, redeemed) "
            "VALUES (1, 'paper_win', 'favorite', 'btc-updown-5m-1234', 'btc', '5m', 'UP', 0.95, 5, 0.25, '2026-03-31T10:00:00', 0)"
        )
        conn.execute(
            "INSERT INTO trades (id, type, strategy, slug, coin, interval, side, buy_price, contracts, pnl, sniped_at, redeemed) "
            "VALUES (2, 'paper_loss', 'favorite', 'eth-updown-5m-5678', 'eth', '5m', 'DOWN', 0.90, 5, -4.50, '2026-03-31T10:05:00', 0)"
        )
        conn.execute(
            "INSERT INTO trades (id, type, strategy, slug, coin, interval, side, buy_price, contracts, pnl, sniped_at, redeemed, extras) "
            "VALUES (3, 'win', 'favorite', 'btc-updown-5m-9999', 'btc', '5m', 'UP', 0.98, 10, 0.20, '2026-03-31T10:10:00', 1, "
            "'{\"ev_up\": 0.03}')"
        )
        conn.commit()
        conn.close()

        health = HealthState()
        health.last_tick = time.time()
        shutdown_event = threading.Event()

        server = start_api_server(health, None, shutdown_event, "test-v1", data_dir=tmp_path, port=18096, bind="127.0.0.1")
        try:
            resp = urllib.request.urlopen("http://127.0.0.1:18096/api/trades", timeout=5)
            data = json.loads(resp.read())
            assert isinstance(data, list)
            assert len(data) == 3
            # Check first trade fields
            t1 = data[0]
            assert t1["type"] == "paper_win"
            assert t1["coin"] == "btc"
            assert t1["redeemed"] is False
            # Check extras were merged into trade dict
            t3 = data[2]
            assert t3["ev_up"] == 0.03
        finally:
            server.shutdown()

    def test_trades_endpoint_with_limit(self, tmp_path):
        """GET /api/trades?limit=2 respects limit parameter."""
        import urllib.request

        db_path = tmp_path / "bot.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""CREATE TABLE trades (
            id INTEGER PRIMARY KEY, type TEXT NOT NULL, strategy TEXT NOT NULL,
            slug TEXT NOT NULL, condition_id TEXT, coin TEXT NOT NULL DEFAULT '',
            interval TEXT NOT NULL DEFAULT '', side TEXT, buy_price REAL,
            contracts INTEGER, pnl REAL, sniped_at TEXT, resolved_at TEXT,
            end_timestamp INTEGER, market_mode TEXT, skip_reason TEXT,
            ticks_evaluated INTEGER, ev_id INTEGER, token_id TEXT,
            redeemed INTEGER DEFAULT 0, order_id TEXT, min_price REAL,
            midpoint REAL, extras TEXT
        )""")
        for i in range(5):
            conn.execute(
                "INSERT INTO trades (id, type, strategy, slug, coin, interval, sniped_at, redeemed) "
                f"VALUES ({i+1}, 'paper_win', 'favorite', 'btc-updown-5m-{i}', 'btc', '5m', '2026-03-31T10:{i:02d}:00', 0)"
            )
        conn.commit()
        conn.close()

        health = HealthState()
        health.last_tick = time.time()
        shutdown_event = threading.Event()

        server = start_api_server(health, None, shutdown_event, "test-v1", data_dir=tmp_path, port=18097, bind="127.0.0.1")
        try:
            resp = urllib.request.urlopen("http://127.0.0.1:18097/api/trades?limit=2", timeout=5)
            data = json.loads(resp.read())
            assert len(data) == 2
            # Should return last 2 (most recent by sniped_at)
            assert data[0]["slug"] == "btc-updown-5m-3"
            assert data[1]["slug"] == "btc-updown-5m-4"
        finally:
            server.shutdown()

    def test_trades_endpoint_no_data_dir(self):
        """GET /api/trades with no data_dir returns empty list."""
        import urllib.request

        health = HealthState()
        health.last_tick = time.time()
        shutdown_event = threading.Event()

        server = start_api_server(health, None, shutdown_event, "test-v1", data_dir=None, port=18098, bind="127.0.0.1")
        try:
            resp = urllib.request.urlopen("http://127.0.0.1:18098/api/trades", timeout=5)
            data = json.loads(resp.read())
            assert data == []
        finally:
            server.shutdown()

    def test_logs_endpoint(self, tmp_path):
        """GET /api/logs returns log lines from bot.log."""
        import urllib.request

        log_file = tmp_path / "bot.log"
        lines = [f"2026-03-31 10:{i:02d}:00 INFO  Line {i}" for i in range(10)]
        log_file.write_text("\n".join(lines) + "\n")

        health = HealthState()
        health.last_tick = time.time()
        shutdown_event = threading.Event()

        server = start_api_server(health, None, shutdown_event, "test-v1", data_dir=tmp_path, port=18099, bind="127.0.0.1")
        try:
            resp = urllib.request.urlopen("http://127.0.0.1:18099/api/logs", timeout=5)
            data = json.loads(resp.read())
            assert "lines" in data
            # Default is 20 lines, we have 10
            assert len(data["lines"]) == 10
            assert "Line 9" in data["lines"][-1]
        finally:
            server.shutdown()

    def test_logs_endpoint_with_lines_param(self, tmp_path):
        """GET /api/logs?lines=3 respects lines parameter."""
        import urllib.request

        log_file = tmp_path / "bot.log"
        lines = [f"Line {i}" for i in range(10)]
        log_file.write_text("\n".join(lines) + "\n")

        health = HealthState()
        health.last_tick = time.time()
        shutdown_event = threading.Event()

        server = start_api_server(health, None, shutdown_event, "test-v1", data_dir=tmp_path, port=18100, bind="127.0.0.1")
        try:
            resp = urllib.request.urlopen("http://127.0.0.1:18100/api/logs?lines=3", timeout=5)
            data = json.loads(resp.read())
            assert len(data["lines"]) == 3
            assert data["lines"][0] == "Line 7"
            assert data["lines"][-1] == "Line 9"
        finally:
            server.shutdown()

    def test_logs_endpoint_no_log_file(self, tmp_path):
        """GET /api/logs when bot.log doesn't exist returns empty."""
        import urllib.request

        health = HealthState()
        health.last_tick = time.time()
        shutdown_event = threading.Event()

        server = start_api_server(health, None, shutdown_event, "test-v1", data_dir=tmp_path, port=18101, bind="127.0.0.1")
        try:
            resp = urllib.request.urlopen("http://127.0.0.1:18101/api/logs", timeout=5)
            data = json.loads(resp.read())
            assert data["lines"] == []
        finally:
            server.shutdown()

    def test_logs_endpoint_no_data_dir(self):
        """GET /api/logs with no data_dir returns empty."""
        import urllib.request

        health = HealthState()
        health.last_tick = time.time()
        shutdown_event = threading.Event()

        server = start_api_server(health, None, shutdown_event, "test-v1", data_dir=None, port=18102, bind="127.0.0.1")
        try:
            resp = urllib.request.urlopen("http://127.0.0.1:18102/api/logs", timeout=5)
            data = json.loads(resp.read())
            assert data["lines"] == []
        finally:
            server.shutdown()

    def test_ready_endpoint_when_ready(self):
        """GET /api/ready returns 200 when feed is healthy and ticking (lines 31-33)."""
        import urllib.request

        health = HealthState()
        health.last_tick = time.time()
        health.feed_healthy = True
        shutdown_event = threading.Event()

        server = start_api_server(health, None, shutdown_event, "test-v1", port=18105, bind="127.0.0.1")
        try:
            resp = urllib.request.urlopen("http://127.0.0.1:18105/api/ready", timeout=5)
            data = json.loads(resp.read())
            assert resp.status == 200
            assert data["ready"] is True
        finally:
            server.shutdown()

    def test_ready_endpoint_when_not_ready(self):
        """GET /api/ready returns 503 when health is not ready (lines 31-33)."""
        import urllib.error
        import urllib.request

        health = HealthState()
        # last_tick = 0 means hasn't started yet
        shutdown_event = threading.Event()

        server = start_api_server(health, None, shutdown_event, "test-v1", port=18106, bind="127.0.0.1")
        try:
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                urllib.request.urlopen("http://127.0.0.1:18106/api/ready", timeout=5)
            assert exc_info.value.code == 503
            data = json.loads(exc_info.value.read())
            assert data["ready"] is False
        finally:
            server.shutdown()

    def test_ready_endpoint_legacy_path(self):
        """GET /ready legacy path also works (lines 31-33)."""
        import urllib.request

        health = HealthState()
        health.last_tick = time.time()
        health.feed_healthy = True
        shutdown_event = threading.Event()

        server = start_api_server(health, None, shutdown_event, "test-v1", port=18107, bind="127.0.0.1")
        try:
            resp = urllib.request.urlopen("http://127.0.0.1:18107/ready", timeout=5)
            data = json.loads(resp.read())
            assert data["ready"] is True
        finally:
            server.shutdown()

    def test_status_with_state_error(self, tmp_path):
        """State.to_dashboard_dict() raising should yield empty state (lines 40-41)."""
        import urllib.request
        from unittest.mock import MagicMock

        health = HealthState()
        health.last_tick = time.time()
        shutdown_event = threading.Event()

        state = MagicMock()
        state.to_dashboard_dict.side_effect = Exception("boom")

        server = start_api_server(health, state, shutdown_event, "test-v1", port=18108, bind="127.0.0.1")
        try:
            resp = urllib.request.urlopen("http://127.0.0.1:18108/api/status", timeout=5)
            data = json.loads(resp.read())
            assert data["state"] == {}
        finally:
            server.shutdown()

    def test_trades_bad_extras_json_ignored(self, tmp_path):
        """Trade with invalid extras JSON should still be returned (lines 95-96)."""
        import urllib.request

        db_path = tmp_path / "bot.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""CREATE TABLE trades (
            id INTEGER PRIMARY KEY, type TEXT NOT NULL, strategy TEXT NOT NULL,
            slug TEXT NOT NULL, condition_id TEXT, coin TEXT NOT NULL DEFAULT '',
            interval TEXT NOT NULL DEFAULT '', side TEXT, buy_price REAL,
            contracts INTEGER, pnl REAL, sniped_at TEXT, resolved_at TEXT,
            end_timestamp INTEGER, market_mode TEXT, skip_reason TEXT,
            ticks_evaluated INTEGER, ev_id INTEGER, token_id TEXT,
            redeemed INTEGER DEFAULT 0, order_id TEXT, min_price REAL,
            midpoint REAL, extras TEXT
        )""")
        conn.execute(
            "INSERT INTO trades (id, type, strategy, slug, coin, interval, sniped_at, redeemed, extras) "
            "VALUES (1, 'win', 'favorite', 'btc-5m-100', 'btc', '5m', '2026-03-31T10:00:00', 0, 'not-valid-json{{')"
        )
        conn.commit()
        conn.close()

        health = HealthState()
        health.last_tick = time.time()
        shutdown_event = threading.Event()

        server = start_api_server(health, None, shutdown_event, "test-v1", data_dir=tmp_path, port=18109, bind="127.0.0.1")
        try:
            resp = urllib.request.urlopen("http://127.0.0.1:18109/api/trades", timeout=5)
            data = json.loads(resp.read())
            assert len(data) == 1
            assert data[0]["slug"] == "btc-5m-100"
        finally:
            server.shutdown()

    def test_trades_corrupt_db_skipped(self, tmp_path):
        """A corrupt DB file should be skipped (lines 98-99)."""
        import urllib.request

        # Create a corrupt file that looks like a rotated db
        bad_db = tmp_path / "bot_2026-01-01.db"
        bad_db.write_text("not a valid sqlite database")

        health = HealthState()
        health.last_tick = time.time()
        shutdown_event = threading.Event()

        server = start_api_server(health, None, shutdown_event, "test-v1", data_dir=tmp_path, port=18110, bind="127.0.0.1")
        try:
            resp = urllib.request.urlopen("http://127.0.0.1:18110/api/trades", timeout=5)
            data = json.loads(resp.read())
            assert data == []
        finally:
            server.shutdown()

    def test_logs_endpoint_read_error(self, tmp_path):
        """If log file read fails, return empty lines (lines 121-122)."""
        import os
        import urllib.request

        # Create a log file then make it unreadable
        log_file = tmp_path / "bot.log"
        log_file.write_text("some log line\n")
        os.chmod(str(log_file), 0o000)

        health = HealthState()
        health.last_tick = time.time()
        shutdown_event = threading.Event()

        server = start_api_server(health, None, shutdown_event, "test-v1", data_dir=tmp_path, port=18111, bind="127.0.0.1")
        try:
            resp = urllib.request.urlopen("http://127.0.0.1:18111/api/logs", timeout=5)
            data = json.loads(resp.read())
            assert data["lines"] == []
        finally:
            server.shutdown()
            os.chmod(str(log_file), 0o644)  # restore for cleanup

    def test_stop_endpoint_no_shutdown_event(self):
        """POST /api/stop with no shutdown_event still returns success."""
        import urllib.request

        health = HealthState()
        health.last_tick = time.time()

        from timba.server import BotAPIHandler
        # Temporarily set shutdown_event to None
        server = start_api_server(health, None, None, "test-v1", port=18112, bind="127.0.0.1")
        BotAPIHandler.shutdown_event = None
        try:
            req = urllib.request.Request("http://127.0.0.1:18112/api/stop", method="POST", data=b"")
            resp = urllib.request.urlopen(req, timeout=5)
            data = json.loads(resp.read())
            assert data["status"] == "stopping"
        finally:
            server.shutdown()

    def test_bind_defaults_to_env_var(self, monkeypatch):
        """start_api_server uses TIMBA_BIND env var when bind is empty (line 152)."""
        import urllib.request

        monkeypatch.setenv("TIMBA_BIND", "127.0.0.1")
        health = HealthState()
        health.last_tick = time.time()
        shutdown_event = threading.Event()

        server = start_api_server(health, None, shutdown_event, "test-v1", port=18113)
        try:
            resp = urllib.request.urlopen("http://127.0.0.1:18113/api/health", timeout=5)
            data = json.loads(resp.read())
            assert data["status"] == "ok"
        finally:
            server.shutdown()
