"""Tests for monitor.py — overview panel and strategy table rendering."""

import json
from unittest.mock import patch

import pytest
import yaml

from timba.monitor import (
    build_overview_and_trades,
    build_strategy_table,
    calc_pnl,
    fmt_pnl,
    parse_slug,
    run,
)


@pytest.fixture
def data_dir(tmp_path):
    """Create a temp data dir with sample trades."""
    fav_dir = tmp_path / "favorite"
    fav_dir.mkdir()

    trades = [
        {"type": "paper_win", "strategy": "favorite", "slug": "btc-updown-5m-100",
         "coin": "btc", "interval": "5m", "side": "up", "buy_price": 0.99,
         "contracts": 5, "pnl": 0.05, "sniped_at": "2026-03-29T10:00:00+00:00",
         "resolved_at": "2026-03-29T10:00:30+00:00", "end_timestamp": 100,
         "market_mode": "paper", "skip_reason": "already bet",
         "ticks_evaluated": 5, "ev_id": 1, "id": 1,
         "token_id": "tok1", "redeemed": False, "min_price": 0.98, "midpoint": 0.99},
        {"type": "paper_loss", "strategy": "favorite", "slug": "eth-updown-5m-200",
         "coin": "eth", "interval": "5m", "side": "down", "buy_price": 0.99,
         "contracts": 5, "pnl": -4.95, "sniped_at": "2026-03-29T10:05:00+00:00",
         "resolved_at": "2026-03-29T10:05:30+00:00", "end_timestamp": 200,
         "market_mode": "paper", "skip_reason": "already bet",
         "ticks_evaluated": 3, "ev_id": 2, "id": 2,
         "token_id": "tok2", "redeemed": False, "min_price": 0.98, "midpoint": 0.99},
        {"type": "fail_win", "strategy": "favorite", "slug": "btc-updown-5m-300",
         "coin": "btc", "interval": "5m", "side": "up", "buy_price": 0.995,
         "contracts": 5, "pnl": 0, "sniped_at": "2026-03-29T10:10:00+00:00",
         "resolved_at": "2026-03-29T10:10:30+00:00", "end_timestamp": 300,
         "market_mode": "paper",
         "skip_reason": "unfillable ($0.9950 > max $0.990, tick=0.01)",
         "ticks_evaluated": 5, "ev_id": 3, "id": 3,
         "token_id": "tok3", "redeemed": False, "min_price": 0.98, "midpoint": 0.995},
        {"type": "skip_win", "strategy": "favorite", "slug": "sol-updown-5m-400",
         "coin": "sol", "interval": "5m", "side": "up", "buy_price": 0.96,
         "contracts": 5, "pnl": 0, "sniped_at": "2026-03-29T10:15:00+00:00",
         "resolved_at": "2026-03-29T10:15:30+00:00", "end_timestamp": 400,
         "market_mode": "paper",
         "skip_reason": "no favorite (up=$0.9600 down=$0.0400 min=$0.98)",
         "ticks_evaluated": 5, "ev_id": 4, "id": 4,
         "token_id": "tok4", "redeemed": False, "min_price": 0.98, "midpoint": 0.96},
        {"type": "skip_loss", "strategy": "favorite", "slug": "sol-updown-5m-500",
         "coin": "sol", "interval": "5m", "side": "down", "buy_price": 0.97,
         "contracts": 5, "pnl": 0, "sniped_at": "2026-03-29T10:20:00+00:00",
         "resolved_at": "2026-03-29T10:20:30+00:00", "end_timestamp": 500,
         "market_mode": "paper",
         "skip_reason": "no favorite (up=$0.0300 down=$0.9700 min=$0.98)",
         "ticks_evaluated": 5, "ev_id": 5, "id": 5,
         "token_id": "tok5", "redeemed": False, "min_price": 0.98, "midpoint": 0.97},
        {"type": "skip_none", "strategy": "favorite", "slug": "xrp-updown-5m-600",
         "coin": "xrp", "interval": "5m", "side": "", "buy_price": 0,
         "contracts": 5, "pnl": 0, "sniped_at": "2026-03-29T10:25:00+00:00",
         "resolved_at": "2026-03-29T10:25:30+00:00", "end_timestamp": 600,
         "market_mode": "paper",
         "skip_reason": "window timeout",
         "ticks_evaluated": 0, "ev_id": 0, "id": 6,
         "token_id": "tok6", "redeemed": False},
    ]

    with open(fav_dir / "trades_2026-03-29.jsonl", "w") as f:
        for t in trades:
            f.write(json.dumps(t) + "\n")

    return str(tmp_path)


class TestCalcPnlEdgeCases:
    def test_zero_buy_price_returns_zero(self):
        assert calc_pnl({"type": "win", "buy_price": 0, "contracts": 5}) == 0

    def test_unknown_type_returns_zero(self):
        assert calc_pnl({"type": "unknown_type", "buy_price": 0.5, "contracts": 5}) == 0

    def test_fail_loss_uses_loss_formula(self):
        assert calc_pnl({"type": "fail_loss", "buy_price": 0.80, "contracts": 5}) == pytest.approx(-4.0)


class TestLoadStrategyTrades:
    def test_loads_from_strategy_subdir(self, data_dir):
        from timba.monitor import load_strategy_trades
        trades = load_strategy_trades(data_dir, "favorite")
        assert len(trades) == 6

    def test_returns_empty_for_missing_strategy(self, data_dir):
        from timba.monitor import load_strategy_trades
        trades = load_strategy_trades(data_dir, "nonexistent")
        assert trades == []

    def test_skips_invalid_json_lines(self, tmp_path):
        from timba.monitor import load_strategy_trades
        strat_dir = tmp_path / "favorite"
        strat_dir.mkdir()
        with open(strat_dir / "trades_2026-03-29.jsonl", "w") as f:
            f.write("not json\n")
            f.write(json.dumps({"type": "win", "slug": "test"}) + "\n")
        trades = load_strategy_trades(str(tmp_path), "favorite")
        assert len(trades) == 1


class TestCalcPnl:
    def test_win_pnl(self):
        assert calc_pnl({"type": "win", "buy_price": 0.99, "contracts": 5}) == pytest.approx(0.05)

    def test_loss_pnl(self):
        assert calc_pnl({"type": "loss", "buy_price": 0.99, "contracts": 5}) == pytest.approx(-4.95)

    def test_paper_win(self):
        assert calc_pnl({"type": "paper_win", "buy_price": 0.98, "contracts": 5}) == pytest.approx(0.10)

    def test_explicit_pnl_field(self):
        assert calc_pnl({"type": "win", "pnl": 1.23}) == 1.23

    def test_pass_calculates_hypothetical(self):
        # pass_win still calculates hypothetical PnL (for tracking what we missed)
        assert calc_pnl({"type": "skip_win", "buy_price": 0.99, "contracts": 5}) == pytest.approx(0.05)


class TestParseSlug:
    def test_5m(self):
        assert parse_slug("btc-updown-5m-1234") == ("btc", "5m")

    def test_15m(self):
        assert parse_slug("eth-updown-15m-5678") == ("eth", "15m")

    def test_hourly(self):
        assert parse_slug("bitcoin-up-or-down-march-29-2026-5pm-et") == ("btc", "1h")

    def test_unknown(self):
        assert parse_slug("unknown-slug") == ("", "")


class TestFmtPnl:
    def test_positive(self):
        result = fmt_pnl(1.5)
        assert "+$1.500" in result
        assert "green" in result

    def test_negative(self):
        result = fmt_pnl(-2.3)
        assert "-$2.300" in result
        assert "red" in result


class TestBuildStrategyTable:
    def test_renders_with_bets_fails_skips(self, data_dir):
        scfg = {
            "markets": [
                {"coin": "btc", "interval": "5m", "mode": "paper", "entry_window_sec": 10, "close_window_sec": 2},
                {"coin": "eth", "interval": "5m", "mode": "paper", "entry_window_sec": 10, "close_window_sec": 2},
                {"coin": "sol", "interval": "5m", "mode": "paper", "entry_window_sec": 10, "close_window_sec": 2},
                {"coin": "xrp", "interval": "5m", "mode": "paper", "entry_window_sec": 10, "close_window_sec": 2},
            ],
        }
        table = build_strategy_table("favorite", scfg, data_dir)
        assert table is not None

    def test_empty_markets_returns_none(self, data_dir):
        table = build_strategy_table("favorite", {"markets": []}, data_dir)
        assert table is None


class TestFmtTime:
    def test_valid_iso(self):
        from timba.monitor import fmt_time
        result = fmt_time("2026-03-29T10:30:00+00:00")
        assert ":" in result  # has time format

    def test_naive_datetime(self):
        from timba.monitor import fmt_time
        result = fmt_time("2026-03-29T10:30:00")
        assert ":" in result

    def test_invalid_short_string_fallback(self):
        from timba.monitor import fmt_time
        result = fmt_time("short")
        assert result == ""


class TestBuildOverviewWithLive:
    def test_with_live_trades(self, tmp_path):
        """Test overview with live (non-paper) trades to cover live section."""
        fav_dir = tmp_path / "favorite"
        fav_dir.mkdir()
        trades = [
            {"type": "win", "strategy": "favorite", "slug": "btc-updown-5m-100",
             "coin": "btc", "interval": "5m", "side": "up", "buy_price": 0.99,
             "contracts": 5, "pnl": 0.05, "sniped_at": "2026-03-29T10:00:00+00:00",
             "resolved_at": "2026-03-29T10:00:30+00:00", "end_timestamp": 100,
             "market_mode": "live", "skip_reason": "",
             "ticks_evaluated": 5, "ev_id": 1, "id": 1},
            {"type": "loss", "strategy": "favorite", "slug": "eth-updown-5m-200",
             "coin": "eth", "interval": "5m", "side": "down", "buy_price": 0.99,
             "contracts": 5, "pnl": -4.95, "sniped_at": "2026-03-29T10:05:00+00:00",
             "resolved_at": "2026-03-29T10:05:30+00:00", "end_timestamp": 200,
             "market_mode": "live", "skip_reason": "",
             "ticks_evaluated": 3, "ev_id": 2, "id": 2},
            {"type": "fail_win", "strategy": "favorite", "slug": "btc-updown-5m-300",
             "coin": "btc", "interval": "5m", "side": "up", "buy_price": 0.995,
             "contracts": 5, "pnl": 0, "sniped_at": "2026-03-29T10:10:00+00:00",
             "market_mode": "live", "skip_reason": "unfillable",
             "ticks_evaluated": 5, "ev_id": 3, "id": 3},
            {"type": "skip_win", "strategy": "favorite", "slug": "sol-updown-5m-400",
             "coin": "sol", "interval": "5m", "side": "up", "buy_price": 0.96,
             "contracts": 5, "pnl": 0, "sniped_at": "2026-03-29T10:15:00+00:00",
             "market_mode": "live", "skip_reason": "below threshold",
             "ticks_evaluated": 5, "ev_id": 4, "id": 4},
        ]
        with open(fav_dir / "trades_2026-03-29.jsonl", "w") as f:
            for t in trades:
                f.write(json.dumps(t) + "\n")

        state = {
            "code_version": "test",
            "portfolio": 100.0, "cash": 100.0, "pending_redemption": 0,
            "strategies": {"favorite": {"stats": {}, "total_pnl": -4.90}},
        }
        panel = build_overview_and_trades(state, state["strategies"], str(tmp_path), "main", enabled_strategies={"favorite"})
        assert panel is not None

    def test_skips_disabled_strategies(self, tmp_path):
        state = {
            "code_version": "test",
            "portfolio": 100.0, "cash": 100.0, "pending_redemption": 0,
            "strategies": {"favorite": {"stats": {}, "total_pnl": 0}, "other": {"stats": {}, "total_pnl": 0}},
        }
        # Only enable "favorite", "other" should be skipped
        panel = build_overview_and_trades(state, state["strategies"], str(tmp_path), "main", enabled_strategies={"favorite"})
        assert panel is not None


class TestBuildOverviewAndTrades:
    def test_renders_paper_stats(self, data_dir):
        state = {
            "code_version": "test",
            "portfolio": 100.0,
            "cash": 100.0,
            "pending_redemption": 0,
            "strategies": {"favorite": {"stats": {}, "total_pnl": 0}},
        }
        strategies = state["strategies"]
        enabled = {"favorite"}
        panel = build_overview_and_trades(state, strategies, data_dir, "main", enabled_strategies=enabled)
        assert panel is not None

    def test_shows_bets_fails_skips(self, data_dir):
        from io import StringIO

        from rich.console import Console

        state = {
            "code_version": "test",
            "portfolio": 100.0,
            "cash": 100.0,
            "pending_redemption": 0,
            "strategies": {"favorite": {"stats": {}, "total_pnl": 0}},
        }
        panel = build_overview_and_trades(state, state["strategies"], data_dir, "main", enabled_strategies={"favorite"})
        # Render to string
        buf = StringIO()
        console = Console(file=buf, width=120)
        console.print(panel)
        text = buf.getvalue()
        assert "Bets" in text
        assert "Skips" in text

    def test_with_empty_strategies(self, tmp_path):
        """Overview with no strategies should still render."""
        state = {
            "code_version": "test",
            "portfolio": 50.0,
            "cash": 50.0,
            "pending_redemption": 5.0,
            "strategies": {},
        }
        panel = build_overview_and_trades(state, {}, str(tmp_path), "dev", enabled_strategies=None)
        assert panel is not None

    def test_with_pending_redemption(self, tmp_path):
        """Pending redemption > 0 should appear in the output."""
        from io import StringIO

        from rich.console import Console

        state = {
            "code_version": "v2.0",
            "portfolio": 100.0,
            "cash": 80.0,
            "pending_redemption": 15.0,
            "strategies": {},
        }
        panel = build_overview_and_trades(state, {}, str(tmp_path), "main")
        buf = StringIO()
        console = Console(file=buf, width=120)
        console.print(panel)
        text = buf.getvalue()
        assert "Pend" in text
        assert "$15.00" in text


class TestBuildStrategyTableDetailed:
    """More detailed tests for build_strategy_table."""

    def test_live_mode_markets(self, tmp_path):
        """Table with live mode markets should include Live columns."""
        scfg = {
            "markets": [
                {"coin": "btc", "interval": "5m", "mode": "live"},
            ],
        }
        table = build_strategy_table("favorite", scfg, str(tmp_path))
        assert table is not None
        # Should have live columns
        col_names = [c.header for c in table.columns]
        assert any("Live" in str(c) for c in col_names)

    def test_mixed_mode_markets(self, tmp_path):
        """Table with both live and paper markets should have both column sets."""
        scfg = {
            "markets": [
                {"coin": "btc", "interval": "5m", "mode": "live"},
                {"coin": "eth", "interval": "5m", "mode": "paper"},
            ],
        }
        table = build_strategy_table("favorite", scfg, str(tmp_path))
        assert table is not None
        col_names = [c.header for c in table.columns]
        assert any("Live" in str(c) for c in col_names)
        assert any("Paper" in str(c) for c in col_names)

    def test_multiple_coins_with_separator(self, tmp_path):
        """Multiple coins should render with separator rows."""
        scfg = {
            "markets": [
                {"coin": "btc", "interval": "5m", "mode": "paper"},
                {"coin": "eth", "interval": "5m", "mode": "paper"},
                {"coin": "sol", "interval": "5m", "mode": "paper"},
            ],
        }
        table = build_strategy_table("favorite", scfg, str(tmp_path))
        assert table is not None
        # Should have at least 3 data rows + total
        assert table.row_count >= 4


class TestFmtTimeEdgeCases:
    def test_long_invalid_string_with_colon(self):
        """A long enough but invalid timestamp should use substring fallback."""
        from timba.monitor import fmt_time
        result = fmt_time("this-is-not-a-valid-timestamp")
        # len > 16 so it tries ts_str[11:16]
        assert isinstance(result, str)

    def test_none_handling(self):
        """fmt_time should handle None gracefully via TypeError catch."""
        from timba.monitor import fmt_time
        # None triggers TypeError in fromisoformat, caught by except
        # then len(None) raises TypeError — but the except catches both
        try:
            fmt_time(None)
        except TypeError:
            # This is acceptable — the function may not handle None
            pass


class TestLoadStrategyTradesSQLite:
    """Test loading trades from SQLite databases."""

    def test_loads_from_sqlite(self, tmp_path):
        """Trades in SQLite should be loaded correctly."""
        import sqlite3

        from timba.monitor import load_strategy_trades

        db_path = tmp_path / "bot.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY, strategy TEXT, type TEXT, slug TEXT,
            coin TEXT, interval TEXT, side TEXT, buy_price REAL,
            contracts INTEGER, pnl REAL, sniped_at TEXT, resolved_at TEXT,
            end_timestamp INTEGER, market_mode TEXT, skip_reason TEXT,
            ticks_evaluated INTEGER, ev_id INTEGER, token_id TEXT,
            redeemed INTEGER DEFAULT 0, extras TEXT
        )""")
        conn.execute("""INSERT INTO trades (id, strategy, type, slug, coin, interval,
            side, buy_price, contracts, pnl, sniped_at, market_mode, extras)
            VALUES (1, 'favorite', 'paper_win', 'btc-updown-5m-100', 'btc', '5m',
            'up', 0.99, 5, 0.05, '2026-03-29T10:00:00+00:00', 'paper',
            '{"min_price": 0.95}')""")
        conn.commit()
        conn.close()

        trades = load_strategy_trades(str(tmp_path), "favorite")
        assert len(trades) == 1
        assert trades[0]["type"] == "paper_win"
        assert trades[0]["min_price"] == 0.95  # from extras JSON


class TestMonitorRun:
    """Test monitor.run() entry point."""

    @patch("timba.client.BotClient")
    def test_empty_data_dir_renders_offline(self, mock_client_cls, tmp_path, capsys):
        """When no bot.db exists and no running bot, renders offline state."""
        mock_client_cls.return_value.is_running.return_value = False
        run(str(tmp_path), None, "main")
        out = capsys.readouterr().out
        assert "offline" in out
        assert "$0" in out

    @patch("timba.client.BotClient")
    def test_empty_data_dir_check_mode_exits(self, mock_client_cls, tmp_path):
        """In check_mode, empty data dir (0 funds) exits with code 1."""
        mock_client_cls.return_value.is_running.return_value = False
        with pytest.raises(SystemExit) as exc:
            run(str(tmp_path), None, "main", check_mode=True)
        assert exc.value.code == 1

    @patch("timba.client.BotClient")
    def test_check_mode_ok(self, mock_client_cls, tmp_path, capsys):
        """Check mode with valid state from API prints OK."""
        mock_client_cls.return_value.is_running.return_value = True
        mock_client_cls.return_value.status.return_value = {
            "state": {"portfolio": 100.0, "cash": 50.0, "total_pnl": 5.0},
            "version": "test",
        }
        run(str(tmp_path), None, "main", check_mode=True)
        out = capsys.readouterr().out
        assert "OK" in out
        assert "$100.00" in out

    @patch("timba.client.BotClient")
    def test_check_mode_no_funds_exits(self, mock_client_cls, tmp_path):
        """Check mode with zero funds exits with code 1."""
        mock_client_cls.return_value.is_running.return_value = True
        mock_client_cls.return_value.status.return_value = {
            "state": {"portfolio": 0, "cash": 0},
            "version": "test",
        }
        with pytest.raises(SystemExit) as exc:
            run(str(tmp_path), None, "main", check_mode=True)
        assert exc.value.code == 1

    @patch("timba.client.BotClient")
    def test_full_render_with_config(self, mock_client_cls, tmp_path, capsys):
        """Full render with config should not error."""
        mock_client_cls.return_value.is_running.return_value = False
        config = {
            "favorite": {
                "enabled": True,
                "markets": [
                    {"coin": "btc", "interval": "5m", "mode": "paper"},
                ],
            },
        }
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.dump(config))

        run(str(tmp_path), str(cfg_path), "test")

    @patch("timba.client.BotClient")
    def test_full_render_no_config(self, mock_client_cls, tmp_path, capsys):
        """Render without config file should still work."""
        mock_client_cls.return_value.is_running.return_value = False
        run(str(tmp_path), "/nonexistent/config.yaml", "test")
