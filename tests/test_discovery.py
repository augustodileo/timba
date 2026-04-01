"""Tests for discovery.py — market discovery and position registration."""

import time
from unittest.mock import MagicMock, patch

from timba.config import Config
from timba.discovery import DiscoveryWorker
from timba.market import UpDownMarket
from timba.market_cache import MarketCache
from timba.strategies.favorite import FavoriteStrategy


def _make_config(tmp_path, coin="btc", interval="5m", mode="paper"):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(f"""
favorite:
  enabled: true
  min_price: 0.95
  min_signal_chg: 0.05
  contracts_per_trade: 10
  markets:
    - coin: {coin}
      interval: {interval}
      mode: "{mode}"
      entry_window_sec: 10
      close_window_sec: 3
""")
    return Config.load(cfg_file)


def _make_market(coin="btc", interval="5m", remaining=30):
    end_ts = int(time.time()) + remaining
    slug = f"{coin}-updown-{interval}-{end_ts}"
    return UpDownMarket(
        condition_id="0xtest",
        question=f"{coin} Up or Down",
        slug=slug,
        token_id_up="tok_up",
        token_id_down="tok_down",
        end_timestamp=end_ts,
        coin=coin,
        interval=interval,
        gamma_price_up=0.6,
        gamma_price_down=0.4,
        liquidity=500,
    )


def _make_worker(config, data_dir="/tmp/test"):
    strat = FavoriteStrategy()
    scfg = config.get_strategy("favorite")
    gcfg_raw = {k: scfg.get(k) for k in ["min_price", "min_signal_chg", "contracts_per_trade", "resolve_delay_sec"] if scfg.get(k) is not None}
    markets_list = scfg.markets

    positions = {"favorite": {}}
    seen_slugs = {"favorite": {}}
    strategies = {"favorite": strat}
    strategy_configs = {"favorite": (gcfg_raw, markets_list)}
    market_cache = MagicMock(spec=MarketCache)

    worker = DiscoveryWorker(
        config=config,
        positions=positions,
        seen_slugs=seen_slugs,
        strategies=strategies,
        strategy_configs=strategy_configs,
        market_cache=market_cache,
        data_dir=data_dir,
    )
    return worker, positions, seen_slugs, market_cache


class TestDiscoverAndRegister:

    @patch("timba.discovery.market_mod.discover_active_markets")
    def test_registers_new_market(self, mock_discover, tmp_path):
        config = _make_config(tmp_path)
        worker, positions, _, cache = _make_worker(config)
        mkt = _make_market(remaining=30)
        mock_discover.return_value = [mkt]

        worker.discover_and_register()

        assert mkt.slug in positions["favorite"]
        cache.track.assert_called_once_with(mkt.slug, mkt.token_id_up, mkt.token_id_down)

    @patch("timba.discovery.market_mod.discover_active_markets")
    def test_skips_expired_market(self, mock_discover, tmp_path):
        config = _make_config(tmp_path)
        worker, positions, _, _ = _make_worker(config)
        mkt = _make_market(remaining=1)  # < 3s remaining
        mock_discover.return_value = [mkt]

        worker.discover_and_register()

        assert len(positions["favorite"]) == 0

    @patch("timba.discovery.market_mod.discover_active_markets")
    def test_skips_already_registered(self, mock_discover, tmp_path):
        config = _make_config(tmp_path)
        worker, positions, _, _ = _make_worker(config)
        mkt = _make_market(remaining=30)
        positions["favorite"][mkt.slug] = MagicMock()
        mock_discover.return_value = [mkt]

        worker.discover_and_register()

        # Still just the original mock, no new position created
        assert isinstance(positions["favorite"][mkt.slug], MagicMock)

    @patch("timba.discovery.market_mod.discover_active_markets")
    def test_skips_seen_slugs(self, mock_discover, tmp_path):
        config = _make_config(tmp_path)
        worker, positions, seen, _ = _make_worker(config)
        mkt = _make_market(remaining=30)
        seen["favorite"][mkt.slug] = time.time()
        mock_discover.return_value = [mkt]

        worker.discover_and_register()

        assert mkt.slug not in positions["favorite"]

    @patch("timba.discovery.market_mod.discover_active_markets")
    def test_skips_unconfigured_coin(self, mock_discover, tmp_path):
        config = _make_config(tmp_path, coin="btc")
        worker, positions, _, _ = _make_worker(config)
        mkt = _make_market(coin="eth", remaining=30)  # config has btc, not eth
        mock_discover.return_value = [mkt]

        worker.discover_and_register()

        assert len(positions["favorite"]) == 0

    @patch("timba.discovery.market_mod.discover_active_markets")
    def test_skips_mode_off(self, mock_discover, tmp_path):
        config = _make_config(tmp_path, mode="off")
        worker, positions, _, _ = _make_worker(config)
        mkt = _make_market(remaining=30)
        mock_discover.return_value = [mkt]

        worker.discover_and_register()

        assert len(positions["favorite"]) == 0

    @patch("timba.discovery.market_mod.discover_active_markets")
    def test_skips_market_not_yet_started(self, mock_discover, tmp_path):
        config = _make_config(tmp_path)
        worker, positions, _, _ = _make_worker(config)
        # Market ends in 600s but interval is 300s, so it started 300s ago — OK
        # But if remaining > interval_sec, it hasn't started yet
        mkt = _make_market(remaining=600)  # 5m interval = 300s, remaining 600 > 300
        mock_discover.return_value = [mkt]

        worker.discover_and_register()

        assert len(positions["favorite"]) == 0

    @patch("timba.discovery.market_mod.discover_active_markets")
    def test_mid_window_sets_skip_flag(self, mock_discover, tmp_path):
        config = _make_config(tmp_path)
        worker, positions, _, _ = _make_worker(config)
        # entry_window=10, close_window=3 — mid-window means remaining between 3 and 10
        mkt = _make_market(remaining=7)
        mock_discover.return_value = [mkt]

        worker.discover_and_register()

        pos = positions["favorite"][mkt.slug]
        assert pos._skip_first_window is True

    @patch("timba.discovery.market_mod.discover_active_markets")
    def test_early_window_no_skip_flag(self, mock_discover, tmp_path):
        config = _make_config(tmp_path)
        worker, positions, _, _ = _make_worker(config)
        # remaining=30 > entry_window=10 → not in entry window yet
        mkt = _make_market(remaining=30)
        mock_discover.return_value = [mkt]

        worker.discover_and_register()

        pos = positions["favorite"][mkt.slug]
        assert pos._skip_first_window is False


class TestBuildSeriesList:

    def test_builds_from_config(self, tmp_path):
        config = _make_config(tmp_path, coin="btc", interval="5m")
        worker, _, _, _ = _make_worker(config)

        series = worker._build_series_list()

        assert len(series) == 1
        assert series[0].coin == "btc"
        assert series[0].interval == "5m"
        assert series[0].interval_sec == 300


class TestRunLoop:

    @patch("timba.discovery.market_mod.discover_active_markets")
    @patch("timba.discovery.time.sleep")
    def test_run_loop_calls_discover(self, mock_sleep, mock_discover, tmp_path):
        config = _make_config(tmp_path)
        worker, _, _, _ = _make_worker(config)
        mock_discover.return_value = []

        call_count = 0

        def stop_after_one():
            nonlocal call_count
            call_count += 1
            return call_count <= 1

        worker.run_loop(stop_after_one)

        mock_discover.assert_called_once()

    @patch("timba.discovery.market_mod.discover_active_markets")
    @patch("timba.discovery.time.sleep")
    def test_run_loop_survives_error(self, mock_sleep, mock_discover, tmp_path):
        config = _make_config(tmp_path)
        worker, _, _, _ = _make_worker(config)
        mock_discover.side_effect = [Exception("API down"), []]

        # run_loop checks is_running() on every sleep(1) in the interval loop,
        # so we stop after discover has been called twice
        def stop_when_done():
            return mock_discover.call_count < 2

        worker.run_loop(stop_when_done)

        assert mock_discover.call_count == 2
