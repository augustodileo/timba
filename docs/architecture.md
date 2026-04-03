# Architecture

## Bot/CLI split

Timba runs as a long-lived process (`timba start`) that exposes an HTTP API on port 8080. CLI commands talk to the running bot via this API.

```
timba start          -->  Bot process (HTTP API on :8080)
                          Writes ~/.timba/bot.json {pid, port}

timba status         -->  BotClient reads bot.json, calls GET /api/status
timba stop           -->  BotClient reads bot.json, calls POST /api/stop
timba monitor        -->  BotClient polls GET /api/status + /api/trades
```

The bot writes `~/.timba/bot.json` on startup (PID + port). The CLI auto-discovers the bot from this file. On shutdown, `bot.json` is removed.

## Threading model

```
Main loop (every 1s) -- ONLY evaluation + bet decisions:
  1. Drain mutation queue (apply new positions, state updates, trade records)
  2. Cleanup stale positions (markets ended 5+ min ago)
  3. Read tick data from _recorded_ticks (in-memory, same data as SQLite)
  4. strategy.evaluate(pos, tick) -> BetDecision
  5. Write EV to SQLite (non-blocking queue)
  6. If should_bet -> spawn order thread

Background threads:
  MarketCache     -- continuous CLOB polling (midpoints, fills, tick_size)
  PriceFeed       -- continuous Coinbase polling (signal direction, change%)
  TickRecorder    -- 0.5s: snapshot cache -> write tick to SQLite -> store in memory
  Discovery       -- 4min: Gamma API -> register new market positions
  Resolution      -- 2s: check CLOB midpoint -> determine win/loss -> queue trade record
  Scheduler       -- 5s: balance sync, redemption -> queue state writes
  DB writer       -- single thread drains write queue -> batched SQLite commits
  Order threads   -- one per bet: round price, check tick_size, fill or fail
  API server      -- HTTP server thread for CLI communication
```

The main loop does **zero HTTP calls and zero disk I/O**. All network I/O happens in background threads. All SQLite writes go through a non-blocking queue. The DB writer thread owns the only write connection.

## Data flow

```
ticks.id  <--  evs.tick_id    (which market snapshot the strategy evaluated)
evs.id    <--  trades.ev_id   (which EV triggered the trade)
trades.id                     (unique trade identifier)
```

Tick recorder writes snapshots to SQLite and stores them in memory. The eval loop reads from memory (not live cache) to guarantee the EV references the exact tick that was written to disk.

## HTTP API

See [API Reference](api.md) for full endpoint documentation.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/health` | GET | Liveness check (status, uptime, feed) |
| `/api/ready` | GET | Readiness check (200 if ready, 503 if not) |
| `/api/status` | GET | Full dashboard: health + state + strategies + version |
| `/api/trades` | GET | Recent trades from SQLite |
| `/api/logs` | GET | Recent log lines from bot.log |
| `/api/stop` | POST | Graceful shutdown |

## SQLite schema

Three tables with foreign key traceability:

**ticks** -- raw market data, one per market per ~0.5s

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment tick ID |
| ts | TEXT | ISO timestamp |
| slug | TEXT | Market slug |
| coin | TEXT | btc, eth, sol, etc. |
| interval | TEXT | 5m, 15m, 1h, 4h |
| mid_up/mid_down | REAL | CLOB midpoints |
| fill_up/fill_down | REAL | Fill prices |
| signal_dir | TEXT | Coinbase signal direction |
| signal_chg | REAL | Signal change % |
| signal_trend_sec | REAL | Signal trend duration |
| signal_rev | INTEGER | Signal reversal flag |
| price_open/price_now | REAL | Coinbase open/current price |

**evs** -- strategy evaluation results, one per strategy per tick

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment EV ID |
| tick_id | INTEGER FK | References ticks.id |
| slug | TEXT | Market slug |
| strategy | TEXT | Strategy name |
| remaining/progress | REAL | Window timing |
| ev_up/ev_down | REAL | Expected value per side |
| p_up/p_down | REAL | Probability estimates |
| extras | TEXT (JSON) | Strategy-specific fields |

**trades** -- trade outcomes

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment trade ID |
| type | TEXT | win, loss, paper_win, paper_loss, fail_win, fail_loss, skip_win, skip_loss, skip_none |
| strategy | TEXT | Strategy name |
| slug | TEXT | Market slug |
| ev_id | INTEGER FK | References evs.id |
| side | TEXT | up or down |
| buy_price | REAL | Fill price |
| contracts | INTEGER | Number of contracts |
| pnl | REAL | Profit/loss |
| market_mode | TEXT | paper or live |
| order_id | TEXT | CLOB order ID (live only) |
| extras | TEXT (JSON) | Strategy-specific fields |

**Storage details**: WAL journal mode, single writer thread (queue-based). DB rotates daily at midnight UTC or when exceeding 500MB. Rotated files named `bot_YYYY-MM-DD.db`, kept for backtest.

## Order lifecycle

```
Strategy returns BetDecision(price=0.985)
  -> spawn _handle_order thread
  -> round UP to nearest tick: $0.985 -> $0.99 (tick=0.01)
  -> check: $0.99 <= max $0.99? YES -> fill
     Paper: instant fill at rounded price
     Live: POST to CLOB (GTD, 65s expiry) -> poll fills -> SNIPED or SKIPPED
  -> if rounded > max: poll cache 10s for tick_size change -> FAILED if unchanged
```

## Trade outcomes

| Type | Meaning |
|------|---------|
| `paper_win/loss` | Paper bet filled, market resolved |
| `win/loss` | Live bet filled, real money |
| `fail_win/loss` | Wanted to bet, tick_size blocked fill |
| `skip_win/loss` | Below threshold, didn't bet (tracks what would have happened) |
| `skip_none` | No side picked (truly uncertain) |

See also: [API Reference](api.md) | [Configuration](configuration.md) | [Development](development.md)
