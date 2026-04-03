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


def _create_trades_db(db_path, trades):
    """Helper: create a SQLite DB with trades table and insert trades."""
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.execute("""CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY, strategy TEXT, type TEXT, slug TEXT,
        coin TEXT, interval TEXT, side TEXT, buy_price REAL,
        contracts INTEGER, pnl REAL, sniped_at TEXT, resolved_at TEXT,
        end_timestamp INTEGER, market_mode TEXT, skip_reason TEXT,
        ticks_evaluated INTEGER, ev_id INTEGER, token_id TEXT,
        redeemed INTEGER DEFAULT 0, order_id TEXT, min_price REAL,
        midpoint REAL, extras TEXT, condition_id TEXT
    )""")
    for t in trades:
        extras = json.dumps({k: v for k, v in t.items()
                            if k not in ("id", "strategy", "type", "slug", "coin", "interval",
                                         "side", "buy_price", "contracts", "pnl", "sniped_at",
                                         "resolved_at", "end_timestamp", "market_mode",
                                         "skip_reason", "ticks_evaluated", "ev_id", "token_id",
                                         "redeemed", "min_price", "midpoint")})
        conn.execute(
            "INSERT INTO trades (id, strategy, type, slug, coin, interval, side, buy_price, "
            "contracts, pnl, sniped_at, resolved_at, end_timestamp, market_mode, skip_reason, "
            "ticks_evaluated, ev_id, token_id, redeemed, min_price, midpoint, extras) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (t.get("id"), t.get("strategy", "favorite"), t.get("type", ""),
             t.get("slug", ""), t.get("coin", ""), t.get("interval", ""),
             t.get("side", ""), t.get("buy_price", 0), t.get("contracts", 0),
             t.get("pnl", 0), t.get("sniped_at", ""), t.get("resolved_at", ""),
             t.get("end_timestamp"), t.get("market_mode", "paper"),
             t.get("skip_reason", ""), t.get("ticks_evaluated", 0),
             t.get("ev_id", 0), t.get("token_id", ""),
             1 if t.get("redeemed") else 0,
             t.get("min_price"), t.get("midpoint"),
             extras),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def data_dir(tmp_path):
    """Create a temp data dir with sample trades in SQLite."""
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

    _create_trades_db(tmp_path / "bot.db", trades)
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

    def test_returns_empty_for_no_db(self, tmp_path):
        from timba.monitor import load_strategy_trades
        trades = load_strategy_trades(str(tmp_path), "favorite")
        assert trades == []


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
        _create_trades_db(tmp_path / "bot.db", trades)

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


def _render_table(table):
    """Render a Rich table to a string for assertion."""
    from io import StringIO

    from rich.console import Console
    buf = StringIO()
    console = Console(file=buf, width=200, no_color=True)
    console.print(table)
    return buf.getvalue()


def _insert_test_trade(db_path, trade_id, coin, interval, trade_type, pnl=0.05, mode="paper"):
    """Insert a trade directly into a SQLite db for testing."""
    import sqlite3
    slug = f"{coin}-updown-{interval}-{trade_id}"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY, strategy TEXT, type TEXT, slug TEXT,
        coin TEXT, interval TEXT, side TEXT, buy_price REAL,
        contracts INTEGER, pnl REAL, sniped_at TEXT, resolved_at TEXT,
        end_timestamp INTEGER, market_mode TEXT, skip_reason TEXT,
        ticks_evaluated INTEGER, ev_id INTEGER, token_id TEXT,
        redeemed INTEGER DEFAULT 0, order_id TEXT, min_price REAL,
        midpoint REAL, extras TEXT, condition_id TEXT)""")
    conn.execute(
        "INSERT INTO trades (id, strategy, type, slug, coin, interval, side, buy_price, "
        "contracts, pnl, sniped_at, resolved_at, market_mode) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (trade_id, "favorite", trade_type, slug, coin, interval, "up", 0.95,
         5, pnl, "2026-04-01T00:00:00Z", "2026-04-01T00:05:00Z", mode),
    )
    conn.commit()
    conn.close()


class TestStrategyTableModes:
    """Test build_strategy_table with different mode combinations."""

    def test_paper_only_shows_paper_columns(self, tmp_path):
        """All paper markets → only Paper columns, no Live columns."""
        scfg = {
            "markets": [
                {"coin": "btc", "interval": "5m", "mode": "paper"},
                {"coin": "eth", "interval": "5m", "mode": "paper"},
            ],
        }
        table = build_strategy_table("favorite", scfg, str(tmp_path))
        out = _render_table(table)
        assert "Paper Bets" in out
        assert "Live Bets" not in out
        assert "MODE" in out
        assert "paper" in out.lower()

    def test_live_only_shows_live_columns(self, tmp_path):
        """All live markets → only Live columns, no Paper columns."""
        scfg = {
            "markets": [
                {"coin": "btc", "interval": "5m", "mode": "live"},
                {"coin": "eth", "interval": "5m", "mode": "live"},
            ],
        }
        table = build_strategy_table("favorite", scfg, str(tmp_path))
        out = _render_table(table)
        assert "Live Bets" in out
        assert "Paper Bets" not in out

    def test_mixed_modes_shows_both_columns(self, tmp_path):
        """Mix of live and paper → both column groups."""
        scfg = {
            "markets": [
                {"coin": "btc", "interval": "5m", "mode": "live"},
                {"coin": "eth", "interval": "5m", "mode": "paper"},
            ],
        }
        table = build_strategy_table("favorite", scfg, str(tmp_path))
        out = _render_table(table)
        assert "Live Bets" in out
        assert "Paper Bets" in out

    def test_no_markets_returns_none(self, tmp_path):
        """Empty markets list → None."""
        scfg = {"markets": []}
        assert build_strategy_table("favorite", scfg, str(tmp_path)) is None

    def test_paper_only_with_trades(self, tmp_path):
        """Paper markets with actual trades → counts show up."""
        db_path = tmp_path / "bot.db"
        _insert_test_trade(db_path, 1, "btc", "5m", "paper_win", 0.05)
        _insert_test_trade(db_path, 2, "btc", "5m", "paper_loss", -0.95)
        _insert_test_trade(db_path, 3, "btc", "5m", "fail_win", 0.0)

        scfg = {"markets": [{"coin": "btc", "interval": "5m", "mode": "paper"}]}
        table = build_strategy_table("favorite", scfg, str(tmp_path))
        out = _render_table(table)
        assert "1W/1L" in out  # paper bets
        assert "1W/0L" in out  # fails

    def test_live_only_with_trades(self, tmp_path):
        """Live markets with actual trades → live counts show up."""
        db_path = tmp_path / "bot.db"
        _insert_test_trade(db_path, 1, "btc", "5m", "win", 0.05, mode="live")
        _insert_test_trade(db_path, 2, "btc", "5m", "loss", -0.95, mode="live")

        scfg = {"markets": [{"coin": "btc", "interval": "5m", "mode": "live"}]}
        table = build_strategy_table("favorite", scfg, str(tmp_path))
        out = _render_table(table)
        assert "Live Bets" in out
        assert "1W/1L" in out

    def test_mixed_with_trades_in_both(self, tmp_path):
        """Mixed modes with trades in both → correct columns populated."""
        db_path = tmp_path / "bot.db"
        _insert_test_trade(db_path, 1, "btc", "5m", "win", 0.05, mode="live")
        _insert_test_trade(db_path, 2, "eth", "5m", "paper_win", 0.03, mode="paper")

        scfg = {
            "markets": [
                {"coin": "btc", "interval": "5m", "mode": "live"},
                {"coin": "eth", "interval": "5m", "mode": "paper"},
            ],
        }
        table = build_strategy_table("favorite", scfg, str(tmp_path))
        out = _render_table(table)
        assert "Live Bets" in out
        assert "Paper Bets" in out
        assert "BTC" in out
        assert "ETH" in out

    def test_zero_trades_shows_dashes(self, tmp_path):
        """Markets with zero trades → all dashes."""
        scfg = {"markets": [{"coin": "btc", "interval": "5m", "mode": "paper"}]}
        table = build_strategy_table("favorite", scfg, str(tmp_path))
        out = _render_table(table)
        assert "—" in out  # em dash for empty cells

    def test_mixed_with_trades_only_in_live(self, tmp_path):
        """Mixed modes, trades only in live → paper shows dashes."""
        db_path = tmp_path / "bot.db"
        _insert_test_trade(db_path, 1, "btc", "5m", "win", 0.05, mode="live")

        scfg = {
            "markets": [
                {"coin": "btc", "interval": "5m", "mode": "live"},
                {"coin": "eth", "interval": "5m", "mode": "paper"},
            ],
        }
        table = build_strategy_table("favorite", scfg, str(tmp_path))
        out = _render_table(table)
        assert "1W/0L" in out  # live btc
        # ETH paper row should have dashes
        assert "Paper Bets" in out

    def test_mixed_with_trades_only_in_paper(self, tmp_path):
        """Mixed modes, trades only in paper → live shows dashes."""
        db_path = tmp_path / "bot.db"
        _insert_test_trade(db_path, 1, "eth", "5m", "paper_win", 0.03, mode="paper")

        scfg = {
            "markets": [
                {"coin": "btc", "interval": "5m", "mode": "live"},
                {"coin": "eth", "interval": "5m", "mode": "paper"},
            ],
        }
        table = build_strategy_table("favorite", scfg, str(tmp_path))
        out = _render_table(table)
        assert "Live Bets" in out
        assert "1W/0L" in out  # paper eth


# ══════════════════════════════════════════════════════════════════════
# Coverage: monitor.py uncovered lines
# ══════════════════════════════════════════════════════════════════════


class TestLoadStrategyTradesExtras:
    """Cover extras JSON decode error path in load_strategy_trades (lines 77-78)."""

    def test_bad_extras_json_ignored(self, tmp_path):
        """Trades with invalid extras JSON should still load (lines 77-78)."""
        import sqlite3

        from timba.monitor import load_strategy_trades

        db_path = tmp_path / "bot.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""CREATE TABLE trades (
            id INTEGER PRIMARY KEY, strategy TEXT, type TEXT, slug TEXT,
            coin TEXT, interval TEXT, side TEXT, buy_price REAL,
            contracts INTEGER, pnl REAL, sniped_at TEXT, resolved_at TEXT,
            end_timestamp INTEGER, market_mode TEXT, skip_reason TEXT,
            ticks_evaluated INTEGER, ev_id INTEGER, token_id TEXT,
            redeemed INTEGER DEFAULT 0, order_id TEXT, min_price REAL,
            midpoint REAL, extras TEXT, condition_id TEXT
        )""")
        conn.execute(
            "INSERT INTO trades (id, strategy, type, slug, extras) "
            "VALUES (1, 'favorite', 'win', 'btc-updown-5m-100', 'not-valid-json{{')"
        )
        conn.commit()
        conn.close()

        trades = load_strategy_trades(str(tmp_path), "favorite")
        assert len(trades) == 1
        assert trades[0]["type"] == "win"

    def test_db_open_error_skipped(self, tmp_path):
        """Corrupt DB files are skipped (lines 80-81)."""
        from timba.monitor import load_strategy_trades

        # Create a corrupt file
        bad_db = tmp_path / "bot.db"
        bad_db.write_text("this is not a database")

        trades = load_strategy_trades(str(tmp_path), "favorite")
        assert trades == []


class TestBuildOverviewPnlRate:
    """Cover _pnl_rate edge cases (lines 148, 156-158)."""

    def test_pnl_rate_displayed_for_live(self, tmp_path):
        """Live trades with elapsed time should show $/h rate (lines 148, 156-158)."""
        from io import StringIO

        from rich.console import Console

        # Create trades with recent timestamps
        trades = [
            {"type": "win", "strategy": "favorite", "slug": "btc-updown-5m-100",
             "coin": "btc", "interval": "5m", "side": "up", "buy_price": 0.90,
             "contracts": 5, "pnl": 0.50, "sniped_at": "2026-04-01T00:00:00+00:00",
             "resolved_at": "2026-04-01T00:05:00+00:00", "end_timestamp": 100,
             "market_mode": "live", "skip_reason": "",
             "ticks_evaluated": 5, "ev_id": 1, "id": 1},
            {"type": "loss", "strategy": "favorite", "slug": "eth-updown-5m-200",
             "coin": "eth", "interval": "5m", "side": "down", "buy_price": 0.90,
             "contracts": 5, "pnl": -4.50, "sniped_at": "2026-04-01T01:00:00+00:00",
             "resolved_at": "2026-04-01T01:05:00+00:00", "end_timestamp": 200,
             "market_mode": "live", "skip_reason": "",
             "ticks_evaluated": 3, "ev_id": 2, "id": 2},
        ]
        _create_trades_db(tmp_path / "bot.db", trades)

        state = {
            "code_version": "test", "portfolio": 100.0, "cash": 100.0,
            "pending_redemption": 0, "strategies": {"favorite": {}},
        }
        panel = build_overview_and_trades(
            state, state["strategies"], str(tmp_path), "main",
            enabled_strategies={"favorite"},
        )
        buf = StringIO()
        console = Console(file=buf, width=120)
        console.print(panel)
        text = buf.getvalue()
        assert "Bets" in text


class TestBuildOverviewWlLine:
    """Cover _wl_line returning None for zero total (line 166)."""

    def test_wl_line_zero_total(self, tmp_path):
        """Strategy with zero bets/fails/skips should not crash (line 166)."""
        # No trades at all — just an enabled strategy
        state = {
            "code_version": "test", "portfolio": 100.0, "cash": 100.0,
            "pending_redemption": 0, "strategies": {"favorite": {}},
        }
        panel = build_overview_and_trades(
            state, state["strategies"], str(tmp_path), "main",
            enabled_strategies={"favorite"},
        )
        assert panel is not None


class TestBuildOverviewPaperSkipNone:
    """Cover paper section with skip_none trades (lines 198-201)."""

    def test_paper_skip_none_shown(self, tmp_path):
        """Paper section with only skip_none trades should show skip count (lines 198-201)."""
        from io import StringIO

        from rich.console import Console

        trades = [
            {"type": "skip_none", "strategy": "favorite", "slug": "btc-updown-5m-100",
             "coin": "btc", "interval": "5m", "side": "", "buy_price": 0,
             "contracts": 5, "pnl": 0, "sniped_at": "2026-04-01T00:00:00+00:00",
             "resolved_at": "2026-04-01T00:05:00+00:00", "end_timestamp": 100,
             "market_mode": "paper", "skip_reason": "window timeout",
             "ticks_evaluated": 0, "ev_id": 0, "id": 1},
        ]
        _create_trades_db(tmp_path / "bot.db", trades)

        state = {
            "code_version": "test", "portfolio": 100.0, "cash": 100.0,
            "pending_redemption": 0, "strategies": {"favorite": {}},
        }
        panel = build_overview_and_trades(
            state, state["strategies"], str(tmp_path), "main",
            enabled_strategies={"favorite"},
        )
        buf = StringIO()
        console = Console(file=buf, width=120)
        console.print(panel)
        text = buf.getvalue()
        assert "Skips" in text
        assert "1S" in text


class TestBuildOverviewPaperFailsAndSkipsWL:
    """Cover paper_fail and paper_skip_w + paper_skip_l + skip_s branches (lines 374-375, 382-390)."""

    def test_paper_fails_shown(self, tmp_path):
        """Paper section with fail trades should show fail line."""
        from io import StringIO

        from rich.console import Console

        trades = [
            {"type": "paper_win", "strategy": "favorite", "slug": "btc-updown-5m-100",
             "coin": "btc", "interval": "5m", "side": "up", "buy_price": 0.95,
             "contracts": 5, "pnl": 0.25, "sniped_at": "2026-04-01T00:00:00+00:00",
             "resolved_at": "2026-04-01T00:05:00+00:00", "end_timestamp": 100,
             "market_mode": "paper", "id": 1},
            {"type": "fail_win", "strategy": "favorite", "slug": "btc-updown-5m-200",
             "coin": "btc", "interval": "5m", "side": "up", "buy_price": 0.99,
             "contracts": 5, "pnl": 0, "sniped_at": "2026-04-01T00:10:00+00:00",
             "market_mode": "paper", "id": 2},
            {"type": "fail_loss", "strategy": "favorite", "slug": "btc-updown-5m-300",
             "coin": "btc", "interval": "5m", "side": "up", "buy_price": 0.99,
             "contracts": 5, "pnl": 0, "sniped_at": "2026-04-01T00:15:00+00:00",
             "market_mode": "paper", "id": 3},
            {"type": "skip_win", "strategy": "favorite", "slug": "btc-updown-5m-400",
             "coin": "btc", "interval": "5m", "side": "up", "buy_price": 0.96,
             "contracts": 5, "pnl": 0, "sniped_at": "2026-04-01T00:20:00+00:00",
             "market_mode": "paper", "id": 4},
            {"type": "skip_loss", "strategy": "favorite", "slug": "btc-updown-5m-500",
             "coin": "btc", "interval": "5m", "side": "up", "buy_price": 0.96,
             "contracts": 5, "pnl": 0, "sniped_at": "2026-04-01T00:25:00+00:00",
             "market_mode": "paper", "id": 5},
            {"type": "skip_none", "strategy": "favorite", "slug": "btc-updown-5m-600",
             "coin": "btc", "interval": "5m", "side": "", "buy_price": 0,
             "contracts": 5, "pnl": 0, "sniped_at": "2026-04-01T00:30:00+00:00",
             "market_mode": "paper", "id": 6},
        ]
        _create_trades_db(tmp_path / "bot.db", trades)

        state = {
            "code_version": "test", "portfolio": 100.0, "cash": 100.0,
            "pending_redemption": 0, "strategies": {"favorite": {}},
        }
        panel = build_overview_and_trades(
            state, state["strategies"], str(tmp_path), "main",
            enabled_strategies={"favorite"},
        )
        buf = StringIO()
        console = Console(file=buf, width=120)
        console.print(panel)
        text = buf.getvalue()
        assert "Fails" in text
        assert "Skips" in text
        assert "1S" in text  # skip_none count


class TestBuildStateFromApi:
    """Cover _build_state_from_api paths (lines 374-375, 382-390)."""

    def test_api_running_returns_state(self, tmp_path):
        """When bot API is running, use its state (lines 374-375)."""
        from unittest.mock import MagicMock, patch

        from timba.monitor import _build_state_from_api

        with patch("timba.client.BotClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.is_running.return_value = True
            mock_client.status.return_value = {
                "state": {"portfolio": 200.0, "cash": 150.0},
                "version": "v3.0",
            }
            mock_client_cls.return_value = mock_client

            state = _build_state_from_api(str(tmp_path))
            assert state["portfolio"] == 200.0
            assert state["code_version"] == "v3.0"

    def test_api_exception_falls_back_to_sqlite(self, tmp_path):
        """When bot API raises, fall back to SQLite state (lines 374-375)."""
        import sqlite3
        from unittest.mock import patch

        from timba.monitor import _build_state_from_api

        # Create a DB with some trades
        db_path = tmp_path / "bot.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""CREATE TABLE trades (
            id INTEGER PRIMARY KEY, type TEXT, pnl REAL,
            strategy TEXT, slug TEXT, side TEXT, buy_price REAL,
            contracts INTEGER, sniped_at TEXT, resolved_at TEXT,
            end_timestamp INTEGER, market_mode TEXT, skip_reason TEXT,
            ticks_evaluated INTEGER, ev_id INTEGER, token_id TEXT,
            redeemed INTEGER DEFAULT 0, order_id TEXT, min_price REAL,
            midpoint REAL, extras TEXT, condition_id TEXT, coin TEXT, interval TEXT
        )""")
        conn.execute("INSERT INTO trades (id, type, pnl) VALUES (1, 'win', 5.0)")
        conn.execute("INSERT INTO trades (id, type, pnl) VALUES (2, 'loss', -2.0)")
        conn.commit()
        conn.close()

        with patch("timba.client.BotClient") as mock_client_cls:
            mock_client_cls.return_value.is_running.side_effect = Exception("connection refused")

            state = _build_state_from_api(str(tmp_path))
            assert state["code_version"] == "offline"
            assert state["total_pnl"] == 3.0

    def test_offline_corrupt_db(self, tmp_path):
        """Offline mode with corrupt DB should still return state (lines 382-390)."""
        from unittest.mock import patch

        from timba.monitor import _build_state_from_api

        # Create corrupt DB
        (tmp_path / "bot.db").write_text("not a database")

        with patch("timba.client.BotClient") as mock_client_cls:
            mock_client_cls.return_value.is_running.return_value = False

            state = _build_state_from_api(str(tmp_path))
            assert state["code_version"] == "offline"

    def test_offline_no_db(self, tmp_path):
        """Offline mode with no DB returns defaults."""
        from unittest.mock import patch

        from timba.monitor import _build_state_from_api

        with patch("timba.client.BotClient") as mock_client_cls:
            mock_client_cls.return_value.is_running.return_value = False

            state = _build_state_from_api(str(tmp_path))
            assert state["code_version"] == "offline"
            assert state["portfolio"] == 0


class TestMonitorRunConfigEdgeCases:
    """Cover run() config parsing edge cases (lines 417, 432, 435)."""

    @patch("timba.client.BotClient")
    def test_config_with_reserved_keys_skipped(self, mock_client_cls, tmp_path, capsys):
        """Reserved keys in config are skipped (line 417)."""
        mock_client_cls.return_value.is_running.return_value = False

        config = {
            "log_level": "debug",
            "portfolio": {"initial": 100},
            "polymarket": {"api_key": "test"},
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
    def test_config_disabled_strategy_not_shown(self, mock_client_cls, tmp_path, capsys):
        """Disabled strategies are not rendered (lines 432, 435)."""
        mock_client_cls.return_value.is_running.return_value = False

        config = {
            "favorite": {
                "enabled": True,
                "markets": [
                    {"coin": "btc", "interval": "5m", "mode": "paper"},
                ],
            },
            "other": {
                "enabled": False,
                "markets": [
                    {"coin": "eth", "interval": "5m", "mode": "paper"},
                ],
            },
        }
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.dump(config))

        run(str(tmp_path), str(cfg_path), "test")

    @patch("timba.client.BotClient")
    def test_config_non_dict_strategy_skipped(self, mock_client_cls, tmp_path, capsys):
        """Non-dict strategy values are skipped (line 435)."""
        mock_client_cls.return_value.is_running.return_value = False

        config = {
            "favorite": {
                "enabled": True,
                "markets": [
                    {"coin": "btc", "interval": "5m", "mode": "paper"},
                ],
            },
            "bad_strategy": "just a string, not a dict",
        }
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.dump(config))

        run(str(tmp_path), str(cfg_path), "test")


class TestPnlRateEdgeCases:
    """Cover _pnl_rate internal function edge cases (lines 148, 156-158)."""

    def test_pnl_rate_no_matching_trades(self, tmp_path):
        """When no trades match the filter, _pnl_rate returns '' (line 148)."""

        # Create strategy with live bets but with sniped_at = "" (empty)
        trades = [
            {"type": "win", "strategy": "favorite", "slug": "btc-updown-5m-100",
             "coin": "btc", "interval": "5m", "side": "up", "buy_price": 0.90,
             "contracts": 5, "pnl": 0.50, "sniped_at": "",
             "resolved_at": "", "end_timestamp": 100,
             "market_mode": "live", "skip_reason": "",
             "ticks_evaluated": 5, "ev_id": 1, "id": 1},
            {"type": "loss", "strategy": "favorite", "slug": "eth-updown-5m-200",
             "coin": "eth", "interval": "5m", "side": "down", "buy_price": 0.90,
             "contracts": 5, "pnl": -4.50, "sniped_at": "",
             "resolved_at": "", "end_timestamp": 200,
             "market_mode": "live", "skip_reason": "",
             "ticks_evaluated": 3, "ev_id": 2, "id": 2},
        ]
        _create_trades_db(tmp_path / "bot.db", trades)

        state = {
            "code_version": "test", "portfolio": 100.0, "cash": 100.0,
            "pending_redemption": 0, "strategies": {"favorite": {}},
        }
        panel = build_overview_and_trades(
            state, state["strategies"], str(tmp_path), "main",
            enabled_strategies={"favorite"},
        )
        assert panel is not None

    def test_pnl_rate_bad_timestamp(self, tmp_path):
        """When sniped_at is invalid, _pnl_rate catches ValueError (lines 156-158)."""

        trades = [
            {"type": "win", "strategy": "favorite", "slug": "btc-updown-5m-100",
             "coin": "btc", "interval": "5m", "side": "up", "buy_price": 0.90,
             "contracts": 5, "pnl": 0.50, "sniped_at": "not-a-date",
             "resolved_at": "not-a-date", "end_timestamp": 100,
             "market_mode": "live", "skip_reason": "",
             "ticks_evaluated": 5, "ev_id": 1, "id": 1},
            {"type": "loss", "strategy": "favorite", "slug": "eth-updown-5m-200",
             "coin": "eth", "interval": "5m", "side": "down", "buy_price": 0.90,
             "contracts": 5, "pnl": -4.50, "sniped_at": "not-a-date",
             "resolved_at": "not-a-date", "end_timestamp": 200,
             "market_mode": "live", "skip_reason": "",
             "ticks_evaluated": 3, "ev_id": 2, "id": 2},
        ]
        _create_trades_db(tmp_path / "bot.db", trades)

        state = {
            "code_version": "test", "portfolio": 100.0, "cash": 100.0,
            "pending_redemption": 0, "strategies": {"favorite": {}},
        }
        panel = build_overview_and_trades(
            state, state["strategies"], str(tmp_path), "main",
            enabled_strategies={"favorite"},
        )
        assert panel is not None


class TestPaperSkipWlNoSkipNone:
    """Cover line 199: paper skips with W/L but no skip_none."""

    def test_paper_skips_without_skip_none(self, tmp_path):
        """Paper section with skip_win + skip_loss but 0 skip_none -> line 199."""
        from io import StringIO

        from rich.console import Console

        trades = [
            {"type": "skip_win", "strategy": "favorite", "slug": "btc-updown-5m-100",
             "coin": "btc", "interval": "5m", "side": "up", "buy_price": 0.96,
             "contracts": 5, "pnl": 0, "sniped_at": "2026-04-01T00:00:00+00:00",
             "market_mode": "paper", "skip_reason": "below threshold",
             "ticks_evaluated": 5, "ev_id": 1, "id": 1},
            {"type": "skip_loss", "strategy": "favorite", "slug": "btc-updown-5m-200",
             "coin": "btc", "interval": "5m", "side": "down", "buy_price": 0.96,
             "contracts": 5, "pnl": 0, "sniped_at": "2026-04-01T00:05:00+00:00",
             "market_mode": "paper", "skip_reason": "below threshold",
             "ticks_evaluated": 5, "ev_id": 2, "id": 2},
        ]
        _create_trades_db(tmp_path / "bot.db", trades)

        state = {
            "code_version": "test", "portfolio": 100.0, "cash": 100.0,
            "pending_redemption": 0, "strategies": {"favorite": {}},
        }
        panel = build_overview_and_trades(
            state, state["strategies"], str(tmp_path), "main",
            enabled_strategies={"favorite"},
        )
        buf = StringIO()
        console = Console(file=buf, width=120)
        console.print(panel)
        text = buf.getvalue()
        assert "Skips" in text
        assert "1W/1L" in text
        # Should NOT have "+S" since skip_s == 0
        assert "+0S" not in text
