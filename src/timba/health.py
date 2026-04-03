"""Liveness endpoint + log file handler for monitoring."""

import json
import logging
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

logger = logging.getLogger(__name__)

# Patterns to strip from logs before publishing
SENSITIVE_PATTERNS = [
    re.compile(r"0x[a-fA-F0-9]{40,}"),      # wallet addresses / tx hashes
    re.compile(r"USDC balance: \$[\d.]+"),    # balance
    re.compile(r"ip[=:]\s*[\d.]+"),           # IP addresses
    re.compile(r"Funder address: .+"),         # funder
    re.compile(r"PRIVATE_KEY"),                # key references
]


def sanitize_log_line(line: str) -> str:
    """Remove sensitive data from a log line."""
    for pattern in SENSITIVE_PATTERNS:
        line = pattern.sub("[REDACTED]", line)
    return line


class HealthState:
    """Liveness state — is the bot alive and its feeds working?

    This is NOT the dashboard. Portfolio, cash, PnL, and trade stats
    live in State.to_dashboard_dict() and are served via /api/status.

    Thread safety: _lock ensures the HTTP server reads a consistent
    snapshot while the main loop writes.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.started_at = time.time()
        self.last_tick = 0.0
        self.feed_healthy = True
        self.errors = 0

    def update(self, **kwargs: object) -> None:
        """Atomically update multiple fields. Called from main loop."""
        with self._lock:
            for k, v in kwargs.items():
                setattr(self, k, v)

    def is_ready(self) -> bool:
        """Readiness: main loop is ticking and feed is healthy."""
        with self._lock:
            if self.last_tick == 0:
                return False  # hasn't started yet
            return (time.time() - self.last_tick) < 10 and self.feed_healthy

    def to_dict(self) -> dict:
        with self._lock:
            now = time.time()
            return {
                "status": "ok" if (now - self.last_tick) < 10 else "stale",
                "uptime_seconds": int(now - self.started_at),
                "last_tick_seconds_ago": int(now - self.last_tick) if self.last_tick else -1,
                "feed_healthy": self.feed_healthy,
                "errors": self.errors,
            }


class HealthHandler(BaseHTTPRequestHandler):
    """HTTP handler for /health endpoint."""
    health_state: HealthState = None

    def do_GET(self) -> None:
        if self.path == "/health":
            data = self.health_state.to_dict() if self.health_state else {"status": "unknown"}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())
        elif self.path == "/ready":
            ready = self.health_state.is_ready() if self.health_state else False
            code = 200 if ready else 503
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ready": ready}).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass  # suppress HTTP access logs


class LogFileHandler(logging.Handler):
    """Logging handler that writes sanitized logs to a shared file.

    Keeps the last max_lines lines. Safe for the dashboard to read.
    """

    def __init__(self, path: Path, max_lines: int = 200) -> None:
        super().__init__()
        self.path = path
        self.max_lines = max_lines
        self._lines: list[str] = []
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
            line = sanitize_log_line(line)
            with self._lock:
                self._lines.append(line)
                if len(self._lines) > self.max_lines:
                    self._lines = self._lines[-self.max_lines:]
                self.path.write_text("\n".join(self._lines) + "\n")
        except Exception:
            pass


def start_health_server(health_state: HealthState, port: int = 8080, bind: str = "") -> HTTPServer:
    """Start the health HTTP server in a background thread.

    bind defaults to HEALTH_BIND env var, or 127.0.0.1 if unset.
    Docker should set HEALTH_BIND=0.0.0.0 for cross-container access.
    """
    import os
    if not bind:
        bind = os.environ.get("HEALTH_BIND", "127.0.0.1")
    HealthHandler.health_state = health_state
    server = HTTPServer((bind, port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("Health server started on port %d", port)
    return server
