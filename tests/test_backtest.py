"""Tests for backtest package -- tick EV replay and trade simulation."""

import json

import pytest

from timba.backtest.analyze_ticks import analyze_ticks_main
from timba.backtest.analyze_trades import _group_stats, _pnl_for, analyze_main
from timba.backtest.common import (
    load_ticks,
    load_ticks_from_file,
    load_trade_outcomes,
    next_output_path,
    run_formula,
    validate_tick,
    validate_trade,
)
from timba.backtest.trades import backtest_main
from timba.config import Config

_tick_id_counter = 0

def _make_tick(slug="btc-updown-5m-100", coin="btc", interval="5m",
               progress=0.5, remaining=15.0, state="watching", side="up",
               mid_up=0.8, mid_down=0.2, fill_up=0.8, fill_down=0.2,
               signal_dir="up", signal_chg=0.15, signal_trend_sec=100,
               signal_rev=False, ev_up=0.01, ev_down=0.0,
               p_up=0.81, p_down=0.2, ts="2026-03-26T10:00:00+00:00"):
    global _tick_id_counter
    _tick_id_counter += 1
    return {
        "id": _tick_id_counter,
        "ts": ts, "slug": slug, "coin": coin, "interval": interval,
        "state": state, "remaining": remaining, "progress": progress,
        "side": side, "ev_up": ev_up, "ev_down": ev_down,
        "p_up": p_up, "p_down": p_down,
        "mid_up": mid_up, "mid_down": mid_down,
        "fill_up": fill_up, "fill_down": fill_down,
        "signal_dir": signal_dir, "signal_chg": signal_chg,
        "signal_trend_sec": signal_trend_sec, "signal_rev": signal_rev,
    }


def _make_trade(slug="btc-updown-5m-100", coin="btc", interval="5m",
                trade_type="paper_win", side="up", midpoint=0.8,
                buy_price=0.8, contracts=5):
    return {
        "type": trade_type, "slug": slug, "coin": coin,
        "interval": interval, "side": side, "midpoint": midpoint,
        "buy_price": buy_price, "contracts": contracts,
        "signal_direction": "up", "signal_change_pct": 0.15,
        "signal_seconds_trending": 100, "signal_reversed_recently": False,
        "signal_confidence": 0.8, "window_progress": 0.5,
        "pnl": 0.0,
    }


def _make_config():
    cfg = Config()
    from timba.config import StrategyConfig
    cfg.strategies["favorite"] = StrategyConfig({
        "enabled": True,
        "min_price": 0.95,
        "min_signal_chg": 0.05,
        "contracts_per_trade": 5,
        "markets": [{
            "coin": "btc", "interval": "5m", "mode": "paper",
            "entry_window_sec": 10, "close_window_sec": 3,
        }],
    })
    return cfg


# -- common.py tests --

class TestValidateTick:
    def test_valid_tick(self):
        assert validate_tick(_make_tick()) is None

    def test_missing_field(self):
        tick = _make_tick()
        del tick["mid_up"]
        assert validate_tick(tick) == "missing mid_up"

    def test_missing_signal(self):
        tick = _make_tick()
        del tick["signal_dir"]
        assert validate_tick(tick) == "missing signal_dir"


class TestValidateTrade:
    def test_valid(self):
        assert validate_trade(_make_trade()) is None

    def test_missing_field(self):
        trade = _make_trade()
        del trade["side"]
        assert "missing side" in validate_trade(trade)

    def test_buy_price_out_of_range(self):
        trade = _make_trade(buy_price=1.5)
        assert "buy_price out of range" in validate_trade(trade)


class TestRunFormula:
    def test_returns_p_win_and_ev(self):
        trade = _make_trade()
        p_win, ev = run_formula(trade, 0.05, 1.0, 0.30, 300.0)
        assert 0 <= p_win <= 1
        assert isinstance(ev, float)


class TestLoadTicks:
    def test_groups_by_slug(self, tmp_path):
        f = tmp_path / "ticks_2026-03-26.jsonl"
        ticks = [
            _make_tick(slug="btc-5m-100", ts="2026-03-26T10:00:00+00:00"),
            _make_tick(slug="btc-5m-100", ts="2026-03-26T10:00:01+00:00"),
            _make_tick(slug="eth-5m-200", ts="2026-03-26T10:00:00+00:00"),
        ]
        f.write_text("\n".join(json.dumps(t) for t in ticks))

        by_slug, skipped = load_ticks(tmp_path)
        assert skipped == 0
        assert len(by_slug) == 2
        assert len(by_slug["btc-5m-100"]) == 2

    def test_skips_invalid_ticks(self, tmp_path):
        f = tmp_path / "ticks_2026-03-26.jsonl"
        f.write_text(json.dumps(_make_tick()) + "\n" + json.dumps({"slug": "x"}))

        by_slug, skipped = load_ticks(tmp_path)
        assert skipped == 1
        assert len(by_slug) == 1

    def test_excludes_backtest_files(self, tmp_path):
        (tmp_path / "ticks_2026-03-26.jsonl").write_text(
            json.dumps(_make_tick(slug="a")))
        (tmp_path / "ticks_2026-03-26.backtest.1.0.0.jsonl").write_text(
            json.dumps(_make_tick(slug="b")))

        by_slug, _ = load_ticks(tmp_path)
        assert "a" in by_slug
        assert "b" not in by_slug

    def test_empty_dir(self, tmp_path):
        by_slug, skipped = load_ticks(tmp_path)
        assert by_slug == {}


class TestLoadTicksFromFile:
    def test_loads_single_file(self, tmp_path):
        f = tmp_path / "ticks_2026-03-26.backtest.1.0.0.jsonl"
        f.write_text(json.dumps(_make_tick(slug="a")))

        by_slug, _ = load_ticks_from_file(f)
        assert "a" in by_slug


class TestLoadTradeOutcomes:
    def test_loads_by_slug(self, tmp_path):
        strat_dir = tmp_path / "favorite"
        strat_dir.mkdir()
        f = strat_dir / "trades_2026-03-26.jsonl"
        f.write_text("\n".join(json.dumps(_make_trade(slug=s)) for s in ["a", "b"]))

        outcomes = load_trade_outcomes(tmp_path)
        assert "a" in outcomes
        assert "b" in outcomes


class TestNextOutputPath:
    def test_first_run(self, tmp_path):
        original = tmp_path / "ticks_2026-03-26.jsonl"
        original.touch()
        assert next_output_path(original).name == "ticks_2026-03-26.backtest.jsonl"

    def test_second_run_increments(self, tmp_path):
        original = tmp_path / "ticks_2026-03-26.jsonl"
        original.touch()
        (tmp_path / "ticks_2026-03-26.backtest.jsonl").touch()
        assert next_output_path(original).name == "ticks_2026-03-26.backtest_1.jsonl"

    def test_third_run(self, tmp_path):
        original = tmp_path / "ticks_2026-03-26.jsonl"
        original.touch()
        (tmp_path / "ticks_2026-03-26.backtest.jsonl").touch()
        (tmp_path / "ticks_2026-03-26.backtest_1.jsonl").touch()
        assert next_output_path(original).name == "ticks_2026-03-26.backtest_2.jsonl"


# -- resolve_from_ticks tests --

class TestResolveFromTicks:
    def test_up_wins(self):
        from timba.backtest.common import resolve_from_ticks
        assert resolve_from_ticks([{"mid_up": 0.99, "mid_down": 0.01}]) == "up"

    def test_down_wins(self):
        from timba.backtest.common import resolve_from_ticks
        assert resolve_from_ticks([{"mid_up": 0.01, "mid_down": 0.99}]) == "down"

    def test_ambiguous_returns_none(self):
        from timba.backtest.common import resolve_from_ticks
        assert resolve_from_ticks([{"mid_up": 0.50, "mid_down": 0.50}]) is None

    def test_empty_returns_none(self):
        from timba.backtest.common import resolve_from_ticks
        assert resolve_from_ticks([]) is None


# -- backtest_main tests (full: ticks -> EVs -> trades, all SQLite) --

class TestBacktestMain:
    def _setup_source_db(self, tmp_path):
        """Create a source env with ticks in SQLite."""
        source_dir = tmp_path / "data" / "main"
        source_dir.mkdir(parents=True, exist_ok=True)

        import sqlite3
        source_db = source_dir / "bot.db"
        conn = sqlite3.connect(str(source_db))
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS ticks (
                id INTEGER PRIMARY KEY, ts TEXT NOT NULL, slug TEXT NOT NULL,
                coin TEXT NOT NULL, interval TEXT NOT NULL,
                mid_up REAL NOT NULL, mid_down REAL NOT NULL,
                fill_up REAL NOT NULL, fill_down REAL NOT NULL,
                signal_dir TEXT NOT NULL, signal_chg REAL NOT NULL,
                signal_trend_sec REAL NOT NULL, signal_rev INTEGER NOT NULL,
                price_open REAL NOT NULL DEFAULT 0.0, price_now REAL NOT NULL DEFAULT 0.0
            );
            CREATE TABLE IF NOT EXISTS evs (
                id INTEGER PRIMARY KEY, tick_id INTEGER NOT NULL, slug TEXT NOT NULL DEFAULT '',
                strategy TEXT NOT NULL, remaining REAL, progress REAL,
                ev_up REAL, ev_down REAL, p_up REAL, p_down REAL, extras TEXT,
                FOREIGN KEY (tick_id) REFERENCES ticks(id)
            );
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY, type TEXT NOT NULL, strategy TEXT NOT NULL,
                slug TEXT NOT NULL, condition_id TEXT, coin TEXT NOT NULL DEFAULT '',
                interval TEXT NOT NULL DEFAULT '', side TEXT, buy_price REAL,
                contracts INTEGER, pnl REAL, sniped_at TEXT, resolved_at TEXT,
                end_timestamp INTEGER, market_mode TEXT,
                skip_reason TEXT, ticks_evaluated INTEGER, ev_id INTEGER,
                token_id TEXT, redeemed INTEGER DEFAULT 0, order_id TEXT,
                min_price REAL, midpoint REAL, extras TEXT,
                FOREIGN KEY (ev_id) REFERENCES evs(id)
            );
        """)
        ticks = [
            _make_tick(slug="btc-updown-5m-100", mid_up=0.98, mid_down=0.02,
                       fill_up=0.98, fill_down=0.05,
                       ts="2026-03-26T10:04:50+00:00"),
            _make_tick(slug="btc-updown-5m-100", mid_up=0.99, mid_down=0.01,
                       fill_up=0.99, fill_down=0.05,
                       ts="2026-03-26T10:04:55+00:00"),
        ]
        for t in ticks:
            conn.execute(
                "INSERT INTO ticks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (t["id"], t["ts"], t["slug"], t["coin"], t["interval"],
                 t["mid_up"], t["mid_down"], t["fill_up"], t["fill_down"],
                 t["signal_dir"], t["signal_chg"], t["signal_trend_sec"],
                 1 if t["signal_rev"] else 0, t.get("price_open", 0), t.get("price_now", 0)),
            )
        conn.commit()
        conn.close()
        return source_dir

    def test_full_backtest_writes_trades(self, tmp_path, capsys, monkeypatch):
        self._setup_source_db(tmp_path)
        bt_dir = tmp_path / "bt"

        # Point "data/main" to our test source
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir(exist_ok=True)
        # source_dir is already at tmp_path/data/main

        backtest_main(_make_config(), bt_dir, source_env="main")
        out = capsys.readouterr().out
        assert "BACKTEST TRADES" in out
        assert "BTC" in out

        # Verify trades in backtest DB
        import sqlite3
        conn = sqlite3.connect(str(bt_dir / "bot.db"))
        conn.row_factory = sqlite3.Row
        trades = conn.execute("SELECT * FROM trades").fetchall()
        assert len(trades) >= 1
        conn.close()

    def test_no_source_dbs_exits(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data" / "empty").mkdir(parents=True)
        bt_dir = tmp_path / "bt"
        with pytest.raises(SystemExit):
            backtest_main(_make_config(), bt_dir, source_env="empty")

    def test_report_shows_analyze_hint(self, tmp_path, capsys, monkeypatch):
        self._setup_source_db(tmp_path)
        bt_dir = tmp_path / "bt"
        monkeypatch.chdir(tmp_path)

        backtest_main(_make_config(), bt_dir, source_env="main")
        out = capsys.readouterr().out
        assert "analyze-trades" in out


# -- analyze.py tests --

def _make_resolved_trade(trade_type="paper_win", coin="btc", interval="5m",
                         buy_price=0.8, contracts=5, resolved_at="2026-03-26T10:00:00+00:00"):
    return {
        "type": trade_type, "slug": f"{coin}-{interval}-100",
        "coin": coin, "interval": interval, "side": "up",
        "midpoint": 0.8, "buy_price": buy_price, "contracts": contracts,
        "pnl": 0.0, "resolved_at": resolved_at, "best_ev": 0.01,
    }


class TestPnlFor:
    def test_win_pnl(self):
        t = _make_resolved_trade(trade_type="paper_win", buy_price=0.8, contracts=10)
        assert _pnl_for(t) == pytest.approx(2.0)  # (1-0.8)*10

    def test_loss_pnl(self):
        t = _make_resolved_trade(trade_type="paper_loss", buy_price=0.8, contracts=10)
        assert _pnl_for(t) == pytest.approx(-8.0)  # -0.8*10

    def test_uses_pnl_field_when_set(self):
        t = _make_resolved_trade(trade_type="paper_win")
        t["pnl"] = 1.23
        assert _pnl_for(t) == 1.23


class TestGroupStats:
    def test_basic_stats(self):
        trades = [
            _make_resolved_trade(trade_type="paper_win", buy_price=0.8, contracts=10),
            _make_resolved_trade(trade_type="paper_loss", buy_price=0.9, contracts=10),
        ]
        s = _group_stats(trades)
        assert s["count"] == 2
        assert s["wins"] == 1
        assert s["losses"] == 1
        assert s["pnl"] == pytest.approx(2.0 + -9.0)  # win + loss


class TestAnalyzeMain:
    def test_full_report(self, tmp_path, capsys):
        trades = [
            _make_resolved_trade("paper_win", "btc", "5m", 0.8, 5, "2026-03-25T10:00:00+00:00"),
            _make_resolved_trade("paper_loss", "btc", "5m", 0.9, 5, "2026-03-25T11:00:00+00:00"),
            _make_resolved_trade("paper_win", "eth", "15m", 0.7, 5, "2026-03-26T10:00:00+00:00"),
            _make_resolved_trade("skip_win", "btc", "5m", 0.8, 5, "2026-03-26T12:00:00+00:00"),
        ]
        (tmp_path / "favorite").mkdir()
        (tmp_path / "favorite" / "trades_2026-03-26.jsonl").write_text(
            "\n".join(json.dumps(t) for t in trades))

        analyze_main(tmp_path)
        out = capsys.readouterr().out
        assert "Trade Analysis" in out
        assert "Price Distribution" in out
        assert "Per Coin" in out
        assert "BTC" in out
        assert "ETH" in out

    def test_no_trades_exits(self, tmp_path):
        with pytest.raises(SystemExit):
            analyze_main(tmp_path)

    def test_with_strategy(self, tmp_path, capsys):
        (tmp_path / "favorite").mkdir()
        trades = [
            _make_resolved_trade("paper_win", buy_price=0.95),
            _make_resolved_trade("paper_win", buy_price=0.99),
            _make_resolved_trade("paper_loss", buy_price=0.55),
        ]
        (tmp_path / "favorite" / "trades_2026-03-26.jsonl").write_text(
            "\n".join(json.dumps(t) for t in trades))

        analyze_main(tmp_path, strategy="favorite")
        out = capsys.readouterr().out
        assert "FAVORITE" in out
        assert "Price Distribution" in out

    def test_pass_analysis(self, tmp_path, capsys):
        trades = [
            {**_make_resolved_trade("skip_win"), "best_ev": 0.02},
            {**_make_resolved_trade("skip_loss"), "best_ev": 0.0},
        ]
        (tmp_path / "favorite").mkdir()
        (tmp_path / "favorite" / "trades_2026-03-26.jsonl").write_text(
            "\n".join(json.dumps(t) for t in trades))

        analyze_main(tmp_path)
        out = capsys.readouterr().out
        assert "Skip Analysis" in out
        assert "Would have won" in out


# -- analyze_ticks.py tests --

class TestAnalyzeTicksMain:
    def test_full_report(self, tmp_path, capsys):
        ticks = [
            _make_tick(slug="btc-5m-100", coin="btc", interval="5m",
                       ev_up=0.03, ev_down=0.0, p_up=0.83, p_down=0.2,
                       signal_dir="up", progress=0.5,
                       ts="2026-03-26T10:00:00+00:00"),
            _make_tick(slug="btc-5m-100", coin="btc", interval="5m",
                       ev_up=0.05, ev_down=0.0, p_up=0.85, p_down=0.2,
                       signal_dir="up", progress=0.9,
                       ts="2026-03-26T10:00:01+00:00"),
            _make_tick(slug="eth-15m-200", coin="eth", interval="15m",
                       ev_up=0.0, ev_down=0.02, p_up=0.2, p_down=0.82,
                       signal_dir="down", progress=0.7,
                       ts="2026-03-26T10:00:00+00:00"),
        ]
        (tmp_path / "ticks_2026-03-26.jsonl").write_text(
            "\n".join(json.dumps(t) for t in ticks))

        trades = [
            {"type": "paper_win", "slug": "btc-5m-100", "coin": "btc", "interval": "5m"},
            {"type": "paper_loss", "slug": "eth-15m-200", "coin": "eth", "interval": "15m"},
        ]
        (tmp_path / "favorite").mkdir(parents=True, exist_ok=True)
        (tmp_path / "favorite" / "trades_2026-03-26.jsonl").write_text(
            "\n".join(json.dumps(t) for t in trades))

        analyze_ticks_main(tmp_path)
        out = capsys.readouterr().out

        assert "TICK ANALYSIS" in out
        assert "EV CALIBRATION" in out
        assert "PER COIN" in out
        assert "PER INTERVAL" in out
        assert "PER COIN + INTERVAL" in out
        assert "SIGNAL vs OUTCOME" in out
        assert "BTC" in out
        assert "ETH" in out
        assert "Gap" in out

    def test_no_tick_files_exits(self, tmp_path):
        with pytest.raises(SystemExit):
            analyze_ticks_main(tmp_path)

    def test_without_trade_outcomes(self, tmp_path, capsys):
        """No trade data -> no calibration, just header."""
        ticks = [_make_tick(slug="btc-5m-100", ev_up=0.03)]
        (tmp_path / "ticks_2026-03-26.jsonl").write_text(json.dumps(ticks[0]))

        analyze_ticks_main(tmp_path)
        out = capsys.readouterr().out
        assert "No markets with trade outcomes" in out

    def test_skipped_markets_shown(self, tmp_path, capsys):
        """Markets with no +EV should appear in skipped section."""
        ticks = [_make_tick(slug="btc-5m-100", ev_up=-0.01, ev_down=-0.02)]
        (tmp_path / "ticks_2026-03-26.jsonl").write_text(json.dumps(ticks[0]))
        trades = [{"type": "paper_win", "slug": "btc-5m-100"}]
        (tmp_path / "favorite").mkdir(parents=True, exist_ok=True)
        (tmp_path / "favorite" / "trades_2026-03-26.jsonl").write_text(json.dumps(trades[0]))

        analyze_ticks_main(tmp_path)
        out = capsys.readouterr().out
        assert "SKIPPED MARKETS" in out
        assert "Would have won:" in out

    def test_calibration_gap(self, tmp_path, capsys):
        """Check that calibration table shows predicted vs actual."""
        # 2 markets with +EV, one wins one loses -> 50% actual WR
        ticks_a = [_make_tick(slug="a", ev_up=0.02, p_up=0.95)]
        ticks_b = [_make_tick(slug="b", ev_up=0.02, p_up=0.95)]
        (tmp_path / "ticks_2026-03-26.jsonl").write_text(
            json.dumps(ticks_a[0]) + "\n" + json.dumps(ticks_b[0]))

        trades = [
            {"type": "paper_win", "slug": "a"},
            {"type": "paper_loss", "slug": "b"},
        ]
        (tmp_path / "favorite").mkdir(parents=True, exist_ok=True)
        (tmp_path / "favorite" / "trades_2026-03-26.jsonl").write_text(
            "\n".join(json.dumps(t) for t in trades))

        analyze_ticks_main(tmp_path)
        out = capsys.readouterr().out
        assert "EV CALIBRATION" in out
        # Should show 50% actual WR vs 95% predicted -> negative gap
        assert "50.0%" in out


# -- _pnl_for edge cases --


class TestPnlForEdgeCases:
    """Cover _pnl_for branches: zero buy, loss calc, win calc, explicit pnl."""

    def test_pnl_zero_buy_price(self):
        trade = _make_trade(buy_price=0, trade_type="paper_win")
        trade["pnl"] = 0.0
        assert _pnl_for(trade) == 0.0

    def test_pnl_loss_calculation(self):
        trade = _make_trade(buy_price=0.95, contracts=10, trade_type="loss")
        trade["pnl"] = 0.0
        assert _pnl_for(trade) == -9.5

    def test_pnl_win_calculation(self):
        trade = _make_trade(buy_price=0.95, contracts=10, trade_type="win")
        trade["pnl"] = 0.0
        assert _pnl_for(trade) == pytest.approx(0.5)

    def test_pnl_explicit_pnl_used(self):
        trade = _make_trade(buy_price=0.50, contracts=10, trade_type="loss")
        trade["pnl"] = 1.23
        assert _pnl_for(trade) == 1.23

    def test_pnl_paper_win(self):
        trade = _make_trade(buy_price=0.90, contracts=5, trade_type="paper_win")
        trade["pnl"] = 0.0
        assert _pnl_for(trade) == pytest.approx(0.5)

    def test_pnl_unknown_type(self):
        trade = _make_trade(buy_price=0.50, contracts=5, trade_type="unknown")
        trade["pnl"] = 0.0
        assert _pnl_for(trade) == 0.0


# -- validate_trade edge cases --


class TestValidateTradeEdgeCases:
    """Cover validate_trade branches: empty side, buy_price boundary."""

    def test_empty_side_rejected(self):
        trade = _make_trade(side="", buy_price=0.80)
        reason = validate_trade(trade)
        assert reason is not None
        assert "empty side" in reason

    def test_buy_price_zero_rejected(self):
        trade = _make_trade(side="up", buy_price=0)
        reason = validate_trade(trade)
        assert reason is not None
        assert "out of range" in reason

    def test_buy_price_one_rejected(self):
        trade = _make_trade(side="up", buy_price=1.0)
        reason = validate_trade(trade)
        assert reason is not None
        assert "out of range" in reason

    def test_valid_trade_passes(self):
        trade = _make_trade(side="up", buy_price=0.80, contracts=5)
        assert validate_trade(trade) is None


# -- analyze_trades.py internal helper tests --


class TestEnrichTrades:
    """Test _enrich_trades: joins trade -> ev -> tick."""

    def test_enriches_with_signal_data(self):
        from timba.backtest.analyze_trades import _enrich_trades

        trades = [{"ev_id": 1, "side": "up", "buy_price": 0.95}]
        evs = {1: {"tick_id": 10, "ev_up": 0.02, "ev_down": 0.01,
                    "p_up": 0.97, "p_down": 0.03, "remaining": 5.0, "progress": 0.8}}
        ticks = {10: {"signal_dir": "up", "signal_chg": 0.15, "signal_trend_sec": 120,
                       "signal_rev": False, "mid_up": 0.97, "mid_down": 0.03,
                       "fill_up": 0.97, "fill_down": 0.04}}

        result = _enrich_trades(trades, evs, ticks)
        assert len(result) == 1
        assert result[0]["_signal_dir"] == "up"
        assert result[0]["_signal_chg"] == 0.15
        assert result[0]["_signal_trend_sec"] == 120
        assert result[0]["_remaining"] == 5.0
        assert result[0]["_progress"] == 0.8
        assert result[0]["_has_signal"] is True

    def test_enriches_without_ev_or_tick(self):
        from timba.backtest.analyze_trades import _enrich_trades

        trades = [{"ev_id": 99, "side": "down", "buy_price": 0.90}]
        evs = {}
        ticks = {}

        result = _enrich_trades(trades, evs, ticks)
        assert len(result) == 1
        assert result[0]["_signal_dir"] == ""
        assert result[0]["_has_signal"] is False


class TestAvgMedian:
    """Test _avg and _median helper functions."""

    def test_avg_normal(self):
        from timba.backtest.analyze_trades import _avg
        assert _avg([1, 2, 3]) == pytest.approx(2.0)

    def test_avg_empty(self):
        from timba.backtest.analyze_trades import _avg
        assert _avg([]) == 0.0

    def test_median_odd(self):
        from timba.backtest.analyze_trades import _median
        assert _median([3, 1, 2]) == 2

    def test_median_even(self):
        from timba.backtest.analyze_trades import _median
        assert _median([1, 2, 3, 4]) == pytest.approx(2.5)

    def test_median_empty(self):
        from timba.backtest.analyze_trades import _median
        assert _median([]) == 0.0

    def test_median_single(self):
        from timba.backtest.analyze_trades import _median
        assert _median([42]) == 42


class TestSignalStats:
    """Test _signal_stats computation."""

    def test_basic_signal_stats(self):
        from timba.backtest.analyze_trades import _signal_stats

        trades = [
            {"side": "up", "_signal_dir": "up", "_signal_chg": 0.20,
             "_signal_trend_sec": 100, "_signal_rev": False, "_has_signal": True,
             "_remaining": 5.0, "_progress": 0.8, "midpoint": 0.95, "buy_price": 0.95,
             "type": "paper_win", "contracts": 5},
            {"side": "down", "_signal_dir": "down", "_signal_chg": 0.10,
             "_signal_trend_sec": 50, "_signal_rev": True, "_has_signal": True,
             "_remaining": 3.0, "_progress": 0.9, "midpoint": 0.90, "buy_price": 0.90,
             "type": "paper_loss", "contracts": 5},
        ]
        stats = _signal_stats(trades)
        assert stats["n"] == 2
        assert stats["agree_count"] == 2
        assert stats["rev_count"] == 1
        assert stats["chg_avg"] == pytest.approx(0.15)
        assert stats["remaining_avg"] == pytest.approx(4.0)

    def test_empty_trades(self):
        from timba.backtest.analyze_trades import _signal_stats
        stats = _signal_stats([])
        assert stats["n"] == 0
        assert stats["chg_avg"] == 0.0


class TestLoadEvsById:
    """Test _load_evs_by_id: loads EVs from SQLite databases."""

    def test_loads_from_sqlite(self, tmp_path):
        import sqlite3

        from timba.backtest.analyze_trades import _load_evs_by_id

        db_path = tmp_path / "bot.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""CREATE TABLE evs (
            id INTEGER PRIMARY KEY, tick_id INTEGER, slug TEXT,
            strategy TEXT, remaining REAL, progress REAL,
            ev_up REAL, ev_down REAL, p_up REAL, p_down REAL, extras TEXT
        )""")
        conn.execute("""INSERT INTO evs VALUES
            (1, 10, 'btc-5m-100', 'favorite', 5.0, 0.8, 0.02, 0.01, 0.97, 0.03,
             '{"custom_field": 42}')""")
        conn.commit()
        conn.close()

        evs = _load_evs_by_id(tmp_path, "favorite")
        assert len(evs) == 1
        assert evs[1]["tick_id"] == 10
        assert evs[1]["custom_field"] == 42

    def test_empty_dir(self, tmp_path):
        from timba.backtest.analyze_trades import _load_evs_by_id
        evs = _load_evs_by_id(tmp_path, "favorite")
        assert evs == {}


class TestLoadTicksById:
    """Test _load_ticks_by_id: loads ticks from SQLite databases."""

    def test_loads_all_ticks(self, tmp_path):
        import sqlite3

        from timba.backtest.analyze_trades import _load_ticks_by_id

        db_path = tmp_path / "bot.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""CREATE TABLE ticks (
            id INTEGER PRIMARY KEY, ts TEXT, slug TEXT, coin TEXT, interval TEXT,
            mid_up REAL, mid_down REAL, fill_up REAL, fill_down REAL,
            signal_dir TEXT, signal_chg REAL, signal_trend_sec REAL, signal_rev INTEGER,
            price_open REAL, price_now REAL
        )""")
        conn.execute("""INSERT INTO ticks VALUES
            (1, '2026-03-29T10:00:00', 'btc-5m-100', 'btc', '5m',
             0.95, 0.05, 0.96, 0.06, 'up', 0.15, 120, 0, 60000, 60100)""")
        conn.execute("""INSERT INTO ticks VALUES
            (2, '2026-03-29T10:00:01', 'btc-5m-100', 'btc', '5m',
             0.96, 0.04, 0.97, 0.05, 'up', 0.20, 121, 1, 60000, 60200)""")
        conn.commit()
        conn.close()

        ticks = _load_ticks_by_id(tmp_path)
        assert len(ticks) == 2
        assert ticks[2]["signal_rev"] is True

    def test_loads_specific_ids(self, tmp_path):
        import sqlite3

        from timba.backtest.analyze_trades import _load_ticks_by_id

        db_path = tmp_path / "bot.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""CREATE TABLE ticks (
            id INTEGER PRIMARY KEY, ts TEXT, slug TEXT, coin TEXT, interval TEXT,
            mid_up REAL, mid_down REAL, fill_up REAL, fill_down REAL,
            signal_dir TEXT, signal_chg REAL, signal_trend_sec REAL, signal_rev INTEGER,
            price_open REAL, price_now REAL
        )""")
        for i in range(1, 4):
            conn.execute("""INSERT INTO ticks VALUES
                (?, '2026-03-29T10:00:00', 'btc-5m-100', 'btc', '5m',
                 0.95, 0.05, 0.96, 0.06, 'up', 0.15, 120, 0, 60000, 60100)""", (i,))
        conn.commit()
        conn.close()

        ticks = _load_ticks_by_id(tmp_path, tick_ids={1, 3})
        assert len(ticks) == 2
        assert 1 in ticks
        assert 3 in ticks
        assert 2 not in ticks
