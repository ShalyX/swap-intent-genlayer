# SwapIntent threat model

## Assets and authority

- The user's declared swap bounds and recipient.
- The authoritative quote document and its fingerprint.
- The one-use permit bound to a route hash.
- The downstream executor's eventual swap call.

The intent owner controls the bounds and may revise or cancel the intent. The
validator committee controls the consensus-backed quote decision. Only the
configured executor can consume a permit. The downstream executor controls the
actual token movement and must bind its calldata to the consumed route hash.

## Threats and controls

| Threat | Control | Remaining limit |
| --- | --- | --- |
| Solver changes the router | Source and submitted router must match the intent allowlist | The owner chose the allowlist |
| Solver changes the path | Source, submitted quote, and consumed permit must share the exact route hash | The executor must bind calldata to that hash |
| Solver hides a worse output or fee | Validators bind source amounts and deterministic min-output/max-fee checks | A malicious source can still be unavailable |
| Quote is stale | Quote expiry must be in the future and no later than intent expiry | A source may change immediately after inspection |
| Indicative quote is treated as executable | `indicative` quotes become `unclear` and cannot issue permits | Source semantics must be represented honestly |
| Leader invents a favorable quote | Validators independently fetch and compare every consequential field and fingerprint | Highly dynamic sources may fail closed |
| Owner changes terms after approval | Revision update clears the quote and invalidates the permit | Owner authority itself is out of scope |
| Unauthorized executor consumes permit | Exact executor address is enforced | A compromised executor can consume its own permit |
| Permit replay | `consumed` and `status` are checked before every consumption | The downstream action must also be replay-safe |
| Source prompt injection | Quote content is parsed as data; no instructions from the source are followed | The contract does not prove the source's honesty |
| Downstream swap differs from approved route | Consumption requires the exact route hash | The executor must enforce hash-to-calldata binding |

## Deliberate exclusions

SwapIntent does not custody ERC-20 assets, choose a best quote, guarantee DEX
liquidity, perform slippage calculations from an unverified price feed, or
replace a downstream router's deterministic checks. Its guarantee is narrower:
the designated executor can consume only a consensus-approved, source-bound
route that satisfied the user's explicit bounds at evaluation time.
