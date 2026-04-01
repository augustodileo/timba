"""Tests for CLOB helpers: midpoint, orderbook simulation, retry."""

from unittest.mock import MagicMock

import pytest
import requests

from timba.clob_helpers import (
    _cancel,
    _get_midpoint,
    retry_api,
    simulate_fill,
)


class TestGetMidpoint:
    def test_returns_float(self):
        clob = MagicMock()
        mid_obj = MagicMock()
        mid_obj.value = 0.55
        clob.get_midpoint.return_value = mid_obj
        result = _get_midpoint(clob, "token123")
        assert result == pytest.approx(0.55)

    def test_returns_none_on_error(self):
        clob = MagicMock()
        clob.get_midpoint.side_effect = requests.ConnectionError("err")
        assert _get_midpoint(clob, "token123") is None


class TestRetryApi:
    def test_succeeds_first_try(self):
        fn = MagicMock(return_value=42)
        assert retry_api(fn, retries=2) == 42
        assert fn.call_count == 1

    def test_retries_on_failure(self):
        fn = MagicMock(side_effect=[requests.ConnectionError("err"), requests.Timeout("err"), 42])
        assert retry_api(fn, retries=2, backoff=0.01) == 42
        assert fn.call_count == 3

    def test_raises_after_all_retries(self):
        fn = MagicMock(side_effect=requests.ConnectionError("permanent"))
        with pytest.raises(requests.ConnectionError, match="permanent"):
            retry_api(fn, retries=1, backoff=0.01)
        assert fn.call_count == 2


class TestSimulateFill:
    def test_walks_through_asks(self):
        clob = MagicMock()
        ask1 = MagicMock(price=0.93, size=80)
        ask2 = MagicMock(price=0.95, size=50)
        ask3 = MagicMock(price=0.97, size=200)
        book = MagicMock(asks=[ask1, ask2, ask3])
        clob.get_order_book.return_value = book

        result = simulate_fill(clob, "token", 200)
        assert result is not None
        avg_price, filled, tick = result
        assert filled == 200
        assert avg_price == pytest.approx(189.8 / 200, abs=0.001)

    def test_partial_fill(self):
        clob = MagicMock()
        ask1 = MagicMock(price=0.92, size=50)
        book = MagicMock(asks=[ask1])
        clob.get_order_book.return_value = book

        result = simulate_fill(clob, "token", 200)
        avg_price, filled, tick = result
        assert filled == 50
        assert avg_price == pytest.approx(0.92)

    def test_empty_book_returns_none(self):
        clob = MagicMock()
        book = MagicMock(asks=[])
        clob.get_order_book.return_value = book
        assert simulate_fill(clob, "token", 200) is None

    def test_api_error_returns_none(self):
        clob = MagicMock()
        clob.get_order_book.side_effect = requests.Timeout("timeout")
        assert simulate_fill(clob, "token", 200) is None


class TestCancel:
    def test_cancel_paper_order_skipped(self):
        clob = MagicMock()
        _cancel(clob, "paper_up_test")
        clob.cancel.assert_not_called()

    def test_cancel_none_skipped(self):
        clob = MagicMock()
        _cancel(clob, None)
        clob.cancel.assert_not_called()

    def test_cancel_real_order(self):
        clob = MagicMock()
        _cancel(clob, "0xorder123")
        clob.cancel.assert_called_once_with("0xorder123")

    def test_cancel_error_ignored(self):
        clob = MagicMock()
        clob.cancel.side_effect = requests.ConnectionError("err")
        _cancel(clob, "0xorder123")  # should not raise
