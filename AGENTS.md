# AGENTS.md

## Project Overview

Timba is a multi-strategy automated trading bot for Polymarket's recurring crypto Up/Down binary markets. Pluggable strategy framework where each strategy is a Python file in `strategies/` that auto-registers. The trader is strategy-agnostic -- it discovers markets, records ticks, and delegates all decisions to strategies.

Resolution source: Chainlink data streams (BTC/USD, ETH/USD, etc.) -- NOT Coinbase directly. Coinbase is the signal proxy.

## Architecture

**Bot/CLI split**: `timba start` runs the bot (HTTP API on :8080). CLI commands (`timba status`, `timba stop`) talk to it via the API. Bot writes `~/.timba/bot.json` with PID/port on startup; CLI auto-discovers from it.

**Key modules:**
- `trader.py` -- main eval loop (1s), background thread orchestration
- `server.py` -- HTTP API (health, status, trades, logs, stop)
- `client.py` -- CLI client for the HTTP API
- `strategies/` -- pluggable strategies (auto-discovered from YAML keys)
- `db.py` -- SQLite storage (single writer thread, non-blocking queue)
- `orders.py` -- order execution, cash lock, CLOB placement
- `discovery.py` -- Gamma API market polling
- `resolution.py` -- CLOB resolution, trade recording
- `tick_recorder.py` -- continuous market data capture to SQLite

**Threading model**: main loop does zero HTTP/disk I/O. All network I/O in background threads (MarketCache, PriceFeed, Discovery, Resolution, TickRecorder). All SQLite writes go through a non-blocking queue drained by a single DB writer thread.

**Data flow**: ticks.id <- evs.tick_id <- trades.ev_id (full traceability chain)

## Build & Test

```bash
make test      # uv run pytest with 75% coverage minimum
make build     # runs tests, then builds binary via PyInstaller
make install   # build + copy to ~/.local/bin/ + config to ~/.timba/
make docker    # build Docker image
make clean     # remove build artifacts
```

Dev run: `uv run timba start --config ./config.yaml`

## Code Conventions

- **All config from config.yaml** -- NEVER hardcode default values in function signatures, constants, or code. The config is the single source of truth. Dataclass fields use sentinel `-1.0` if Python requires a default.
- **Tests required** -- CI enforces 75% minimum coverage. Add tests with new code.
- **Strategy framework** -- add `strategies/mystrat.py`, implement `Strategy` ABC, call `register()` at module level, add `mystrat:` section to config.yaml. Auto-discovered, auto-validated.
- **Schema validation** -- config validated via JSON Schema on every load. Base schema in `config.schema.yaml`, strategy fields injected from `config_schema()`.
- Use `mode: live` not `mode: on` -- YAML parses `on` as boolean `True`.
- **Type annotations required** -- ruff enforces `ANN` rules. All function arguments and return types must be annotated.
- **Error return conventions**:
  - `None` — "no data available" (e.g. `get_midpoint() -> float | None`, `get_direction() -> Signal | None`)
  - `False` — "operation refused" (e.g. `deduct_cash() -> bool`, `check_liquidity() -> bool`)
  - `(0, 0)` — "no result but not an error" (e.g. `poll_order_fill() -> tuple[float, float]`)
  - `[]` — "empty collection" (e.g. `load_trades() -> list[dict]`)
  - `raise` — "programmer error / system broken" (e.g. `db.init()` not called)
  - Never mix patterns within the same function. Type hints make the convention explicit.

## Security Rules

- **NEVER** commit real private keys, wallet addresses, or API keys. Leaked credentials can drain the wallet instantly.
- Always use hardhat/dummy values in tests:
  - Private key: `0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80`
  - Address: `0xDeaDbeefdEAdbeefdEadbEEFdeadbeEFdEaDbeeF`
- All secrets from environment variables only -- `~/.timba/.env`
- Pre-commit hook blocks leaked keys.

## Data Integrity

- `~/.timba/` is the home directory (override: `TIMBA_HOME`)
- SQLite (WAL mode) for ticks/evs/trades, in-memory State for portfolio/cash
- **NEVER** set `BOT_ENV=main` when testing -- `data/main/` is live production data
- **NEVER** delete data directories without explicit permission
- DB rotates daily; rotated files kept for backtest

## Key Files

| File | Purpose |
|------|---------|
| `strategies/__init__.py` | Strategy ABC, TickData, BetDecision, registry |
| `strategies/favorite.py` | Buy near-certain side (>=$0.98), always sets side for tracking |
| `trader.py` | Main loop + background thread orchestration |
| `server.py` | HTTP API server (health, status, trades, logs, stop) |
| `client.py` | CLI client for HTTP API |
| `orders.py` | OrderManager: cash lock, execute_bet, CLOB placement |
| `db.py` | SQLite: single writer thread, non-blocking queue, WAL mode |
| `config.py` | StrategyConfig auto-discovery from YAML |
| `schema.py` | Dynamic JSON Schema builder + validator |
| `cli.py` | CLI entry point (subcommands: start, stop, status, init) |
