# SwapIntent

SwapIntent is a reusable GenLayer Intelligent Contract for issuing one-use,
consensus-backed permits for agent-generated DeFi swap routes.

It is deliberately non-custodial. SwapIntent does not custody ERC-20 tokens or
claim that a swap executed merely because a permit was approved. It binds a
route proposal to an explicit intent, independently fetches the authoritative
quote document inside GenVM, and lets only the named downstream executor
consume the exact route permit. The executor or router performs the actual
deterministic swap in its own integration.

## The protocol problem

Intent-based trading lets agents request a result instead of prescribing every
low-level call. That flexibility creates a dangerous boundary: a solver can
return a different router, route, amount, fee, or expiry than the user
approved, while a basic contract may only check one minimum-output field.

SwapIntent makes the consequential route fields first-class protocol state:

- market and token pair;
- maximum input and minimum output;
- maximum fee in basis points;
- allowlisted router;
- exact route hash;
- recipient and designated executor; and
- intent and quote expiry windows.

Validators independently fetch and normalize the quote document. Consensus
binds the source fingerprint, router, route hash, amounts, fee, expiry, quote
kind, decision, reason codes, and confidence band. A permit is issued only for
a firm quote that satisfies every bound.

## Lifecycle

1. The intent owner creates an intent with hard execution bounds.
2. A solver submits a public quote URL and its claimed route fields.
3. Anyone can call `evaluate_intent`.
4. Validators fetch the quote document inside the non-deterministic flow and
   independently derive `approved`, `rejected`, `unclear`, or `source_failure`.
5. A firm, matching quote receives a one-use permit bound to the exact route.
6. The designated executor calls `consume_permit` with that route hash.
7. The executor performs the actual swap only after the permit is consumed.

Owner revision updates clear the quote and invalidate any outstanding permit.
Cancellation also invalidates an approved permit. Replays, wrong callers,
wrong route hashes, stale quotes, source failures, slippage, fee limits, and
router mismatches fail closed.

## Contract surface

- `create_intent(...)` — publish the user's bounded swap intent.
- `update_intent(...)` — revise bounds and increment the intent revision.
- `submit_quote(...)` — bind a solver's quote claims to a public source.
- `evaluate_intent(intent_id)` — run the consensus-backed quote inspection.
- `consume_permit(intent_id, route_hash)` — consume the exact one-use permit.
- `cancel_intent(intent_id)` — invalidate an unconsumed intent or permit.
- `get_intent(intent_id)` and `is_permit_valid(...)` — integration and audit views.

## Quote document

The declared quote URL must return JSON containing the consequential fields:

```json
{
  "market": "ETH/USDC",
  "router": "0x...",
  "route_hash": "0x...",
  "input_amount": "100",
  "output_amount": "2500",
  "fee_bps": "30",
  "expires_at": "1790000000",
  "quote_kind": "firm"
}
```

`quote_kind` may be `firm` or `indicative`. Indicative quotes become
`unclear` and never receive a permit.

## Why GenLayer is central

The quote source is live and non-deterministic. Validators must independently
fetch it and agree on the complete normalized result before deterministic
permit state can change. This is the part a conventional contract cannot do
without a trusted oracle or centralized solver.

## Run checks

```powershell
python -m pip install -r requirements.txt
python -m pytest -p no:cacheprovider tests -q
python -m genvm_linter.cli check --json contracts/swap_intent.py
```

## Limits

SwapIntent is a route authorization primitive, not a DEX, price oracle, token
custodian, or guarantee that a downstream router will execute successfully.
Integrators must bind the consumed route hash to the calldata they actually
send, enforce token allowances and balance checks, and handle finality and
failed child messages explicitly.
