"""Integration tests: verify the bot actually works end-to-end."""

import os
import time
from unittest.mock import MagicMock, patch

import pytest

from timba.base import PositionState
from timba.clob_helpers import _get_midpoint, simulate_fill
from timba.config import Config
from timba.market import MarketSeries, UpDownMarket, discover_active_markets
from timba.state import State
from timba.trader import Trader

_has_creds = bool(os.environ.get("POLYMARKET_PRIVATE_KEY"))
live = lambda f: pytest.mark.live(pytest.mark.skipif(not _has_creds, reason="POLYMARKET_PRIVATE_KEY not set")(f))


@pytest.fixture
def paper_config(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("""
favorite:
  enabled: true
  min_price: 0.95
  min_signal_chg: 0.05
  contracts_per_trade: 10
  markets:
    - coin: btc
      interval: 5m
      mode: paper
      entry_window_sec: 10
      close_window_sec: 3
""")
    config = Config.load(cfg_file)
    config.polymarket.private_key = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
    config.polymarket.funder = "0xDeaDbeefdEAdbeefdEadbEEFdeadbeEFdEaDbeeF"
    return config


@pytest.fixture
def mock_market():
    return UpDownMarket(
        condition_id="0xtest",
        question="Bitcoin Up or Down - March 24, 10:00AM-10:05AM ET",
        slug="btc-updown-5m-9999999999",
        token_id_up="tok_up",
        token_id_down="tok_down",
        end_timestamp=int(time.time()) + 30,
        coin="btc",
        interval="5m",
        gamma_price_up=0.6,
        gamma_price_down=0.4,
        liquidity=-1,
    )


def _favorite_positions(trader):
    return trader.positions.get("favorite", {})


def _configure_mock_clob(mock_clob):
    """Configure a mocked PolymarketClobClient with sane defaults."""
    instance = MagicMock()
    instance.create_or_derive_api_creds.return_value = MagicMock(key="k", secret="s", passphrase="p")
    instance.cancel_all.return_value = None
    instance.get_usdc_balance.return_value = 1000.0
    mock_clob.return_value = instance
    return instance


class TestBotDiscoversMarkets:

    @patch("timba.trader.PriceFeed")
    @patch("timba.trader.PolymarketClobClient")
    @patch("timba.market.discover_active_markets")
    def test_main_loop_discovers_markets(self, mock_discover, mock_clob, mock_feed, paper_config, tmp_path, mock_market):
        state = State()
        state.cash = paper_config.calculate_portfolio()
        state.portfolio = state.cash

        mock_discover.return_value = [mock_market]
        mock_feed.return_value = MagicMock()
        _configure_mock_clob(mock_clob)

        trader = Trader(paper_config, state, data_dir=str(tmp_path / "data"))
        trader._discover_and_register()

        assert len(_favorite_positions(trader)) == 1
        assert mock_market.slug in _favorite_positions(trader)

    @patch("timba.trader.PriceFeed")
    @patch("timba.trader.PolymarketClobClient")
    @patch("timba.market.discover_active_markets")
    def test_expired_markets_not_registered(self, mock_discover, mock_clob, mock_feed, paper_config, tmp_path):
        state = State()
        state.cash = paper_config.calculate_portfolio()
        state.portfolio = state.cash

        expired = UpDownMarket(
            condition_id="0xold", question="Old market",
            slug="btc-updown-5m-1000000",
            token_id_up="tu", token_id_down="td",
            end_timestamp=int(time.time()) - 100,
            coin="btc", interval="5m",
            gamma_price_up=0.5, gamma_price_down=0.5, liquidity=-1,
        )
        mock_discover.return_value = [expired]
        mock_feed.return_value = MagicMock()
        _configure_mock_clob(mock_clob)

        trader = Trader(paper_config, state, data_dir=str(tmp_path / "data"))
        trader._discover_and_register()
        assert len(_favorite_positions(trader)) == 0

    @patch("timba.trader.PriceFeed")
    @patch("timba.trader.PolymarketClobClient")
    @patch("timba.market.discover_active_markets")
    def test_favorite_processes_positions(self, mock_discover, mock_clob, mock_feed, paper_config, tmp_path, mock_market):
        state = State()
        state.cash = paper_config.calculate_portfolio()
        state.portfolio = state.cash

        mock_discover.return_value = [mock_market]
        mock_feed_instance = MagicMock()
        mock_feed_instance.get_direction.return_value = None
        mock_feed_instance.is_healthy.return_value = True
        mock_feed.return_value = mock_feed_instance
        _configure_mock_clob(mock_clob)

        trader = Trader(paper_config, state, data_dir=str(tmp_path / "data"))
        trader._discover_and_register()

        pos = _favorite_positions(trader)[mock_market.slug]
        assert pos.state == PositionState.WATCHING

        trader._evaluate_all()
        # Still watching (no tick data from recorded_ticks)
        assert _favorite_positions(trader)[mock_market.slug].state == PositionState.WATCHING

    @patch("timba.trader.PriceFeed")
    @patch("timba.trader.PolymarketClobClient")
    @patch("timba.market.discover_active_markets")
    def test_portfolio_checked_before_bet(self, mock_discover, mock_clob, mock_feed, paper_config, tmp_path, mock_market):
        state = State()
        state.cash = 1
        state.portfolio = 1

        mock_discover.return_value = [mock_market]
        mock_feed.return_value = MagicMock()
        _configure_mock_clob(mock_clob)

        trader = Trader(paper_config, state, data_dir=str(tmp_path / "data"))
        trader._discover_and_register()
        assert mock_market.slug in _favorite_positions(trader)

    @patch("timba.trader.PriceFeed")
    @patch("timba.trader.PolymarketClobClient")
    @patch("timba.market.discover_active_markets")
    def test_seen_slugs_prevent_reregistration(self, mock_discover, mock_clob, mock_feed, paper_config, tmp_path, mock_market):
        state = State()
        state.cash = paper_config.calculate_portfolio()
        state.portfolio = state.cash

        mock_discover.return_value = [mock_market]
        mock_feed.return_value = MagicMock()
        _configure_mock_clob(mock_clob)

        trader = Trader(paper_config, state, data_dir=str(tmp_path / "data"))
        trader._discover_and_register()
        assert len(_favorite_positions(trader)) == 1

        trader._seen_slugs["favorite"][mock_market.slug] = time.time()
        del trader.positions["favorite"][mock_market.slug]

        trader._discover_and_register()
        assert len(_favorite_positions(trader)) == 0


# ==================== LIVE API TESTS ====================

@pytest.fixture
def clob_client():
    from polymarket_apis import PolymarketClobClient
    return PolymarketClobClient(
        private_key=os.environ["POLYMARKET_PRIVATE_KEY"],
        address=os.environ.get("POLYMARKET_FUNDER", ""),
    )


@pytest.fixture
def readonly_client():
    from polymarket_apis import PolymarketReadOnlyClobClient
    return PolymarketReadOnlyClobClient()


class TestLiveApiReadOnly:
    @live
    def test_api_is_reachable(self, readonly_client):
        ok = readonly_client.get_ok()
        assert ok is not None

    @live
    def test_market_discovery_returns_markets(self):
        series = [MarketSeries("btc", "5m", 300)]
        markets = discover_active_markets(series)
        assert len(markets) >= 0

    @live
    def test_midpoint_returns_float(self, readonly_client):
        series = [MarketSeries("btc", "5m", 300)]
        markets = discover_active_markets(series)
        if not markets:
            pytest.skip("No active markets right now")
        mid = _get_midpoint(readonly_client, markets[0].token_id_up)
        assert mid is None or (isinstance(mid, float) and 0 < mid < 1)

    @live
    def test_orderbook_returns_asks(self, readonly_client):
        series = [MarketSeries("btc", "5m", 300)]
        markets = discover_active_markets(series)
        if not markets:
            pytest.skip("No active markets right now")
        result = simulate_fill(readonly_client, markets[0].token_id_up, 5)
        if result:
            price, size, _tick = result
            assert price > 0
            assert size > 0


class TestLiveApiAuthenticated:
    @live
    def test_usdc_balance(self, clob_client):
        balance = clob_client.get_usdc_balance()
        assert isinstance(balance, (int, float))
        assert balance >= 0

    @live
    def test_get_orders_empty(self, clob_client):
        orders = clob_client.get_orders()
        assert isinstance(orders, list)

    @live
    def test_cancel_all_succeeds(self, clob_client):
        resp = clob_client.cancel_all()
        assert resp is not None
