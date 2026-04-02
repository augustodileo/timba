# Timba

Automated trading bot for [Polymarket](https://polymarket.com)'s recurring crypto Up/Down binary markets.

## Get Started

### Requirements

- A [Polymarket](https://polymarket.com) account with trading enabled
- A wallet with USDC on Polygon
- `POLYMARKET_PRIVATE_KEY` — wallet private key
- `POLYMARKET_FUNDER` — Polymarket proxy wallet address
- `RELAYER_API_KEY` — relayer API key (optional — without it, wins must be redeemed manually)
- `RELAYER_API_KEY_ADDRESS` — relayer address (optional)

### Install

**Binary**

Installs a standalone binary and default config to `~/.timba/config.yaml`.

**Linux / macOS**

```bash
curl -fsSL https://raw.githubusercontent.com/augustodileo/timba/main/install.sh | sh
```

**Windows (PowerShell)**

```powershell
irm https://raw.githubusercontent.com/augustodileo/timba/main/install.ps1 | iex
```

Then start the bot:

```bash
timba start
```

On first run, `timba start` prompts for your credentials and saves them to `~/.timba/.env`. Credentials can also be set as environment variables to skip the prompt:

```bash
export POLYMARKET_PRIVATE_KEY=0x...
export POLYMARKET_FUNDER=0x...
export RELAYER_API_KEY=...              # optional
export RELAYER_API_KEY_ADDRESS=0x...    # optional
timba start
```

Starts trading in **paper mode** by default. To reconfigure credentials later, use `timba init`.

**Docker**

```bash
docker run \
  -e POLYMARKET_PRIVATE_KEY=0x... \
  -e POLYMARKET_FUNDER=0x... \
  -e RELAYER_API_KEY=... \
  -e RELAYER_API_KEY_ADDRESS=0x... \
  ghcr.io/augustodileo/timba:latest
```

Uses the default config (paper mode, all coins). Mount a custom config to override:

```bash
docker run \
  -e POLYMARKET_PRIVATE_KEY=0x... \
  -e POLYMARKET_FUNDER=0x... \
  -e RELAYER_API_KEY=... \
  -e RELAYER_API_KEY_ADDRESS=0x... \
  -v ./config.yaml:/app/config.yaml:ro \
  ghcr.io/augustodileo/timba:latest
```

**Upgrade**

Re-running the installer downloads the latest release. Config and data are preserved.

```bash
curl -fsSL https://raw.githubusercontent.com/augustodileo/timba/main/install.sh | sh
```

```powershell
irm https://raw.githubusercontent.com/augustodileo/timba/main/install.ps1 | iex
```

### Usage

Once the bot is running (binary or Docker):

```bash
timba status             # check if it's running
timba monitor            # live dashboard with trades and per-coin P&L
timba stop               # stop the bot
```

## Configuration

Edit `~/.timba/config.yaml` to set which coins, intervals, and mode:

```yaml
favorite:
  enabled: true
  min_price: 0.98
  contracts_per_trade: 5
  markets:
    - coin: btc
      interval: 5m
      mode: paper          # paper or live
      entry_window_sec: 15
      close_window_sec: 2
```

See [docs/configuration.md](docs/configuration.md) for the full reference.

## Commands

| Command | Description |
|---------|-------------|
| `timba start` | Start the bot (auto-init on first run) |
| `timba stop` | Stop the running bot |
| `timba status` | Show bot status and P&L |
| `timba monitor` | Live dashboard with trades and per-coin stats |
| `timba check-wallet` | Verify Polymarket credentials |
| `timba backtest` | Replay historical data through strategies |
| `timba analyze trades` | Trade history breakdown |
| `timba analyze ticks` | EV calibration check |
| `timba init` | Reconfigure credentials |

## Build from Source

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/augustodileo/timba.git
cd timba
make build               # runs tests, then builds dist/timba
make install             # builds + installs to ~/.local/bin/
make docker              # builds Docker image
```

See [docs/development.md](docs/development.md) for the full development guide.

## Documentation

- [Configuration Reference](docs/configuration.md)
- [Architecture](docs/architecture.md)
- [Development Guide](docs/development.md)
- [HTTP API](docs/api.md)
