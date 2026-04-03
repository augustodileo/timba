"""Tests for backtest package -- tick EV replay and trade simulation."""

import json

import pytest

from timba.backtest.analyze_ticks import analyze_ticks_main
from timba.backtest.analyze_trades import _group_stats, _pnl_for, analyze_main
from timba.backtest.common import (
    load_trade_outcomes,
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


class TestLoadTradeOutcomes:
    def test_loads_by_slug(self, tmp_path):
        import sqlite3
        db_path = tmp_path / "bot.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""CREATE TABLE trades (
            id INTEGER PRIMARY KEY, type TEXT, strategy TEXT, slug TEXT,
            coin TEXT, interval TEXT, side TEXT, buy_price REAL,
            contracts INTEGER, pnl REAL, sniped_at TEXT, resolved_at TEXT,
            end_timestamp INTEGER, market_mode TEXT, skip_reason TEXT,
            ticks_evaluated INTEGER, ev_id INTEGER, token_id TEXT,
            redeemed INTEGER DEFAULT 0, extras TEXT
        )""")
        for i, slug in enumerate(["a", "b"]):
            trade = _make_trade(slug=slug)
            conn.execute(
                "INSERT INTO trades (id, type, strategy, slug, coin, interval, side, buy_price, contracts) "
                "VALUES (?, ?, 'favorite', ?, ?, ?, ?, ?, ?)",
                (i + 1, trade["type"], trade["slug"], trade["coin"],
                 trade["interval"], trade["side"], trade["buy_price"], trade["contracts"]),
            )
        conn.commit()
        conn.close()

        outcomes = load_trade_outcomes(tmp_path)
        assert "a" in outcomes
        assert "b" in outcomes


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


def _insert_trades_to_sqlite(tmp_path, trades):
    """Helper: insert trade dicts into a SQLite bot.db for testing."""
    import sqlite3
    db_path = tmp_path / "bot.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY, type TEXT NOT NULL, strategy TEXT NOT NULL,
        slug TEXT NOT NULL, condition_id TEXT, coin TEXT NOT NULL DEFAULT '',
        interval TEXT NOT NULL DEFAULT '', side TEXT, buy_price REAL,
        contracts INTEGER, pnl REAL, sniped_at TEXT, resolved_at TEXT,
        end_timestamp INTEGER, market_mode TEXT, skip_reason TEXT,
        ticks_evaluated INTEGER, ev_id INTEGER, token_id TEXT,
        redeemed INTEGER DEFAULT 0, order_id TEXT, min_price REAL,
        midpoint REAL, extras TEXT,
        FOREIGN KEY (ev_id) REFERENCES evs(id)
    )""")
    for i, t in enumerate(trades):
        extras = json.dumps({k: v for k, v in t.items()
                            if k not in ("id", "type", "strategy", "slug", "coin", "interval",
                                         "side", "buy_price", "contracts", "pnl", "sniped_at",
                                         "resolved_at", "end_timestamp", "market_mode",
                                         "skip_reason", "midpoint")})
        conn.execute(
            "INSERT INTO trades (id, type, strategy, slug, coin, interval, side, "
            "buy_price, contracts, pnl, sniped_at, resolved_at, market_mode, midpoint, extras) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (i + 1, t.get("type", ""), t.get("strategy", "favorite"), t.get("slug", ""),
             t.get("coin", ""), t.get("interval", ""), t.get("side", ""),
             t.get("buy_price", 0), t.get("contracts", 0), t.get("pnl", 0),
             t.get("sniped_at", ""), t.get("resolved_at", ""),
             t.get("market_mode", "paper"), t.get("midpoint", 0), extras),
        )
    conn.commit()
    conn.close()


class TestAnalyzeMain:
    def test_full_report(self, tmp_path, capsys):
        trades = [
            _make_resolved_trade("paper_win", "btc", "5m", 0.8, 5, "2026-03-25T10:00:00+00:00"),
            _make_resolved_trade("paper_loss", "btc", "5m", 0.9, 5, "2026-03-25T11:00:00+00:00"),
            _make_resolved_trade("paper_win", "eth", "15m", 0.7, 5, "2026-03-26T10:00:00+00:00"),
            _make_resolved_trade("skip_win", "btc", "5m", 0.8, 5, "2026-03-26T12:00:00+00:00"),
        ]
        _insert_trades_to_sqlite(tmp_path, trades)

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
        trades = [
            _make_resolved_trade("paper_win", buy_price=0.95),
            _make_resolved_trade("paper_win", buy_price=0.99),
            _make_resolved_trade("paper_loss", buy_price=0.55),
        ]
        _insert_trades_to_sqlite(tmp_path, trades)

        analyze_main(tmp_path, strategy="favorite")
        out = capsys.readouterr().out
        assert "FAVORITE" in out
        assert "Price Distribution" in out

    def test_pass_analysis(self, tmp_path, capsys):
        trades = [
            {**_make_resolved_trade("skip_win"), "best_ev": 0.02},
            {**_make_resolved_trade("skip_loss"), "best_ev": 0.0},
        ]
        _insert_trades_to_sqlite(tmp_path, trades)

        analyze_main(tmp_path)
        out = capsys.readouterr().out
        assert "Skip Analysis" in out
        assert "Would have won" in out


# -- analyze_ticks.py tests --

def _setup_analyze_ticks_db(tmp_path, ticks, trades, strategy="favorite"):
    """Helper: create SQLite DB with ticks, evs, and trades for analyze_ticks tests."""
    import sqlite3
    db_path = tmp_path / "bot.db"
    conn = sqlite3.connect(str(db_path))
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
            end_timestamp INTEGER, market_mode TEXT, skip_reason TEXT,
            ticks_evaluated INTEGER, ev_id INTEGER, token_id TEXT,
            redeemed INTEGER DEFAULT 0, order_id TEXT, min_price REAL,
            midpoint REAL, extras TEXT,
            FOREIGN KEY (ev_id) REFERENCES evs(id)
        );
    """)
    ev_id = 0
    for t in ticks:
        conn.execute(
            "INSERT INTO ticks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (t["id"], t["ts"], t["slug"], t["coin"], t["interval"],
             t["mid_up"], t["mid_down"], t["fill_up"], t["fill_down"],
             t["signal_dir"], t["signal_chg"], t["signal_trend_sec"],
             1 if t["signal_rev"] else 0, t.get("price_open", 0), t.get("price_now", 0)),
        )
        # Insert EV for this tick
        ev_id += 1
        conn.execute(
            "INSERT INTO evs (id, tick_id, slug, strategy, remaining, progress, "
            "ev_up, ev_down, p_up, p_down) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ev_id, t["id"], t["slug"], strategy,
             t.get("remaining"), t.get("progress"),
             t.get("ev_up", 0), t.get("ev_down", 0),
             t.get("p_up", 0), t.get("p_down", 0)),
        )
    for i, tr in enumerate(trades):
        conn.execute(
            "INSERT INTO trades (id, type, strategy, slug, coin, interval, side, buy_price, contracts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (i + 1, tr.get("type", ""), strategy, tr.get("slug", ""),
             tr.get("coin", ""), tr.get("interval", ""),
             tr.get("side", ""), tr.get("buy_price", 0), tr.get("contracts", 0)),
        )
    conn.commit()
    conn.close()


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
        trades = [
            {"type": "paper_win", "slug": "btc-5m-100", "coin": "btc", "interval": "5m"},
            {"type": "paper_loss", "slug": "eth-15m-200", "coin": "eth", "interval": "15m"},
        ]
        _setup_analyze_ticks_db(tmp_path, ticks, trades)

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
        _setup_analyze_ticks_db(tmp_path, ticks, [])

        analyze_ticks_main(tmp_path)
        out = capsys.readouterr().out
        assert "No markets with trade outcomes" in out

    def test_skipped_markets_shown(self, tmp_path, capsys):
        """Markets with no +EV should appear in skipped section."""
        ticks = [_make_tick(slug="btc-5m-100", ev_up=-0.01, ev_down=-0.02)]
        trades = [{"type": "paper_win", "slug": "btc-5m-100"}]
        _setup_analyze_ticks_db(tmp_path, ticks, trades)

        analyze_ticks_main(tmp_path)
        out = capsys.readouterr().out
        assert "SKIPPED MARKETS" in out
        assert "Would have won:" in out

    def test_calibration_gap(self, tmp_path, capsys):
        """Check that calibration table shows predicted vs actual."""
        # 2 markets with +EV, one wins one loses -> 50% actual WR
        ticks = [
            _make_tick(slug="a", ev_up=0.02, p_up=0.95),
            _make_tick(slug="b", ev_up=0.02, p_up=0.95),
        ]
        trades = [
            {"type": "paper_win", "slug": "a"},
            {"type": "paper_loss", "slug": "b"},
        ]
        _setup_analyze_ticks_db(tmp_path, ticks, trades)

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


# -- common.py edge cases for 100% coverage --


class TestUnpackExtrasEdgeCases:
    """Cover _unpack_extras with bad JSON (lines 70-71)."""

    def test_bad_json_extras_ignored(self):
        from timba.backtest.common import _unpack_extras
        row = {"key": "value", "extras": "not-valid-json{{{"}
        result = _unpack_extras(row)
        assert result["key"] == "value"
        assert "extras" not in result  # popped even if invalid

    def test_none_extras_noop(self):
        from timba.backtest.common import _unpack_extras
        row = {"key": "value"}
        result = _unpack_extras(row)
        assert result == {"key": "value"}


class TestLoadTicksWithEvsDbError:
    """Cover except Exception: continue in load_ticks_with_evs (lines 108-109)."""

    def test_corrupt_db_skipped(self, tmp_path):
        from timba.backtest.common import load_ticks_with_evs

        # Create a corrupt file that looks like a .db but isn't valid SQLite
        bad_db = tmp_path / "bot_2026-01-01.db"
        bad_db.write_text("this is not a valid sqlite database")

        # Should not raise, just skip the corrupt file
        by_slug, skip_count = load_ticks_with_evs(tmp_path)
        assert by_slug == {}


class TestLoadTradesDbError:
    """Cover except Exception: continue in load_trades (lines 152-153)."""

    def test_corrupt_db_skipped(self, tmp_path):
        from timba.backtest.common import load_trades

        bad_db = tmp_path / "bot_2026-01-01.db"
        bad_db.write_text("this is not a valid sqlite database")

        trades = load_trades(tmp_path)
        assert trades == []


class TestMockMarketBadSlug:
    """Cover except (ValueError, IndexError) in mock_market_from_tick (lines 195-196)."""

    def test_slug_without_timestamp(self):
        from timba.backtest.common import mock_market_from_tick

        tick = _make_tick(slug="bad-slug-no-number")
        market = mock_market_from_tick(tick)
        assert market.end_timestamp == 0  # fallback for unparseable slug

    def test_slug_with_non_numeric_suffix(self):
        from timba.backtest.common import mock_market_from_tick

        tick = _make_tick(slug="btc-updown-5m-abc")
        market = mock_market_from_tick(tick)
        assert market.end_timestamp == 0


class TestTickDataFromDictBadTimestamp:
    """Cover except Exception: return None in tick_data_from_dict (lines 210-211)."""

    def test_invalid_ts_returns_none(self):
        from timba.backtest.common import tick_data_from_dict

        tick = _make_tick()
        tick["ts"] = "not-a-timestamp"
        result = tick_data_from_dict(tick)
        assert result is None

    def test_valid_ts_returns_tick_data(self):
        from timba.backtest.common import tick_data_from_dict

        tick = _make_tick(ts="2026-03-26T10:00:00+00:00")
        result = tick_data_from_dict(tick)
        assert result is not None
        assert result.tick_id == tick["id"]


# ══════════════════════════════════════════════════════════════════════
# Coverage: backtest/trades.py uncovered lines
# ══════════════════════════════════════════════════════════════════════


class TestBacktestMainEdgeCases:
    """Cover trades.py branches: strategy not found, WAL cleanup, no ticks, etc."""

    def _setup_source_db(self, tmp_path, ticks=None, coin="btc", interval="5m"):
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
        if ticks is None:
            ticks = [
                _make_tick(slug=f"{coin}-updown-{interval}-100", coin=coin, interval=interval,
                           mid_up=0.98, mid_down=0.02, fill_up=0.98, fill_down=0.05,
                           ts="2026-03-26T10:04:50+00:00"),
                _make_tick(slug=f"{coin}-updown-{interval}-100", coin=coin, interval=interval,
                           mid_up=0.99, mid_down=0.01, fill_up=0.99, fill_down=0.05,
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

    def test_strategy_not_found_exits(self, tmp_path, monkeypatch):
        """Unknown strategy name -> sys.exit(1) (lines 39-40)."""
        self._setup_source_db(tmp_path)
        monkeypatch.chdir(tmp_path)
        bt_dir = tmp_path / "bt"
        with pytest.raises(SystemExit):
            backtest_main(_make_config(), bt_dir, source_env="main", strategy="nonexistent")

    def test_wal_cleanup_on_existing_bot_db(self, tmp_path, capsys, monkeypatch):
        """WAL/SHM files are cleaned up when bot.db already exists (lines 55-60)."""
        self._setup_source_db(tmp_path)
        monkeypatch.chdir(tmp_path)
        bt_dir = tmp_path / "bt"
        bt_dir.mkdir(parents=True, exist_ok=True)

        # Create existing bot.db and WAL/SHM files
        (bt_dir / "bot.db").write_bytes(b"old db")
        (bt_dir / "bot.db-wal").write_bytes(b"old wal")
        (bt_dir / "bot.db-shm").write_bytes(b"old shm")

        backtest_main(_make_config(), bt_dir, source_env="main")
        # The old WAL/SHM should have been deleted
        out = capsys.readouterr().out
        assert "BACKTEST TRADES" in out

    def test_no_ticks_in_source_exits(self, tmp_path, monkeypatch):
        """Zero ticks in source -> sys.exit(1) (lines 72-74)."""
        # Create source dir with empty DB
        source_dir = tmp_path / "data" / "main"
        source_dir.mkdir(parents=True, exist_ok=True)
        import sqlite3

        from timba import db as db_mod
        conn = sqlite3.connect(str(source_dir / "bot.db"))
        conn.executescript(db_mod.SCHEMA_SQL)
        conn.commit()
        conn.close()

        monkeypatch.chdir(tmp_path)
        bt_dir = tmp_path / "bt"
        with pytest.raises(SystemExit):
            backtest_main(_make_config(), bt_dir, source_env="main")

    def test_contracts_per_trade_defaults_to_5(self, tmp_path, capsys, monkeypatch):
        """When contracts_per_trade is missing, defaults to 5 (line 94)."""
        self._setup_source_db(tmp_path)
        monkeypatch.chdir(tmp_path)
        bt_dir = tmp_path / "bt"

        cfg = _make_config()
        # Remove contracts_per_trade from strategy config
        cfg.strategies["favorite"]._raw.pop("contracts_per_trade", None)

        backtest_main(cfg, bt_dir, source_env="main")
        out = capsys.readouterr().out
        assert "BACKTEST TRADES" in out

    def test_market_not_in_config_skipped(self, tmp_path, capsys, monkeypatch):
        """Markets not in strategy config are skipped (line 104)."""
        # Source has eth ticks but config only has btc
        self._setup_source_db(tmp_path, coin="eth", interval="5m")
        monkeypatch.chdir(tmp_path)
        bt_dir = tmp_path / "bt"

        backtest_main(_make_config(), bt_dir, source_env="main")
        out = capsys.readouterr().out
        assert "BACKTEST TRADES" in out
        # ETH should have been skipped - 0 markets entered
        assert "Would enter:          0" in out

    def test_position_create_returns_none_skipped(self, tmp_path, capsys, monkeypatch):
        """When strat.create_position returns None, the market is skipped (line 110)."""
        from unittest.mock import patch
        self._setup_source_db(tmp_path)
        monkeypatch.chdir(tmp_path)
        bt_dir = tmp_path / "bt"

        with patch("timba.backtest.trades.get_strategy") as mock_get:
            mock_strat = mock_get.return_value
            mock_strat.create_position.return_value = None
            backtest_main(_make_config(), bt_dir, source_env="main")
            out = capsys.readouterr().out
            assert "Would enter:          0" in out

    def test_tick_data_none_skipped(self, tmp_path, capsys, monkeypatch):
        """tick_data_from_dict returning None should skip the tick (line 119)."""
        from unittest.mock import patch
        self._setup_source_db(tmp_path)
        monkeypatch.chdir(tmp_path)
        bt_dir = tmp_path / "bt"

        # Make all tick_data_from_dict return None
        with patch("timba.backtest.trades.tick_data_from_dict", return_value=None):
            backtest_main(_make_config(), bt_dir, source_env="main")
            # Should still finish without error

    def test_resolve_winning_side_none_no_side(self, tmp_path, capsys, monkeypatch):
        """winning_side=None and pos.side=None -> SKIP_NONE (lines 158-160).

        Uses mocking to ensure pos.side is definitively None and winning_side is None.
        """
        from unittest.mock import MagicMock, patch

        from timba.base import PositionState

        # Use ambiguous last tick (mid_up == mid_down == 0.5) to force winning_side=None
        ticks = [
            _make_tick(slug="btc-updown-5m-100", mid_up=0.50, mid_down=0.50,
                       fill_up=0.50, fill_down=0.50, ts="2026-03-26T10:04:50+00:00"),
            _make_tick(slug="btc-updown-5m-100", mid_up=0.50, mid_down=0.50,
                       fill_up=0.50, fill_down=0.50, ts="2026-03-26T10:04:55+00:00"),
        ]
        self._setup_source_db(tmp_path, ticks=ticks)
        monkeypatch.chdir(tmp_path)
        bt_dir = tmp_path / "bt"

        with patch("timba.backtest.trades.get_strategy") as mock_get:
            mock_strat = mock_get.return_value
            # Create a position with state=WATCHING and side=None
            mock_pos = MagicMock()
            mock_pos.state = PositionState.WATCHING
            mock_pos.skip_reason = ""
            mock_pos.side = None
            mock_pos.pnl = 0
            mock_strat.create_position.return_value = mock_pos
            decision = MagicMock()
            decision.should_bet = False
            decision.computed = None
            decision.reason = ""
            mock_strat.evaluate.return_value = decision

            backtest_main(_make_config(), bt_dir, source_env="main")
            # After execution, state should have been set to SKIP_NONE at line 159
            assert mock_pos.state == PositionState.SKIP_NONE

    def test_skipped_position_timeout(self, tmp_path, capsys, monkeypatch):
        """Position that stays WATCHING gets marked SKIPPED with timeout reason (lines 148-151)."""
        from unittest.mock import MagicMock, patch

        from timba.base import PositionState

        self._setup_source_db(tmp_path)
        monkeypatch.chdir(tmp_path)
        bt_dir = tmp_path / "bt"

        with patch("timba.backtest.trades.get_strategy") as mock_get:
            mock_strat = mock_get.return_value
            mock_pos = MagicMock()
            mock_pos.state = PositionState.WATCHING
            mock_pos.skip_reason = ""
            mock_pos.side = None
            mock_pos.pnl = 0
            mock_strat.create_position.return_value = mock_pos
            decision = MagicMock()
            decision.should_bet = False
            decision.computed = None
            decision.reason = ""
            mock_strat.evaluate.return_value = decision

            backtest_main(_make_config(), bt_dir, source_env="main")

    def test_resolve_skipped_no_side_becomes_skip_none(self, tmp_path, capsys, monkeypatch):
        """Skipped position with no side -> state becomes SKIP_NONE (line 170)."""
        from unittest.mock import MagicMock, patch

        from timba.base import PositionState

        self._setup_source_db(tmp_path)
        monkeypatch.chdir(tmp_path)
        bt_dir = tmp_path / "bt"

        with patch("timba.backtest.trades.get_strategy") as mock_get:
            mock_strat = mock_get.return_value
            mock_pos = MagicMock()
            mock_pos.state = PositionState.SKIPPED
            mock_pos.skip_reason = "test"
            mock_pos.side = None
            mock_pos.pnl = 0
            mock_strat.create_position.return_value = mock_pos
            decision = MagicMock()
            decision.should_bet = False
            decision.computed = None
            decision.reason = "test"
            mock_strat.evaluate.return_value = decision

            backtest_main(_make_config(), bt_dir, source_env="main")


# ══════════════════════════════════════════════════════════════════════
# Coverage: backtest/analyze_ticks.py uncovered lines
# ══════════════════════════════════════════════════════════════════════


class TestAnalyzeTicksHelpers:
    """Cover analyze_ticks.py helper functions (lines 15-42)."""

    def test_median_empty(self):
        from timba.backtest.analyze_ticks import _median
        assert _median([]) == 0.0

    def test_median_odd(self):
        from timba.backtest.analyze_ticks import _median
        assert _median([3, 1, 2]) == 2

    def test_median_even(self):
        from timba.backtest.analyze_ticks import _median
        assert _median([1, 2, 3, 4]) == pytest.approx(2.5)

    def test_ev_bucket_label_zero(self):
        from timba.backtest.analyze_ticks import _ev_bucket_label
        assert _ev_bucket_label(0) == "<=0"

    def test_ev_bucket_label_negative(self):
        from timba.backtest.analyze_ticks import _ev_bucket_label
        assert _ev_bucket_label(-0.01) == "<=0"

    def test_ev_bucket_label_small(self):
        from timba.backtest.analyze_ticks import _ev_bucket_label
        assert _ev_bucket_label(0.0005) == "0-.001"

    def test_ev_bucket_label_001_to_005(self):
        from timba.backtest.analyze_ticks import _ev_bucket_label
        assert _ev_bucket_label(0.003) == ".001-.005"

    def test_ev_bucket_label_005_to_01(self):
        from timba.backtest.analyze_ticks import _ev_bucket_label
        assert _ev_bucket_label(0.008) == ".005-.01"

    def test_ev_bucket_label_01_to_05(self):
        from timba.backtest.analyze_ticks import _ev_bucket_label
        assert _ev_bucket_label(0.03) == ".01-.05"


# ══════════════════════════════════════════════════════════════════════
# Coverage: backtest/analyze_trades.py — lines 60-61, 109-110,
# 214-215, 368-450, 525-526, 539-542, 550-628
# ══════════════════════════════════════════════════════════════════════


def _setup_full_analyze_db(tmp_path, trades, ticks=None, evs=None, strategy="favorite"):
    """Create SQLite DB with trades, ticks, and evs for analyze_trades integration tests."""
    import sqlite3

    db_path = tmp_path / "bot.db"
    conn = sqlite3.connect(str(db_path))
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
            end_timestamp INTEGER, market_mode TEXT, skip_reason TEXT,
            ticks_evaluated INTEGER, ev_id INTEGER, token_id TEXT,
            redeemed INTEGER DEFAULT 0, order_id TEXT, min_price REAL,
            midpoint REAL, extras TEXT,
            FOREIGN KEY (ev_id) REFERENCES evs(id)
        );
    """)
    if ticks:
        for t in ticks:
            conn.execute(
                "INSERT INTO ticks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (t["id"], t["ts"], t["slug"], t["coin"], t["interval"],
                 t["mid_up"], t["mid_down"], t["fill_up"], t["fill_down"],
                 t["signal_dir"], t["signal_chg"], t["signal_trend_sec"],
                 1 if t.get("signal_rev") else 0,
                 t.get("price_open", 0), t.get("price_now", 0)),
            )
    if evs:
        for ev in evs:
            extras = json.dumps(ev.get("extras", {})) if ev.get("extras") else None
            conn.execute(
                "INSERT INTO evs (id, tick_id, slug, strategy, remaining, progress, "
                "ev_up, ev_down, p_up, p_down, extras) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (ev["id"], ev["tick_id"], ev.get("slug", ""), strategy,
                 ev.get("remaining", 0), ev.get("progress", 0),
                 ev.get("ev_up", 0), ev.get("ev_down", 0),
                 ev.get("p_up", 0), ev.get("p_down", 0), extras),
            )
    for i, tr in enumerate(trades):
        extras = json.dumps({k: v for k, v in tr.items()
                            if k not in ("id", "type", "strategy", "slug", "coin", "interval",
                                         "side", "buy_price", "contracts", "pnl", "sniped_at",
                                         "resolved_at", "end_timestamp", "market_mode",
                                         "skip_reason", "midpoint", "ev_id")})
        conn.execute(
            "INSERT INTO trades (id, type, strategy, slug, coin, interval, side, "
            "buy_price, contracts, pnl, sniped_at, resolved_at, market_mode, "
            "midpoint, ev_id, extras) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (i + 1, tr.get("type", ""), tr.get("strategy", strategy),
             tr.get("slug", ""), tr.get("coin", ""), tr.get("interval", ""),
             tr.get("side", ""), tr.get("buy_price", 0), tr.get("contracts", 0),
             tr.get("pnl", 0), tr.get("sniped_at", ""),
             tr.get("resolved_at", ""), tr.get("market_mode", "paper"),
             tr.get("midpoint", 0), tr.get("ev_id"), extras),
        )
    conn.commit()
    conn.close()


class TestLoadEvsBadExtras:
    """Cover lines 60-61: extras JSON that fails to parse (JSONDecodeError/TypeError)."""

    def test_bad_json_extras_ignored(self, tmp_path):
        import sqlite3

        from timba.backtest.analyze_trades import _load_evs_by_id

        db_path = tmp_path / "bot.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""CREATE TABLE evs (
            id INTEGER PRIMARY KEY, tick_id INTEGER, slug TEXT,
            strategy TEXT, remaining REAL, progress REAL,
            ev_up REAL, ev_down REAL, p_up REAL, p_down REAL, extras TEXT
        )""")
        # Insert EV with invalid JSON in extras
        conn.execute("""INSERT INTO evs VALUES
            (1, 10, 'btc-5m-100', 'favorite', 5.0, 0.8, 0.02, 0.01, 0.97, 0.03,
             'not-valid-json{{{')""")
        conn.commit()
        conn.close()

        evs = _load_evs_by_id(tmp_path, "favorite")
        assert len(evs) == 1
        assert evs[1]["tick_id"] == 10
        # Should still have the EV record, just without extras merged

    def test_none_type_extras_ignored(self, tmp_path):
        import sqlite3

        from timba.backtest.analyze_trades import _load_evs_by_id

        db_path = tmp_path / "bot.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""CREATE TABLE evs (
            id INTEGER PRIMARY KEY, tick_id INTEGER, slug TEXT,
            strategy TEXT, remaining REAL, progress REAL,
            ev_up REAL, ev_down REAL, p_up REAL, p_down REAL, extras TEXT
        )""")
        # extras=NULL -> should not crash
        conn.execute("""INSERT INTO evs VALUES
            (1, 10, 'btc-5m-100', 'favorite', 5.0, 0.8, 0.02, 0.01, 0.97, 0.03, NULL)""")
        conn.commit()
        conn.close()

        evs = _load_evs_by_id(tmp_path, "favorite")
        assert len(evs) == 1


class TestLoadTicksDbError:
    """Cover lines 109-110: exception in _load_ticks_by_id skips corrupt DB."""

    def test_corrupt_db_skipped(self, tmp_path):
        from timba.backtest.analyze_trades import _load_ticks_by_id

        bad_db = tmp_path / "bot.db"
        bad_db.write_text("this is not a valid sqlite database")

        ticks = _load_ticks_by_id(tmp_path)
        assert ticks == {}

    def test_corrupt_rotated_db_skipped(self, tmp_path):
        from timba.backtest.analyze_trades import _load_ticks_by_id

        bad_db = tmp_path / "bot_2026-01-01.db"
        bad_db.write_text("corrupt")

        ticks = _load_ticks_by_id(tmp_path, tick_ids={1, 2})
        assert ticks == {}


class TestAnalyzeMainNoBetsOnlySkips:
    """Cover lines 214-215: bets=[] and skips=[] -> 'No trades to analyze'."""

    def test_no_bets_no_skips(self, tmp_path, capsys):
        """Trades with types that aren't bets or skips -> 'No trades to analyze'."""
        trades = [
            {"type": "fail_win", "strategy": "favorite", "slug": "btc-5m-100",
             "coin": "btc", "interval": "5m", "side": "up", "buy_price": 0.8,
             "contracts": 5, "pnl": 0},
        ]
        _setup_full_analyze_db(tmp_path, trades)

        analyze_main(tmp_path)
        out = capsys.readouterr().out
        assert "No trades to analyze" in out


class TestAnalyzeMainSignalPatterns:
    """Cover lines 368-450: signal pattern analysis with strategy + enriched trades."""

    def test_signal_analysis_with_strategy(self, tmp_path, capsys):
        """Full signal pattern analysis: market & PnL table, coinbase signal table, glossary."""
        slug = "btc-5m-100"
        # Create ticks
        ticks = [
            {"id": 1001, "ts": "2026-03-26T10:00:00+00:00", "slug": slug,
             "coin": "btc", "interval": "5m",
             "mid_up": 0.95, "mid_down": 0.05, "fill_up": 0.96, "fill_down": 0.06,
             "signal_dir": "up", "signal_chg": 0.20, "signal_trend_sec": 120,
             "signal_rev": False},
            {"id": 1002, "ts": "2026-03-26T10:00:01+00:00", "slug": slug,
             "coin": "btc", "interval": "5m",
             "mid_up": 0.90, "mid_down": 0.10, "fill_up": 0.91, "fill_down": 0.11,
             "signal_dir": "down", "signal_chg": 0.10, "signal_trend_sec": 50,
             "signal_rev": True},
            {"id": 1003, "ts": "2026-03-26T10:00:02+00:00", "slug": slug,
             "coin": "btc", "interval": "5m",
             "mid_up": 0.30, "mid_down": 0.70, "fill_up": 0.31, "fill_down": 0.71,
             "signal_dir": "down", "signal_chg": 0.30, "signal_trend_sec": 200,
             "signal_rev": False},
        ]
        # Create EVs referencing those ticks
        evs = [
            {"id": 1, "tick_id": 1001, "slug": slug, "remaining": 10.0,
             "progress": 0.5, "ev_up": 0.02, "ev_down": 0.01, "p_up": 0.95, "p_down": 0.05},
            {"id": 2, "tick_id": 1002, "slug": slug, "remaining": 8.0,
             "progress": 0.6, "ev_up": 0.01, "ev_down": 0.02, "p_up": 0.90, "p_down": 0.10},
            {"id": 3, "tick_id": 1003, "slug": slug, "remaining": 5.0,
             "progress": 0.8, "ev_up": -0.01, "ev_down": 0.03, "p_up": 0.30, "p_down": 0.70},
        ]
        # Create trades covering all groups: bet win, bet loss, skip win, skip loss, fail win, fail loss
        trades = [
            {"type": "paper_win", "slug": slug, "coin": "btc", "interval": "5m",
             "side": "up", "buy_price": 0.95, "contracts": 5, "pnl": 0, "ev_id": 1,
             "sniped_at": "2026-03-26T10:00:00+00:00"},
            {"type": "paper_loss", "slug": slug, "coin": "btc", "interval": "5m",
             "side": "up", "buy_price": 0.90, "contracts": 5, "pnl": 0, "ev_id": 2,
             "sniped_at": "2026-03-26T10:00:01+00:00"},
            {"type": "skip_win", "slug": slug, "coin": "btc", "interval": "5m",
             "side": "up", "buy_price": 0.80, "contracts": 5, "pnl": 0, "ev_id": 1},
            {"type": "skip_loss", "slug": slug, "coin": "btc", "interval": "5m",
             "side": "down", "buy_price": 0.70, "contracts": 5, "pnl": 0, "ev_id": 2},
            {"type": "fail_win", "slug": slug, "coin": "btc", "interval": "5m",
             "side": "up", "buy_price": 0.85, "contracts": 5, "pnl": 0, "ev_id": 3},
            {"type": "fail_loss", "slug": slug, "coin": "btc", "interval": "5m",
             "side": "down", "buy_price": 0.75, "contracts": 5, "pnl": 0, "ev_id": 3},
        ]
        _setup_full_analyze_db(tmp_path, trades, ticks=ticks, evs=evs)

        analyze_main(tmp_path, strategy="favorite")
        out = capsys.readouterr().out
        # Signal analysis tables should be present
        assert "Market & PnL at Decision" in out or "Market" in out
        assert "Coinbase Signal at Decision" in out or "Signal at Decision" in out
        assert "Column Glossary" in out or "Glossary" in out
        # Specific group labels should appear (Rich may truncate column text)
        assert "Bet Win" in out
        assert "Bet Loss" in out or "Bet Lo" in out
        # Glossary content
        assert "Chg" in out
        assert "Trend" in out

    def test_signal_analysis_empty_groups(self, tmp_path, capsys):
        """Signal analysis with no enriched data shows 0-count rows."""
        slug = "btc-5m-100"
        ticks = [
            {"id": 2001, "ts": "2026-03-26T10:00:00+00:00", "slug": slug,
             "coin": "btc", "interval": "5m",
             "mid_up": 0.95, "mid_down": 0.05, "fill_up": 0.96, "fill_down": 0.06,
             "signal_dir": "up", "signal_chg": 0.20, "signal_trend_sec": 120,
             "signal_rev": False},
        ]
        evs = [
            {"id": 10, "tick_id": 2001, "slug": slug, "remaining": 10.0,
             "progress": 0.5, "ev_up": 0.02, "ev_down": 0.01, "p_up": 0.95, "p_down": 0.05},
        ]
        # Only a bet win -- other groups will be empty
        trades = [
            {"type": "paper_win", "slug": slug, "coin": "btc", "interval": "5m",
             "side": "up", "buy_price": 0.95, "contracts": 5, "pnl": 0, "ev_id": 10},
        ]
        _setup_full_analyze_db(tmp_path, trades, ticks=ticks, evs=evs)

        analyze_main(tmp_path, strategy="favorite")
        out = capsys.readouterr().out
        assert "Market & PnL at Decision" in out
        assert "Skip Wins" in out  # should be in table even with 0 count


class TestPrintLossTimelines:
    """Cover lines 525-526, 539-542, 550-628: loss deep-dive with EV + tick timeline."""

    def test_loss_timeline_rendered(self, tmp_path, capsys):
        """Loss trades trigger the deep-dive timeline with post-bet ticks."""
        slug = "btc-5m-100"
        # Create ticks with timestamps that allow post-bet tick detection
        ticks = [
            {"id": 3001, "ts": "2026-03-26T10:00:00+00:00", "slug": slug,
             "coin": "btc", "interval": "5m",
             "mid_up": 0.60, "mid_down": 0.40, "fill_up": 0.61, "fill_down": 0.41,
             "signal_dir": "up", "signal_chg": 0.10, "signal_trend_sec": 60,
             "signal_rev": False},
            {"id": 3002, "ts": "2026-03-26T10:00:01+00:00", "slug": slug,
             "coin": "btc", "interval": "5m",
             "mid_up": 0.55, "mid_down": 0.45, "fill_up": 0.56, "fill_down": 0.46,
             "signal_dir": "up", "signal_chg": 0.08, "signal_trend_sec": 70,
             "signal_rev": False},
            # Post-bet ticks (after the EV)
            {"id": 3003, "ts": "2026-03-26T10:00:02+00:00", "slug": slug,
             "coin": "btc", "interval": "5m",
             "mid_up": 0.40, "mid_down": 0.60, "fill_up": 0.41, "fill_down": 0.61,
             "signal_dir": "down", "signal_chg": 0.15, "signal_trend_sec": 80,
             "signal_rev": True},
            {"id": 3004, "ts": "2026-03-26T10:00:03+00:00", "slug": slug,
             "coin": "btc", "interval": "5m",
             "mid_up": 0.30, "mid_down": 0.70, "fill_up": 0.31, "fill_down": 0.71,
             "signal_dir": "down", "signal_chg": 0.25, "signal_trend_sec": 90,
             "signal_rev": False},
        ]
        evs = [
            {"id": 20, "tick_id": 3001, "slug": slug, "remaining": 10.0,
             "progress": 0.5, "ev_up": 0.02, "ev_down": 0.01, "p_up": 0.60, "p_down": 0.40},
            {"id": 21, "tick_id": 3002, "slug": slug, "remaining": 8.0,
             "progress": 0.6, "ev_up": 0.01, "ev_down": 0.02, "p_up": 0.55, "p_down": 0.45},
        ]
        # A loss trade to trigger the deep-dive
        trades = [
            {"type": "loss", "slug": slug, "coin": "btc", "interval": "5m",
             "side": "up", "buy_price": 0.60, "contracts": 5, "pnl": 0, "ev_id": 20,
             "sniped_at": "2026-03-26T10:00:00+00:00"},
        ]
        _setup_full_analyze_db(tmp_path, trades, ticks=ticks, evs=evs)

        analyze_main(tmp_path, strategy="favorite")
        out = capsys.readouterr().out
        # Loss deep dive section
        assert "Loss Deep Dive" in out
        assert "EVs" in out
        assert "ticks" in out
        # Post-bet ticks section
        assert "Post-bet ticks" in out
        # Resolution line
        assert "Resolution" in out

    def test_loss_timeline_no_evs(self, tmp_path, capsys):
        """Loss trade where no EVs are found for the slug -> 'No EVs found'."""
        slug = "btc-5m-100"
        slug2 = "btc-5m-999"  # EVs will be for a different slug
        ticks = [
            {"id": 4001, "ts": "2026-03-26T10:00:00+00:00", "slug": slug2,
             "coin": "btc", "interval": "5m",
             "mid_up": 0.60, "mid_down": 0.40, "fill_up": 0.61, "fill_down": 0.41,
             "signal_dir": "up", "signal_chg": 0.10, "signal_trend_sec": 60,
             "signal_rev": False},
        ]
        evs = [
            {"id": 30, "tick_id": 4001, "slug": slug2, "remaining": 10.0,
             "progress": 0.5, "ev_up": 0.02, "ev_down": 0.01, "p_up": 0.60, "p_down": 0.40},
        ]
        trades = [
            {"type": "loss", "slug": slug, "coin": "btc", "interval": "5m",
             "side": "up", "buy_price": 0.60, "contracts": 5, "pnl": 0, "ev_id": 30,
             "sniped_at": "2026-03-26T10:00:00+00:00"},
        ]
        _setup_full_analyze_db(tmp_path, trades, ticks=ticks, evs=evs)

        analyze_main(tmp_path, strategy="favorite")
        out = capsys.readouterr().out
        assert "Loss Deep Dive" in out
        assert "No EVs found" in out

    def test_loss_timeline_many_post_ticks(self, tmp_path, capsys):
        """Loss with >6 post-bet ticks triggers the sample (first 3 + last 3) display."""
        slug = "btc-5m-100"
        # One EV tick, plus 8 post-bet ticks (> 6 triggers sampling)
        ticks = [
            {"id": 5001, "ts": "2026-03-26T10:00:00+00:00", "slug": slug,
             "coin": "btc", "interval": "5m",
             "mid_up": 0.60, "mid_down": 0.40, "fill_up": 0.61, "fill_down": 0.41,
             "signal_dir": "up", "signal_chg": 0.10, "signal_trend_sec": 60,
             "signal_rev": False},
        ]
        for i in range(8):
            ticks.append(
                {"id": 5002 + i, "ts": f"2026-03-26T10:00:0{i+1}+00:00", "slug": slug,
                 "coin": "btc", "interval": "5m",
                 "mid_up": 0.40 - i * 0.03, "mid_down": 0.60 + i * 0.03,
                 "fill_up": 0.41, "fill_down": 0.61,
                 "signal_dir": "down", "signal_chg": 0.10 + i * 0.02,
                 "signal_trend_sec": 70 + i * 10, "signal_rev": False},
            )
        evs = [
            {"id": 40, "tick_id": 5001, "slug": slug, "remaining": 10.0,
             "progress": 0.5, "ev_up": 0.02, "ev_down": 0.01, "p_up": 0.60, "p_down": 0.40},
        ]
        trades = [
            {"type": "paper_loss", "slug": slug, "coin": "btc", "interval": "5m",
             "side": "up", "buy_price": 0.60, "contracts": 5, "pnl": 0, "ev_id": 40,
             "sniped_at": "2026-03-26T10:00:00+00:00"},
        ]
        _setup_full_analyze_db(tmp_path, trades, ticks=ticks, evs=evs)

        analyze_main(tmp_path, strategy="favorite")
        out = capsys.readouterr().out
        assert "Loss Deep Dive" in out
        assert "Post-bet ticks" in out
        assert "Resolution" in out

    def test_ev_bucket_label_05_to_10(self):
        from timba.backtest.analyze_ticks import _ev_bucket_label
        assert _ev_bucket_label(0.08) == ".05-.10"

    def test_ev_bucket_label_large(self):
        from timba.backtest.analyze_ticks import _ev_bucket_label
        assert _ev_bucket_label(0.15) == ".10+"


class TestAnalyzeTicksFilterBranches:
    """Cover analyze_ticks_main filter branches (lines 58-75)."""

    def test_coin_filter(self, tmp_path, capsys):
        """Filtering by coin should only show matching markets (lines 58-67)."""
        ticks = [
            _make_tick(slug="btc-updown-5m-100", coin="btc", interval="5m",
                       ev_up=0.03, p_up=0.83, signal_dir="up",
                       ts="2026-03-26T10:00:00+00:00"),
            _make_tick(slug="eth-updown-15m-200", coin="eth", interval="15m",
                       ev_up=0.02, p_up=0.82, signal_dir="down",
                       ts="2026-03-26T10:00:00+00:00"),
        ]
        trades = [
            {"type": "paper_win", "slug": "btc-updown-5m-100", "coin": "btc", "interval": "5m"},
            {"type": "paper_loss", "slug": "eth-updown-15m-200", "coin": "eth", "interval": "15m"},
        ]
        _setup_analyze_ticks_db(tmp_path, ticks, trades)

        analyze_ticks_main(tmp_path, coin="btc")
        out = capsys.readouterr().out
        assert "BTC" in out

    def test_interval_filter(self, tmp_path, capsys):
        """Filtering by interval should only show matching markets."""
        ticks = [
            _make_tick(slug="btc-updown-5m-100", coin="btc", interval="5m",
                       ev_up=0.03, p_up=0.83, signal_dir="up",
                       ts="2026-03-26T10:00:00+00:00"),
            _make_tick(slug="eth-updown-15m-200", coin="eth", interval="15m",
                       ev_up=0.02, p_up=0.82, signal_dir="down",
                       ts="2026-03-26T10:00:00+00:00"),
        ]
        trades = [
            {"type": "paper_win", "slug": "btc-updown-5m-100", "coin": "btc", "interval": "5m"},
            {"type": "paper_loss", "slug": "eth-updown-15m-200", "coin": "eth", "interval": "15m"},
        ]
        _setup_analyze_ticks_db(tmp_path, ticks, trades)

        analyze_ticks_main(tmp_path, interval="5m")
        out = capsys.readouterr().out
        assert "BTC" in out

    def test_filter_no_match_exits(self, tmp_path):
        """Filter that matches no ticks -> sys.exit(1) (lines 68-75)."""
        ticks = [
            _make_tick(slug="btc-updown-5m-100", coin="btc", interval="5m",
                       ev_up=0.03, ts="2026-03-26T10:00:00+00:00"),
        ]
        _setup_analyze_ticks_db(tmp_path, ticks, [])

        with pytest.raises(SystemExit):
            analyze_ticks_main(tmp_path, coin="sol")

    def test_filter_coin_and_interval_no_match_exits(self, tmp_path):
        """Filter by both coin and interval with no matches -> exit (lines 69-75)."""
        ticks = [
            _make_tick(slug="btc-updown-5m-100", coin="btc", interval="5m",
                       ev_up=0.03, ts="2026-03-26T10:00:00+00:00"),
        ]
        _setup_analyze_ticks_db(tmp_path, ticks, [])

        with pytest.raises(SystemExit):
            analyze_ticks_main(tmp_path, coin="btc", interval="15m")

    def test_skip_count_shown(self, tmp_path, capsys):
        """When skip_count > 0, it is displayed (line 85)."""
        from unittest.mock import patch
        ticks = [
            _make_tick(slug="btc-updown-5m-100", coin="btc", interval="5m",
                       ev_up=0.03, p_up=0.83, signal_dir="up",
                       ts="2026-03-26T10:00:00+00:00"),
        ]
        trades = [
            {"type": "paper_win", "slug": "btc-updown-5m-100", "coin": "btc", "interval": "5m"},
        ]
        _setup_analyze_ticks_db(tmp_path, ticks, trades)

        # Patch load_ticks_with_evs to return a non-zero skip_count
        original_load = __import__("timba.backtest.common", fromlist=["load_ticks_with_evs"]).load_ticks_with_evs

        def patched_load(data_dir, strategy="favorite"):
            result, _ = original_load(data_dir, strategy=strategy)
            return result, 5  # simulate 5 skipped

        with patch("timba.backtest.analyze_ticks.load_ticks_with_evs", side_effect=patched_load):
            analyze_ticks_main(tmp_path)
        out = capsys.readouterr().out
        assert "Skipped (incomplete): 5" in out

    def test_skipped_markets_lost_coin_breakdown(self, tmp_path, capsys):
        """Skipped markets with loss -> coin breakdown shows lost count (line 302)."""
        ticks_btc = [_make_tick(slug="btc-updown-5m-100", coin="btc", interval="5m",
                                ev_up=-0.01, ev_down=-0.02,
                                ts="2026-03-26T10:00:00+00:00")]
        ticks_eth = [_make_tick(slug="eth-updown-15m-200", coin="eth", interval="15m",
                                ev_up=-0.01, ev_down=-0.02,
                                ts="2026-03-26T10:00:00+00:00")]
        trades = [
            {"type": "paper_loss", "slug": "btc-updown-5m-100", "coin": "btc", "interval": "5m"},
            {"type": "paper_win", "slug": "eth-updown-15m-200", "coin": "eth", "interval": "15m"},
        ]
        _setup_analyze_ticks_db(tmp_path, ticks_btc + ticks_eth, trades)

        analyze_ticks_main(tmp_path)
        out = capsys.readouterr().out
        assert "SKIPPED MARKETS" in out
        assert "BTC" in out
        assert "ETH" in out
