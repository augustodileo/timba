import time
from unittest.mock import MagicMock, patch

import pytest

from timba import db
from timba.config import Config
from timba.market import UpDownMarket
from timba.state import State
from timba.trader import Trader


@pytest.fixture(autouse=True)
def _reset_db():
    """Reset db module state after each test to prevent cross-test pollution."""
    yield
    db.reset()


@pytest.fixture
def tmp_data_dir(tmp_path):
    return tmp_path / "data"


@pytest.fixture
def sample_config_yaml(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("""
favorite:
  enabled: true
  min_price: 0.95
  min_signal_chg: 0.05
  contracts_per_trade: 10
  markets:
    - coin: btc
      interval: 15m
      entry_window_sec: 60
      close_window_sec: 4
    - coin: eth
      interval: 5m
      entry_window_sec: 10
      close_window_sec: 3
""")
    return cfg


def _mock_trader(config, state, data_dir="data"):
    """Create a Trader with mocked CLOB client."""
    with patch("timba.trader.PolymarketClobClient") as mock_clob, \
         patch("timba.trader.create_relay_client"):
        mock_instance = MagicMock()
        mock_instance.create_or_derive_api_creds.return_value = MagicMock(key="k", secret="s", passphrase="p")
        mock_instance.cancel_all.return_value = None
        mock_instance.get_usdc_balance.return_value = 1000.0
        mock_clob.return_value = mock_instance
        return Trader(config, state, data_dir=data_dir)


@pytest.fixture
def trader_setup(tmp_path):
    """Create a Trader with mocked CLOB/feed for testing individual methods."""
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
    data_dir = tmp_path / "data"
    db.init(data_dir)
    state = State()
    state.init_portfolio(config.calculate_portfolio())

    with patch("timba.trader.PriceFeed") as mock_feed:
        with patch("timba.trader.PolymarketClobClient") as mock_clob:
            mock_feed_instance = MagicMock()
            mock_feed_instance.get_direction.return_value = None
            mock_feed_instance.is_healthy.return_value = True
            mock_feed.return_value = mock_feed_instance
            mock_clob_instance = MagicMock()
            mock_clob_instance.create_or_derive_api_creds.return_value = MagicMock()
            mock_clob_instance.cancel_all.return_value = None
            mock_clob_instance.get_usdc_balance.return_value = 1000.0
            mock_clob.return_value = mock_clob_instance
            trader = Trader(config, state, data_dir=str(data_dir))
            yield trader, state


@pytest.fixture
def sample_updown_market():
    return UpDownMarket(
        condition_id="0xabc123",
        question="Bitcoin Up or Down - March 23, 7:00PM-7:15PM ET",
        slug="btc-updown-15m-1774400000",
        token_id_up="token_up_123",
        token_id_down="token_down_456",
        end_timestamp=int(time.time()) + 600,
        coin="btc",
        interval="15m",
        gamma_price_up=0.55,
        gamma_price_down=0.45,
        liquidity=-1,
    )
