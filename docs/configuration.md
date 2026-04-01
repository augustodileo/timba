# Configuration

Timba uses two config files: `config.yaml` for bot settings and `.env` for secrets.

## config.yaml

Location search order:
1. Explicit `--config` flag
2. `~/.timba/config.yaml` (binary install)
3. `./config.yaml` (dev, repo root)

### Global fields

| Field | Type | Description |
|-------|------|-------------|
| `log_level` | string | Python log level: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `polymarket.max_workers` | integer | Parallel CLOB requests for market cache polling (1-50) |

### Strategy sections

Each strategy has a top-level key matching its name. Reserved keys (not strategies): `log_level`, `polymarket`.

```yaml
favorite:
  enabled: true
  min_price: 0.98              # minimum midpoint to bet
  min_signal_chg: 0.15         # minimum Coinbase signal change % to act
  contracts_per_trade: 5       # contracts per order (min: 5)
  resolve_delay_sec: 30        # seconds to wait before resolving
  markets:
    - coin: btc
      interval: 5m
      mode: paper
      entry_window_sec: 15
      close_window_sec: 2
```

### Market fields (common to all strategies)

| Field | Type | Valid values |
|-------|------|-------------|
| `coin` | string | `btc`, `eth`, `sol`, `xrp`, `bnb`, `doge`, `hype` |
| `interval` | string | `5m`, `15m`, `1h`, `4h` |
| `mode` | string | `paper`, `live`, `off` |
| `entry_window_sec` | integer | Seconds before close to start evaluating |
| `close_window_sec` | integer | Seconds before close to stop evaluating |

**Important**: Use `mode: live` not `mode: on` -- YAML parses unquoted `on` as boolean `True`.

### Schema validation

Config is validated against a JSON Schema on every `Config.load()`. The schema is built dynamically:
- Base schema: `src/timba/config.schema.yaml` (global fields + shared `$defs`)
- Strategy schemas: each strategy declares its fields via `config_schema()`, injected at validation time

Catches: unknown keys, wrong types, invalid enums (`coin: bitcoin`), out-of-range values, missing required fields, unknown strategy sections.

## .env (secrets)

Location: `~/.timba/.env` (created by `timba init`, permissions `chmod 600`).

| Variable | Required | Description |
|----------|----------|-------------|
| `POLYMARKET_PRIVATE_KEY` | Yes | Wallet private key for signing orders |
| `POLYMARKET_FUNDER` | Yes | Polymarket proxy wallet address |
| `RELAYER_API_KEY` | No | For automatic position redemption |
| `RELAYER_API_KEY_ADDRESS` | No | Address associated with relayer key |

Without relayer keys, winning positions must be redeemed manually on Polymarket.

## Environment variables

| Variable | Description |
|----------|-------------|
| `TIMBA_HOME` | Override home directory (default: `~/.timba/`) |
| `BOT_ENV` | Override environment name (default: git branch name) |
| `TIMBA_BIND` | API server bind address (default: `127.0.0.1`) |
| `LOG_LEVEL` | Override log level from config |

## Adding a new market

Add an entry to the strategy's `markets:` list:

```yaml
favorite:
  markets:
    - coin: eth
      interval: 15m
      mode: paper          # start with paper, switch to live when confident
      entry_window_sec: 30
      close_window_sec: 2
```

The bot discovers markets automatically via the Gamma API. No token IDs or slugs needed.

See also: [Architecture](architecture.md) | [Development](development.md)
