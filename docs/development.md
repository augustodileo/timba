# Development

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (manages Python version and dependencies)
- Git

## Setup

```bash
git clone https://github.com/augustodileo/timba.git
cd timba
uv sync --all-extras          # install all dependencies
```

## Running

```bash
uv run timba start                          # auto-inits ~/.timba/ on first run
uv run timba start --config ./config.yaml   # use repo's default config
uv run timba --check-wallet                 # verify credentials without starting
uv run timba --test-live                    # place+cancel test order on CLOB
```

## Testing

```bash
make test                                   # full test suite with 75% coverage gate
uv run pytest tests/ -v                     # without coverage
uv run pytest tests/ -v -k "test_monitor"   # specific test
uv run pytest tests/ -v --cov=timba --cov-report=term-missing
```

CI enforces 75% minimum coverage. Always add tests with new code.

## Building

```bash
make build     # runs tests, then builds standalone binary (PyInstaller -> dist/timba)
make install   # build + copy to ~/.local/bin/ + config to ~/.timba/
make docker    # build Docker image
make package   # build + tar.gz for CI release
make clean     # remove build artifacts
```

Version is derived from `git describe --tags --always`.

## Adding a new strategy

1. Create `src/timba/strategies/mystrat.py`:

```python
from timba.strategies import Strategy, TickData, BetDecision, register
from timba.base import MarketPosition

class MyStrategy(Strategy):
    name = "mystrat"

    @classmethod
    def config_schema(cls) -> dict:
        return {
            "strategy": {
                "threshold": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "market": {
                "required": ["lookback_sec"],
                "properties": {
                    "lookback_sec": {"type": "integer", "minimum": 0},
                },
            },
        }

    def create_position(self, market, market_cfg, global_cfg):
        # Return a MarketPosition subclass
        ...

    def evaluate(self, pos, tick):
        # Return BetDecision(should_bet=True/False, side, price, size, reason, computed)
        ...

    def on_bet(self, pos, decision):
        # Store decision-time values on position
        ...

    def resolve(self, pos, won):
        # Calculate PnL, set pos.pnl
        ...

    def extra_fields(self, pos):
        # Strategy-specific fields for trade record
        return {}

register(MyStrategy())
```

2. Add to `config.yaml`:

```yaml
mystrat:
  enabled: true
  threshold: 0.5
  markets:
    - coin: btc
      interval: 5m
      mode: paper
      entry_window_sec: 60
      close_window_sec: 10
      lookback_sec: 120
```

3. Done. The strategy is auto-discovered from the YAML key, auto-wired, and auto-validated against the schema.

Common market fields (`coin`, `interval`, `mode`, `entry_window_sec`, `close_window_sec`) are added to every strategy automatically. Only declare strategy-specific fields in `config_schema()`.

## Backtest

```bash
uv run timba --backtest --source-env main --strategy favorite
uv run timba --backtest --source-env main --strategy favorite --since 2026-03-30
uv run timba --analyze-trades --backtest --strategy favorite
uv run timba --analyze-ticks --coin btc --interval 5m
uv run timba --backtest-clean
```

The backtest uses the same `evaluate()`, `on_bet()`, `resolve()` functions as the live bot. Only input source (SQLite copy vs cache), fill (instant vs CLOB), and resolution (last tick vs CLOB poll) differ.

## Release process

Releases are triggered by pushing a git tag:

```bash
git tag v1.0.0
git push origin v1.0.0
```

This triggers the CD workflow which:
1. Runs the full test suite
2. Builds binaries for 4 platforms (linux-x64/arm64, darwin-x64/arm64)
3. Creates a GitHub Release with `.tar.gz` + `.sha256` per platform

Manual release: GitHub Actions -> CD -> Run workflow -> enter version.

### Release checklist

Before tagging:
1. Tests pass: `make test`
2. AGENTS.md is current
3. README.md is current
4. `config.yaml` (root) reflects any new/changed settings
5. Bot has run for at least 2 full market cycles with the new code

## Project structure

```
src/timba/
  strategies/          Strategy plugins (auto-discovered)
    __init__.py        Strategy ABC, TickData, BetDecision, registry
    favorite.py        Buy near-certain side (>=$0.98)
  trader.py            Main eval loop + background thread orchestration
  server.py            HTTP API server
  client.py            CLI client for HTTP API
  cli.py               CLI entry point (subcommands)
  db.py                SQLite storage layer
  orders.py            Order execution, cash lock, CLOB placement
  discovery.py         Gamma API market polling
  resolution.py        CLOB resolution, trade recording
  tick_recorder.py     Market data capture to SQLite
  market_cache.py      Background CLOB poller
  feed.py              Coinbase price feed
  base.py              MarketPosition state machine
  config.py            Config auto-discovery from YAML
  schema.py            JSON Schema builder + validator
  state.py             State persistence, cash/portfolio
  monitor.py           Rich terminal dashboard
  reconcile.py         Startup reconciliation with CLOB
```

See also: [Architecture](architecture.md) | [Configuration](configuration.md) | [API Reference](api.md)
