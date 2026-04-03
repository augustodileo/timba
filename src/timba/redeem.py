"""Auto-redeem winning positions via Polymarket Relayer (gas-free)."""

import logging

from web3 import Web3

from timba.constants import CTF_ADDRESS, POLYGON_CHAIN_ID, RELAYER_URL, USDC_E_ADDRESS

logger = logging.getLogger(__name__)
PARENT_COLLECTION_ID = bytes(32)
INDEX_SETS = [1, 2]  # Process both outcome tokens

CTF_ABI = [{
    "name": "redeemPositions",
    "type": "function",
    "inputs": [
        {"name": "collateralToken", "type": "address"},
        {"name": "parentCollectionId", "type": "bytes32"},
        {"name": "conditionId", "type": "bytes32"},
        {"name": "indexSets", "type": "uint256[]"},
    ],
    "outputs": [],
}]


def _encode_redeem(condition_id: str) -> str:
    """Encode a redeemPositions call for the CTF contract."""
    w3 = Web3()
    contract = w3.eth.contract(address=CTF_ADDRESS, abi=CTF_ABI)
    data = contract.encode_abi("redeemPositions", [
        USDC_E_ADDRESS,
        PARENT_COLLECTION_ID,
        bytes.fromhex(condition_id[2:] if condition_id.startswith("0x") else condition_id),
        INDEX_SETS,
    ])
    return data


def check_needs_redeem(clob_client: object, token_id: str) -> bool:
    """Check if a token has a balance that needs redeeming."""
    try:
        balance = clob_client.get_token_balance(token_id)
        return float(balance) > 0
    except (OSError, ValueError, KeyError):
        return True  # assume yes if we can't check


def redeem_position(relay_client: object, condition_id: str) -> bool:
    """Redeem a resolved position via the Relayer (gas-free).

    Returns True if transaction submitted successfully.
    """
    from py_builder_relayer_client.models import OperationType, SafeTransaction

    try:
        data = _encode_redeem(condition_id)
        txn = SafeTransaction(
            to=CTF_ADDRESS,
            operation=OperationType.Call,
            data=data,
            value="0",
        )
        resp = relay_client.execute([txn], metadata="bot-redeem")
        logger.info("Redeem submitted for %s: tx=%s", condition_id[:10], resp.transaction_id)

        try:
            resp.wait()
            logger.info("Redeem confirmed for %s", condition_id[:10])
        except (OSError, TimeoutError, ValueError) as e:
            logger.warning("Redeem submitted but confirmation failed for %s: %s", condition_id[:10], e)

        return True
    except (OSError, TimeoutError, ValueError):
        logger.exception("Failed to redeem %s", condition_id[:10])
        return False


def create_relay_client(private_key: str, relayer_api_key: str, relayer_api_key_address: str) -> object:
    """Create a RelayClient using Relayer API key auth."""
    from py_builder_relayer_client.client import BuilderConfig, RelayClient
    from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds

    dummy_creds = BuilderApiKeyCreds(key="unused", secret="unused", passphrase="unused")
    relay = RelayClient(
        relayer_url=RELAYER_URL,
        chain_id=POLYGON_CHAIN_ID,
        private_key=private_key,
        builder_config=BuilderConfig(local_builder_creds=dummy_creds),
    )

    api_key = relayer_api_key
    api_key_address = relayer_api_key_address

    def _relayer_headers(method: str, request_path: str, body: str | None = None) -> dict[str, str]:
        return {
            "RELAYER_API_KEY": api_key,
            "RELAYER_API_KEY_ADDRESS": api_key_address,
        }

    relay._generate_builder_headers = _relayer_headers
    return relay
