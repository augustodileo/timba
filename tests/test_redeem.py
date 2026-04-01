"""Tests for auto-redemption via Polymarket Relayer."""

from unittest.mock import MagicMock

from timba.redeem import _encode_redeem, check_needs_redeem, create_relay_client, redeem_position


class TestEncodeRedeem:
    def test_encodes_valid_condition_id(self):
        data = _encode_redeem("0x0000000000000000000000000000000000000000000000000000000000000001")
        assert data.startswith("0x01b7037c")  # redeemPositions selector
        assert len(data) > 10

    def test_encodes_without_0x_prefix(self):
        data = _encode_redeem("0000000000000000000000000000000000000000000000000000000000000002")
        assert data.startswith("0x01b7037c")


class TestCheckNeedsRedeem:
    def test_balance_positive(self):
        clob = MagicMock()
        clob.get_token_balance.return_value = 5.0
        assert check_needs_redeem(clob, "tok") is True

    def test_balance_zero(self):
        clob = MagicMock()
        clob.get_token_balance.return_value = 0.0
        assert check_needs_redeem(clob, "tok") is False

    def test_api_error_assumes_yes(self):
        clob = MagicMock()
        clob.get_token_balance.side_effect = OSError("err")
        assert check_needs_redeem(clob, "tok") is True


class TestRedeemPosition:
    def test_success(self):
        relay = MagicMock()
        resp = MagicMock()
        resp.transaction_id = "tx-123"
        resp.wait.return_value = MagicMock()
        relay.execute.return_value = resp

        result = redeem_position(relay, "0xabc123" + "0" * 58)
        assert result is True
        relay.execute.assert_called_once()

    def test_failure_returns_false(self):
        relay = MagicMock()
        relay.execute.side_effect = OSError("401 unauthorized")

        result = redeem_position(relay, "0xabc123" + "0" * 58)
        assert result is False

    def test_wait_failure_still_returns_true(self):
        relay = MagicMock()
        resp = MagicMock()
        resp.transaction_id = "tx-123"
        resp.wait.side_effect = TimeoutError("timeout")
        relay.execute.return_value = resp

        result = redeem_position(relay, "0xabc123" + "0" * 58)
        assert result is True


class TestCreateRelayClient:
    def test_overrides_headers(self):
        client = create_relay_client(
            "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80",  # hardhat test key
            "my-key", "0xmy-addr",
        )
        headers = client._generate_builder_headers("POST", "/submit")
        assert headers["RELAYER_API_KEY"] == "my-key"
        assert headers["RELAYER_API_KEY_ADDRESS"] == "0xmy-addr"
