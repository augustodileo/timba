# API Reference

Timba exposes an HTTP API on port 8080 (default) when the bot is running. The API is used by CLI commands and can be used by external tools.

## Connection

The bot writes `~/.timba/bot.json` on startup:
```json
{"pid": 12345, "port": 8080}
```

The CLI client (`BotClient`) reads this file to auto-discover the bot. You can also connect directly:

```python
from timba.client import BotClient

client = BotClient()                    # auto-discover from bot.json
client = BotClient(host="127.0.0.1", port=8080)  # explicit
```

Bind address defaults to `127.0.0.1` (localhost only). Override with `TIMBA_BIND` env var.

## Endpoints

### GET /api/health

Liveness check. Returns bot status and uptime.

**Response:**
```json
{
  "status": "ok",
  "uptime_seconds": 3600,
  "last_tick_seconds_ago": 1,
  "feed_healthy": true,
  "errors": 0
}
```

| Field | Type | Description |
|-------|------|-------------|
| `status` | string | `"ok"` if last tick < 10s ago, `"stale"` otherwise |
| `uptime_seconds` | integer | Seconds since bot started |
| `last_tick_seconds_ago` | integer | Seconds since last tick (-1 if no tick yet) |
| `feed_healthy` | boolean | Whether the price feed is responsive |
| `errors` | integer | Error count in current session |

This is a pure liveness check — no business data. Portfolio, cash, PnL, and trade stats are in `/api/status`.

**CLI usage:** `BotClient.health()` / `BotClient.is_running()`

Also available at `/health` (alias).

---

### GET /api/ready

Readiness check. Returns `200` if the bot is ready to trade (main loop ticking, feed healthy). Returns `503` if not ready. Use this in Kubernetes readiness probes or load balancer health checks.

**Response (ready):** `200 OK`
```json
{"ready": true}
```

**Response (not ready):** `503 Service Unavailable`
```json
{"ready": false}
```

Also available at `/ready` (alias).

---

### GET /api/status

Full bot status: health + portfolio state + strategies + version.

**Response:**
```json
{
  "health": {
    "status": "ok",
    "uptime_seconds": 3600,
    "last_tick_seconds_ago": 1,
    "feed_healthy": true,
    "errors": 0
  },
  "state": {
    "portfolio": 112.50,
    "cash": 92.50,
    "pending_redemption": 20.00,
    "total_pnl": 12.50,
    "started_at": "2026-03-30T10:00:00+00:00"
  },
  "strategies": {
    "favorite": {
      "markets": [
        {"coin": "btc", "interval": "5m", "mode": "paper", "entry_window_sec": 10, "close_window_sec": 3}
      ]
    }
  },
  "version": "v1.0.0"
}
```

| Top-level key | Source | Description |
|---------------|--------|-------------|
| `health` | `HealthState.to_dict()` | Same as `/api/health` response |
| `state` | `State.to_dashboard_dict()` | In-memory portfolio state |
| `strategies` | Config | Enabled strategies with their market configs |
| `version` | Build | Git version tag |

**Note:** `portfolio`, `cash`, and `total_pnl` appear in both `health` and `state`. The `state` object is the authoritative source — `health` mirrors these values for the liveness endpoint.

**CLI usage:** `BotClient.status()` -> `timba status`

---

### GET /api/trades

Recent trades from SQLite. Scans all DB files (rotated + current).

**Query parameters:**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | integer | 100 | Maximum number of trades to return |

**Response:** Array of trade objects, sorted by time (most recent last).

```json
[
  {
    "id": 7,
    "type": "paper_win",
    "strategy": "favorite",
    "slug": "btc-updown-5m-1774871400",
    "coin": "btc",
    "interval": "5m",
    "side": "up",
    "buy_price": 0.99,
    "contracts": 5,
    "pnl": 0.05,
    "sniped_at": "2026-03-29T12:55:00Z",
    "resolved_at": "2026-03-29T12:55:30Z",
    "market_mode": "paper",
    "ev_id": 108,
    "redeemed": false
  }
]
```

Strategy-specific fields from the `extras` JSON column are flattened into the top-level object.

**CLI usage:** `BotClient.trades(limit=100)` -> `timba status` (shows summary)

---

### GET /api/logs

Recent log lines from `bot.log`.

**Query parameters:**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `lines` | integer | 20 | Number of log lines to return |

**Response:**
```json
{
  "lines": [
    "12:55:00 INFO     BET ev #8501 | ETH 5m UP | UP @$0.9900 | 5 contracts | paper",
    "12:55:30 INFO     RESOLVE BET WIN ev #8501 | ETH 5m favorite | UP @$0.9900 | pnl=$+0.050"
  ]
}
```

**CLI usage:** `BotClient.logs(lines=20)`

---

### POST /api/stop

Graceful shutdown. Sets the shutdown event, which causes the main loop to exit cleanly.

**Request body:** Empty.

**Response:**
```json
{"status": "stopping"}
```

**CLI usage:** `BotClient.stop()` -> `timba stop`

## CLI commands and API mapping

| CLI command | API call | Description |
|-------------|----------|-------------|
| `timba start` | Starts the bot (no API call) | Run the bot process |
| `timba stop` | `POST /api/stop` | Graceful shutdown |
| `timba status` | `GET /api/status` + `GET /api/trades` | Show status and trade summary |
| `timba monitor` | `GET /api/status` + `GET /api/trades` (polling) | Live dashboard |
| `timba init` | None (local only) | Set up ~/.timba/ credentials |

## Error handling

- Unknown paths return `404` with no body.
- The API server suppresses HTTP access logs (no stdout noise).
- Read-only SQLite connections (`?mode=ro`) prevent accidental writes from the API thread.
- If `data_dir` is not set, trade/log endpoints return empty results.

See also: [Architecture](architecture.md) | [Configuration](configuration.md)
