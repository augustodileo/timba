import time

from timba.feed import COIN_TO_PAIR, DirectionSignal, PriceFeed


class TestPriceFeedOffline:
    """Test feed logic without hitting real APIs."""

    def test_coin_to_pair_mapping(self):
        assert COIN_TO_PAIR["btc"] == "BTC-USD"
        assert COIN_TO_PAIR["eth"] == "ETH-USD"
        assert COIN_TO_PAIR["sol"] == "SOL-USD"
        assert COIN_TO_PAIR["doge"] == "DOGE-USD"

    def test_get_price_none_when_no_data(self):
        f = PriceFeed(coins=["btc"], poll_interval=999)
        assert f.get_price("btc") is None

    def test_get_direction_none_when_no_data(self):
        f = PriceFeed(coins=["btc"], poll_interval=999)
        assert f.get_direction("btc", int(time.time()) - 60) is None

    def test_get_direction_with_injected_data(self):
        f = PriceFeed(coins=["btc"], poll_interval=999)
        now = time.time()
        # Inject fake price history: price went from 100 to 101 (up)
        f._prices["btc"] = 101.0
        f._history["btc"] = [
            (now - 60, 100.0),
            (now - 30, 100.5),
            (now - 10, 101.0),
        ]
        signal = f.get_direction("btc", int(now - 60))
        assert signal is not None
        assert signal.direction == "up"
        assert signal.change_pct > 0

    def test_get_direction_down(self):
        f = PriceFeed(coins=["btc"], poll_interval=999)
        now = time.time()
        f._prices["btc"] = 99.0
        f._history["btc"] = [
            (now - 60, 100.0),
            (now - 30, 99.5),
            (now - 10, 99.0),
        ]
        signal = f.get_direction("btc", int(now - 60))
        assert signal.direction == "down"
        assert signal.change_pct < 0

    def test_get_direction_flat(self):
        f = PriceFeed(coins=["btc"], poll_interval=999)
        now = time.time()
        f._prices["btc"] = 100.001
        f._history["btc"] = [
            (now - 60, 100.0),
            (now - 10, 100.001),
        ]
        signal = f.get_direction("btc", int(now - 60))
        assert signal.direction == "flat"

    def test_reversed_recently_detected(self):
        f = PriceFeed(coins=["btc"], poll_interval=999)
        now = time.time()
        f._prices["btc"] = 100.5
        # Price went below open, then above — reversal in last 30s
        f._history["btc"] = [
            (now - 60, 100.0),
            (now - 40, 101.0),
            (now - 20, 99.5),  # below open
            (now - 10, 100.5),  # back above
        ]
        signal = f.get_direction("btc", int(now - 60))
        assert signal.reversed_recently is True

    def test_confidence_higher_for_big_moves(self):
        f = PriceFeed(coins=["btc"], poll_interval=999)
        now = time.time()

        # Small move
        f._prices["btc"] = 100.05
        f._history["btc"] = [(now - 60, 100.0), (now - 10, 100.05)]
        small = f.get_direction("btc", int(now - 60))

        # Big move
        f._prices["btc"] = 100.5
        f._history["btc"] = [(now - 60, 100.0), (now - 10, 100.5)]
        big = f.get_direction("btc", int(now - 60))

        assert big.confidence > small.confidence


class TestDirectionSignal:
    def test_dataclass_fields(self):
        s = DirectionSignal(direction="up", change_pct=0.1,
                           seconds_trending=100, reversed_recently=False,
                           confidence=0.8)
        assert s.direction == "up"
        assert s.confidence == 0.8
