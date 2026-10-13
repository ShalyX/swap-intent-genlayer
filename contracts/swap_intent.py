# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

"""Consensus-backed permits for agent-generated DeFi swap intents.

SwapIntent does not custody ERC-20 tokens or pretend to execute a swap. It
binds a route proposal to explicit protocol constraints, independently fetches
the authoritative quote document inside GenVM, and issues a one-use permit to
the exact downstream executor only after consensus on every consequential
field. A router or executor can consume that permit and perform the actual
swap in its own deterministic call path.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from genlayer import *


ERROR_EXPECTED = "[EXPECTED]"
ERROR_EXTERNAL = "[EXTERNAL]"
ERROR_LLM = "[LLM_ERROR]"

MAX_ID_LENGTH = 100
MAX_MARKET_LENGTH = 120
MAX_URL_LENGTH = 500
MAX_ASSET_LENGTH = 42
MAX_RISK_POLICY_LENGTH = 1600
MAX_RATIONALE_LENGTH = 900
MAX_BODY_LENGTH = 12000
VALID_DECISIONS = ("approved", "rejected", "unclear", "source_failure")
VALID_QUOTE_KINDS = ("firm", "indicative")
ZERO_ADDRESS = "0x" + "0" * 40


@allow_storage
@dataclass
class Intent:
    intent_id: str
    owner: Address
    market: str
    sell_asset: str
    buy_asset: str
    max_input: u256
    min_output: u256
    max_fee_bps: u256
    allowed_router: str
    recipient: str
    executor: str
    valid_until: u256
    risk_policy: str
    revision: u256
    status: str
    decision: str
    reason_codes: str
    confidence_band: str
    rationale: str
    quote_url: str
    quote_provider: str
    quoted_router: str
    quoted_route_hash: str
    quoted_input: u256
    quoted_output: u256
    quoted_fee_bps: u256
    quote_expires_at: u256
    quote_kind: str
    source_snapshot: str
    permit_nonce: u256
    consumed: bool


def _text(value, field_name: str, maximum: int) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise gl.vm.UserError(f"{ERROR_EXPECTED} {field_name} is required")
    if len(normalized) > maximum:
        raise gl.vm.UserError(f"{ERROR_EXPECTED} {field_name} exceeds {maximum} characters")
    return normalized


def _address_text(value) -> str:
    try:
        return value.as_hex.lower()
    except AttributeError:
        return str(value).strip().lower()


def _address(value, field_name: str) -> str:
    normalized = _address_text(value)
    if (
        len(normalized) != 42
        or not normalized.startswith("0x")
        or normalized == ZERO_ADDRESS
        or any(character not in "0123456789abcdef" for character in normalized[2:])
    ):
        raise gl.vm.UserError(f"{ERROR_EXPECTED} {field_name} must be a non-zero 20-byte address")
    return normalized


def _url(value, field_name: str) -> str:
    normalized = _text(value, field_name, MAX_URL_LENGTH)
    if not (normalized.startswith("https://") or normalized.startswith("http://")):
        raise gl.vm.UserError(f"{ERROR_EXPECTED} {field_name} must use http or https")
    return normalized


def _route_hash(value, field_name: str = "route_hash") -> str:
    normalized = _text(value, field_name, 66).lower()
    if (
        len(normalized) != 66
        or not normalized.startswith("0x")
        or any(character not in "0123456789abcdef" for character in normalized[2:])
    ):
        raise gl.vm.UserError(f"{ERROR_EXPECTED} {field_name} must be a 32-byte 0x hash")
    return normalized


def _normalize_body(raw_body) -> str:
    if isinstance(raw_body, bytes):
        raw_body = raw_body.decode("utf-8", errors="replace")
    return " ".join(str(raw_body or "").split())[:MAX_BODY_LENGTH]


def _uint_from_source(value, field_name: str) -> int:
    try:
        parsed = int(str(value))
    except Exception:
        raise ValueError(f"{field_name} is not an integer")
    if parsed < 0:
        raise ValueError(f"{field_name} cannot be negative")
    return parsed


def _source_failure(url: str, status: int, digest: str, reason: str) -> dict:
    return {
        "decision": "source_failure",
        "reason_codes": "SOURCE_FAILURE",
        "confidence_band": "low",
        "rationale": reason[:MAX_RATIONALE_LENGTH],
        "source_snapshot": f"{url}|{status}|{digest}",
        "router": ZERO_ADDRESS,
        "route_hash": "0x" + "0" * 64,
        "input_amount": 0,
        "output_amount": 0,
        "fee_bps": 0,
        "expires_at": 0,
        "quote_kind": "",
    }


def _inspect_quote(
    quote_url: str,
    intent_market: str,
    allowed_router: str,
    submitted_router: str,
    submitted_route_hash: str,
    submitted_input: u256,
    submitted_output: u256,
    submitted_fee_bps: u256,
    max_input: u256,
    min_output: u256,
    max_fee_bps: u256,
    valid_until: u256,
    now: int,
) -> dict:
    try:
        response = gl.nondet.web.get(quote_url)
        status = int(response.status)
        body = _normalize_body(response.body)
    except Exception:
        return _source_failure(
            quote_url,
            0,
            "0" * 64,
            "The authoritative quote endpoint could not be reached.",
        )

    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    if status < 200 or status >= 300 or not body:
        return _source_failure(
            quote_url,
            status,
            digest,
            f"The authoritative quote endpoint returned HTTP {status} or an empty body.",
        )

    try:
        document = json.loads(body)
        required = {
            "market",
            "router",
            "route_hash",
            "input_amount",
            "output_amount",
            "fee_bps",
            "expires_at",
            "quote_kind",
        }
        if not isinstance(document, dict) or not required.issubset(document.keys()):
            raise ValueError("quote document is missing a consequential field")
        market = str(document["market"]).strip()
        router = str(document["router"]).strip().lower()
        route_hash = str(document["route_hash"]).strip().lower()
        input_amount = _uint_from_source(document["input_amount"], "input_amount")
        output_amount = _uint_from_source(document["output_amount"], "output_amount")
        fee_bps = _uint_from_source(document["fee_bps"], "fee_bps")
        expires_at = _uint_from_source(document["expires_at"], "expires_at")
        quote_kind = str(document["quote_kind"]).strip().lower()
        if not market or len(market) > MAX_MARKET_LENGTH:
            raise ValueError("market is invalid")
        if (
            len(router) != 42
            or not router.startswith("0x")
            or router == ZERO_ADDRESS
            or any(character not in "0123456789abcdef" for character in router[2:])
        ):
            raise ValueError("router is invalid")
        if (
            len(route_hash) != 66
            or not route_hash.startswith("0x")
            or any(character not in "0123456789abcdef" for character in route_hash[2:])
        ):
            raise ValueError("route_hash is invalid")
        if quote_kind not in VALID_QUOTE_KINDS:
            raise ValueError("quote_kind is invalid")
    except Exception as error:
        return _source_failure(
            quote_url,
            status,
            digest,
            f"The quote document could not be normalized: {error}",
        )

    reasons = []
    if market != intent_market:
        reasons.append("MARKET_MISMATCH")
    if router != allowed_router or router != submitted_router:
        reasons.append("ROUTER_MISMATCH")
    if route_hash != submitted_route_hash:
        reasons.append("ROUTE_MISMATCH")
    if input_amount != int(submitted_input):
        reasons.append("INPUT_MISMATCH")
    if output_amount != int(submitted_output):
        reasons.append("OUTPUT_MISMATCH")
    if fee_bps != int(submitted_fee_bps):
        reasons.append("FEE_MISMATCH")
    if input_amount > int(max_input):
        reasons.append("INPUT_LIMIT")
    if output_amount < int(min_output):
        reasons.append("SLIPPAGE")
    if fee_bps > int(max_fee_bps):
        reasons.append("FEE_LIMIT")
    if expires_at <= now:
        reasons.append("QUOTE_EXPIRED")
    elif expires_at > int(valid_until):
        reasons.append("QUOTE_WINDOW")

    reasons = sorted(set(reasons))
    if reasons:
        decision = "rejected"
        confidence = "high"
        rationale = "The quote failed one or more deterministic intent constraints."
    elif quote_kind == "indicative":
        decision = "unclear"
        confidence = "medium"
        reasons = ["INDICATIVE_QUOTE"]
        rationale = "The source describes an indicative quote, not a firm executable route."
    else:
        decision = "approved"
        confidence = "high"
        rationale = "The source quote matches the declared route and satisfies every intent bound."

    return {
        "decision": decision,
        "reason_codes": "|".join(reasons),
        "confidence_band": confidence,
        "rationale": rationale,
        "source_snapshot": f"{quote_url}|{status}|{digest}",
        "router": router,
        "route_hash": route_hash,
        "input_amount": input_amount,
        "output_amount": output_amount,
        "fee_bps": fee_bps,
        "expires_at": expires_at,
        "quote_kind": quote_kind,
    }


def _parse_inspection(raw_value) -> dict:
    if isinstance(raw_value, str):
        try:
            raw_value = json.loads(raw_value)
        except Exception:
            raise gl.vm.UserError(f"{ERROR_LLM} inspection was not valid JSON")
    if not isinstance(raw_value, dict):
        raise gl.vm.UserError(f"{ERROR_LLM} inspection must be an object")
    fields = {
        "decision",
        "reason_codes",
        "confidence_band",
        "rationale",
        "source_snapshot",
        "router",
        "route_hash",
        "input_amount",
        "output_amount",
        "fee_bps",
        "expires_at",
        "quote_kind",
    }
    if set(raw_value.keys()) != fields:
        raise gl.vm.UserError(f"{ERROR_LLM} inspection fields are incomplete")
    decision = str(raw_value["decision"]).strip().lower()
    if decision not in VALID_DECISIONS:
        raise gl.vm.UserError(f"{ERROR_LLM} invalid decision")
    confidence = str(raw_value["confidence_band"]).strip().lower()
    if confidence not in ("high", "medium", "low"):
        raise gl.vm.UserError(f"{ERROR_LLM} invalid confidence band")
    reason_codes = str(raw_value["reason_codes"] or "").strip()
    rationale = _text(raw_value["rationale"], "rationale", MAX_RATIONALE_LENGTH)
    return {
        "decision": decision,
        "reason_codes": reason_codes,
        "confidence_band": confidence,
        "rationale": rationale,
        "source_snapshot": str(raw_value["source_snapshot"]),
        "router": str(raw_value["router"]).lower(),
        "route_hash": str(raw_value["route_hash"]).lower(),
        "input_amount": int(raw_value["input_amount"]),
        "output_amount": int(raw_value["output_amount"]),
        "fee_bps": int(raw_value["fee_bps"]),
        "expires_at": int(raw_value["expires_at"]),
        "quote_kind": str(raw_value["quote_kind"]).lower(),
    }


def _equivalent(leader: dict, validator: dict) -> bool:
    fields = (
        "decision",
        "reason_codes",
        "confidence_band",
        "source_snapshot",
        "router",
        "route_hash",
        "input_amount",
        "output_amount",
        "fee_bps",
        "expires_at",
        "quote_kind",
    )
    return all(leader.get(field) == validator.get(field) for field in fields)


class SwapIntent(gl.Contract):
    """Issue one-use permits for routes that satisfy an explicit swap intent."""

    intents: TreeMap[str, Intent]
    intent_order: DynArray[str]
    next_permit_nonce: u256

    def __init__(self):
        self.next_permit_nonce = 1

    def _now(self) -> int:
        return int(datetime.now(timezone.utc).timestamp())

    def _sender_hex(self) -> str:
        return _address_text(gl.message.sender_address)

    def _get_intent(self, intent_id: str) -> Intent:
        if intent_id not in self.intents:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Intent not found")
        return self.intents[intent_id]

    def _require_owner(self, intent: Intent) -> None:
        if self._sender_hex() != intent.owner.as_hex.lower():
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Only the intent owner may do this")

    def _reset_quote(self, intent: Intent) -> None:
        intent.status = "awaiting_quote"
        intent.decision = "pending"
        intent.reason_codes = ""
        intent.confidence_band = ""
        intent.rationale = ""
        intent.quote_url = ""
        intent.quote_provider = ""
        intent.quoted_router = ""
        intent.quoted_route_hash = ""
        intent.quoted_input = 0
        intent.quoted_output = 0
        intent.quoted_fee_bps = 0
        intent.quote_expires_at = 0
        intent.quote_kind = ""
        intent.source_snapshot = ""
        intent.permit_nonce = 0
        intent.consumed = False

    @gl.public.write
    def create_intent(
        self,
        intent_id: str,
        market: str,
        sell_asset: Address,
        buy_asset: Address,
        max_input: u256,
        min_output: u256,
        max_fee_bps: u256,
        allowed_router: Address,
        recipient: Address,
        executor: Address,
        valid_until: u256,
        risk_policy: str,
    ) -> None:
        intent_id = _text(intent_id, "intent_id", MAX_ID_LENGTH)
        if intent_id in self.intents:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Intent ID already exists")
        if max_input == 0 or min_output == 0:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} swap bounds must be greater than zero")
        if max_fee_bps > 10000:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} max_fee_bps cannot exceed 10000")
        if valid_until <= self._now():
            raise gl.vm.UserError(f"{ERROR_EXPECTED} valid_until must be in the future")

        self.intents[intent_id] = Intent(
            intent_id=intent_id,
            owner=gl.message.sender_address,
            market=_text(market, "market", MAX_MARKET_LENGTH),
            sell_asset=_address(sell_asset, "sell_asset"),
            buy_asset=_address(buy_asset, "buy_asset"),
            max_input=max_input,
            min_output=min_output,
            max_fee_bps=max_fee_bps,
            allowed_router=_address(allowed_router, "allowed_router"),
            recipient=_address(recipient, "recipient"),
            executor=_address(executor, "executor"),
            valid_until=valid_until,
            risk_policy=_text(risk_policy, "risk_policy", MAX_RISK_POLICY_LENGTH),
            revision=1,
            status="awaiting_quote",
            decision="pending",
            reason_codes="",
            confidence_band="",
            rationale="",
            quote_url="",
            quote_provider="",
            quoted_router="",
            quoted_route_hash="",
            quoted_input=0,
            quoted_output=0,
            quoted_fee_bps=0,
            quote_expires_at=0,
            quote_kind="",
            source_snapshot="",
            permit_nonce=0,
            consumed=False,
        )
        self.intent_order.append(intent_id)

    @gl.public.write
    def update_intent(
        self,
        intent_id: str,
        market: str,
        max_input: u256,
        min_output: u256,
        max_fee_bps: u256,
        allowed_router: Address,
        recipient: Address,
        executor: Address,
        valid_until: u256,
        risk_policy: str,
    ) -> None:
        intent = self._get_intent(intent_id)
        self._require_owner(intent)
        if intent.status == "settled":
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Settled intent cannot be updated")
        if max_input == 0 or min_output == 0:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} swap bounds must be greater than zero")
        if max_fee_bps > 10000:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} max_fee_bps cannot exceed 10000")
        if valid_until <= self._now():
            raise gl.vm.UserError(f"{ERROR_EXPECTED} valid_until must be in the future")
        intent.market = _text(market, "market", MAX_MARKET_LENGTH)
        intent.max_input = max_input
        intent.min_output = min_output
        intent.max_fee_bps = max_fee_bps
        intent.allowed_router = _address(allowed_router, "allowed_router")
        intent.recipient = _address(recipient, "recipient")
        intent.executor = _address(executor, "executor")
        intent.valid_until = valid_until
        intent.risk_policy = _text(risk_policy, "risk_policy", MAX_RISK_POLICY_LENGTH)
        intent.revision += 1
        self._reset_quote(intent)

    @gl.public.write
    def submit_quote(
        self,
        intent_id: str,
        quote_url: str,
        router: Address,
        route_hash: str,
        quoted_input: u256,
        quoted_output: u256,
        quoted_fee_bps: u256,
    ) -> None:
        intent = self._get_intent(intent_id)
        if intent.status != "awaiting_quote":
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Intent is not awaiting a quote")
        if quoted_input == 0 or quoted_output == 0:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} quote amounts must be greater than zero")
        if quoted_fee_bps > 10000:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} quoted_fee_bps cannot exceed 10000")
        intent.quote_url = _url(quote_url, "quote_url")
        intent.quote_provider = self._sender_hex()
        intent.quoted_router = _address(router, "router")
        intent.quoted_route_hash = _route_hash(route_hash)
        intent.quoted_input = quoted_input
        intent.quoted_output = quoted_output
        intent.quoted_fee_bps = quoted_fee_bps
        intent.status = "quoted"
        intent.decision = "pending"

    def _evaluate_with_consensus(self, intent: Intent, now: int) -> dict:
        inputs = (
            intent.quote_url,
            intent.market,
            intent.allowed_router,
            intent.quoted_router,
            intent.quoted_route_hash,
            intent.quoted_input,
            intent.quoted_output,
            intent.quoted_fee_bps,
            intent.max_input,
            intent.min_output,
            intent.max_fee_bps,
            intent.valid_until,
            now,
        )

        def run() -> dict:
            return _inspect_quote(*inputs)

        def validator_fn(leader_result: gl.vm.Result) -> bool:
            if not isinstance(leader_result, gl.vm.Return):
                return False
            try:
                leader = _parse_inspection(leader_result.calldata)
                validator = run()
                return _equivalent(leader, validator)
            except Exception:
                return False

        return gl.vm.run_nondet_unsafe(run, validator_fn)

    @gl.public.write
    def evaluate_intent(self, intent_id: str) -> None:
        intent = self._get_intent(intent_id)
        if intent.status != "quoted":
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Intent is not ready for evaluation")
        now = self._now()
        if intent.valid_until <= now:
            intent.status = "expired"
            intent.decision = "rejected"
            intent.reason_codes = "INTENT_EXPIRED"
            intent.confidence_band = "high"
            intent.rationale = "The intent validity window expired before evaluation."
            return

        result = self._evaluate_with_consensus(intent, now)
        intent.decision = result["decision"]
        intent.reason_codes = result["reason_codes"]
        intent.confidence_band = result["confidence_band"]
        intent.rationale = result["rationale"]
        intent.source_snapshot = result["source_snapshot"]
        intent.quote_expires_at = result["expires_at"]
        intent.quote_kind = result["quote_kind"]
        intent.status = result["decision"]
        if result["decision"] == "approved":
            intent.permit_nonce = self.next_permit_nonce
            self.next_permit_nonce += 1

    @gl.public.write
    def cancel_intent(self, intent_id: str) -> None:
        intent = self._get_intent(intent_id)
        self._require_owner(intent)
        if intent.status == "settled":
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Settled intent cannot be cancelled")
        intent.status = "cancelled"
        intent.decision = "rejected"
        intent.reason_codes = "INTENT_CANCELLED"
        intent.confidence_band = "high"
        intent.rationale = "The intent owner cancelled the route before settlement."
        intent.permit_nonce = 0
        intent.consumed = False

    @gl.public.write
    def consume_permit(self, intent_id: str, route_hash: str) -> None:
        intent = self._get_intent(intent_id)
        if intent.status != "approved" or intent.consumed:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Intent has no active permit")
        if self._sender_hex() != intent.executor:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Caller is not the designated executor")
        if _route_hash(route_hash) != intent.quoted_route_hash:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Route hash does not match the approved permit")
        if intent.valid_until <= self._now():
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Permit has expired")
        intent.consumed = True
        intent.status = "settled"

    def _intent_to_dict(self, intent: Intent) -> dict:
        return {
            "intent_id": intent.intent_id,
            "owner": intent.owner.as_hex,
            "market": intent.market,
            "sell_asset": intent.sell_asset,
            "buy_asset": intent.buy_asset,
            "max_input": intent.max_input,
            "min_output": intent.min_output,
            "max_fee_bps": intent.max_fee_bps,
            "allowed_router": intent.allowed_router,
            "recipient": intent.recipient,
            "executor": intent.executor,
            "valid_until": intent.valid_until,
            "risk_policy": intent.risk_policy,
            "revision": intent.revision,
            "status": intent.status,
            "decision": intent.decision,
            "reason_codes": intent.reason_codes,
            "confidence_band": intent.confidence_band,
            "rationale": intent.rationale,
            "quote_url": intent.quote_url,
            "quote_provider": intent.quote_provider,
            "quoted_router": intent.quoted_router,
            "quoted_route_hash": intent.quoted_route_hash,
            "quoted_input": intent.quoted_input,
            "quoted_output": intent.quoted_output,
            "quoted_fee_bps": intent.quoted_fee_bps,
            "quote_expires_at": intent.quote_expires_at,
            "quote_kind": intent.quote_kind,
            "source_snapshot": intent.source_snapshot,
            "permit_nonce": intent.permit_nonce,
            "consumed": intent.consumed,
        }

    @gl.public.view
    def get_intent(self, intent_id: str) -> dict:
        return self._intent_to_dict(self._get_intent(intent_id))

    @gl.public.view
    def is_permit_valid(self, intent_id: str, route_hash: str) -> bool:
        intent = self._get_intent(intent_id)
        return bool(
            intent.status == "approved"
            and not intent.consumed
            and self._sender_hex() == intent.executor
            and _route_hash(route_hash) == intent.quoted_route_hash
            and intent.valid_until > self._now()
        )

    @gl.public.view
    def get_intent_ids(self) -> dict:
        return {str(index): self.intent_order[index] for index in range(len(self.intent_order))}
