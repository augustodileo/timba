"""CLI entry point for timba."""

import argparse
import getpass
import json
import logging
import os
import sys
import time as _time
from pathlib import Path

import requests

# NO heavy imports at module level -- lazy import inside command handlers
from timba.version import get_version

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _timba_home() -> Path:
    """Return the timba home directory: TIMBA_HOME or ~/.timba/."""
    return Path(os.environ.get("TIMBA_HOME", "~/.timba")).expanduser()


def _template(name: str) -> str:
    """Read a template file bundled in the binary or from the repo root."""
    # PyInstaller: files embedded via --add-data
    if hasattr(sys, '_MEIPASS'):
        p = Path(sys._MEIPASS) / name
        if p.exists():
            return p.read_text()
    # Dev: repo root (cli.py is at src/timba/cli.py)
    repo = Path(__file__).resolve().parent.parent.parent
    p = repo / name
    if p.exists():
        return p.read_text()
    raise FileNotFoundError(f"Template not found: {name}")


def _resolve_config(explicit: str | None) -> Path:
    """Find config file. Search order: explicit flag, CWD, deploy/, ~/.timba/."""
    if explicit:
        p = Path(explicit)
        if p.exists():
            return p
        print(f"Error: config file not found: {p}", file=sys.stderr)
        sys.exit(1)

    home = _timba_home()
    config = home / "config.yaml"
    if config.exists():
        return config

    print(f"Error: config not found at {config}", file=sys.stderr)
    print("Run 'timba init' or 'timba start' to set up.", file=sys.stderr)
    sys.exit(1)


def _load_env():
    """Source ~/.timba/.env if secrets aren't already set."""
    if os.environ.get("POLYMARKET_PRIVATE_KEY"):
        return  # already in environment
    env_file = _timba_home() / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if key and value and key not in os.environ:
                os.environ[key] = value


def _detect_env() -> str:
    """Detect environment name: BOT_ENV > git branch > 'main'."""
    env = os.environ.get("BOT_ENV")
    if env:
        return env
    try:
        import subprocess
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            stderr=subprocess.DEVNULL, text=True,
        ).strip()
        if branch and branch != "HEAD":
            return branch
    except Exception:
        pass
    return "main"


def _data_dir() -> Path:
    """Return data directory: ~/.timba/data/{env}/."""
    env = _detect_env()
    d = _timba_home() / "data" / env
    d.mkdir(parents=True, exist_ok=True)
    return d


def _setup_logging(log_level: str):
    """Configure logging with the given level string."""
    level = getattr(logging, log_level.upper())
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    )
    for name in ("httpx", "httpcore", "urllib3", "requests",
                 "hpack", "h2", "h11", "polymarket_apis", "web3",
                 "websockets", "asyncio", "parso", "PIL"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _print_market_table(config):
    """Print the market mode table for each enabled strategy."""
    for name, scfg in config.strategies.items():
        if not scfg.enabled or not scfg.markets:
            continue
        markets = scfg.markets
        coins = sorted(set(m["coin"] for m in markets))
        intervals = sorted(set(m["interval"] for m in markets))

        sys.stdout.write(f"  {name} markets:\n")
        header = "  {:>6s}".format("")
        for iv in intervals:
            header += f"  {iv:>4s}"
        sys.stdout.write(header + "\n")

        for coin in coins:
            row = f"  {coin.upper():>6s}"
            for iv in intervals:
                match = next((m for m in markets if m["coin"] == coin and m["interval"] == iv), None)
                if match:
                    m_mode = str(match.get("mode", "live")).upper()
                    label = {"LIVE": "LIVE", "PAPER": " PPR", "OFF": " OFF"}.get(m_mode, f"{m_mode:>4}")
                    row += f"  {label}"
                else:
                    row += "   ---"
            sys.stdout.write(row + "\n")
        sys.stdout.write("\n")

    portfolio = config.calculate_portfolio()
    if portfolio > 0:
        sys.stdout.write(f"  Estimated capital: ${portfolio:.0f}\n")
    sys.stdout.write("\n" + "-" * 58 + "\n\n")
    sys.stdout.flush()


def _check_geoblock():
    """Check if the current IP is blocked by Polymarket. Exits if blocked."""
    try:
        resp = requests.get("https://polymarket.com/api/geoblock", timeout=10)
        data = resp.json()
        blocked = data.get("blocked", False)
        ip = data.get("ip", "?")
        country = data.get("country", "?")
        region = data.get("region", "?")
        print(f"  Geoblock: blocked={blocked} | ip={ip} | {country}/{region}")
        if blocked:
            print(f"\nERROR: Polymarket blocks trading from {country}/{region}.", file=sys.stderr)
            print("Move your bot to an allowed region.", file=sys.stderr)
            sys.exit(1)
    except Exception as e:
        print(f"  Geoblock check failed: {e} (continuing anyway)")


def _check_wallet(config):
    """Verify wallet credentials and print account info."""
    from polymarket_apis import PolymarketClobClient

    pk = config.polymarket.private_key
    funder = config.polymarket.funder

    if not pk:
        print("ERROR: POLYMARKET_PRIVATE_KEY not set", file=sys.stderr)
        print("  Set it as an environment variable or in .env", file=sys.stderr)
        sys.exit(1)

    _check_geoblock()

    print("Connecting to Polymarket CLOB...")
    try:
        client = PolymarketClobClient(
            private_key=pk,
            address=funder,
            signature_type=config.polymarket.signature_type,
        )
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
    except Exception as e:
        print(f"ERROR: Failed to create CLOB client: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        ok = client.get_ok()
        print(f"  API status: {ok}")
    except Exception as e:
        print(f"ERROR: API unreachable: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        usdc = client.get_usdc_balance()
        print(f"  USDC balance: ${usdc:.2f}")
    except Exception as e:
        print(f"  USDC balance: unavailable ({e})")

    print(f"  Funder address: {funder or '(not set -- will be derived)'}")

    try:
        orders = client.get_orders()
        print(f"  Open orders: {len(orders)}")
    except Exception as e:
        print(f"  Open orders: unavailable ({e})")

    print("\nWallet check passed. Ready for live trading.")


def _test_live_order(config):
    """Place a test order at $0.01 (will never fill), verify it exists, then cancel it."""
    from polymarket_apis import PolymarketClobClient
    from polymarket_apis.types.clob_types import OrderArgs

    from timba.market import MarketSeries, discover_active_markets

    pk = config.polymarket.private_key
    funder = config.polymarket.funder
    client = PolymarketClobClient(
        private_key=pk,
        address=funder,
        signature_type=config.polymarket.signature_type,
    )
    creds = client.create_or_derive_api_creds()
    client.set_api_creds(creds)

    print("\n=== Live Order Test ===")
    print("Finding an active market...")

    series = [MarketSeries("btc", "5m", 300)]
    markets = discover_active_markets(series)
    if not markets:
        series = [MarketSeries("btc", "15m", 900)]
        markets = discover_active_markets(series)
    if not markets:
        print("ERROR: No active markets found. Try again in a moment.", file=sys.stderr)
        sys.exit(1)

    market = markets[0]
    print(f"  Market: {market.slug}")
    print(f"  Token (UP): {market.token_id_up}")

    try:
        usdc = client.get_usdc_balance()
    except Exception:
        usdc = 0

    if usdc < 1:
        print(f"\n  USDC balance is ${usdc:.2f} -- skipping place+cancel test.")
        print("  Deposit USDC to Polymarket to run the full order lifecycle test.")
        print("\nWallet and API test passed. Deposit funds to test order placement.")
        return

    print("  Placing test order: 5 contracts @ $0.01 (will NOT fill)...")
    try:
        order_args = OrderArgs(
            token_id=market.token_id_up,
            price=0.01,
            size=5,
            side="BUY",
        )
        resp = client.create_and_post_order(order_args)
        if resp and not resp.success:
            print(f"  Order rejected: {resp.error_msg}")
            print("\nOrder placement test failed -- but API connection works.")
            return
        order_id = str(resp.order_id) if resp and resp.order_id else None
        print(f"  Order placed: {order_id} (status: {resp.status if resp else 'unknown'})")
    except Exception as e:
        print(f"ERROR: Failed to place order: {e}", file=sys.stderr)
        sys.exit(1)

    _time.sleep(2)
    print("  Checking order status...")
    try:
        orders = client.get_orders()
        found = any(str(o.order_id) == order_id for o in orders) if order_id else False
        print(f"  Open orders: {len(orders)} (ours found: {found})")
    except Exception as e:
        print(f"  Could not verify order: {e}")

    print("  Cancelling all orders...")
    try:
        client.cancel_all()
        print("  Cancelled.")
    except Exception as e:
        print(f"ERROR: Failed to cancel: {e}", file=sys.stderr)
        sys.exit(1)

    _time.sleep(1)
    try:
        orders = client.get_orders()
        print(f"  Open orders after cancel: {len(orders)}")
    except Exception as e:
        print(f"  Could not verify cancellation: {e}")

    print("\nLive order test passed. Full lifecycle works: place -> verify -> cancel.")


# ---------------------------------------------------------------------------
# Bot lifecycle helpers
# ---------------------------------------------------------------------------

def _write_bot_json(port: int):
    """Write bot.json with PID and port to ~/.timba/."""
    home = _timba_home()
    home.mkdir(parents=True, exist_ok=True)
    bot_json = home / "bot.json"
    bot_json.write_text(json.dumps({"pid": os.getpid(), "port": port}))


def _remove_bot_json():
    """Remove bot.json on shutdown."""
    try:
        bot_json = _timba_home() / "bot.json"
        if bot_json.exists():
            bot_json.unlink()
    except OSError:
        pass


def _ensure_initialized() -> bool:
    """Check if credentials are set up. Config comes from install.

    Returns True if .env exists OR credentials are already in environment
    (e.g. Docker -e POLYMARKET_PRIVATE_KEY=...).
    """
    if os.environ.get("POLYMARKET_PRIVATE_KEY"):
        return True
    home = _timba_home()
    return (home / ".env").exists()


def _prompt_env(env_path: Path):
    """Prompt for credentials and write .env."""
    print()
    pk = getpass.getpass("  Polymarket private key: ")
    funder = input("  Funder address: ").strip()

    content = f"""# Timba credentials
POLYMARKET_PRIVATE_KEY={pk}
POLYMARKET_FUNDER={funder}

# Optional -- wins must be redeemed manually without these
RELAYER_API_KEY=
RELAYER_API_KEY_ADDRESS=
"""
    env_path.write_text(content)
    env_path.chmod(0o600)
    print("  Saved .env (owner read+write only)")


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

# -- init ------------------------------------------------------------------

def cmd_init(args):
    """Set up credentials in ~/.timba/. Config is provided by the installer."""
    home = _timba_home()
    home.mkdir(parents=True, exist_ok=True)

    env_path = home / ".env"

    if env_path.exists():
        overwrite = input(f"  {env_path} already exists. Overwrite? [y/N] ").strip().lower()
        if overwrite != "y":
            print("  Kept existing .env")
        else:
            _prompt_env(env_path)
    else:
        _prompt_env(env_path)

    # Ensure config exists (install.sh puts it here, but for dev/manual installs fall back to template)
    config_path = home / "config.yaml"
    if not config_path.exists():
        config_path.write_text(_template("config.yaml"))
        print("  Created default config.yaml")

    (home / "data").mkdir(exist_ok=True)
    print(f"\n  Home: {home}")
    print(f"  Config: {config_path}")


# -- start -----------------------------------------------------------------

def cmd_start(args):
    """Start the bot. Auto-initializes if needed."""
    import atexit
    import signal
    import threading

    # Only auto-init when no explicit config is given (discovery mode)
    explicit_config = getattr(args, "config", None)
    if not explicit_config and not _ensure_initialized():
        print("  First run -- setting up ~/.timba/\n")
        cmd_init(args)
        print()

    from timba.config import Config
    from timba.schema import ConfigValidationError
    from timba.state import State

    config_path = _resolve_config(explicit_config)
    _load_env()

    try:
        config = Config.load(config_path)
    except ConfigValidationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    # Log level priority: CLI flag > env var > config.yaml
    log_level = getattr(args, "log_level", None) or os.environ.get("LOG_LEVEL") or config.log_level or "INFO"
    _setup_logging(log_level)

    data_dir = _data_dir()
    version = get_version()

    sys.stdout.write(f"\n{_banner()}  Polymarket Crypto Trading Bot  {version}\n\n")
    _print_market_table(config)

    logging.getLogger(__name__).info("Environment: %s -> %s", _detect_env(), data_dir)

    # Always connect to CLOB for real prices (needed even for paper markets)
    if not config.polymarket.private_key:
        print("ERROR: POLYMARKET_PRIVATE_KEY not set", file=sys.stderr)
        sys.exit(1)

    from polymarket_apis import PolymarketClobClient
    try:
        clob = PolymarketClobClient(
            private_key=config.polymarket.private_key,
            address=config.polymarket.funder,
            signature_type=config.polymarket.signature_type,
        )
        creds = clob.create_or_derive_api_creds()
        clob.set_api_creds(creds)
    except Exception as e:
        print(f"ERROR: Could not connect to CLOB: {e}", file=sys.stderr)
        sys.exit(1)

    # Initialize SQLite database and seed ID counters
    from timba import db
    from timba.state import init_trade_ids
    from timba.ticks import init_ids
    db.init(data_dir)
    init_ids()
    init_trade_ids()

    # Reconcile state with CLOB reality (sets authoritative cash)
    state = State()
    state.pending_redemption = db.get_pending_redemption()
    from timba.reconcile import reconcile_startup
    reconcile_startup(clob, state)
    state.init_portfolio(state.cash)

    from timba.trader import Trader
    trader = Trader(config, state, data_dir=data_dir)

    # Shutdown event for graceful stop via API
    shutdown_event = threading.Event()

    # Start API server
    port = getattr(args, "port", 8080) or 8080
    from timba.server import start_api_server
    api_server = start_api_server(trader.health, state, shutdown_event, version, data_dir=data_dir, config=config, port=port)

    # Write bot.json after API server is confirmed listening
    _write_bot_json(port)
    atexit.register(_remove_bot_json)

    # Sanitized log file for dashboard
    from timba.health import LogFileHandler
    log_handler = LogFileHandler(data_dir / "bot.log")
    log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(message)s", datefmt="%H:%M:%S"))
    logging.getLogger("timba").addHandler(log_handler)

    # Signal handlers to set shutdown event
    def _signal_handler(signum, frame):
        logging.getLogger(__name__).info("Received signal %d, shutting down...", signum)
        shutdown_event.set()

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    trader.run(shutdown_event=shutdown_event)

    # Cleanup
    api_server.shutdown()
    _remove_bot_json()


# -- stop ------------------------------------------------------------------

def cmd_stop(args):
    """Stop the running bot via HTTP API."""
    from timba.client import BotClient

    client = BotClient(host=args.host, port=args.port)
    if not client.is_running():
        print("Bot is not running (could not connect).", file=sys.stderr)
        sys.exit(1)
    try:
        resp = client.stop()
        print(f"Stop signal sent. Response: {resp.get('status', 'unknown')}")
    except Exception as e:
        print(f"Error sending stop: {e}", file=sys.stderr)
        sys.exit(1)


# -- status ----------------------------------------------------------------

def cmd_status(args):
    """Query running bot status via HTTP API."""
    from collections import Counter

    from timba.client import BotClient
    from timba.monitor import calc_pnl

    client = BotClient(host=args.host, port=args.port)
    if not client.is_running():
        print(f"{_banner()}Bot is not running")
        sys.exit(1)
    try:
        data = client.status()
        trades = client.trades(limit=10000)

        health = data.get("health", {})
        state = data.get("state", {})
        ver = data.get("version", "?")

        status = health.get("status", "unknown")
        uptime_s = health.get("uptime_seconds", 0)
        h, m = divmod(uptime_s // 60, 60)

        print(f"{_banner()}Version: {ver}")
        print(f"Status:  {status}  Uptime: {h}h{m:02d}m")
        print(f"Cash:    ${state.get('cash', 0):.2f}")
        print(f"Portfolio: ${state.get('portfolio', 0):.2f}")

        strategies = sorted(set(t.get("strategy", "") for t in trades if t.get("strategy")))
        for sname in strategies:
            strades = [t for t in trades if t.get("strategy") == sname]
            type_counts = Counter(t.get("type", "") for t in strades)

            print(f"\n{sname.upper()}")

            # Live
            w = type_counts.get("win", 0)
            lo = type_counts.get("loss", 0)
            if w + lo > 0:
                live_pnl = sum(calc_pnl(t) for t in strades if t.get("type") in ("win", "loss"))
                sum(type_counts.get(k, 0) for k in ("fail_win", "fail_loss") if any(t.get("market_mode") == "live" for t in strades if t.get("type") == k))
                print("  Live")
                print(f"    PnL:   ${live_pnl:+.3f}")
                print(f"    Bets:  {w}W/{lo}L")

            # Paper
            pw = type_counts.get("paper_win", 0)
            pl = type_counts.get("paper_loss", 0)
            if pw + pl > 0:
                paper_pnl = sum(calc_pnl(t) for t in strades if t.get("type", "").startswith("paper"))
                print("  Paper")
                print(f"    PnL:   ${paper_pnl:+.3f}")
                print(f"    Bets:  {pw}W/{pl}L {pw/max(1,pw+pl)*100:.0f}%")

            fw = type_counts.get("fail_win", 0)
            fl = type_counts.get("fail_loss", 0)
            if fw + fl > 0:
                print(f"    Fails: {fw}W/{fl}L")

            sw = type_counts.get("skip_win", 0)
            sl = type_counts.get("skip_loss", 0)
            ss = type_counts.get("skip_none", 0)
            if sw + sl + ss > 0:
                skip_line = f"    Skips: {sw}W/{sl}L"
                if ss:
                    skip_line += f" +{ss}S"
                print(skip_line)

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


# -- monitor ---------------------------------------------------------------

def cmd_monitor(args):
    """Live dashboard — polls bot API and renders with Rich."""
    import time
    from collections import Counter
    from datetime import datetime, timezone

    from rich.columns import Columns
    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table

    from timba.client import BotClient
    from timba.monitor import IV_ORDER, calc_pnl, fmt_pnl, fmt_time, parse_slug

    client = BotClient(host=args.host, port=args.port)
    interval = args.interval
    console = Console()

    def render():
        banner = _banner().rstrip()

        if not client.is_running():
            return Panel(f"[dim]{banner}[/]\n\n[red]Bot is not running[/]", title="Timba")

        try:
            data = client.status()
            trades = client.trades(limit=10000)
        except Exception as e:
            return Panel(f"[dim]{banner}[/]\n\n[red]Error: {e}[/]", title="Timba")

        health = data.get("health", {})
        state = data.get("state", {})
        ver = data.get("version", "?")

        # ── Left panel: banner + overview + recent trades ──
        lines = []
        lines.append(f"[dim]{banner}[/]")
        lines.append("")
        lines.append(f"Version: {ver}")
        lines.append("")
        portfolio = state.get("portfolio", 0)
        cash = state.get("cash", 0)
        pending = state.get("pending_redemption", 0)
        pend = f"  Pend: ${pending:.2f}" if pending else ""
        lines.append(f"Portfolio: ${portfolio:.2f}")
        lines.append(f"Cash:      ${cash:.2f}{pend}")

        # Per-strategy summary (from config, with trade data overlaid)
        config_strategies = data.get("strategies", {})
        trade_strategies = set(t.get("strategy", "") for t in trades if t.get("strategy"))
        strategy_names = sorted(set(config_strategies.keys()) | trade_strategies)
        for sname in strategy_names:
            strades = [t for t in trades if t.get("strategy") == sname]
            type_counts = Counter(t.get("type", "") for t in strades)

            pw = type_counts.get("paper_win", 0)
            pl = type_counts.get("paper_loss", 0)
            w = type_counts.get("win", 0)
            lo = type_counts.get("loss", 0)
            paper_pnl = sum(calc_pnl(t) for t in strades if t.get("type", "").startswith("paper"))
            live_pnl = sum(calc_pnl(t) for t in strades if t.get("type") in ("win", "loss"))

            fw = type_counts.get("fail_win", 0) + type_counts.get("fail_loss", 0)
            sw = sum(type_counts.get(k, 0) for k in ("skip_win", "skip_loss", "skip_none"))

            def _pnl_rate(filtered, total):
                if not filtered:
                    return ""
                first = min((t.get("sniped_at", "") for t in filtered), default="")
                if not first:
                    return ""
                try:
                    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(first)).total_seconds() / 60
                    if elapsed > 1:
                        rate = total / (elapsed / 60)
                        c = "green" if rate >= 0 else "red"
                        return f" [{c}]${rate:+.2f}/h[/]"
                except (ValueError, TypeError):
                    pass
                return ""

            lines.append("")
            lines.append(f"[bold]{sname.upper()}[/]")

            if w + lo > 0:
                lines.append("  [dim]── LIVE ──[/]")
                color = "green" if live_pnl >= 0 else "red"
                rate = _pnl_rate([t for t in strades if t.get("type") in ("win", "loss")], live_pnl)
                lines.append(f"  Bets:   {w}W/{lo}L {w/max(1,w+lo)*100:.0f}% [{color}]${live_pnl:+.3f}[/]{rate}")

            lines.append("  [dim]── PAPER ──[/]")
            color = "green" if paper_pnl >= 0 else "red"
            rate = _pnl_rate([t for t in strades if t.get("type", "").startswith("paper")], paper_pnl)
            lines.append(f"  Bets:   {pw}W/{pl}L [{color}]${paper_pnl:+.3f}[/]{rate}")
            pfw = sum(1 for t in strades if t.get("type") == "fail_win")
            pfl = sum(1 for t in strades if t.get("type") == "fail_loss")
            lines.append(f"  Fails:  {pfw}W/{pfl}L")
            psw = sum(1 for t in strades if t.get("type") == "skip_win")
            psl = sum(1 for t in strades if t.get("type") == "skip_loss")
            pss = type_counts.get("skip_none", 0)
            skip_line = f"  Skips:  {psw}W/{psl}L"
            if pss:
                skip_line += f" +{pss}S"
            lines.append(skip_line)

        # Recent trades
        def _fmt_trade(t):
            won = t["type"].endswith("_win") or t["type"] == "win"
            r = "[green]W[/]" if won else "[red]L[/]"
            _coin, _iv = parse_slug(t.get("slug", ""))
            side = t.get("side", "-")[0].upper() if t.get("side") else "-"
            fill = t.get("buy_price", 0)
            p = calc_pnl(t)
            sn = t.get("strategy", "?")[:4]
            ts = fmt_time(t.get("resolved_at") or t.get("sniped_at") or "")
            color = "green" if p >= 0 else "red"
            return f"{sn} {r} %-4s %3s %s $%.4f [{color}]%+.3f[/]   %s" % (_coin.upper(), _iv, side, fill, p, ts)

        # ── Right panel: per-coin/interval table (build first to measure height) ──
        # Seed from config so table shows even with zero trades
        coin_iv_trades = {}
        for scfg in config_strategies.values():
            for m in scfg.get("markets", []):
                key = (m.get("coin", ""), m.get("interval", ""))
                if key[0] and key[1]:
                    coin_iv_trades.setdefault(key, [])
        for t in trades:
            coin, iv = parse_slug(t.get("slug", ""))
            if coin and iv:
                coin_iv_trades.setdefault((coin, iv), []).append(t)

        sorted_keys = sorted(coin_iv_trades.keys(), key=lambda k: (k[0], IV_ORDER.get(k[1], 9)))

        # Build mode lookup from config
        mode_lookup = {}
        for scfg in config_strategies.values():
            for m in scfg.get("markets", []):
                mode_lookup[(m.get("coin", ""), m.get("interval", ""))] = m.get("mode", "paper")

        # Detect which modes exist from config
        has_live = any(mode_lookup.get(k) == "live" for k in sorted_keys)
        has_paper = any(mode_lookup.get(k, "paper") == "paper" for k in sorted_keys)

        if sorted_keys:
            strat_title = ", ".join(s.upper() for s in strategy_names) or "TRADES"
            table = Table(title=f"[bold]{strat_title}[/]", border_style="cyan", show_lines=False, pad_edge=False)
            table.add_column("COIN", style="bold", width=5)
            table.add_column("INT", width=4)
            table.add_column("MODE", width=5)
            if has_live:
                table.add_column("Live Bets", width=11)
                table.add_column("Live Fails", width=11)
                table.add_column("Live Skips", width=11)
                table.add_column("Live PnL", width=11, justify="right")
            if has_paper:
                table.add_column("Paper Bets", width=11)
                table.add_column("Paper Fails", width=11)
                table.add_column("Paper Skips", width=11)
                table.add_column("Paper PnL", width=11, justify="right")

            ncols = 3 + (4 if has_live else 0) + (4 if has_paper else 0)
            live_t = {"bw": 0, "bl": 0, "fw": 0, "fl": 0, "sw": 0, "sl": 0, "pnl": 0.0}
            paper_t = {"bw": 0, "bl": 0, "fw": 0, "fl": 0, "sw": 0, "sl": 0, "pnl": 0.0}
            prev_coin = None

            def _wl(w, l):
                return f"{w}W/{l}L" if w + l > 0 else "—"

            for coin, iv in sorted_keys:
                ct = coin_iv_trades[(coin, iv)]
                if prev_coin and prev_coin != coin:
                    table.add_row(*[""] * ncols)
                prev_coin = coin

                mode = mode_lookup.get((coin, iv), "paper")
                is_paper = mode == "paper"
                mode_label = f"[green]{mode}[/]" if mode == "live" else f"[dim]{mode}[/]"

                if is_paper:
                    bw = sum(1 for t in ct if t.get("type") == "paper_win")
                    bl = sum(1 for t in ct if t.get("type") == "paper_loss")
                    bpnl = sum(calc_pnl(t) for t in ct if t.get("type") in ("paper_win", "paper_loss"))
                else:
                    bw = sum(1 for t in ct if t.get("type") == "win")
                    bl = sum(1 for t in ct if t.get("type") == "loss")
                    bpnl = sum(calc_pnl(t) for t in ct if t.get("type") in ("win", "loss"))

                fw = sum(1 for t in ct if t.get("type") == "fail_win")
                fl = sum(1 for t in ct if t.get("type") == "fail_loss")
                sw = sum(1 for t in ct if t.get("type") == "skip_win")
                sl = sum(1 for t in ct if t.get("type") in ("skip_loss", "skip_none"))

                totals = paper_t if is_paper else live_t
                totals["bw"] += bw; totals["bl"] += bl
                totals["fw"] += fw; totals["fl"] += fl
                totals["sw"] += sw; totals["sl"] += sl
                totals["pnl"] += bpnl

                bets_s = _wl(bw, bl)
                fails_s = _wl(fw, fl)
                skips_s = _wl(sw, sl) if sw + sl > 0 else "—"
                pnl_s = fmt_pnl(bpnl) if bw + bl > 0 else "—"

                row = [coin.upper(), iv, mode_label]
                if has_live:
                    row += [bets_s, fails_s, skips_s, pnl_s] if not is_paper else ["—", "—", "—", "—"]
                if has_paper:
                    row += [bets_s, fails_s, skips_s, pnl_s] if is_paper else ["—", "—", "—", "—"]
                table.add_row(*row)

            table.add_section()
            total_row = ["[bold]TOTAL[/]", "", ""]
            if has_live:
                total_row += [
                    f"[bold]{_wl(live_t['bw'], live_t['bl'])}[/]",
                    f"[bold]{_wl(live_t['fw'], live_t['fl'])}[/]",
                    f"[bold]{_wl(live_t['sw'], live_t['sl'])}[/]",
                    fmt_pnl(live_t["pnl"]),
                ]
            if has_paper:
                total_row += [
                    f"[bold]{_wl(paper_t['bw'], paper_t['bl'])}[/]",
                    f"[bold]{_wl(paper_t['fw'], paper_t['fl'])}[/]",
                    f"[bold]{_wl(paper_t['sw'], paper_t['sl'])}[/]",
                    fmt_pnl(paper_t["pnl"]),
                ]
            table.add_row(*total_row)

            # Measure actual rendered table height
            _measure_console = Console(file=__import__("io").StringIO(), width=120)
            with _measure_console.capture() as _cap:
                _measure_console.print(table)
            table_height = _cap.get().count("\n")
        else:
            table_height = 0

        # Fill recent trades so overview matches table height
        # Panel adds 2 lines (top/bottom border), so target content = table_height - 2
        target_content = max(15, table_height - 2) if table_height else 15
        available = target_content - len(lines)

        paper_trades = [t for t in trades if t.get("type") in ("paper_win", "paper_loss")]
        paper_trades.sort(key=lambda t: t.get("resolved_at") or t.get("sniped_at") or "", reverse=True)
        live_trades = [t for t in trades if t.get("type") in ("win", "loss")]
        live_trades.sort(key=lambda t: t.get("resolved_at") or t.get("sniped_at") or "", reverse=True)

        if live_trades and available > 2:
            lines.append("")
            lines.append("[bold]─── LIVE ───[/]")
            n = min(len(live_trades), (available - 4) // 2) if paper_trades else min(len(live_trades), available - 2)
            for t in live_trades[:max(1, n)]:
                lines.append(_fmt_trade(t))
            available = target_content - len(lines)

        lines.append("")
        lines.append("[bold]─── PAPER ───[/]")
        if paper_trades and available > 2:
            for t in paper_trades[:max(1, available - 2)]:
                lines.append(_fmt_trade(t))
        else:
            lines.append("[dim]No trades yet[/]")

        now = datetime.now().strftime("%d-%m-%Y %H:%M:%S")
        status = health.get("status", "unknown")
        status_icon = "[green]●[/]" if status == "ok" else "[yellow]●[/]" if status == "stale" else "[red]●[/]"
        overview = Panel("\n".join(lines), title=f"{status_icon} [bold]MONITOR[/]  {now}", border_style="blue",
                         height=table_height if table_height else None)

        if sorted_keys:
            top_row = Columns([overview, table], padding=(0, 1), expand=False)
        else:
            top_row = overview

        # Logs section
        try:
            log_lines = client.logs(lines=15)
        except Exception:
            log_lines = []

        log_panel = None
        if log_lines:
            from rich.text import Text
            log_text = Text()
            for line in log_lines:
                if " ERROR " in line or "Traceback" in line:
                    log_text.append(line + "\n", style="red")
                elif " WARNING " in line:
                    log_text.append(line + "\n", style="yellow")
                else:
                    log_text.append(line + "\n", style="dim")
            log_panel = Panel(log_text, title="[dim]Logs[/]", border_style="dim")

        if log_panel:
            return Group(top_row, log_panel)
        return top_row

    try:
        with Live(render(), console=console, screen=True, auto_refresh=False) as live:
            while True:
                time.sleep(interval)
                live.update(render(), refresh=True)
    except KeyboardInterrupt:
        pass


# -- check-wallet ----------------------------------------------------------

def cmd_check_wallet(args):
    """Verify wallet credentials (no bot needed)."""
    from timba.config import Config
    from timba.schema import ConfigValidationError

    config_path = _resolve_config(args.config)
    _load_env()
    try:
        config = Config.load(config_path)
    except ConfigValidationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    _check_wallet(config)


# -- test-live --------------------------------------------------------------

def cmd_test_live(args):
    """Test the full order lifecycle (no bot needed)."""
    from timba.config import Config
    from timba.schema import ConfigValidationError

    config_path = _resolve_config(args.config)
    _load_env()
    try:
        config = Config.load(config_path)
    except ConfigValidationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    _check_wallet(config)
    _test_live_order(config)


# -- backtest ---------------------------------------------------------------

def cmd_backtest(args):
    """Run full backtest from historical tick data."""
    from timba.config import Config
    from timba.schema import ConfigValidationError

    config_path = _resolve_config(args.config)
    _load_env()
    try:
        config = Config.load(config_path)
    except ConfigValidationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    # Minimal logging for backtest
    log_level = os.environ.get("LOG_LEVEL") or config.log_level or "INFO"
    _setup_logging(log_level)

    data_dir = _data_dir()
    bt_dir = data_dir / "backtest"

    from timba.backtest import backtest_main
    backtest_main(config, bt_dir, source_env=args.source_env, strategy=args.strategy, since=args.since)


# -- analyze trades ---------------------------------------------------------

def cmd_analyze_trades(args):
    """Analyze trade results: price distribution, PnL breakdowns."""
    from timba.config import Config
    from timba.schema import ConfigValidationError

    config_path = _resolve_config(args.config)
    _load_env()
    try:
        config = Config.load(config_path)
    except ConfigValidationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    log_level = os.environ.get("LOG_LEVEL") or config.log_level or "INFO"
    _setup_logging(log_level)

    data_dir = _data_dir()
    target_dir = (data_dir / "backtest") if args.backtest else data_dir

    from timba.backtest.analyze_trades import analyze_main
    analyze_main(target_dir, strategy=args.strategy)


# -- analyze ticks ----------------------------------------------------------

def cmd_analyze_ticks(args):
    """Analyze tick EVs vs actual trade outcomes."""
    from timba.config import Config
    from timba.schema import ConfigValidationError

    config_path = _resolve_config(args.config)
    _load_env()
    try:
        config = Config.load(config_path)
    except ConfigValidationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    log_level = os.environ.get("LOG_LEVEL") or config.log_level or "INFO"
    _setup_logging(log_level)

    data_dir = _data_dir()

    from timba.backtest import analyze_ticks_main
    analyze_ticks_main(data_dir, coin=args.coin, interval=args.interval, strategy=args.strategy)


# -- backtest-clean ---------------------------------------------------------

def cmd_backtest_clean(args):
    """Delete backtest data directory."""
    import shutil

    from timba.config import Config
    from timba.schema import ConfigValidationError

    config_path = _resolve_config(args.config)
    _load_env()
    try:
        Config.load(config_path)
    except ConfigValidationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    data_dir = _data_dir()
    bt_dir = data_dir / "backtest"
    if bt_dir.exists():
        shutil.rmtree(bt_dir)
        print(f"Removed: {bt_dir}")
    else:
        print(f"Nothing to clean: {bt_dir}")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def _banner() -> str:
    try:
        import pyfiglet
        return pyfiglet.figlet_format("Timba", font="big_money-ne").rstrip() + "\n"
    except Exception:
        return "  Timba\n"


def _build_parser() -> argparse.ArgumentParser:
    ver = get_version()
    banner = _banner()
    parser = argparse.ArgumentParser(
        prog="timba",
        description=f"{banner}  Polymarket crypto trading bot  {ver}\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"{banner}{ver}",
    )

    sub = parser.add_subparsers(dest="command", help="Available commands")

    # -- init --
    p = sub.add_parser("init", help="Setup or reconfigure ~/.timba/")
    p.set_defaults(func=cmd_init)

    # -- start --
    p = sub.add_parser("start", help="Start the bot")
    p.add_argument("--config", help="Path to config file")
    p.add_argument("--port", type=int, default=8080, help="API port (default: 8080)")
    p.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Logging level")
    p.set_defaults(func=cmd_start)

    # -- stop --
    p = sub.add_parser("stop", help="Stop the running bot")
    p.add_argument("--host", help="Bot host (default: auto-discover)")
    p.add_argument("--port", type=int, help="Bot port (default: auto-discover)")
    p.set_defaults(func=cmd_stop)

    # -- status --
    p = sub.add_parser("status", help="Show bot status")
    p.add_argument("--host", help="Bot host")
    p.add_argument("--port", type=int, help="Bot port")
    p.set_defaults(func=cmd_status)

    # -- monitor --
    p = sub.add_parser("monitor", help="Live dashboard (polls bot API)")
    p.add_argument("--host", help="Bot host")
    p.add_argument("--port", type=int, help="Bot port")
    p.add_argument("--interval", type=int, default=1, help="Refresh interval in seconds (default: 1)")
    p.set_defaults(func=cmd_monitor)

    # -- check-wallet --
    p = sub.add_parser("check-wallet", help="Verify wallet credentials")
    p.add_argument("--config", help="Path to config file")
    p.set_defaults(func=cmd_check_wallet)

    # -- test-live --
    p = sub.add_parser("test-live", help="Test order lifecycle (place + cancel)")
    p.add_argument("--config", help="Path to config file")
    p.set_defaults(func=cmd_test_live)

    # -- backtest --
    p = sub.add_parser("backtest", help="Run full backtest")
    p.add_argument("--config", help="Path to config file")
    p.add_argument("--source-env", default="main", help="Source environment for tick data (default: main)")
    p.add_argument("--strategy", default="favorite", help="Strategy to backtest")
    p.add_argument("--since", help="Date filter for ticks (e.g. 2026-03-30)")
    p.set_defaults(func=cmd_backtest)

    # -- analyze (nested: trades / ticks) --
    analyze_p = sub.add_parser("analyze", help="Analyze trades or ticks")
    analyze_sub = analyze_p.add_subparsers(dest="analyze_command")

    p = analyze_sub.add_parser("trades", help="Analyze trades: price distribution, PnL")
    p.add_argument("--config", help="Path to config file")
    p.add_argument("--strategy", default="favorite", help="Strategy to analyze")
    p.add_argument("--backtest", action="store_true", help="Analyze backtest results")
    p.set_defaults(func=cmd_analyze_trades)

    p = analyze_sub.add_parser("ticks", help="Analyze tick EVs vs outcomes")
    p.add_argument("--config", help="Path to config file")
    p.add_argument("--strategy", default="favorite", help="Strategy to analyze")
    p.add_argument("--coin", help="Filter by coin")
    p.add_argument("--interval", help="Filter by interval")
    p.set_defaults(func=cmd_analyze_ticks)

    # -- backtest-clean --
    p = sub.add_parser("backtest-clean", help="Delete backtest data")
    p.add_argument("--config", help="Path to config file")
    p.set_defaults(func=cmd_backtest_clean)

    return parser


def main():
    parser = _build_parser()
    args = parser.parse_args()

    if not hasattr(args, "func"):
        # No subcommand given, or analyze with no sub-subcommand
        if args.command == "analyze":
            parser.parse_args(["analyze", "--help"])
        else:
            parser.print_help()
        sys.exit(0)

    args.func(args)


if __name__ == "__main__":
    main()
