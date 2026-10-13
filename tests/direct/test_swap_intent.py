import json
import time


CONTRACT = "contracts/swap_intent.py"
ROUTER = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
EXECUTOR = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
RECIPIENT = "0xcccccccccccccccccccccccccccccccccccccccc"
SELL_ASSET = "0x1111111111111111111111111111111111111111"
BUY_ASSET = "0x2222222222222222222222222222222222222222"
QUOTE_URL = "https://quotes.example/swap/eth-usdc-1"
ROUTE_HASH = "0x" + "ab" * 32


def address(account) -> str:
    return f"0x{account.hex()}"


def deploy(direct_deploy):
    return direct_deploy(CONTRACT, sdk_version="v0.2.16")


def quote_document(
    *,
    market="ETH/USDC",
    router=ROUTER,
    route_hash=ROUTE_HASH,
    input_amount=100,
    output_amount=2500,
    fee_bps=30,
    expires_at=None,
    quote_kind="firm",
):
    return json.dumps(
        {
            "market": market,
            "router": router,
            "route_hash": route_hash,
            "input_amount": str(input_amount),
            "output_amount": str(output_amount),
            "fee_bps": str(fee_bps),
            "expires_at": str(expires_at or int(time.time()) + 1800),
            "quote_kind": quote_kind,
        }
    )


def create_intent(contract, direct_vm, direct_alice, intent_id="swap-1"):
    direct_vm.sender = direct_alice
    contract.create_intent(
        intent_id,
        "ETH/USDC",
        SELL_ASSET,
        BUY_ASSET,
        100,
        2400,
        100,
        ROUTER,
        RECIPIENT,
        EXECUTOR,
        int(time.time()) + 3600,
        "Use the approved router and do not exceed the configured fee or slippage bounds.",
    )


def submit_quote(contract, direct_vm, direct_bob, intent_id="swap-1", url=QUOTE_URL):
    direct_vm.sender = direct_bob
    contract.submit_quote(
        intent_id,
        url,
        ROUTER,
        ROUTE_HASH,
        100,
        2500,
        30,
    )


def mock_quote(direct_vm, body=None, status=200):
    direct_vm.mock_web(
        r"https://quotes\.example/swap/eth-usdc-1",
        {"status": status, "body": body or quote_document()},
    )


def test_address_normalizer_accepts_studio_decimal_u160_values(
    direct_vm, direct_deploy, direct_alice
):
    contract = deploy(direct_deploy)
    direct_vm.sender = direct_alice
    contract.create_intent(
        "swap-decimal-addresses",
        "ETH/USDC",
        int(SELL_ASSET[2:], 16),
        str(int(BUY_ASSET[2:], 16)),
        100,
        2400,
        100,
        ROUTER,
        RECIPIENT,
        EXECUTOR,
        int(time.time()) + 3600,
        "Use the approved router and do not exceed the configured fee or slippage bounds.",
    )
    intent = contract.get_intent("swap-decimal-addresses")
    assert intent["sell_asset"] == SELL_ASSET
    assert intent["buy_asset"] == BUY_ASSET


def test_intent_binds_protocol_bounds_and_quote_version(
    direct_vm, direct_deploy, direct_alice
):
    contract = deploy(direct_deploy)
    create_intent(contract, direct_vm, direct_alice)
    intent = contract.get_intent("swap-1")

    assert intent["status"] == "awaiting_quote"
    assert intent["revision"] == 1
    assert intent["market"] == "ETH/USDC"
    assert intent["max_input"] == 100
    assert intent["min_output"] == 2400
    assert intent["max_fee_bps"] == 100
    assert intent["allowed_router"].lower() == ROUTER


def test_consensus_approved_quote_issues_one_use_executor_permit(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    contract = deploy(direct_deploy)
    create_intent(contract, direct_vm, direct_alice)
    submit_quote(contract, direct_vm, direct_bob)
    mock_quote(direct_vm)

    direct_vm.sender = direct_alice
    contract.evaluate_intent("swap-1")
    intent = contract.get_intent("swap-1")

    assert intent["status"] == "approved"
    assert intent["decision"] == "approved"
    assert intent["quoted_output"] == 2500
    assert intent["source_snapshot"].startswith(QUOTE_URL + "|200|")
    assert intent["permit_nonce"] == 1
    assert intent["consumed"] is False

    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("Caller is not the designated executor"):
        contract.consume_permit("swap-1", ROUTE_HASH)

    direct_vm.sender = bytes.fromhex(EXECUTOR[2:])
    contract.consume_permit("swap-1", ROUTE_HASH)
    assert contract.get_intent("swap-1")["status"] == "settled"

    with direct_vm.expect_revert("Intent has no active permit"):
        contract.consume_permit("swap-1", ROUTE_HASH)


def test_slippage_and_fee_bounds_reject_quote_without_permit(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    contract = deploy(direct_deploy)
    create_intent(contract, direct_vm, direct_alice)
    submit_quote(contract, direct_vm, direct_bob)
    mock_quote(direct_vm, quote_document(output_amount=2399, fee_bps=101))

    direct_vm.sender = direct_alice
    contract.evaluate_intent("swap-1")
    intent = contract.get_intent("swap-1")

    assert intent["status"] == "rejected"
    assert intent["decision"] == "rejected"
    assert intent["reason_codes"] == "FEE_LIMIT|FEE_MISMATCH|OUTPUT_MISMATCH|SLIPPAGE"
    assert intent["permit_nonce"] == 0


def test_source_failure_fails_closed(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    contract = deploy(direct_deploy)
    create_intent(contract, direct_vm, direct_alice)
    submit_quote(contract, direct_vm, direct_bob)
    mock_quote(direct_vm, body="offline", status=503)

    direct_vm.sender = direct_alice
    contract.evaluate_intent("swap-1")
    intent = contract.get_intent("swap-1")

    assert intent["status"] == "source_failure"
    assert intent["decision"] == "source_failure"
    assert intent["reason_codes"] == "SOURCE_FAILURE"
    assert intent["permit_nonce"] == 0


def test_router_route_and_source_claims_are_bound(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    contract = deploy(direct_deploy)
    create_intent(contract, direct_vm, direct_alice)
    submit_quote(contract, direct_vm, direct_bob)
    mock_quote(
        direct_vm,
        quote_document(router="0xdddddddddddddddddddddddddddddddddddddddd"),
    )

    direct_vm.sender = direct_alice
    contract.evaluate_intent("swap-1")
    intent = contract.get_intent("swap-1")

    assert intent["status"] == "rejected"
    assert intent["reason_codes"] == "ROUTER_MISMATCH"


def test_cancel_invalidates_approved_permit(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    contract = deploy(direct_deploy)
    create_intent(contract, direct_vm, direct_alice)
    submit_quote(contract, direct_vm, direct_bob)
    mock_quote(direct_vm)

    direct_vm.sender = direct_alice
    contract.evaluate_intent("swap-1")
    contract.cancel_intent("swap-1")
    intent = contract.get_intent("swap-1")

    assert intent["status"] == "cancelled"
    assert intent["decision"] == "rejected"
    assert intent["reason_codes"] == "INTENT_CANCELLED"
    assert intent["consumed"] is False

    direct_vm.sender = bytes.fromhex(EXECUTOR[2:])
    with direct_vm.expect_revert("Intent has no active permit"):
        contract.consume_permit("swap-1", ROUTE_HASH)


def test_revision_update_invalidates_a_quote_before_consumption(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    contract = deploy(direct_deploy)
    create_intent(contract, direct_vm, direct_alice)
    submit_quote(contract, direct_vm, direct_bob)
    mock_quote(direct_vm)

    direct_vm.sender = direct_alice
    contract.evaluate_intent("swap-1")
    assert contract.get_intent("swap-1")["status"] == "approved"

    contract.update_intent(
        "swap-1",
        "ETH/USDC",
        100,
        2450,
        100,
        ROUTER,
        RECIPIENT,
        EXECUTOR,
        int(time.time()) + 3600,
        "Use the approved router and require the tighter output bound.",
    )
    intent = contract.get_intent("swap-1")
    assert intent["revision"] == 2
    assert intent["status"] == "awaiting_quote"
    assert intent["permit_nonce"] == 0


def test_executor_must_supply_the_exact_approved_route_hash(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    contract = deploy(direct_deploy)
    create_intent(contract, direct_vm, direct_alice)
    submit_quote(contract, direct_vm, direct_bob)
    mock_quote(direct_vm)

    direct_vm.sender = direct_alice
    contract.evaluate_intent("swap-1")
    direct_vm.sender = bytes.fromhex(EXECUTOR[2:])
    with direct_vm.expect_revert("Route hash does not match the approved permit"):
        contract.consume_permit("swap-1", "0x" + "cd" * 32)


def test_indicative_quote_requires_human_review(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    contract = deploy(direct_deploy)
    create_intent(contract, direct_vm, direct_alice)
    submit_quote(contract, direct_vm, direct_bob)
    mock_quote(direct_vm, quote_document(quote_kind="indicative"))

    direct_vm.sender = direct_alice
    contract.evaluate_intent("swap-1")
    intent = contract.get_intent("swap-1")

    assert intent["status"] == "unclear"
    assert intent["decision"] == "unclear"
    assert intent["reason_codes"] == "INDICATIVE_QUOTE"
    assert intent["permit_nonce"] == 0
