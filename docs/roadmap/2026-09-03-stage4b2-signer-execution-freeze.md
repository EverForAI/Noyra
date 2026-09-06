# Stage 4-B-2 Signer and Payment Execution Freeze

Date: 2026-09-03
Status: freeze review approved; ready for independent commit
Base: `113a888 feat: add wallet bounty payment ledger domain`

## Delivered

- Independent `WalletSigner` protocol, deterministic `MockSigner`, and a fixed-route HTTPS signer client for an isolated testnet/production signer service.
- Canonical native-asset and ERC-20 `transfer(address,uint256)` envelopes only; no arbitrary RPC, URL, calldata, approval, swap, trading, or key material.
- Durable execution and attempt records with chain ID, nonce, gas, request identity, receipt state, state hashes, append-only audit links, transition triggers, and subject isolation.
- `reserved -> signing -> broadcast -> confirmed|failed|unknown` lifecycle, explicit unknown retry with the original nonce, receipt-first retry behavior, restart recovery, and failed/unknown refund.
- Settlement only after a successful chain receipt. Failed or ambiguous execution never posts the paid journal.
- Operator HTTP routes, bounded diagnostics, admin controls, OpenAPI inventory, integrity checking, and runtime export ownership.
- Fail-closed behavior when no signer is injected. Signer exceptions are stored only as fixed classifications.

## Verification

- Full repository: `1151 passed, 8 skipped, 251 subtests passed`.
- Final focused signer/execution/API/UI suite: `26 passed`.
- `ruff check src tests`, `ruff format --check src tests`, `mypy`, `compileall`, `git diff --check`, `node --check`, OpenAPI YAML parsing, and `pip check` passed.
- Schema 55 to 56 migration and repeated initialization are covered.

## Freeze decision

Approved as one cohesive Stage 4-B-2 change. The diff contains the signer
boundary, execution lifecycle, schema/integrity/export ownership, operator API
and UI controls, and their tests. No unrelated historical change or partial
Stage 4-B-3 orchestration was found, so the change does not require splitting.

The final review also confirmed strict signer response validation, public-DNS
HTTPS transport defaults, fixed routes, bounded headers/body/deadline, unique
nonce and transaction-hash guards, indexed execution history, streamed
integrity verification, current-schema trigger repair, and a complete HTTP
execute-to-confirm happy path.

## Review boundary

This change does not provision a real key, accept a key through API/environment configuration, or contact a live chain during tests. A production deployment must inject a separately operated signer implementation. Real small-value testnet transfer and long-running fault exercises remain Stage 4-B-4 release gates.

## Next stage

Stage 4-B-3 integrates autonomous goals, published help tasks, inbound human submissions, evidence review, payment policy, signer execution, chain receipt, and operator intervention into one end-to-end workflow.
