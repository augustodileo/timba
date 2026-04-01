import time
from unittest.mock import MagicMock, patch

from timba.market import (
    COIN_FULL_NAME,
    MarketSeries,
    UpDownMarket,
    _fetch_market_by_slug,
    _generate_hourly_slugs,
    _parse_hourly_slug_timestamp,
    _parse_updown_market,
    discover_active_markets,
)


class TestParseUpdownMarket:
    def _series(self):
        return MarketSeries("btc", "15m", 900)

    def test_valid_up_down(self):
        raw = {
            "conditionId": "0xabc",
            "question": "Bitcoin Up or Down - March 23, 7:00PM-7:15PM ET",
            "slug": "btc-updown-15m-1774400000",
            "outcomes": '["Up", "Down"]',
            "clobTokenIds": '["tok_up", "tok_down"]',
            "outcomePrices": '["0.55", "0.45"]',
        }
        market = _parse_updown_market(raw, self._series())
        assert market is not None
        assert market.token_id_up == "tok_up"
        assert market.token_id_down == "tok_down"
        assert market.gamma_price_up == 0.55
        assert market.gamma_price_down == 0.45
        assert market.end_timestamp == 1774400000 + 900

    def test_reversed_outcome_order(self):
        raw = {
            "conditionId": "0xdef",
            "question": "BTC Up or Down",
            "slug": "btc-updown-15m-1774400000",
            "outcomes": '["Down", "Up"]',
            "clobTokenIds": '["tok_down", "tok_up"]',
            "outcomePrices": '["0.45", "0.55"]',
        }
        market = _parse_updown_market(raw, self._series())
        assert market is not None
        assert market.token_id_up == "tok_up"
        assert market.token_id_down == "tok_down"

    def test_rejects_yes_no(self):
        raw = {
            "conditionId": "0x",
            "question": "Will BTC hit $100k?",
            "slug": "btc-above-100k",
            "outcomes": '["Yes", "No"]',
            "clobTokenIds": '["t1", "t2"]',
            "outcomePrices": '["0.5", "0.5"]',
        }
        assert _parse_updown_market(raw, self._series()) is None

    def test_rejects_three_outcomes(self):
        raw = {
            "conditionId": "0x",
            "question": "?",
            "slug": "x",
            "outcomes": '["Up", "Down", "Flat"]',
            "clobTokenIds": '["t1", "t2", "t3"]',
        }
        assert _parse_updown_market(raw, self._series()) is None

    def test_outcomes_as_list(self):
        raw = {
            "conditionId": "0x",
            "question": "ETH Up or Down",
            "slug": "eth-updown-15m-1774400000",
            "outcomes": ["Up", "Down"],
            "clobTokenIds": ["tok_up", "tok_down"],
            "outcomePrices": [0.50, 0.50],
        }
        market = _parse_updown_market(raw, MarketSeries("eth", "15m", 900))
        assert market is not None


class TestFetchMarketBySlug:
    @patch("timba.market.requests.get")
    def test_returns_none_for_closed(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value=[{"active": False, "closed": True}]),
        )
        mock_get.return_value.raise_for_status = MagicMock()
        result = _fetch_market_by_slug("slug", MarketSeries("btc", "15m", 900))
        assert result is None

    @patch("timba.market.requests.get")
    def test_returns_none_for_empty(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value=[]),
        )
        mock_get.return_value.raise_for_status = MagicMock()
        result = _fetch_market_by_slug("slug", MarketSeries("btc", "15m", 900))
        assert result is None


class TestDiscoverActiveMarkets:
    @patch("timba.market._fetch_market_by_slug")
    def test_discovers_markets(self, mock_fetch):
        market = UpDownMarket(
            condition_id="0x1",
            question="BTC Up or Down",
            slug="btc-updown-15m-123",
            token_id_up="tu",
            token_id_down="td",
            end_timestamp=int(time.time()) + 900,
            coin="btc",
            interval="15m",
            gamma_price_up=0.5,
            gamma_price_down=0.5,
        )
        # Return the market for every slug query
        mock_fetch.return_value = market

        series = [MarketSeries("btc", "15m", 900)]
        results = discover_active_markets(series, look_ahead=1)
        assert len(results) >= 1

    @patch("timba.market._fetch_market_by_slug")
    def test_handles_fetch_errors(self, mock_fetch):
        import requests as req
        mock_fetch.side_effect = req.ConnectionError("timeout")
        results = discover_active_markets([MarketSeries("btc", "15m", 900)], look_ahead=1)
        assert results == []


# ── Hourly slugs, parsing, edge cases ──


class TestHourlySlugTimestamp:
    def test_parse_5pm(self):
        ts = _parse_hourly_slug_timestamp("bitcoin-up-or-down-march-23-2026-5pm-et")
        assert ts > 0
        import datetime as dt
        d = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc)
        assert d.hour == 21
        assert d.day == 23

    def test_parse_12pm(self):
        ts = _parse_hourly_slug_timestamp("bitcoin-up-or-down-march-23-2026-12pm-et")
        assert ts > 0
        import datetime as dt
        d = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc)
        assert d.hour == 16

    def test_parse_12am(self):
        ts = _parse_hourly_slug_timestamp("bitcoin-up-or-down-march-24-2026-12am-et")
        assert ts > 0
        import datetime as dt
        d = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc)
        assert d.hour == 4

    def test_parse_invalid(self):
        assert _parse_hourly_slug_timestamp("garbage") == 0

    def test_parse_am(self):
        ts = _parse_hourly_slug_timestamp("ethereum-up-or-down-march-23-2026-9am-et")
        assert ts > 0
        import datetime as dt
        d = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc)
        assert d.hour == 13


class TestGenerateHourlySlugs:
    def test_generates_slugs(self):
        slugs = _generate_hourly_slugs("btc", int(time.time()), 2)
        assert len(slugs) >= 3
        for s in slugs:
            assert "bitcoin-up-or-down" in s
            assert s.endswith("-et")

    def test_all_coins(self):
        for coin, full_name in COIN_FULL_NAME.items():
            slugs = _generate_hourly_slugs(coin, int(time.time()), 1)
            assert any(full_name in s for s in slugs)


class TestParseUpdownMarketHourly:
    def test_hourly_market(self):
        raw = {
            "conditionId": "0xabc",
            "question": "Bitcoin Up or Down - March 23, 5PM ET",
            "slug": "bitcoin-up-or-down-march-23-2026-5pm-et",
            "outcomes": '["Up", "Down"]',
            "clobTokenIds": '["tok_up", "tok_down"]',
            "outcomePrices": '["0.55", "0.45"]',
            "active": True,
            "closed": False,
        }
        series = MarketSeries("btc", "1h", 3600)
        market = _parse_updown_market(raw, series)
        assert market is not None
        assert market.end_timestamp > 0
        assert market.coin == "btc"
        assert market.interval == "1h"

    def test_hourly_bad_slug(self):
        raw = {
            "conditionId": "0xabc",
            "question": "Bad",
            "slug": "bad-slug",
            "outcomes": '["Up", "Down"]',
            "clobTokenIds": '["t1", "t2"]',
            "outcomePrices": '["0.5", "0.5"]',
            "active": True,
            "closed": False,
        }
        series = MarketSeries("btc", "1h", 3600)
        market = _parse_updown_market(raw, series)
        assert market is not None
        assert market.end_timestamp == 0


class TestFetchMarketBySlugExtra:
    @patch("timba.market.requests.get")
    def test_http_error(self, mock_get):
        resp = MagicMock()
        resp.raise_for_status.side_effect = Exception("500")
        mock_get.return_value = resp
        try:
            result = _fetch_market_by_slug("test", MarketSeries("btc", "5m", 300))
        except Exception:
            result = None
        assert result is None

    @patch("timba.market.requests.get")
    def test_active_market_returned(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value=[{
                "conditionId": "0x1",
                "question": "BTC Up or Down",
                "slug": "btc-updown-5m-1000000",
                "outcomes": '["Up", "Down"]',
                "clobTokenIds": '["tu", "td"]',
                "outcomePrices": '["0.5", "0.5"]',
                "active": True,
                "closed": False,
            }]),
        )
        mock_get.return_value.raise_for_status = MagicMock()
        result = _fetch_market_by_slug("btc-updown-5m-1000000", MarketSeries("btc", "5m", 300))
        assert result is not None


class TestDiscoverWithHourly:
    @patch("timba.market._fetch_market_by_slug")
    def test_discovers_hourly(self, mock_fetch):
        market = UpDownMarket(
            condition_id="0x1", question="Bitcoin Up or Down - March 23, 5PM ET",
            slug="bitcoin-up-or-down-march-23-2026-5pm-et",
            token_id_up="tu", token_id_down="td",
            end_timestamp=int(time.time()) + 3600,
            coin="btc", interval="1h",
            gamma_price_up=0.5, gamma_price_down=0.5,
        )
        mock_fetch.return_value = market
        results = discover_active_markets([MarketSeries("btc", "1h", 3600)], look_ahead=1)
        assert len(results) >= 1
