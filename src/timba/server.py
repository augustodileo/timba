"""HTTP API server for bot control and monitoring."""

import json
import logging
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

logger = logging.getLogger(__name__)


class BotAPIHandler(BaseHTTPRequestHandler):
    """HTTP handler for bot control API."""

    health_state = None
    bot_state = None       # State instance
    bot_config = None      # Config instance
    shutdown_event = None  # threading.Event
    version = ""
    data_dir = None        # Path to data directory (for trade queries)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        if path in ("/health", "/api/health"):
            data = self.health_state.to_dict() if self.health_state else {"status": "unknown"}
            self._json_response(200, data)
        elif path == "/api/status":
            health = self.health_state.to_dict() if self.health_state else {"status": "unknown"}
            state = {}
            if self.bot_state:
                try:
                    state = self.bot_state.to_dashboard_dict()
                except Exception:
                    state = {}
            strategies = {}
            if self.bot_config:
                for name, scfg in self.bot_config.strategies.items():
                    if scfg.enabled:
                        strategies[name] = {
                            "markets": scfg.markets,
                        }
            self._json_response(200, {
                "health": health,
                "state": state,
                "strategies": strategies,
                "version": self.version,
            })
        elif path == "/api/trades":
            self._handle_trades(params)
        elif path == "/api/logs":
            self._handle_logs(params)
        else:
            self.send_response(404)
            self.end_headers()

    def _handle_trades(self, params: dict):
        """Serve recent trades from SQLite (read-only)."""
        import glob
        import os
        import sqlite3

        limit = int(params.get("limit", [100])[0])
        data_dir = self.data_dir
        if not data_dir:
            self._json_response(200, [])
            return

        # Scan all DBs: rotated + current
        db_files = sorted(glob.glob(os.path.join(str(data_dir), "bot_*.db")))
        current = os.path.join(str(data_dir), "bot.db")
        if os.path.exists(current):
            db_files.append(current)

        trades = []
        for db_file in db_files:
            try:
                conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=2)
                conn.row_factory = sqlite3.Row
                rows = conn.execute("SELECT * FROM trades ORDER BY id").fetchall()
                conn.close()
                for r in rows:
                    d = dict(r)
                    d["redeemed"] = bool(d.get("redeemed"))
                    extras_raw = d.pop("extras", None)
                    if extras_raw:
                        try:
                            d.update(json.loads(extras_raw))
                        except (json.JSONDecodeError, TypeError):
                            pass
                    trades.append(d)
            except Exception:
                continue

        # Sort by time, return last N
        trades.sort(key=lambda t: t.get("sniped_at") or "")
        self._json_response(200, trades[-limit:])

    def _handle_logs(self, params: dict):
        """Serve recent log lines from bot.log."""
        import os
        lines_count = int(params.get("lines", [20])[0])
        data_dir = self.data_dir
        if not data_dir:
            self._json_response(200, {"lines": []})
            return
        log_file = os.path.join(str(data_dir), "bot.log")
        if not os.path.exists(log_file):
            self._json_response(200, {"lines": []})
            return
        try:
            with open(log_file) as f:
                all_lines = f.read().splitlines()
            self._json_response(200, {"lines": all_lines[-lines_count:]})
        except Exception:
            self._json_response(200, {"lines": []})

    def do_POST(self):
        if self.path == "/api/stop":
            if self.shutdown_event:
                self.shutdown_event.set()
            self._json_response(200, {"status": "stopping"})
        else:
            self.send_response(404)
            self.end_headers()

    def _json_response(self, code: int, data: dict):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # suppress HTTP access logs


def start_api_server(health_state, bot_state, shutdown_event, version, data_dir=None, config=None, port=8080, bind=""):
    """Start the API server in a background thread.

    bind defaults to TIMBA_BIND env var, or 127.0.0.1 if unset.
    """
    import os
    if not bind:
        bind = os.environ.get("TIMBA_BIND", "127.0.0.1")
    BotAPIHandler.health_state = health_state
    BotAPIHandler.bot_state = bot_state
    BotAPIHandler.bot_config = config
    BotAPIHandler.shutdown_event = shutdown_event
    BotAPIHandler.version = version
    BotAPIHandler.data_dir = data_dir
    server = HTTPServer((bind, port), BotAPIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("API server started on %s:%d", bind, port)
    return server
