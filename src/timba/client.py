"""Client to communicate with a running timba bot."""

import json
import os
import urllib.error
import urllib.request
from pathlib import Path


def _timba_home() -> Path:
    return Path(os.environ.get("TIMBA_HOME", "~/.timba")).expanduser()


class BotClient:
    """Talk to a running timba bot via its HTTP API."""

    def __init__(self, host: str | None = None, port: int | None = None) -> None:
        if host and port:
            self.base_url = f"http://{host}:{port}"
        else:
            # Auto-discover from bot.json
            info = self._read_bot_info()
            if info:
                self.base_url = f"http://127.0.0.1:{info['port']}"
            else:
                self.base_url = None

    def _read_bot_info(self) -> dict | None:
        bot_json = _timba_home() / "bot.json"
        if bot_json.exists():
            try:
                return json.loads(bot_json.read_text())
            except (json.JSONDecodeError, OSError):
                return None
        return None

    def is_running(self) -> bool:
        """Check if bot is reachable. Re-discovers from bot.json if needed."""
        if not self.base_url:
            info = self._read_bot_info()
            if info:
                self.base_url = f"http://127.0.0.1:{info['port']}"
            else:
                return False
        try:
            self._get("/api/health")
            return True
        except Exception:
            self.base_url = None
            return False

    def health(self) -> dict:
        return self._get("/api/health")

    def status(self) -> dict:
        return self._get("/api/status")

    def trades(self, limit: int = 100) -> list[dict]:
        return self._get(f"/api/trades?limit={limit}")

    def logs(self, lines: int = 20) -> list[str]:
        data = self._get(f"/api/logs?lines={lines}")
        return data.get("lines", [])

    def stop(self) -> dict:
        return self._post("/api/stop")

    def _get(self, path: str) -> dict:
        if not self.base_url:
            raise ConnectionError("Bot not running (no bot.json found in ~/.timba/)")
        url = self.base_url + path
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())

    def _post(self, path: str) -> dict:
        if not self.base_url:
            raise ConnectionError("Bot not running (no bot.json found in ~/.timba/)")
        url = self.base_url + path
        req = urllib.request.Request(url, method="POST", data=b"")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
