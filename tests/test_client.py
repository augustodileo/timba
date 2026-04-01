"""Tests for the bot client."""

import json
import threading
import time

import pytest

from timba.client import BotClient
from timba.health import HealthState
from timba.server import start_api_server


class TestBotClient:
    def test_no_bot_json_not_running(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TIMBA_HOME", str(tmp_path / "no_timba"))
        client = BotClient()
        assert not client.is_running()

    def test_bot_json_invalid_not_running(self, tmp_path, monkeypatch):
        home = tmp_path / "timba_home"
        home.mkdir()
        (home / "bot.json").write_text("not json")
        monkeypatch.setenv("TIMBA_HOME", str(home))
        client = BotClient()
        assert not client.is_running()

    def test_explicit_host_port(self):
        client = BotClient(host="1.2.3.4", port=9999)
        assert client.base_url == "http://1.2.3.4:9999"

    def test_auto_discover_from_bot_json(self, tmp_path, monkeypatch):
        home = tmp_path / "timba_home"
        home.mkdir()
        (home / "bot.json").write_text(json.dumps({"pid": 1234, "port": 8080}))
        monkeypatch.setenv("TIMBA_HOME", str(home))
        client = BotClient()
        assert client.base_url == "http://127.0.0.1:8080"

    def test_health_error_without_base_url(self):
        client = BotClient(host=None, port=None)
        client.base_url = None
        with pytest.raises(ConnectionError):
            client.health()

    def test_stop_error_without_base_url(self):
        client = BotClient(host=None, port=None)
        client.base_url = None
        with pytest.raises(ConnectionError):
            client.stop()


class TestBotClientRediscovery:
    def test_rediscovers_when_bot_starts_later(self, tmp_path, monkeypatch):
        """Client created before bot starts should detect it on next is_running() call."""
        home = tmp_path / "timba_home"
        home.mkdir()
        monkeypatch.setenv("TIMBA_HOME", str(home))

        client = BotClient()
        assert client.base_url is None
        assert not client.is_running()

        # Bot starts — writes bot.json
        (home / "bot.json").write_text(json.dumps({"pid": 1234, "port": 18099}))

        # Start a real server on that port
        health = HealthState()
        health.last_tick = time.time()
        shutdown_event = threading.Event()
        server = start_api_server(health, None, shutdown_event, "test", port=18099, bind="127.0.0.1")
        try:
            assert client.is_running()
            assert client.base_url == "http://127.0.0.1:18099"
        finally:
            server.shutdown()

    def test_clears_base_url_when_bot_stops(self, tmp_path, monkeypatch):
        """When bot stops (API unreachable), base_url should be cleared for re-discovery."""
        home = tmp_path / "timba_home"
        home.mkdir()
        (home / "bot.json").write_text(json.dumps({"pid": 1234, "port": 19999}))
        monkeypatch.setenv("TIMBA_HOME", str(home))

        client = BotClient()
        assert client.base_url is not None

        # Bot is not actually running on 19999
        assert not client.is_running()
        assert client.base_url is None


class TestBotClientIntegration:
    def test_health_against_server(self):
        health = HealthState()
        health.last_tick = time.time()
        shutdown_event = threading.Event()
        server = start_api_server(health, None, shutdown_event, "test", port=18096, bind="127.0.0.1")
        try:
            client = BotClient(host="127.0.0.1", port=18096)
            assert client.is_running()
            data = client.health()
            assert data["status"] == "ok"
        finally:
            server.shutdown()

    def test_status_against_server(self, tmp_path):
        from timba import db as _db
        from timba.state import State
        _db.init(tmp_path / "data")
        health = HealthState()
        health.last_tick = time.time()
        shutdown_event = threading.Event()

        state = State()
        state.cash = 50.0
        state.portfolio = 50.0

        server = start_api_server(health, state, shutdown_event, "v1.2.3", port=18097, bind="127.0.0.1")
        try:
            client = BotClient(host="127.0.0.1", port=18097)
            data = client.status()
            assert data["version"] == "v1.2.3"
            assert data["state"]["cash"] == 50.0
        finally:
            server.shutdown()

    def test_stop_against_server(self):
        health = HealthState()
        shutdown_event = threading.Event()
        server = start_api_server(health, None, shutdown_event, "test", port=18098, bind="127.0.0.1")
        try:
            client = BotClient(host="127.0.0.1", port=18098)
            resp = client.stop()
            assert resp["status"] == "stopping"
            assert shutdown_event.is_set()
        finally:
            server.shutdown()
