import json
import logging
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from timba.cli import _banner, _check_geoblock, _check_wallet, _detect_env, _print_market_table, main
from timba.config import Config
from timba.version import get_version


class TestBanner:
    def test_banner_ascii_art(self):
        out = _banner()
        assert len(out) > 0 and "\n" in out

    def test_market_table_no_strategies(self, capsys):
        cfg = Config()
        _print_market_table(cfg)
        out = capsys.readouterr().out
        assert "---" in out  # separator line


class TestGeoblock:
    @patch("timba.cli.requests")
    def test_not_blocked(self, mock_requests, capsys):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"blocked": False, "ip": "1.2.3.4", "country": "ES", "region": "MD"}
        mock_requests.get.return_value = mock_resp
        _check_geoblock()
        out = capsys.readouterr().out
        assert "blocked=False" in out

    @patch("timba.cli.requests")
    def test_blocked_exits(self, mock_requests):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"blocked": True, "ip": "5.6.7.8", "country": "US", "region": "NY"}
        mock_requests.get.return_value = mock_resp
        with pytest.raises(SystemExit):
            _check_geoblock()

    @patch("timba.cli.requests")
    def test_api_failure_continues(self, mock_requests, capsys):
        mock_requests.get.side_effect = Exception("timeout")
        _check_geoblock()
        out = capsys.readouterr().out
        assert "failed" in out


class TestCheckWallet:
    def test_missing_private_key(self, tmp_path):
        cfg = Config()
        cfg.polymarket.private_key = ""
        with pytest.raises(SystemExit):
            _check_wallet(cfg)

    @patch("timba.cli._check_geoblock")
    @patch("polymarket_apis.PolymarketClobClient")
    def test_success(self, mock_clob_cls, mock_geo, capsys):
        mock_client = MagicMock()
        mock_client.create_or_derive_api_creds.return_value = MagicMock()
        mock_client.get_ok.return_value = "OK"
        mock_client.get_usdc_balance.return_value = 50.0
        mock_client.get_orders.return_value = []
        mock_clob_cls.return_value = mock_client

        cfg = Config()
        cfg.polymarket.private_key = "0xtest"
        cfg.polymarket.funder = "0xfunder"
        _check_wallet(cfg)
        out = capsys.readouterr().out
        assert "API status: OK" in out
        assert "$50.00" in out
        assert "Wallet check passed" in out

    @patch("timba.cli._check_geoblock")
    @patch("polymarket_apis.PolymarketClobClient")
    def test_clob_client_fails(self, mock_clob_cls, mock_geo):
        mock_clob_cls.side_effect = Exception("bad key")
        cfg = Config()
        cfg.polymarket.private_key = "0xtest"
        with pytest.raises(SystemExit):
            _check_wallet(cfg)

    @patch("timba.cli._check_geoblock")
    @patch("polymarket_apis.PolymarketClobClient")
    def test_api_unreachable(self, mock_clob_cls, mock_geo):
        mock_client = MagicMock()
        mock_client.create_or_derive_api_creds.return_value = MagicMock()
        mock_client.get_ok.side_effect = Exception("timeout")
        mock_clob_cls.return_value = mock_client

        cfg = Config()
        cfg.polymarket.private_key = "0xtest"
        with pytest.raises(SystemExit):
            _check_wallet(cfg)

    @patch("timba.cli._check_geoblock")
    @patch("polymarket_apis.PolymarketClobClient")
    def test_balance_unavailable(self, mock_clob_cls, mock_geo, capsys):
        mock_client = MagicMock()
        mock_client.create_or_derive_api_creds.return_value = MagicMock()
        mock_client.get_ok.return_value = "OK"
        mock_client.get_usdc_balance.side_effect = Exception("err")
        mock_client.get_orders.side_effect = Exception("err")
        mock_clob_cls.return_value = mock_client

        cfg = Config()
        cfg.polymarket.private_key = "0xtest"
        _check_wallet(cfg)
        out = capsys.readouterr().out
        assert "unavailable" in out


class TestTestLiveOrder:
    @patch("timba.cli._check_geoblock")
    @patch("polymarket_apis.PolymarketClobClient")
    @patch("timba.market.discover_active_markets")
    def test_no_markets_exits(self, mock_discover, mock_clob_cls, mock_geo):
        mock_client = MagicMock()
        mock_client.create_or_derive_api_creds.return_value = MagicMock()
        mock_clob_cls.return_value = mock_client
        mock_discover.return_value = []

        cfg = Config()
        cfg.polymarket.private_key = "0xtest"
        cfg.polymarket.funder = "0xfunder"
        with pytest.raises(SystemExit):
            from timba.cli import _test_live_order
            _test_live_order(cfg)

    @patch("timba.cli._time")
    @patch("timba.cli._check_geoblock")
    @patch("polymarket_apis.PolymarketClobClient")
    @patch("timba.market.discover_active_markets")
    def test_low_balance_skips_order(self, mock_discover, mock_clob_cls, mock_geo, mock_time, capsys):
        mock_client = MagicMock()
        mock_client.create_or_derive_api_creds.return_value = MagicMock()
        mock_client.get_usdc_balance.return_value = 0.5
        mock_clob_cls.return_value = mock_client

        market = MagicMock()
        market.slug = "btc-test"
        market.token_id_up = "tok123"
        mock_discover.return_value = [market]

        cfg = Config()
        cfg.polymarket.private_key = "0xtest"
        cfg.polymarket.funder = "0xfunder"
        from timba.cli import _test_live_order
        _test_live_order(cfg)
        out = capsys.readouterr().out
        assert "skipping place+cancel" in out

    @patch("timba.cli._time")
    @patch("timba.cli._check_geoblock")
    @patch("polymarket_apis.PolymarketClobClient")
    @patch("timba.market.discover_active_markets")
    def test_full_lifecycle(self, mock_discover, mock_clob_cls, mock_geo, mock_time, capsys):
        mock_client = MagicMock()
        mock_client.create_or_derive_api_creds.return_value = MagicMock()
        mock_client.get_usdc_balance.return_value = 100.0
        mock_resp = MagicMock()
        mock_resp.success = True
        mock_resp.order_id = "0xorder123"
        mock_resp.status = "live"
        mock_client.create_and_post_order.return_value = mock_resp
        mock_client.get_orders.return_value = [MagicMock(order_id="0xorder123")]
        mock_client.cancel_all.return_value = None
        mock_clob_cls.return_value = mock_client

        market = MagicMock()
        market.slug = "btc-test"
        market.token_id_up = "tok123"
        mock_discover.return_value = [market]

        cfg = Config()
        cfg.polymarket.private_key = "0xtest"
        cfg.polymarket.funder = "0xfunder"
        from timba.cli import _test_live_order
        _test_live_order(cfg)
        out = capsys.readouterr().out
        assert "Order placed" in out
        assert "Cancelled" in out
        assert "lifecycle works" in out

    @patch("timba.cli._time")
    @patch("timba.cli._check_geoblock")
    @patch("polymarket_apis.PolymarketClobClient")
    @patch("timba.market.discover_active_markets")
    def test_order_rejected(self, mock_discover, mock_clob_cls, mock_geo, mock_time, capsys):
        mock_client = MagicMock()
        mock_client.create_or_derive_api_creds.return_value = MagicMock()
        mock_client.get_usdc_balance.return_value = 100.0
        mock_resp = MagicMock()
        mock_resp.success = False
        mock_resp.error_msg = "price too low"
        mock_client.create_and_post_order.return_value = mock_resp
        mock_clob_cls.return_value = mock_client

        market = MagicMock()
        market.slug = "btc-test"
        market.token_id_up = "tok123"
        mock_discover.return_value = [market]

        cfg = Config()
        cfg.polymarket.private_key = "0xtest"
        cfg.polymarket.funder = "0xfunder"
        from timba.cli import _test_live_order
        _test_live_order(cfg)
        out = capsys.readouterr().out
        assert "rejected" in out


class TestCli:
    def test_no_command_prints_help(self, capsys):
        """No subcommand prints help and exits 0."""
        with patch("sys.argv", ["timba"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0

    def test_start_config_not_found(self):
        with pytest.raises(SystemExit):
            with patch("sys.argv", ["timba", "start", "--config", "/nonexistent.yaml"]):
                main()

    def test_check_wallet_config_not_found(self):
        with pytest.raises(SystemExit):
            with patch("sys.argv", ["timba", "check-wallet", "--config", "/nonexistent.yaml"]):
                main()


class TestDetectEnv:
    def test_returns_env_var_if_set(self, monkeypatch):
        monkeypatch.setenv("BOT_ENV", "staging")
        assert _detect_env() == "staging"

    def test_falls_back_to_git_branch(self, monkeypatch):
        monkeypatch.delenv("BOT_ENV", raising=False)
        with patch("subprocess.check_output", return_value="feature-branch\n"):
            assert _detect_env() == "feature-branch"

    def test_falls_back_to_main_on_git_error(self, monkeypatch):
        monkeypatch.delenv("BOT_ENV", raising=False)
        with patch("subprocess.check_output", side_effect=Exception("no git")):
            assert _detect_env() == "main"


class TestTimbaHome:
    def test_default_path(self, monkeypatch):
        monkeypatch.delenv("TIMBA_HOME", raising=False)
        from timba.cli import _timba_home
        result = _timba_home()
        assert isinstance(result, Path)
        assert result == Path("~/.timba").expanduser()

    def test_respects_env_var(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TIMBA_HOME", str(tmp_path / "custom"))
        from timba.cli import _timba_home
        assert _timba_home() == tmp_path / "custom"


class TestTemplate:
    def test_reads_config_yaml(self):
        from timba.cli import _template
        content = _template("config.yaml")
        assert isinstance(content, str)
        assert len(content) > 0

    def test_missing_template_raises(self):
        from timba.cli import _template
        with pytest.raises(FileNotFoundError):
            _template("nonexistent_file_xyz.yaml")


class TestLoadEnv:
    def test_loads_env_from_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TIMBA_HOME", str(tmp_path))
        monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)
        monkeypatch.delenv("TEST_VAR_LOAD_ENV", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("TEST_VAR_LOAD_ENV=hello123\n# comment\n\nANOTHER=world\n")
        from timba.cli import _load_env
        _load_env()
        assert os.environ.get("TEST_VAR_LOAD_ENV") == "hello123"
        assert os.environ.get("ANOTHER") == "world"
        # Clean up
        monkeypatch.delenv("TEST_VAR_LOAD_ENV", raising=False)
        monkeypatch.delenv("ANOTHER", raising=False)

    def test_skips_if_key_already_set(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TIMBA_HOME", str(tmp_path))
        monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "existing_key")
        env_file = tmp_path / ".env"
        env_file.write_text("POLYMARKET_PRIVATE_KEY=new_key\n")
        from timba.cli import _load_env
        _load_env()
        # Should not overwrite -- _load_env returns early if POLYMARKET_PRIVATE_KEY is set
        assert os.environ.get("POLYMARKET_PRIVATE_KEY") == "existing_key"

    def test_no_env_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TIMBA_HOME", str(tmp_path))
        monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)
        from timba.cli import _load_env
        # Should not raise even if .env doesn't exist
        _load_env()


class TestResolveConfig:
    def test_explicit_path_found(self, tmp_path):
        cfg = tmp_path / "my.yaml"
        cfg.write_text("favorite:\n  enabled: true\n")
        from timba.cli import _resolve_config
        result = _resolve_config(str(cfg))
        assert result == cfg

    def test_explicit_path_not_found(self):
        from timba.cli import _resolve_config
        with pytest.raises(SystemExit):
            _resolve_config("/nonexistent/config.yaml")

    def test_default_discovery(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TIMBA_HOME", str(tmp_path))
        cfg = tmp_path / "config.yaml"
        cfg.write_text("favorite:\n  enabled: true\n")
        from timba.cli import _resolve_config
        result = _resolve_config(None)
        assert result == cfg

    def test_default_not_found(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TIMBA_HOME", str(tmp_path))
        from timba.cli import _resolve_config
        with pytest.raises(SystemExit):
            _resolve_config(None)


class TestDataDir:
    def test_creates_data_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TIMBA_HOME", str(tmp_path))
        monkeypatch.setenv("BOT_ENV", "testenv")
        from timba.cli import _data_dir
        result = _data_dir()
        assert result == tmp_path / "data" / "testenv"
        assert result.is_dir()


class TestSetupLogging:
    def test_configures_without_error(self):
        from timba.cli import _setup_logging
        # Just verify it doesn't raise for valid levels
        _setup_logging("DEBUG")
        _setup_logging("INFO")
        _setup_logging("WARNING")

    def test_suppresses_noisy_loggers(self):
        from timba.cli import _setup_logging
        _setup_logging("INFO")
        assert logging.getLogger("urllib3").level == logging.WARNING
        assert logging.getLogger("httpx").level == logging.WARNING


class TestBotJsonHelpers:
    def test_write_bot_json(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TIMBA_HOME", str(tmp_path))
        from timba.cli import _write_bot_json
        _write_bot_json(9090)
        bot_json = tmp_path / "bot.json"
        assert bot_json.exists()
        data = json.loads(bot_json.read_text())
        assert data["port"] == 9090
        assert "pid" in data

    def test_remove_bot_json(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TIMBA_HOME", str(tmp_path))
        bot_json = tmp_path / "bot.json"
        bot_json.write_text('{"pid": 1, "port": 8080}')
        from timba.cli import _remove_bot_json
        _remove_bot_json()
        assert not bot_json.exists()

    def test_remove_bot_json_missing_ok(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TIMBA_HOME", str(tmp_path))
        from timba.cli import _remove_bot_json
        # Should not raise even if file doesn't exist
        _remove_bot_json()


class TestEnsureInitialized:
    def test_true_when_env_exists(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TIMBA_HOME", str(tmp_path))
        (tmp_path / ".env").write_text("KEY=val\n")
        from timba.cli import _ensure_initialized
        assert _ensure_initialized() is True

    def test_false_when_no_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TIMBA_HOME", str(tmp_path))
        from timba.cli import _ensure_initialized
        assert _ensure_initialized() is False


class TestBannerFunc:
    def test_returns_nonempty_string(self):
        from timba.cli import _banner
        result = _banner()
        assert isinstance(result, str)
        assert len(result) > 0
        # Should end with newline
        assert result.endswith("\n")


class TestCmdInit:
    def test_creates_env_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TIMBA_HOME", str(tmp_path))
        from timba.cli import cmd_init
        # Mock user input for the prompts
        with patch("timba.cli.getpass.getpass", return_value="0xfakekey"), \
             patch("builtins.input", return_value="0xfakeaddr"):
            args = MagicMock()
            cmd_init(args)
        env_file = tmp_path / ".env"
        assert env_file.exists()
        content = env_file.read_text()
        assert "0xfakekey" in content
        assert "0xfakeaddr" in content

    def test_existing_env_no_overwrite(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("TIMBA_HOME", str(tmp_path))
        env_file = tmp_path / ".env"
        env_file.write_text("OLD=value\n")
        from timba.cli import cmd_init
        with patch("builtins.input", return_value="n"):
            args = MagicMock()
            cmd_init(args)
        assert env_file.read_text() == "OLD=value\n"
        out = capsys.readouterr().out
        assert "Kept existing" in out


class TestCmdStatusNotRunning:
    def test_prints_error_exits_1(self, capsys):
        from timba.cli import cmd_status
        args = MagicMock()
        args.host = None
        args.port = None
        with patch("timba.client.BotClient") as mock_cls:
            mock_cls.return_value.is_running.return_value = False
            with pytest.raises(SystemExit) as exc_info:
                cmd_status(args)
            assert exc_info.value.code == 1


class TestCmdStopNotRunning:
    def test_prints_error_exits_1(self, capsys):
        from timba.cli import cmd_stop
        args = MagicMock()
        args.host = None
        args.port = None
        with patch("timba.client.BotClient") as mock_cls:
            mock_cls.return_value.is_running.return_value = False
            with pytest.raises(SystemExit) as exc_info:
                cmd_stop(args)
            assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "not running" in err.lower()


class TestCliConfigValidation:
    def test_invalid_config_exits_with_error(self, tmp_path, capsys):
        cfg = tmp_path / "bad.yaml"
        cfg.write_text("favorite:\n  typo_key: 123\n")
        with pytest.raises(SystemExit):
            with patch("sys.argv", ["timba", "start", "--config", str(cfg)]):
                main()
        err = capsys.readouterr().err
        assert "validation failed" in err.lower() or "typo_key" in err

    def test_banner_shows_strategy_markets(self, tmp_path, capsys):
        cfg = Config()
        from timba.config import StrategyConfig
        cfg.strategies["favorite"] = StrategyConfig({
            "enabled": True,
            "markets": [
                {"coin": "btc", "interval": "5m", "mode": "paper"},
                {"coin": "eth", "interval": "5m", "mode": "paper"},
            ],
        })
        _print_market_table(cfg)
        out = capsys.readouterr().out
        assert "BTC" in out
        assert "ETH" in out
        assert "PPR" in out


class TestCmdCheckWallet:
    """Test cmd_check_wallet handler: loads config, calls _check_wallet."""

    def test_calls_check_wallet(self, tmp_path, monkeypatch):
        """cmd_check_wallet loads config and calls _check_wallet."""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("""
favorite:
  enabled: true
  min_price: 0.95
  min_signal_chg: 0.05
  contracts_per_trade: 5
  markets:
    - coin: btc
      interval: 5m
      mode: paper
      entry_window_sec: 10
      close_window_sec: 3
""")
        monkeypatch.setenv("TIMBA_HOME", str(tmp_path))
        # Create .env so _load_env doesn't interfere
        (tmp_path / ".env").write_text("")

        from timba.cli import cmd_check_wallet
        args = MagicMock()
        args.config = str(cfg_file)

        with patch("timba.cli._check_wallet") as mock_check:
            cmd_check_wallet(args)
            mock_check.assert_called_once()

    def test_invalid_config_exits(self, tmp_path, monkeypatch):
        """cmd_check_wallet with invalid config should exit."""
        cfg_file = tmp_path / "bad.yaml"
        cfg_file.write_text("favorite:\n  bad_key_xyz: 99\n")
        monkeypatch.setenv("TIMBA_HOME", str(tmp_path))
        (tmp_path / ".env").write_text("")

        from timba.cli import cmd_check_wallet
        args = MagicMock()
        args.config = str(cfg_file)

        with pytest.raises(SystemExit):
            cmd_check_wallet(args)


class TestCmdBacktestClean:
    """Test cmd_backtest_clean handler: creates and removes backtest directory."""

    def test_removes_existing_backtest_dir(self, tmp_path, monkeypatch, capsys):
        """cmd_backtest_clean removes the backtest directory if it exists."""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("""
favorite:
  enabled: true
  min_price: 0.95
  min_signal_chg: 0.05
  contracts_per_trade: 5
  markets:
    - coin: btc
      interval: 5m
      mode: paper
      entry_window_sec: 10
      close_window_sec: 3
""")
        monkeypatch.setenv("TIMBA_HOME", str(tmp_path))
        monkeypatch.setenv("BOT_ENV", "testenv")
        (tmp_path / ".env").write_text("")

        # Create the backtest dir
        bt_dir = tmp_path / "data" / "testenv" / "backtest"
        bt_dir.mkdir(parents=True)
        (bt_dir / "test_file.db").write_text("dummy")

        from timba.cli import cmd_backtest_clean
        args = MagicMock()
        args.config = str(cfg_file)

        cmd_backtest_clean(args)

        assert not bt_dir.exists()
        out = capsys.readouterr().out
        assert "Removed" in out

    def test_no_backtest_dir_prints_nothing_to_clean(self, tmp_path, monkeypatch, capsys):
        """cmd_backtest_clean when no backtest dir prints 'Nothing to clean'."""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("""
favorite:
  enabled: true
  min_price: 0.95
  min_signal_chg: 0.05
  contracts_per_trade: 5
  markets:
    - coin: btc
      interval: 5m
      mode: paper
      entry_window_sec: 10
      close_window_sec: 3
""")
        monkeypatch.setenv("TIMBA_HOME", str(tmp_path))
        monkeypatch.setenv("BOT_ENV", "testenv")
        (tmp_path / ".env").write_text("")

        from timba.cli import cmd_backtest_clean
        args = MagicMock()
        args.config = str(cfg_file)

        cmd_backtest_clean(args)

        out = capsys.readouterr().out
        assert "Nothing to clean" in out


class TestDetectEnvHeadBranch:
    """Edge case: git returns HEAD (detached)."""

    def test_detached_head_falls_back_to_main(self, monkeypatch):
        monkeypatch.delenv("BOT_ENV", raising=False)
        with patch("subprocess.check_output", return_value="HEAD\n"):
            assert _detect_env() == "main"


class TestCmdBacktest:
    """Test cmd_backtest handler."""

    def test_calls_backtest_main(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("""
favorite:
  enabled: true
  min_price: 0.95
  min_signal_chg: 0.05
  contracts_per_trade: 5
  markets:
    - coin: btc
      interval: 5m
      mode: paper
      entry_window_sec: 10
      close_window_sec: 3
""")
        monkeypatch.setenv("TIMBA_HOME", str(tmp_path))
        monkeypatch.setenv("BOT_ENV", "testenv")
        (tmp_path / ".env").write_text("")

        from timba.cli import cmd_backtest
        args = MagicMock()
        args.config = str(cfg_file)
        args.source_env = "main"
        args.strategy = "favorite"
        args.since = None

        with patch("timba.backtest.backtest_main") as mock_bt:
            cmd_backtest(args)
            mock_bt.assert_called_once()

    def test_invalid_config_exits(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / "bad.yaml"
        cfg_file.write_text("favorite:\n  bad_key_xyz: 99\n")
        monkeypatch.setenv("TIMBA_HOME", str(tmp_path))
        (tmp_path / ".env").write_text("")

        from timba.cli import cmd_backtest
        args = MagicMock()
        args.config = str(cfg_file)
        args.source_env = "main"
        args.strategy = "favorite"
        args.since = None

        with pytest.raises(SystemExit):
            cmd_backtest(args)


class TestCmdAnalyzeTrades:
    """Test cmd_analyze_trades handler."""

    def test_calls_analyze_main(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("""
favorite:
  enabled: true
  min_price: 0.95
  min_signal_chg: 0.05
  contracts_per_trade: 5
  markets:
    - coin: btc
      interval: 5m
      mode: paper
      entry_window_sec: 10
      close_window_sec: 3
""")
        monkeypatch.setenv("TIMBA_HOME", str(tmp_path))
        monkeypatch.setenv("BOT_ENV", "testenv")
        (tmp_path / ".env").write_text("")

        from timba.cli import cmd_analyze_trades
        args = MagicMock()
        args.config = str(cfg_file)
        args.strategy = "favorite"
        args.backtest = False

        with patch("timba.backtest.analyze_trades.analyze_main") as mock_analyze:
            cmd_analyze_trades(args)
            mock_analyze.assert_called_once()

    def test_backtest_flag(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("""
favorite:
  enabled: true
  min_price: 0.95
  min_signal_chg: 0.05
  contracts_per_trade: 5
  markets:
    - coin: btc
      interval: 5m
      mode: paper
      entry_window_sec: 10
      close_window_sec: 3
""")
        monkeypatch.setenv("TIMBA_HOME", str(tmp_path))
        monkeypatch.setenv("BOT_ENV", "testenv")
        (tmp_path / ".env").write_text("")

        from timba.cli import cmd_analyze_trades
        args = MagicMock()
        args.config = str(cfg_file)
        args.strategy = "favorite"
        args.backtest = True

        with patch("timba.backtest.analyze_trades.analyze_main") as mock_analyze:
            cmd_analyze_trades(args)
            # When --backtest is set, should pass the backtest subdirectory
            call_args = mock_analyze.call_args
            assert "backtest" in str(call_args[0][0])


class TestCmdAnalyzeTicks:
    """Test cmd_analyze_ticks handler."""

    def test_calls_analyze_ticks_main(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("""
favorite:
  enabled: true
  min_price: 0.95
  min_signal_chg: 0.05
  contracts_per_trade: 5
  markets:
    - coin: btc
      interval: 5m
      mode: paper
      entry_window_sec: 10
      close_window_sec: 3
""")
        monkeypatch.setenv("TIMBA_HOME", str(tmp_path))
        monkeypatch.setenv("BOT_ENV", "testenv")
        (tmp_path / ".env").write_text("")

        from timba.cli import cmd_analyze_ticks
        args = MagicMock()
        args.config = str(cfg_file)
        args.coin = "btc"
        args.interval = "5m"
        args.strategy = "favorite"

        with patch("timba.backtest.analyze_ticks_main") as mock_at:
            cmd_analyze_ticks(args)
            mock_at.assert_called_once()


class TestCmdTestLiveViaHandler:
    """Test cmd_test_live handler wiring."""

    def test_calls_check_wallet_and_test_live(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("""
favorite:
  enabled: true
  min_price: 0.95
  min_signal_chg: 0.05
  contracts_per_trade: 5
  markets:
    - coin: btc
      interval: 5m
      mode: paper
      entry_window_sec: 10
      close_window_sec: 3
""")
        monkeypatch.setenv("TIMBA_HOME", str(tmp_path))
        (tmp_path / ".env").write_text("")

        from timba.cli import cmd_test_live
        args = MagicMock()
        args.config = str(cfg_file)

        with patch("timba.cli._check_wallet") as mock_cw, \
             patch("timba.cli._test_live_order") as mock_tl:
            cmd_test_live(args)
            mock_cw.assert_called_once()
            mock_tl.assert_called_once()


class TestVersion:
    def test_version_returns_string(self):
        v = get_version()
        assert isinstance(v, str)
        assert len(v) > 0

    def test_version_dev_fallback(self):
        with patch.dict("sys.modules", {"timba._version": None}):
            v = get_version()
            assert v == "dev"
