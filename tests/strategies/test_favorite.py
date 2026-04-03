"""Tests for favorite strategy — position creation, evaluation, resolution, extras."""

import time
from unittest.mock import MagicMock

import pytest

from timba.base import PositionState
from timba.feed import DirectionSignal
from timba.market import UpDownMarket
from timba.strategies import BetDecision, TickData
from timba.strategies.favorite import FavoritePosition, FavoriteStrategy


def _make_market(**kw):
    defaults = dict(
        condition_id="0x1", question="test",
        slug="btc-updown-5m-1000000",
        coin="btc", interval="5m",
        token_id_up="tu", token_id_down="td",
        end_timestamp=int(time.time()) + 300,
        gamma_price_up=0.5, gamma_price_down=0.5,
    )
    defaults.update(kw)
    return UpDownMarket(**defaults)


def _make_pos(**kw):
    defaults = dict(
        condition_id="0x1", question="test",
        slug="btc-updown-5m-1000000",
        coin="btc", interval="5m",
        token_id_up="tu", token_id_down="td",
        end_timestamp=int(time.time()) + 5,
        window_start_ts=int(time.time()) - 295,
        contracts=5, entry_window_sec=10, close_window_sec=2,
        min_price=0.98,
    )
    defaults.update(kw)
    return FavoritePosition(**defaults)


def _make_tick(**kw):
    defaults = dict(
        tick_id=1, ts=time.time(),
        signal=DirectionSignal("up", 0.1, 30, False, 0.5),
        mid_up=0.98, mid_down=0.02,
        fill_up=0.99, fill_down=0.05,
        size_up=200, size_down=200,
    )
    defaults.update(kw)
    return TickData(**defaults)


strat = FavoriteStrategy()


class TestCreatePosition:
    def test_creates_valid_position(self):
        market = _make_market()
        cfg = {"entry_window_sec": 10, "close_window_sec": 2}
        global_cfg = MagicMock()
        global_cfg.get = lambda k, d=None: {"contracts_per_trade": 5, "min_price": 0.98, "resolve_delay_sec": 30}.get(k, d)

        pos = strat.create_position(market, cfg, global_cfg)
        assert pos is not None
        assert pos.min_price == 0.98

    def test_returns_none_when_missing_entry_window(self):
        market = _make_market()
        cfg = {"close_window_sec": 2}
        pos = strat.create_position(market, cfg, MagicMock(get=lambda k, d=None: d))
        assert pos is None

    def test_returns_none_when_missing_close_window(self):
        market = _make_market()
        cfg = {"entry_window_sec": 10}
        pos = strat.create_position(market, cfg, MagicMock(get=lambda k, d=None: d))
        assert pos is None

    def test_handles_unparseable_slug(self):
        market = _make_market(slug="bad-slug-no-timestamp")
        cfg = {"entry_window_sec": 10, "close_window_sec": 2}
        global_cfg = MagicMock(get=lambda k, d=None: d)
        pos = strat.create_position(market, cfg, global_cfg)
        assert pos is not None
        assert pos.window_start_ts == market.end_timestamp - 300


class TestEvaluate:
    def test_bets_when_above_min_price(self):
        pos = _make_pos()
        tick = _make_tick(mid_up=0.99, fill_up=0.99)
        decision = strat.evaluate(pos, tick)
        assert decision.should_bet is True
        assert decision.side == "up"

    def test_skips_when_below_min_price(self):
        pos = _make_pos()
        tick = _make_tick(mid_up=0.60, mid_down=0.40, fill_up=0.65, fill_down=0.45)
        decision = strat.evaluate(pos, tick)
        assert decision.should_bet is False
        assert "no favorite" in decision.reason

    def test_skips_when_already_bet(self):
        pos = _make_pos()
        pos.state = PositionState.PAPER
        tick = _make_tick(mid_up=0.99, fill_up=0.99)
        decision = strat.evaluate(pos, tick)
        assert decision.should_bet is False
        assert "already bet" in decision.reason

    def test_computed_has_expected_fields(self):
        pos = _make_pos()
        tick = _make_tick()
        decision = strat.evaluate(pos, tick)
        c = decision.computed
        assert "tick_id" in c
        assert "remaining" in c
        assert "progress" in c
        assert "ev_up" in c
        assert "ev_down" in c

    def test_sets_side_even_when_not_betting(self):
        pos = _make_pos()
        tick = _make_tick(mid_up=0.60, mid_down=0.40)
        strat.evaluate(pos, tick)
        assert pos.side == "up"  # up has higher midpoint


class TestEvaluateDownSide:
    def test_selects_down_when_mid_down_higher(self):
        """evaluate selects 'down' when mid_down > mid_up (line 106)."""
        pos = _make_pos(min_price=0.90)
        tick = _make_tick(mid_up=0.10, mid_down=0.98, fill_up=0.15, fill_down=0.99)
        strat.evaluate(pos, tick)
        assert pos.side == "down"

    def test_weak_signal_skips_bet(self):
        """evaluate skips bet when signal change is below min_signal_chg (line 135)."""
        pos = _make_pos(min_signal_chg=0.50)  # require 0.50% change
        # Signal change is only 0.1% (default in _make_tick)
        tick = _make_tick(mid_up=0.99, fill_up=0.99,
                          signal=DirectionSignal("up", 0.1, 30, False, 0.5))
        decision = strat.evaluate(pos, tick)
        assert decision.should_bet is False
        assert "weak signal" in decision.reason


class TestResolve:
    def test_win_calculates_pnl(self):
        pos = _make_pos(buy_price=0.95, contracts=10)
        pos.state = PositionState.SNIPED
        pos.side = "up"
        strat.resolve(pos, won=True)
        assert pos.pnl == pytest.approx((1.0 - 0.95) * 10)

    def test_loss_calculates_pnl(self):
        pos = _make_pos(buy_price=0.95, contracts=10)
        pos.state = PositionState.SNIPED
        pos.side = "up"
        strat.resolve(pos, won=False)
        assert pos.pnl == pytest.approx(-0.95 * 10)

    def test_paper_win(self):
        pos = _make_pos(buy_price=0.98, contracts=5)
        pos.state = PositionState.PAPER
        pos.side = "up"
        strat.resolve(pos, won=True)
        assert pos.pnl == pytest.approx(0.10)  # (1.0 - 0.98) * 5

    def test_paper_loss(self):
        pos = _make_pos(buy_price=0.98, contracts=5)
        pos.state = PositionState.PAPER
        pos.side = "up"
        strat.resolve(pos, won=False)
        assert pos.pnl == pytest.approx(-4.90)  # -0.98 * 5


class TestOnBet:
    def test_logs_without_error(self):
        pos = _make_pos()
        decision = BetDecision(should_bet=True, side="up", price=0.98, size=5)
        strat.on_bet(pos, decision)  # should not raise


class TestExtraFields:
    def test_returns_min_price_and_midpoint(self):
        pos = _make_pos(min_price=0.95)
        pos.midpoint = 0.975
        fields = strat.extra_fields(pos)
        assert fields == {"min_price": 0.95, "midpoint": 0.975}


class TestConfigSchema:
    def test_declares_strategy_and_market_fields(self):
        schema = FavoriteStrategy.config_schema()
        assert "min_price" in schema["strategy"]
        assert "contracts_per_trade" in schema["strategy"]
        assert "min_price" in schema["market"]["properties"]


class TestBaseConfigSchema:
    """Cover Strategy.config_schema() default return (line 139)."""

    def test_base_config_schema_returns_empty_dict(self):
        from timba.strategies import Strategy

        # Create a minimal concrete subclass that does NOT override config_schema
        class MinimalStrategy(Strategy):
            @property
            def name(self):
                return "minimal"

            def create_position(self, market, market_cfg, global_cfg):
                pass

            def evaluate(self, pos, tick):
                pass

            def on_bet(self, pos, decision):
                pass

            def resolve(self, pos, won):
                pass

            def extra_fields(self, pos):
                return {}

        result = MinimalStrategy.config_schema()
        assert result == {}


class TestLoadStrategiesErrorHandling:
    """Cover the except block in load_strategies (lines 178-180)."""

    def test_bad_strategy_module_logged_not_raised(self, tmp_path, monkeypatch):
        """A strategy module that fails to import should be skipped, not crash."""
        import timba.strategies as strat_mod

        # Create a bad strategy file in the strategies directory
        strategies_dir = strat_mod.__path__[0] if hasattr(strat_mod, '__path__') else None
        if strategies_dir is None:
            pytest.skip("Cannot locate strategies package directory")

        from pathlib import Path
        bad_file = Path(strategies_dir) / "_test_bad_strategy.py"
        try:
            bad_file.write_text("raise ImportError('deliberate test failure')\n")
            # load_strategies globs *.py excluding _ prefixed, so this won't trigger.
            # Instead, monkeypatch importlib.import_module for a known module.
            import importlib
            original_import = importlib.import_module

            def failing_import(name, *args, **kwargs):
                if "timba.strategies.favorite" in name:
                    raise RuntimeError("test load failure")
                return original_import(name, *args, **kwargs)

            monkeypatch.setattr(importlib, "import_module", failing_import)

            # This should not raise — the error is logged and skipped
            strat_mod.load_strategies()
        finally:
            if bad_file.exists():
                bad_file.unlink()
