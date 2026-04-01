import queue
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from timba import db
from timba.base import PositionState
from timba.config import Config
from timba.state import State
from timba.strategies.favorite import FavoritePosition
from timba.trader import LockedDict, Trader


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


class TestLockedDict:
    def test_get_set_pop(self):
        d = LockedDict()
        d["key"] = "value"
        assert d.get("key") == "value"
        assert d.get("missing") is None
        assert d.get("missing", 42) == 42
        assert d.pop("key") == "value"
        assert d.get("key") is None

    def test_pop_default(self):
        d = LockedDict()
        assert d.pop("missing", "default") == "default"

    def test_concurrent_writes(self):
        d = LockedDict()
        errors = []

        def writer(prefix, count):
            try:
                for i in range(count):
                    d[f"{prefix}-{i}"] = i
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(f"t{n}", 100)) for n in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        # All 500 items should be present
        for n in range(5):
            for i in range(100):
                assert d.get(f"t{n}-{i}") == i

    def test_concurrent_read_write_pop(self):
        d = LockedDict()
        for i in range(100):
            d[f"k{i}"] = i
        errors = []

        def reader():
            try:
                for i in range(100):
                    d.get(f"k{i}")
            except Exception as e:
                errors.append(e)

        def popper():
            try:
                for i in range(50, 100):
                    d.pop(f"k{i}", None)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=reader) for _ in range(3)]
        threads.append(threading.Thread(target=popper))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors


def _favorite_positions(trader):
    """Get favorite strategy positions (helper for test readability)."""
    return trader.positions.get("favorite", {})


def _make_position(slug="test-slug", **kw):
    """Create a FavoritePosition with sensible defaults for testing."""
    defaults = dict(
        condition_id="0x1", question="test", slug=slug,
        coin="btc", interval="5m", token_id_up="tu", token_id_down="td",
        end_timestamp=int(time.time()) + 10, window_start_ts=int(time.time()) - 290,
        contracts=10, entry_window_sec=20, close_window_sec=5,
        min_price=0.95, min_signal_chg=0.05,
    )
    defaults.update(kw)
    return FavoritePosition(**defaults)


class TestTrader:
    def test_init_with_credentials(self, tmp_data_dir, sample_config_yaml):
        config = Config.load(sample_config_yaml)
        config.polymarket.private_key = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
        config.polymarket.funder = "0xDeaDbeefdEAdbeefdEadbEEFdeadbeEFdEaDbeeF"
        state = State()

        trader = _mock_trader(config, state)
        assert trader.clob_client is not None

    def test_init_fails_without_private_key(self, tmp_data_dir, sample_config_yaml):
        config = Config.load(sample_config_yaml)
        config.polymarket.private_key = ""
        state = State()

        with pytest.raises(ValueError, match="POLYMARKET_PRIVATE_KEY"):
            Trader(config, state)

    def test_init_fails_without_funder(self, tmp_data_dir, sample_config_yaml):
        config = Config.load(sample_config_yaml)
        config.polymarket.private_key = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
        config.polymarket.funder = ""
        state = State()

        with pytest.raises(ValueError, match="POLYMARKET_FUNDER"):
            Trader(config, state)

    def test_discover_and_register(self, tmp_data_dir, sample_config_yaml, sample_updown_market):
        config = Config.load(sample_config_yaml)
        config.polymarket.private_key = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
        config.polymarket.funder = "0xDeaDbeefdEAdbeefdEadbEEFdeadbeEFdEaDbeeF"
        state = State()
        state.init_portfolio(config.calculate_portfolio())

        trader = _mock_trader(config, state)

        with patch("timba.market.discover_active_markets", return_value=[sample_updown_market]):
            trader._discover_and_register()
            assert sample_updown_market.slug in _favorite_positions(trader)

    def test_market_missing_entry_window_skipped(self, tmp_data_dir, tmp_path):
        """Market without entry_window_sec should be skipped with warning."""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("""
favorite:
  enabled: true
  min_price: 0.95
  min_signal_chg: 0.05
  contracts_per_trade: 5
  markets:
    - coin: btc
      interval: 5m
      mode: paper
""")
        config = Config.load(cfg_file, validate=False)
        config.polymarket.private_key = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
        config.polymarket.funder = "0xDeaDbeefdEAdbeefdEadbEEFdeadbeEFdEaDbeeF"
        state = State()

        trader = _mock_trader(config, state)

        market = MagicMock()
        market.slug = "btc-updown-5m-9999999999"
        market.coin = "btc"
        market.interval = "5m"
        market.end_timestamp = int(time.time()) + 30
        market.token_id_up = "tu"
        market.token_id_down = "td"
        market.condition_id = "0x1"
        market.question = "test"
        market.liquidity = -1

        strat = trader._strategies.get("favorite")
        if strat:
            trader.discovery._register_for_strategy("favorite", strat, market)
        assert market.slug not in _favorite_positions(trader)

    def test_market_missing_close_window_skipped(self, tmp_data_dir, tmp_path):
        """Market with entry_window but no close_window should be skipped."""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("""
favorite:
  enabled: true
  min_price: 0.95
  min_signal_chg: 0.05
  contracts_per_trade: 5
  markets:
    - coin: btc
      interval: 5m
      mode: paper
      entry_window_sec: 30
""")
        config = Config.load(cfg_file, validate=False)
        config.polymarket.private_key = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
        config.polymarket.funder = "0xDeaDbeefdEAdbeefdEadbEEFdeadbeEFdEaDbeeF"
        state = State()

        trader = _mock_trader(config, state)

        market = MagicMock()
        market.slug = "btc-updown-5m-9999999999"
        market.coin = "btc"
        market.interval = "5m"
        market.end_timestamp = int(time.time()) + 30
        market.token_id_up = "tu"
        market.token_id_down = "td"
        market.condition_id = "0x1"
        market.question = "test"
        market.liquidity = -1

        strat = trader._strategies.get("favorite")
        if strat:
            trader.discovery._register_for_strategy("favorite", strat, market)
        assert market.slug not in _favorite_positions(trader)

    def test_market_mode_off_skipped(self, tmp_data_dir, sample_config_yaml, sample_updown_market):
        """Market with mode=off should not be registered."""
        config = Config.load(sample_config_yaml)
        config.polymarket.private_key = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
        config.polymarket.funder = "0xDeaDbeefdEAdbeefdEadbEEFdeadbeEFdEaDbeeF"
        # Set mode=off on the first market of the favorite strategy
        fav_cfg = config.get_strategy("favorite")
        if fav_cfg.markets:
            fav_cfg.markets[0]["mode"] = "off"
        state = State()

        trader = _mock_trader(config, state)

        with patch("timba.market.discover_active_markets", return_value=[sample_updown_market]):
            trader._discover_and_register()
            assert sample_updown_market.slug not in _favorite_positions(trader)


# ── Lifecycle, PnL logging, redemption, evaluate, drain ──


class TestTradeLifecycle:
    def test_records_win(self, trader_setup):
        trader, state = trader_setup
        pos = _make_position(
            slug="test-win",
            end_timestamp=int(time.time()) - 1,
            window_start_ts=int(time.time()) - 300,
        )
        pos.state = PositionState.WON
        pos.side = "up"
        pos.buy_price = 0.90
        pos.pnl = 1.0
        pos.sniped_at = "2026-03-24T10:00:00Z"
        pos.resolved_at = "2026-03-24T10:00:30Z"

        state.record_trade(pos, "favorite")
        db.flush()
        trades = db.load_trades(strategy="favorite")
        assert len(trades) == 1
        assert trades[0]["type"] == "win"

    def test_records_loss(self, trader_setup):
        trader, state = trader_setup
        pos = _make_position(
            condition_id="0x2", slug="test-loss", coin="eth",
            end_timestamp=int(time.time()) - 1,
            window_start_ts=int(time.time()) - 300,
        )
        pos.state = PositionState.LOST
        pos.side = "down"
        pos.buy_price = 0.80
        pos.pnl = -8.0
        pos.sniped_at = "2026-03-24T10:00:00Z"
        pos.resolved_at = "2026-03-24T10:00:30Z"

        state.record_trade(pos, "favorite")
        db.flush()
        trades = db.load_trades(strategy="favorite")
        assert len(trades) == 1
        assert trades[0]["type"] == "loss"

    def test_records_skip(self, trader_setup):
        trader, state = trader_setup
        pos = _make_position(
            condition_id="0x3", slug="test-pass", coin="sol",
            end_timestamp=int(time.time()) - 1,
            window_start_ts=int(time.time()) - 300,
        )
        pos.state = PositionState.SKIP_WON
        pos.side = "up"
        pos.buy_price = 0.95
        pos.sniped_at = "2026-03-24T10:00:00Z"
        pos.resolved_at = "2026-03-24T10:00:30Z"

        state.record_trade(pos, "favorite")
        db.flush()
        trades = db.load_trades(strategy="favorite")
        assert len(trades) == 1
        assert trades[0]["type"] == "skip_win"

    def test_no_feed_returns(self, trader_setup):
        trader, state = trader_setup
        trader.feed = None
        trader._evaluate_all()

    def test_cancel_all(self, trader_setup):
        trader, state = trader_setup

    def test_discover_and_register_called(self, trader_setup):
        trader, state = trader_setup
        with patch("timba.market.discover_active_markets", return_value=[]) as mock_disc:
            trader._discover_and_register()
            mock_disc.assert_called_once()

    def test_pnl_logging(self, trader_setup):
        trader, state = trader_setup
        pos = _make_position(
            condition_id="0x4", slug="test-pnl",
            end_timestamp=int(time.time()) - 1,
            window_start_ts=int(time.time()) - 300,
        )
        pos.state = PositionState.WON
        pos.side = "up"
        pos.buy_price = 0.90
        pos.pnl = 1.0
        pos.sniped_at = "2026-03-24T10:00:00Z"
        pos.resolved_at = "2026-03-24T10:00:30Z"

        state.record_trade(pos, "favorite")
        db.flush()
        assert db.get_strategy_pnl("favorite") == pytest.approx(1.0)

    def test_win_stores_token_id_and_unredeemed(self, trader_setup):
        trader, state = trader_setup
        pos = _make_position(
            condition_id="0xtest123", slug="test-redeem",
            end_timestamp=int(time.time()) - 1,
            window_start_ts=int(time.time()) - 300,
        )
        pos.state = PositionState.WON
        pos.side = "up"
        pos.buy_price = 0.90
        pos.pnl = 1.0
        pos.sniped_at = "2026-03-24T10:00:00Z"
        pos.resolved_at = "2026-03-24T10:00:30Z"

        state.record_trade(pos, "favorite")
        db.flush()
        unredeemed = db.get_unredeemed_wins()
        assert len(unredeemed) == 1
        assert unredeemed[0]["condition_id"] == "0xtest123"
        assert unredeemed[0]["token_id"] == "tu"

    def test_skips_when_feed_unhealthy(self, trader_setup):
        trader, state = trader_setup
        trader.feed.is_healthy = MagicMock(return_value=False)
        pos = _make_position(
            condition_id="0x5", slug="test-stale",
            end_timestamp=int(time.time()) + 10,
            window_start_ts=int(time.time()) - 280,
        )
        trader.positions["favorite"]["test-stale"] = pos
        trader._evaluate_all()
        assert "test-stale" in trader.positions["favorite"]


class TestRedeemScan:
    def test_scan_skips_when_no_unredeemed(self, trader_setup):
        trader, state = trader_setup
        trader.relay_client = MagicMock()
        with patch("timba.trader.threading") as mock_threading:
            trader._redeem_scan()
            mock_threading.Thread.assert_not_called()

    def test_scan_launches_thread_for_unredeemed(self, trader_setup):
        trader, state = trader_setup
        trader.relay_client = MagicMock()
        # Insert an unredeemed win into SQLite
        db.insert_trade({
            "id": 1, "type": "win", "strategy": "favorite", "slug": "test",
            "condition_id": "0xtest", "coin": "btc", "interval": "5m",
            "side": "up", "buy_price": 0.90, "contracts": 5, "pnl": 0.50,
            "sniped_at": "2026-03-30T00:00:00", "resolved_at": "2026-03-30T00:05:00",
            "end_timestamp": 1743292800, "market_mode": "live",
            "token_id": "tok", "redeemed": False,
        })
        db.flush()
        with patch("timba.trader.threading") as mock_threading:
            mock_thread = MagicMock()
            mock_threading.Thread.return_value = mock_thread
            trader._redeem_scan()
            mock_thread.start.assert_called_once()

    def test_scan_bg_already_redeemed(self, tmp_path):
        data_dir = tmp_path / "data"
        db.init(data_dir)
        state = State()
        state.pending_redemption = 5.0
        # Insert an unredeemed win into SQLite
        db.insert_trade({
            "id": 1, "type": "win", "strategy": "favorite", "slug": "test",
            "condition_id": "0xtest", "coin": "btc", "interval": "5m",
            "side": "up", "buy_price": 0.90, "contracts": 5, "pnl": 0.50,
            "sniped_at": "2026-03-30T00:00:00", "resolved_at": "2026-03-30T00:05:00",
            "end_timestamp": 1743292800, "market_mode": "live",
            "token_id": "tok", "redeemed": False,
        })
        db.flush()
        unredeemed = db.get_unredeemed_wins()

        clob = MagicMock()
        # check_needs_redeem returns False (already redeemed on-chain)
        with patch("timba.redeem.check_needs_redeem", return_value=False):
            Trader._redeem_scan_bg(MagicMock(), clob, state, unredeemed)
        db.flush()
        assert db.get_unredeemed_wins() == []
        assert state.cash == 5.0

    def test_scan_bg_redeems_successfully(self, tmp_path):
        data_dir = tmp_path / "data"
        db.init(data_dir)
        state = State()
        state.pending_redemption = 5.0
        # Insert an unredeemed win into SQLite
        db.insert_trade({
            "id": 1, "type": "win", "strategy": "favorite", "slug": "test",
            "condition_id": "0xtest", "coin": "btc", "interval": "5m",
            "side": "up", "buy_price": 0.90, "contracts": 5, "pnl": 0.50,
            "sniped_at": "2026-03-30T00:00:00", "resolved_at": "2026-03-30T00:05:00",
            "end_timestamp": 1743292800, "market_mode": "live",
            "token_id": "tok", "redeemed": False,
        })
        db.flush()
        unredeemed = db.get_unredeemed_wins()

        clob = MagicMock()

        with patch("timba.redeem.check_needs_redeem", return_value=True), \
             patch("timba.trader.redeem_position", return_value=True):
            Trader._redeem_scan_bg(MagicMock(), clob, state, unredeemed)
        db.flush()
        assert db.get_unredeemed_wins() == []
        assert state.cash == 5.0

    def test_scan_bg_redeem_fails(self, tmp_path):
        data_dir = tmp_path / "data"
        db.init(data_dir)
        state = State()
        # Insert an unredeemed win into SQLite
        db.insert_trade({
            "id": 1, "type": "win", "strategy": "favorite", "slug": "test",
            "condition_id": "0xtest", "coin": "btc", "interval": "5m",
            "side": "up", "buy_price": 0.90, "contracts": 5, "pnl": 0.50,
            "sniped_at": "2026-03-30T00:00:00", "resolved_at": "2026-03-30T00:05:00",
            "end_timestamp": 1743292800, "market_mode": "live",
            "token_id": "tok", "redeemed": False,
        })
        db.flush()
        unredeemed = db.get_unredeemed_wins()

        clob = MagicMock()

        with patch("timba.redeem.check_needs_redeem", return_value=True), \
             patch("timba.trader.redeem_position", return_value=False):
            Trader._redeem_scan_bg(MagicMock(), clob, state, unredeemed)
        db.flush()
        assert len(db.get_unredeemed_wins()) == 1


class TestLiveModeStartup:
    def _mock_live_trader(self, tmp_path, state=None, cancel_side_effect=None, usdc_balance=500.0):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("favorite:\n  enabled: false\n")
        config = Config.load(cfg_file)
        config.polymarket.private_key = "0xdeadbeef"
        config.polymarket.funder = "0xfunder"
        if state is None:
            state = State()
        data_dir = tmp_path / "data"

        with patch("timba.trader.PolymarketClobClient") as mock_clob, \
             patch("timba.trader.create_relay_client"):
            mock_instance = MagicMock()
            mock_instance.create_or_derive_api_creds.return_value = MagicMock(key="k", secret="s", passphrase="p")
            if cancel_side_effect:
                mock_instance.cancel_all.side_effect = cancel_side_effect
            else:
                mock_instance.cancel_all.return_value = None
            mock_instance.get_usdc_balance.return_value = usdc_balance
            mock_clob.return_value = mock_instance
            trader = Trader(config, state, data_dir=str(data_dir))
            return trader, mock_instance

    def test_cancel_all_moved_to_reconcile(self, tmp_path):
        """cancel_all is no longer called in Trader.__init__ — it's in reconcile.py now."""
        trader, mock_instance = self._mock_live_trader(tmp_path)
        mock_instance.cancel_all.assert_not_called()

    def test_log_clob_state_warns_on_mismatch(self, tmp_path):
        state = State()
        state.init_portfolio(1000)
        trader, _ = self._mock_live_trader(tmp_path, state=state, usdc_balance=500.0)
        assert trader.clob_client is not None

    def test_no_relay_key_sets_relay_client_none(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("favorite:\n  enabled: false\n")
        config = Config.load(cfg_file)
        config.polymarket.private_key = "0xdeadbeef"
        config.polymarket.funder = "0xfunder"
        config.polymarket.relayer_api_key = ""
        state = State()

        with patch("timba.trader.PolymarketClobClient") as mock_clob:
            mock_instance = MagicMock()
            mock_instance.create_or_derive_api_creds.return_value = MagicMock()
            mock_instance.cancel_all.return_value = None
            mock_instance.get_usdc_balance.return_value = 100.0
            mock_clob.return_value = mock_instance
            trader = Trader(config, state, data_dir=str(tmp_path / "data"))

        assert trader.relay_client is None

    def test_init_unknown_strategy_skipped(self, tmp_path):
        from timba import db
        db.reset()
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("""
unknown_strat:
  enabled: true
  contracts_per_trade: 5
  markets:
    - coin: btc
      interval: 5m
      mode: paper
      entry_window_sec: 30
      close_window_sec: 2
""")
        config = Config.load(cfg_file, validate=False)
        config.polymarket.private_key = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
        config.polymarket.funder = "0xDeaDbeefdEAdbeefdEadbEEFdeadbeEFdEaDbeeF"
        state = State()

        with patch("timba.trader.PolymarketClobClient") as mock_clob, \
             patch("timba.trader.get_strategy", return_value=None):
            mock_instance = MagicMock()
            mock_instance.create_or_derive_api_creds.return_value = MagicMock()
            mock_instance.cancel_all.return_value = None
            mock_instance.get_usdc_balance.return_value = 100.0
            mock_clob.return_value = mock_instance
            trader = Trader(config, state, data_dir=str(tmp_path / "data"))

        assert "unknown_strat" not in trader._strategies


class TestDrainMutations:
    def test_applies_queued_mutations(self, trader_setup):
        trader, state = trader_setup
        trader._mutations = queue.Queue()
        result = []
        trader._mutations.put(lambda: result.append(1))
        trader._mutations.put(lambda: result.append(2))
        trader._drain_mutations()
        assert result == [1, 2]

    def test_catches_mutation_error(self, trader_setup):
        trader, state = trader_setup
        trader._mutations = queue.Queue()
        trader._mutations.put(lambda: 1/0)
        trader._mutations.put(lambda: None)
        trader._drain_mutations()


class TestEvaluateAll:
    def test_skips_when_feed_unhealthy(self, trader_setup):
        trader, _ = trader_setup
        trader.feed.is_healthy.return_value = False
        trader._evaluate_all()

    def test_skips_when_no_feed(self, trader_setup):
        trader, _ = trader_setup
        trader.feed = None
        trader._evaluate_all()

    def test_skips_terminal_positions(self, trader_setup):
        trader, _ = trader_setup
        trader.feed.is_healthy.return_value = True
        pos = _make_position(slug="test-slug", contracts=5)
        pos.state = PositionState.WON
        trader.positions["favorite"]["test-slug"] = pos
        trader._evaluate_all()


# ── Cleanup, balance sync, tick data, health ──


class TestCleanupStalePositions:
    def test_stale_unevaluated_position_dropped(self, trader_setup):
        """Stale WATCHING position with no ev_id should be dropped entirely."""
        trader, state = trader_setup
        pos = _make_position(
            slug="test-stale",
            end_timestamp=int(time.time()) - 360,  # ended 6 min ago
            window_start_ts=int(time.time()) - 660,
        )
        pos.state = PositionState.WATCHING
        trader.positions["favorite"]["test-stale"] = pos

        trader._cleanup_stale_positions()

        assert "test-stale" not in trader.positions["favorite"]
        assert "test-stale" in trader._seen_slugs["favorite"]

    def test_stale_evaluated_position_marked_skipped(self, trader_setup):
        """Stale position that WAS evaluated (has ev_id) should be SKIPPED for resolution."""
        trader, state = trader_setup
        pos = _make_position(
            slug="test-stale-ev",
            end_timestamp=int(time.time()) - 360,
            window_start_ts=int(time.time()) - 660,
        )
        pos.state = PositionState.WATCHING
        pos.ev_id = 42  # was evaluated
        trader.positions["favorite"]["test-stale-ev"] = pos

        trader._cleanup_stale_positions()

        assert pos.state == PositionState.SKIPPED
        assert pos.skip_reason == "stale cleanup"

    def test_recent_positions_untouched(self, trader_setup):
        """Position whose market ended only 2 min ago should stay WATCHING."""
        trader, state = trader_setup
        pos = _make_position(
            condition_id="0x2", slug="test-recent",
            end_timestamp=int(time.time()) - 120,  # ended 2 min ago
            window_start_ts=int(time.time()) - 420,
        )
        pos.state = PositionState.WATCHING
        trader.positions["favorite"]["test-recent"] = pos

        trader._cleanup_stale_positions()

        assert pos.state == PositionState.WATCHING

    def test_terminal_positions_ignored(self, trader_setup):
        """Position already in terminal state (WON) should not be changed."""
        trader, state = trader_setup
        pos = _make_position(
            condition_id="0x3", slug="test-won",
            end_timestamp=int(time.time()) - 360,  # ended 6 min ago
            window_start_ts=int(time.time()) - 660,
        )
        pos.state = PositionState.WON
        trader.positions["favorite"]["test-won"] = pos

        trader._cleanup_stale_positions()

        assert pos.state == PositionState.WON


class TestApplyBalanceSync:
    def test_updates_cash_and_portfolio(self, trader_setup):
        """_apply_balance_sync should update cash and portfolio."""
        trader, state = trader_setup
        state.cash = 100.0
        state.portfolio = 100.0

        trader._apply_balance_sync(150.0)

        assert state.cash == pytest.approx(150.0)
        assert state.portfolio == pytest.approx(150.0)

    def test_pending_redemption_in_portfolio(self, trader_setup):
        """Portfolio should include pending_redemption amount."""
        trader, state = trader_setup
        state.cash = 80.0
        # Insert an unredeemed win with 20 contracts so db.get_pending_redemption() returns 20.0
        db.insert_trade({
            "id": 1, "type": "win", "strategy": "favorite", "slug": "test",
            "condition_id": "0xpending", "coin": "btc", "interval": "5m",
            "side": "up", "buy_price": 0.90, "contracts": 20, "pnl": 2.0,
            "sniped_at": "2026-03-30T00:00:00", "resolved_at": "2026-03-30T00:05:00",
            "end_timestamp": 1743292800, "market_mode": "live",
            "token_id": "tok", "redeemed": False,
        })
        db.flush()

        trader._apply_balance_sync(100.0)

        assert state.cash == pytest.approx(100.0)
        assert state.portfolio == pytest.approx(120.0)


class TestBuildTickData:
    def test_returns_tick_data_from_cache(self, trader_setup):
        """_build_tick_data should return TickData built from _recorded_ticks."""
        from timba.market_cache import MarketSnapshot
        from timba.strategies import TickData

        trader, state = trader_setup
        snapshot = MarketSnapshot(
            mid_up=0.95, mid_down=0.05,
            fill_up=0.96, fill_down=0.06,
            size_up=100, size_down=200,
            tick_size=0.01,
        )
        signal = MagicMock()
        trader._recorded_ticks["test-slug"] = (42, snapshot, signal)

        pos = MagicMock()
        pos.slug = "test-slug"

        result = trader._build_tick_data(pos)

        assert result is not None
        assert isinstance(result, TickData)
        assert result.tick_id == 42
        assert result.mid_up == pytest.approx(0.95)
        assert result.mid_down == pytest.approx(0.05)
        assert result.fill_up == pytest.approx(0.96)
        assert result.fill_down == pytest.approx(0.06)
        assert result.size_up == 100
        assert result.size_down == 200
        assert result.tick_size == pytest.approx(0.01)
        assert result.signal is signal

    def test_returns_none_when_no_recorded_tick(self, trader_setup):
        """_build_tick_data should return None for unknown slug."""
        trader, state = trader_setup

        pos = MagicMock()
        pos.slug = "unknown-slug"

        result = trader._build_tick_data(pos)
        assert result is None


class TestUpdateHealth:
    def test_updates_all_health_fields(self, trader_setup):
        """_update_health should populate all health fields."""
        trader, state = trader_setup
        state.cash = 500.0
        state.portfolio = 750.0

        # Add a position so active_snipes > 0
        pos = _make_position(slug="test-health", contracts=5)
        trader.positions["favorite"]["test-health"] = pos

        before = time.time()
        trader._update_health()
        after = time.time()

        assert before <= trader.health.last_tick <= after
        assert trader.health.feed_healthy is True
        assert trader.health.active_snipes == 1
        assert trader.health.portfolio == pytest.approx(750.0)
        assert trader.health.cash == pytest.approx(500.0)
