import argparse
import json
import logging
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from timba.cli import _banner, _check_geoblock, _check_wallet, _detect_env, _print_market_table, cmd_config, main
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


# ══════════════════════════════════════════════════════════════════════
# Coverage: cli.py command handlers — cmd_start, cmd_status (running),
# cmd_stop (running), cmd_monitor
# ══════════════════════════════════════════════════════════════════════


def _valid_config_file(tmp_path):
    """Create a valid config file and return its path."""
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
    return cfg_file


class TestCmdStartFlow:
    """Cover cmd_start lines 439-517: the main startup sequence."""

    def test_start_full_flow(self, tmp_path, monkeypatch):
        """cmd_start loads config, connects CLOB, inits DB, runs trader."""
        cfg_file = _valid_config_file(tmp_path)
        monkeypatch.setenv("TIMBA_HOME", str(tmp_path))
        monkeypatch.setenv("BOT_ENV", "testenv")
        monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0xtest_key")
        (tmp_path / ".env").write_text("")

        from timba.cli import cmd_start
        args = MagicMock()
        args.config = str(cfg_file)
        args.port = 9999
        args.log_level = "WARNING"

        mock_clob_instance = MagicMock()
        mock_clob_instance.create_or_derive_api_creds.return_value = MagicMock()
        mock_clob_instance.get_usdc_balance.return_value = 100.0

        mock_trader_instance = MagicMock()

        mock_api_server = MagicMock()

        with patch("polymarket_apis.PolymarketClobClient", return_value=mock_clob_instance), \
             patch("timba.cli._banner", return_value="Timba\n"), \
             patch("timba.cli._print_market_table"), \
             patch("timba.cli._detect_env", return_value="testenv"), \
             patch("timba.cli._data_dir", return_value=tmp_path / "data" / "testenv"), \
             patch("timba.db.init"), \
             patch("timba.ticks.init_ids"), \
             patch("timba.state.init_trade_ids"), \
             patch("timba.db.get_pending_redemption", return_value=0), \
             patch("timba.reconcile.reconcile_startup"), \
             patch("timba.trader.Trader", return_value=mock_trader_instance) as mock_trader_cls, \
             patch("timba.server.start_api_server", return_value=mock_api_server), \
             patch("timba.cli._write_bot_json"), \
             patch("timba.cli._remove_bot_json"), \
             patch("timba.health.LogFileHandler", return_value=MagicMock()), \
             patch("signal.signal"), \
             patch("atexit.register"):
            # Make data dir exist
            (tmp_path / "data" / "testenv").mkdir(parents=True, exist_ok=True)
            cmd_start(args)

            # Verify trader was instantiated and run was called
            mock_trader_cls.assert_called_once()
            mock_trader_instance.run.assert_called_once()
            mock_api_server.shutdown.assert_called_once()

    def test_start_no_private_key_exits(self, tmp_path, monkeypatch):
        """cmd_start without POLYMARKET_PRIVATE_KEY should exit."""
        cfg_file = _valid_config_file(tmp_path)
        monkeypatch.setenv("TIMBA_HOME", str(tmp_path))
        monkeypatch.setenv("BOT_ENV", "testenv")
        monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)
        (tmp_path / ".env").write_text("")

        from timba.cli import cmd_start
        args = MagicMock()
        args.config = str(cfg_file)
        args.port = 8080
        args.log_level = None

        with patch("timba.cli._banner", return_value="Timba\n"), \
             patch("timba.cli._print_market_table"), \
             patch("timba.cli._detect_env", return_value="testenv"), \
             patch("timba.cli._data_dir", return_value=tmp_path / "data" / "testenv"):
            (tmp_path / "data" / "testenv").mkdir(parents=True, exist_ok=True)
            with pytest.raises(SystemExit):
                cmd_start(args)

    def test_start_clob_connection_fails(self, tmp_path, monkeypatch):
        """cmd_start with CLOB connection failure should exit."""
        cfg_file = _valid_config_file(tmp_path)
        monkeypatch.setenv("TIMBA_HOME", str(tmp_path))
        monkeypatch.setenv("BOT_ENV", "testenv")
        monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0xtest_key")
        (tmp_path / ".env").write_text("")

        from timba.cli import cmd_start
        args = MagicMock()
        args.config = str(cfg_file)
        args.port = 8080
        args.log_level = None

        with patch("polymarket_apis.PolymarketClobClient", side_effect=Exception("connection failed")), \
             patch("timba.cli._banner", return_value="Timba\n"), \
             patch("timba.cli._print_market_table"), \
             patch("timba.cli._detect_env", return_value="testenv"), \
             patch("timba.cli._data_dir", return_value=tmp_path / "data" / "testenv"):
            (tmp_path / "data" / "testenv").mkdir(parents=True, exist_ok=True)
            with pytest.raises(SystemExit):
                cmd_start(args)

    def test_start_auto_init_when_not_initialized(self, tmp_path, monkeypatch):
        """cmd_start auto-runs cmd_init when no .env and no explicit config."""
        monkeypatch.setenv("TIMBA_HOME", str(tmp_path))
        monkeypatch.setenv("BOT_ENV", "testenv")
        monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)

        from timba.cli import cmd_start
        args = MagicMock()
        args.config = None  # no explicit config -> triggers auto-init
        args.port = 8080
        args.log_level = None

        with patch("timba.cli.cmd_init") as mock_init, \
             patch("timba.cli._ensure_initialized", return_value=False), \
             patch("timba.cli._resolve_config") as mock_resolve:
            # After init, _resolve_config will be called; make it exit to stop the flow
            mock_resolve.side_effect = SystemExit(1)
            with pytest.raises(SystemExit):
                cmd_start(args)
            mock_init.assert_called_once()

    def test_start_invalid_config_exits(self, tmp_path, monkeypatch):
        """cmd_start with invalid config should exit with validation error."""
        cfg_file = tmp_path / "bad.yaml"
        cfg_file.write_text("favorite:\n  typo_key: 123\n")
        monkeypatch.setenv("TIMBA_HOME", str(tmp_path))
        monkeypatch.setenv("BOT_ENV", "testenv")
        (tmp_path / ".env").write_text("")

        from timba.cli import cmd_start
        args = MagicMock()
        args.config = str(cfg_file)
        args.port = 8080
        args.log_level = None

        with pytest.raises(SystemExit):
            cmd_start(args)


class TestCmdStatusRunning:
    """Cover cmd_status lines 551-610: successful status query."""

    def test_status_with_live_and_paper_trades(self, capsys):
        from timba.cli import cmd_status
        args = MagicMock()
        args.host = "127.0.0.1"
        args.port = 8080

        mock_status = {
            "health": {"status": "ok", "uptime_seconds": 3700},
            "state": {"cash": 95.0, "portfolio": 100.0},
            "version": "1.0.0",
        }
        mock_trades = [
            {"type": "win", "strategy": "favorite", "buy_price": 0.95,
             "contracts": 5, "pnl": 0.25, "side": "up"},
            {"type": "loss", "strategy": "favorite", "buy_price": 0.90,
             "contracts": 5, "pnl": -4.50, "side": "up"},
            {"type": "paper_win", "strategy": "favorite", "buy_price": 0.80,
             "contracts": 5, "pnl": 1.0, "side": "up"},
            {"type": "paper_loss", "strategy": "favorite", "buy_price": 0.85,
             "contracts": 5, "pnl": -4.25, "side": "down"},
            {"type": "fail_win", "strategy": "favorite", "buy_price": 0.70,
             "contracts": 5, "pnl": 0, "side": "up"},
            {"type": "fail_loss", "strategy": "favorite", "buy_price": 0.75,
             "contracts": 5, "pnl": 0, "side": "down"},
            {"type": "skip_win", "strategy": "favorite", "buy_price": 0.60,
             "contracts": 5, "pnl": 0, "side": "up"},
            {"type": "skip_loss", "strategy": "favorite", "buy_price": 0.65,
             "contracts": 5, "pnl": 0, "side": "down"},
            {"type": "skip_none", "strategy": "favorite", "buy_price": 0,
             "contracts": 0, "pnl": 0, "side": ""},
        ]

        with patch("timba.client.BotClient") as mock_cls:
            client = mock_cls.return_value
            client.is_running.return_value = True
            client.status.return_value = mock_status
            client.trades.return_value = mock_trades

            cmd_status(args)

        out = capsys.readouterr().out
        assert "Version: 1.0.0" in out
        assert "$95.00" in out
        assert "FAVORITE" in out
        assert "Live" in out
        assert "Paper" in out
        assert "Fails:" in out
        assert "Skips:" in out

    def test_status_api_error(self, capsys):
        from timba.cli import cmd_status
        args = MagicMock()
        args.host = "127.0.0.1"
        args.port = 8080

        with patch("timba.client.BotClient") as mock_cls:
            client = mock_cls.return_value
            client.is_running.return_value = True
            client.status.side_effect = Exception("connection reset")

            with pytest.raises(SystemExit):
                cmd_status(args)

        err = capsys.readouterr().err
        assert "connection reset" in err


class TestCmdStopRunning:
    """Cover cmd_stop lines 522-536: successful stop command."""

    def test_stop_success(self, capsys):
        from timba.cli import cmd_stop
        args = MagicMock()
        args.host = "127.0.0.1"
        args.port = 8080

        with patch("timba.client.BotClient") as mock_cls:
            client = mock_cls.return_value
            client.is_running.return_value = True
            client.stop.return_value = {"status": "shutting_down"}

            cmd_stop(args)

        out = capsys.readouterr().out
        assert "Stop signal sent" in out
        assert "shutting_down" in out

    def test_stop_api_error(self, capsys):
        from timba.cli import cmd_stop
        args = MagicMock()
        args.host = "127.0.0.1"
        args.port = 8080

        with patch("timba.client.BotClient") as mock_cls:
            client = mock_cls.return_value
            client.is_running.return_value = True
            client.stop.side_effect = Exception("timeout")

            with pytest.raises(SystemExit):
                cmd_stop(args)

        err = capsys.readouterr().err
        assert "timeout" in err


class TestCmdMonitor:
    """Cover cmd_monitor lines 615-918: the live dashboard render loop."""

    def test_monitor_not_running(self, capsys):
        """Monitor renders 'not running' panel when bot is down."""
        from timba.cli import cmd_monitor
        args = MagicMock()
        args.host = "127.0.0.1"
        args.port = 8080
        args.interval = 1

        with patch("timba.client.BotClient") as mock_cls, \
             patch("rich.live.Live") as mock_live_cls:
            client = mock_cls.return_value
            client.is_running.return_value = False

            # Make the Live context manager raise KeyboardInterrupt to exit
            mock_live_cls.return_value.__enter__ = MagicMock(side_effect=KeyboardInterrupt)
            mock_live_cls.return_value.__exit__ = MagicMock(return_value=False)

            cmd_monitor(args)
            # Should not crash

    def test_monitor_running_with_trades(self):
        """Monitor renders full dashboard with trade data."""
        from timba.cli import cmd_monitor
        args = MagicMock()
        args.host = "127.0.0.1"
        args.port = 8080
        args.interval = 1

        mock_status = {
            "health": {"status": "ok", "uptime_seconds": 3600},
            "state": {"cash": 95.0, "portfolio": 100.0, "pending_redemption": 1.5},
            "version": "1.0.0",
            "strategies": {
                "favorite": {
                    "markets": [
                        {"coin": "btc", "interval": "5m", "mode": "paper"},
                    ],
                },
            },
        }
        mock_trades = [
            {"type": "paper_win", "strategy": "favorite", "slug": "btc-updown-5m-100",
             "buy_price": 0.95, "contracts": 5, "pnl": 0.25, "side": "up",
             "sniped_at": "2026-03-26T10:00:00+00:00",
             "resolved_at": "2026-03-26T10:05:00+00:00"},
            {"type": "paper_loss", "strategy": "favorite", "slug": "btc-updown-5m-101",
             "buy_price": 0.90, "contracts": 5, "pnl": -4.50, "side": "up",
             "sniped_at": "2026-03-26T10:10:00+00:00",
             "resolved_at": "2026-03-26T10:15:00+00:00"},
        ]

        with patch("timba.client.BotClient") as mock_cls, \
             patch("rich.live.Live") as mock_live_cls:
            client = mock_cls.return_value
            client.is_running.return_value = True
            client.status.return_value = mock_status
            client.trades.return_value = mock_trades
            client.logs.return_value = [
                "10:00:00 INFO     tick",
                "10:01:00 WARNING  stale",
                "10:02:00 ERROR    boom",
            ]

            # Make the Live context manager raise KeyboardInterrupt immediately
            mock_live_cls.return_value.__enter__ = MagicMock(side_effect=KeyboardInterrupt)
            mock_live_cls.return_value.__exit__ = MagicMock(return_value=False)

            cmd_monitor(args)
            # Should not crash

    def test_monitor_api_error(self):
        """Monitor handles API errors gracefully."""
        from timba.cli import cmd_monitor
        args = MagicMock()
        args.host = "127.0.0.1"
        args.port = 8080
        args.interval = 1

        with patch("timba.client.BotClient") as mock_cls, \
             patch("rich.live.Live") as mock_live_cls:
            client = mock_cls.return_value
            client.is_running.return_value = True
            client.status.side_effect = Exception("connection refused")

            mock_live_cls.return_value.__enter__ = MagicMock(side_effect=KeyboardInterrupt)
            mock_live_cls.return_value.__exit__ = MagicMock(return_value=False)

            cmd_monitor(args)


class TestCmdStartLogLevelPriority:
    """Cover log level priority: CLI flag > env var > config.yaml."""

    def test_log_level_from_env_var(self, tmp_path, monkeypatch):
        """LOG_LEVEL env var is used when no CLI flag."""
        cfg_file = _valid_config_file(tmp_path)
        monkeypatch.setenv("TIMBA_HOME", str(tmp_path))
        monkeypatch.setenv("BOT_ENV", "testenv")
        monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0xtest_key")
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        (tmp_path / ".env").write_text("")

        from timba.cli import cmd_start
        args = MagicMock()
        args.config = str(cfg_file)
        args.port = 8080
        args.log_level = None  # no CLI flag

        mock_clob = MagicMock()
        mock_clob.create_or_derive_api_creds.return_value = MagicMock()

        with patch("polymarket_apis.PolymarketClobClient", return_value=mock_clob), \
             patch("timba.cli._banner", return_value="Timba\n"), \
             patch("timba.cli._print_market_table"), \
             patch("timba.cli._detect_env", return_value="testenv"), \
             patch("timba.cli._data_dir", return_value=tmp_path / "data" / "testenv"), \
             patch("timba.cli._setup_logging") as mock_log, \
             patch("timba.db.init"), \
             patch("timba.ticks.init_ids"), \
             patch("timba.state.init_trade_ids"), \
             patch("timba.db.get_pending_redemption", return_value=0), \
             patch("timba.reconcile.reconcile_startup"), \
             patch("timba.trader.Trader") as mock_trader_cls, \
             patch("timba.server.start_api_server", return_value=MagicMock()), \
             patch("timba.cli._write_bot_json"), \
             patch("timba.cli._remove_bot_json"), \
             patch("timba.health.LogFileHandler", return_value=MagicMock()), \
             patch("signal.signal"), \
             patch("atexit.register"):
            (tmp_path / "data" / "testenv").mkdir(parents=True, exist_ok=True)
            mock_trader_cls.return_value = MagicMock()
            cmd_start(args)
            # Should use LOG_LEVEL env var since CLI flag is None
            mock_log.assert_called_once_with("DEBUG")


class TestCmdMonitorLiveTradesSection:
    """Cover the monitor render function branches for live vs paper trade lists."""

    def test_monitor_with_live_and_paper_trades(self):
        """Monitor renders both live and paper recent trade sections."""
        from timba.cli import cmd_monitor
        args = MagicMock()
        args.host = "127.0.0.1"
        args.port = 8080
        args.interval = 1

        mock_status = {
            "health": {"status": "ok", "uptime_seconds": 7200},
            "state": {"cash": 90.0, "portfolio": 100.0, "pending_redemption": 0},
            "version": "1.0.0",
            "strategies": {
                "favorite": {
                    "markets": [
                        {"coin": "btc", "interval": "5m", "mode": "live"},
                        {"coin": "eth", "interval": "5m", "mode": "paper"},
                    ],
                },
            },
        }
        mock_trades = [
            {"type": "win", "strategy": "favorite", "slug": "btc-updown-5m-100",
             "buy_price": 0.95, "contracts": 5, "pnl": 0.25, "side": "up",
             "sniped_at": "2026-03-26T10:00:00+00:00",
             "resolved_at": "2026-03-26T10:05:00+00:00",
             "market_mode": "live"},
            {"type": "loss", "strategy": "favorite", "slug": "btc-updown-5m-101",
             "buy_price": 0.90, "contracts": 5, "pnl": -4.50, "side": "down",
             "sniped_at": "2026-03-26T10:10:00+00:00",
             "resolved_at": "2026-03-26T10:15:00+00:00",
             "market_mode": "live"},
            {"type": "paper_win", "strategy": "favorite", "slug": "eth-updown-5m-200",
             "buy_price": 0.85, "contracts": 5, "pnl": 0.75, "side": "up",
             "sniped_at": "2026-03-26T10:00:00+00:00",
             "resolved_at": "2026-03-26T10:05:00+00:00",
             "market_mode": "paper"},
            {"type": "skip_win", "strategy": "favorite", "slug": "btc-updown-5m-102",
             "buy_price": 0, "contracts": 0, "pnl": 0, "side": "up",
             "market_mode": "live"},
            {"type": "skip_loss", "strategy": "favorite", "slug": "eth-updown-5m-201",
             "buy_price": 0, "contracts": 0, "pnl": 0, "side": "down",
             "market_mode": "paper"},
            {"type": "fail_win", "strategy": "favorite", "slug": "btc-updown-5m-103",
             "buy_price": 0.70, "contracts": 5, "pnl": 0, "side": "up",
             "market_mode": "live"},
            {"type": "fail_loss", "strategy": "favorite", "slug": "eth-updown-5m-202",
             "buy_price": 0.75, "contracts": 5, "pnl": 0, "side": "down",
             "market_mode": "paper"},
            {"type": "skip_none", "strategy": "favorite", "slug": "btc-updown-5m-104",
             "buy_price": 0, "contracts": 0, "pnl": 0, "side": "",
             "market_mode": "paper"},
        ]

        with patch("timba.client.BotClient") as mock_cls, \
             patch("rich.live.Live") as mock_live_cls:
            client = mock_cls.return_value
            client.is_running.return_value = True
            client.status.return_value = mock_status
            client.trades.return_value = mock_trades
            client.logs.return_value = []

            mock_live_cls.return_value.__enter__ = MagicMock(side_effect=KeyboardInterrupt)
            mock_live_cls.return_value.__exit__ = MagicMock(return_value=False)

            cmd_monitor(args)


class TestCmdInitExistingOverwrite:
    """Cover cmd_init with existing .env and user choosing to overwrite."""

    def test_existing_env_overwrite(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("TIMBA_HOME", str(tmp_path))
        env_file = tmp_path / ".env"
        env_file.write_text("OLD=value\n")
        from timba.cli import cmd_init
        # First prompt is "Overwrite? [y/N]" -> "y", then getpass + input for credentials
        with patch("builtins.input", side_effect=["y", "0xnewfunder"]), \
             patch("timba.cli.getpass.getpass", return_value="0xnewkey"):
            args = MagicMock()
            cmd_init(args)
        content = env_file.read_text()
        assert "0xnewkey" in content
        assert "0xnewfunder" in content


class TestPrintMarketTableModes:
    """Cover _print_market_table with various market modes."""

    def test_live_and_off_modes(self, capsys):
        cfg = Config()
        from timba.config import StrategyConfig
        cfg.strategies["favorite"] = StrategyConfig({
            "enabled": True,
            "markets": [
                {"coin": "btc", "interval": "5m", "mode": "live"},
                {"coin": "btc", "interval": "15m", "mode": "off"},
                {"coin": "eth", "interval": "5m", "mode": "paper"},
            ],
        })
        _print_market_table(cfg)
        out = capsys.readouterr().out
        assert "LIVE" in out
        assert "OFF" in out
        assert "PPR" in out

    def test_portfolio_display(self, capsys):
        cfg = Config()
        from timba.config import StrategyConfig
        cfg.strategies["favorite"] = StrategyConfig({
            "enabled": True,
            "contracts_per_trade": 10,
            "markets": [
                {"coin": "btc", "interval": "5m", "mode": "live"},
            ],
        })
        _print_market_table(cfg)
        out = capsys.readouterr().out
        assert "Estimated capital" in out


class TestMainAnalyzeSubcommand:
    """Cover main() with 'analyze' command (no sub-subcommand -> shows help)."""

    def test_analyze_no_subcommand_shows_help(self, capsys):
        with patch("sys.argv", ["timba", "analyze"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0


class TestCmdConfig:
    """Tests for timba config command."""

    def _write_config(self, tmp_path, content):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(content)
        return cfg

    def test_summary_shows_strategy_and_settings(self, tmp_path, capsys):
        cfg = self._write_config(tmp_path, """
favorite:
  enabled: true
  min_price: 0.95
  contracts_per_trade: 10
  markets:
    - coin: btc
      interval: 5m
      mode: paper
      entry_window_sec: 15
      close_window_sec: 2
""")
        args = argparse.Namespace(config=str(cfg), raw=False, verbose=False, no_color=True)
        cmd_config(args)
        out = capsys.readouterr().out
        assert "favorite" in out
        assert "enabled" in out
        assert "min_price: 0.95" in out
        assert "contracts_per_trade: 10" in out
        assert "1 (1 coins: btc)" in out
        assert "paper" in out

    def test_summary_shows_disabled_strategy(self, tmp_path, capsys):
        cfg = self._write_config(tmp_path, """
favorite:
  enabled: false
  markets: []
""")
        args = argparse.Namespace(config=str(cfg), raw=False, verbose=False, no_color=True)
        cmd_config(args)
        out = capsys.readouterr().out
        assert "disabled" in out

    def test_summary_mixed_modes(self, tmp_path, capsys):
        cfg = self._write_config(tmp_path, """
favorite:
  enabled: true
  min_price: 0.95
  markets:
    - coin: btc
      interval: 5m
      mode: live
      entry_window_sec: 15
      close_window_sec: 2
    - coin: eth
      interval: 5m
      mode: paper
      entry_window_sec: 15
      close_window_sec: 2
""")
        args = argparse.Namespace(config=str(cfg), raw=False, verbose=False, no_color=True)
        cmd_config(args)
        out = capsys.readouterr().out
        assert "1 live" in out
        assert "1 paper" in out
        assert "2 (2 coins:" in out

    def test_verbose_shows_per_market(self, tmp_path, capsys):
        cfg = self._write_config(tmp_path, """
favorite:
  enabled: true
  min_price: 0.95
  markets:
    - coin: btc
      interval: 5m
      mode: paper
      entry_window_sec: 15
      close_window_sec: 2
    - coin: eth
      interval: 15m
      mode: live
      entry_window_sec: 30
      close_window_sec: 3
""")
        args = argparse.Namespace(config=str(cfg), raw=False, verbose=True, no_color=True)
        cmd_config(args)
        out = capsys.readouterr().out
        assert "BTC" in out
        assert "ETH" in out
        assert "entry=15s" in out
        assert "entry=30s" in out
        assert "close=2s" in out
        assert "close=3s" in out

    def test_raw_dumps_yaml(self, tmp_path, capsys):
        content = "log_level: DEBUG\nfavorite:\n  enabled: true\n  markets: []\n"
        cfg = self._write_config(tmp_path, content)
        args = argparse.Namespace(config=str(cfg), raw=True, verbose=False, no_color=True)
        cmd_config(args)
        out = capsys.readouterr().out
        assert "log_level: DEBUG" in out

    def test_invalid_config_exits(self, tmp_path):
        cfg = self._write_config(tmp_path, "favorite:\n  typo_key: 123\n")
        args = argparse.Namespace(config=str(cfg), raw=False, verbose=False, no_color=True)
        with pytest.raises(SystemExit):
            cmd_config(args)

    def test_no_markets_shows_none(self, tmp_path, capsys):
        cfg = self._write_config(tmp_path, """
favorite:
  enabled: true
  min_price: 0.95
  markets: []
""")
        args = argparse.Namespace(config=str(cfg), raw=False, verbose=False, no_color=True)
        cmd_config(args)
        out = capsys.readouterr().out
        assert "none" in out
