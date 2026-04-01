import time

import pytest

from timba.base import PositionState
from timba.config import Config, StrategyConfig
from timba.state import State
from timba.strategies.favorite import FavoritePosition


def _make_position(**kw):
    defaults = dict(
        condition_id="0x1", question="Bitcoin Up or Down - March 23, 10:00AM-10:05AM ET",
        slug="btc-updown-5m-123", coin="btc", interval="5m",
        token_id_up="tu", token_id_down="td",
        end_timestamp=int(time.time()) + 10,
        window_start_ts=int(time.time()) - 280,
        contracts=200, entry_window_sec=20, close_window_sec=5, market_mode="paper",
        min_price=0.95, min_signal_chg=0.05,
    )
    defaults.update(kw)
    return FavoritePosition(**defaults)


class TestPortfolioState:
    def test_init_portfolio(self, tmp_path):
        state = State()
        assert state.portfolio == 0
        assert state.cash == 0
        state.init_portfolio(10000)
        assert state.portfolio == 10000
        assert state.cash == 10000

    def test_deduct_cash_success(self, tmp_path):
        state = State()
        state.init_portfolio(1000)
        assert state.deduct_cash(200) is True
        assert state.cash == pytest.approx(800)
        assert state.portfolio == pytest.approx(1000)  # unchanged

    def test_deduct_cash_insufficient(self, tmp_path):
        state = State()
        state.init_portfolio(100)
        assert state.deduct_cash(200) is False
        assert state.cash == pytest.approx(100)  # unchanged
        assert state.portfolio == pytest.approx(100)

    def test_refund_cash(self, tmp_path):
        state = State()
        state.init_portfolio(1000)
        state.deduct_cash(200)
        assert state.cash == pytest.approx(800)
        state.refund_cash(200)
        assert state.cash == pytest.approx(1000)
        assert state.portfolio == pytest.approx(1000)

    def test_portfolio_and_cash_in_dashboard_dict(self, tmp_path):
        from timba import db
        data_dir = tmp_path / "data"
        db.init(data_dir)
        state = State()
        state.init_portfolio(10000)
        state.deduct_cash(500)
        d = state.to_dashboard_dict()
        assert d["portfolio"] == pytest.approx(10000)
        assert d["cash"] == pytest.approx(9500)


class TestPortfolioWithTrades:
    def test_win_defers_cash_until_redeem(self, tmp_path):
        state = State()
        state.init_portfolio(1000)
        state.deduct_cash(184)  # simulate bet cost

        pos = _make_position()
        pos.state = PositionState.WON
        pos.side = "up"
        pos.buy_price = 0.92
        pos.contracts = 200
        pos.pnl = 16.0  # (1.0 - 0.92) * 200
        pos.sniped_at = "2026-03-23T20:00:00Z"
        pos.resolved_at = "2026-03-23T20:00:30Z"

        state.record_trade(pos, "favorite")
        # Cash NOT credited yet (waiting for redemption)
        assert state.cash == pytest.approx(816)  # 1000 - 184
        # Portfolio gains profit
        assert state.portfolio == pytest.approx(1016)  # 1000 + 16
        # Payout is pending
        assert state.pending_redemption == pytest.approx(200)  # 200 contracts * $1.00

        # After redemption, cash is credited
        state.credit_redemption(200)
        assert state.cash == pytest.approx(1016)  # 816 + 200
        assert state.pending_redemption == pytest.approx(0)

    def test_loss_hits_portfolio_not_cash(self, tmp_path):
        state = State()
        state.init_portfolio(1000)
        state.deduct_cash(184)

        pos = _make_position()
        pos.state = PositionState.LOST
        pos.side = "up"
        pos.buy_price = 0.92
        pos.contracts = 200
        pos.pnl = -184.0
        pos.sniped_at = "2026-03-23T20:00:00Z"
        pos.resolved_at = "2026-03-23T20:00:30Z"

        state.record_trade(pos, "favorite")
        # Cash: 1000 - 184 = 816 (unchanged on loss)
        assert state.cash == pytest.approx(816)
        # Portfolio: 1000 + (-184) = 816
        assert state.portfolio == pytest.approx(816)


class TestPortfolioCalculation:
    def test_auto_calculate(self):
        config = Config()
        config.strategies["favorite"] = StrategyConfig({
            "enabled": True, "contracts_per_trade": 200,
            "markets": [
                {"coin": "btc", "interval": "5m"},
                {"coin": "eth", "interval": "5m"},
                {"coin": "btc", "interval": "15m"},
                {"coin": "eth", "interval": "15m"},
                {"coin": "btc", "interval": "1h"},
                {"coin": "eth", "interval": "1h"},
            ],
        })
        portfolio = config.calculate_portfolio()
        # 5m: 2 coins x 2 concurrent x 200 x 0.95 = 760
        # 15m: 2 x 1 x 200 x 0.95 = 380
        # 1h: 2 x 1 x 200 x 0.95 = 380
        # Total: 1520 x 1.5 buffer = 2280
        assert portfolio == pytest.approx(2280)

    def test_smaller_config_smaller_portfolio(self):
        config = Config()
        config.strategies["favorite"] = StrategyConfig({
            "enabled": True, "contracts_per_trade": 50,
            "markets": [{"coin": "btc", "interval": "5m"}],
        })
        portfolio = config.calculate_portfolio()
        # 1 coin x 2 concurrent x 50 x 0.95 x 1.5 = 142.5
        assert portfolio == pytest.approx(142.5)


