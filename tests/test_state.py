import time

import pytest

from timba import db
from timba.base import PositionState
from timba.state import State
from timba.strategies.favorite import FavoritePosition


def _make_position(slug="test-slug", **kw):
    defaults = dict(
        condition_id="0x1", question="Test", slug=slug,
        coin="btc", interval="5m", token_id_up="tu", token_id_down="td",
        end_timestamp=int(time.time()), window_start_ts=int(time.time()) - 300,
        min_price=0.95, min_signal_chg=0.05,
    )
    defaults.update(kw)
    return FavoritePosition(**defaults)


class TestState:
    def test_fresh_state(self, tmp_data_dir):
        state = State()
        assert state.portfolio == 0
        assert state.cash == 0

    def test_daily_trade_log(self, tmp_data_dir):
        db.init(tmp_data_dir)
        state = State()
        pos = _make_position(slug="test-123")
        pos.state = PositionState.WON
        pos.side = "up"
        pos.buy_price = 0.90
        pos.pnl = 1.0
        pos.sniped_at = "2026-03-24T10:00:00Z"
        pos.resolved_at = "2026-03-24T10:00:30Z"
        state.record_trade(pos, "favorite")
        # Trades now go to SQLite
        db.flush()
        trades = db.load_trades(strategy="favorite")
        assert len(trades) == 1
        assert trades[0]["slug"] == "test-123"
        assert trades[0]["type"] == "win"

    def test_dashboard_dict(self, tmp_data_dir):
        db.init(tmp_data_dir)
        state = State()
        state.init_portfolio(10000)
        d = state.to_dashboard_dict()
        assert d["portfolio"] == 10000
        assert d["total_pnl"] == 0

    def test_init_trade_ids_seeds_from_db(self, tmp_data_dir):
        """init_trade_ids() should seed counter from max trade id in SQLite."""
        from timba.state import _next_trade_id, init_trade_ids
        db.init(tmp_data_dir)
        # Insert a trade with id=42 directly
        db.insert_trade({
            "id": 42, "type": "paper_win", "strategy": "favorite",
            "slug": "test-slug", "coin": "btc", "interval": "5m",
            "side": "up", "buy_price": 0.90, "contracts": 5, "pnl": 0.5,
        })
        db.flush()
        init_trade_ids()
        assert _next_trade_id() == 43

    def test_init_trade_ids_empty_db_starts_at_one(self, tmp_data_dir):
        """init_trade_ids() with empty db should start counter at 1."""
        from timba.state import _next_trade_id, init_trade_ids
        db.init(tmp_data_dir)
        init_trade_ids()
        assert _next_trade_id() == 1

    def test_get_strategy_stats_returns_stats(self, tmp_data_dir):
        """get_strategy_stats() should return the stats dict after recording trades."""
        db.init(tmp_data_dir)
        state = State()
        pos = _make_position(slug="test-stats")
        pos.state = PositionState.PAPER_WON
        pos.side = "up"
        pos.buy_price = 0.90
        pos.pnl = 0.5
        state.record_trade(pos, "favorite")

        pos2 = _make_position(
            condition_id="0x2", slug="test-stats-2", coin="eth",
            token_id_up="tu2", token_id_down="td2",
        )
        pos2.state = PositionState.PAPER_LOST
        pos2.side = "down"
        pos2.buy_price = 0.85
        pos2.pnl = -0.85
        state.record_trade(pos2, "favorite")

        db.flush()
        stats = db.get_strategy_stats("favorite")
        assert stats["paper_win"] == 1
        assert stats["paper_loss"] == 1

    def test_get_strategy_pnl_returns_pnl(self, tmp_data_dir):
        """get_strategy_pnl() should return total PnL for the strategy."""
        db.init(tmp_data_dir)
        state = State()
        # Record a real win to accumulate PnL
        pos = _make_position(slug="test-pnl")
        pos.state = PositionState.WON
        pos.side = "up"
        pos.buy_price = 0.90
        pos.pnl = 0.50
        pos.contracts = 5
        state.record_trade(pos, "favorite")

        db.flush()
        assert db.get_strategy_pnl("favorite") == pytest.approx(0.50)

    def test_init_portfolio_sets_cash_and_portfolio(self, tmp_data_dir):
        """init_portfolio should set cash and portfolio from amount."""
        db.init(tmp_data_dir)
        state = State()
        state.init_portfolio(500)
        assert state.cash == pytest.approx(500)
        assert state.portfolio == pytest.approx(500)

    def test_append_trade_log_error_silent(self, tmp_data_dir, monkeypatch):
        """_append_trade_log should silently handle db.insert_trade errors."""
        db.init(tmp_data_dir)
        state = State()
        # Mock db.insert_trade to raise
        monkeypatch.setattr(db, "insert_trade", lambda entry: (_ for _ in ()).throw(RuntimeError("db boom")))
        pos = _make_position(slug="test-err")
        pos.state = PositionState.PAPER_WON
        pos.side = "up"
        pos.buy_price = 0.90
        pos.pnl = 0.5
        # Should not raise despite db.insert_trade failing
        state.record_trade(pos, "favorite")
