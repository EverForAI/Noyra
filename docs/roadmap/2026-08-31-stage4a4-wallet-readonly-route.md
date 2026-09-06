# Stage 4-A-4 Wallet Read-Only Acquisition Route Record

Date: 2026-08-31
Branch: codex/candidate-20260822
Stage 3 freeze tag: noyra-freeze-stage3-20260830
Stage 3 baseline commit: a9e6b9f

## 1. Current conclusion

Stage 4-A covers the wallet read-only foundation, fixed RPC balance reads, a durable acquisition queue, and operator observability. The changes were reviewed as one frozen unit and accepted in the Stage 4-A commit that contains this record. The Stage 3 freeze commit and tag are not rewritten.

This stage establishes auditable public chain metadata and balance observations only. It does not establish wallet control or payment capability.

## 2. Completed sub-stages

### 4-A-1: Wallet read-only foundation

- Register and revoke EVM networks, native assets, token assets, and public addresses.
- Normalize and bound addresses, contract addresses, symbols, decimals, chain IDs, and RPC origins.
- Persist lifecycle state, revision history, audit records, and state hashes; revoke rather than physically delete resources.
- Require networks, assets, and addresses to belong to one subject; balance snapshots must reference one active network.

### 4-A-2: Fixed read-only RPC boundary

- RPC endpoints come only from registered credential-free public HTTPS origins.
- Each read performs a fixed chain identity check and one balance read.
- Native assets use only eth_getBalance; tokens use only ERC-20 balanceOf through eth_call.
- Bound headers, body size, timeout, concurrency, and DNS egress; accept only strict JSON-RPC quantity responses.
- Persist successful results as immutable observations and expose sanitized error codes only.

### 4-A-3: Durable acquisition queue and recovery

- Persist runs, attempts, leases, request budgets, idempotency keys, and backoff times.
- Reserve at most two RPC requests per run and enforce concurrency, minimum interval, window, and lease limits.
- On startup, mark interrupted running runs as unknown without replay; only an explicit operator retry requeues an unknown run.
- Let the scheduler handle retry_wait; do not execute runs whose targets are revoked or whose attempt limit is reached.
- Include run and attempt evidence, state hashes, audit history, and subject ownership in integrity checks and runtime export.

### 4-A-4: Operator API and management observability

- Provide authenticated, subject-scoped, bounded APIs for networks, assets, addresses, balances, and the acquisition queue.
- Provide enqueue, due-run execution, attempt history, explicit unknown retry, and queued/retry-wait cancellation operations.
- Do not expose claim tokens, lease owners, or other lease credentials in API responses or diagnostics.
- Show bounded acquisition status, budget, and failure information in health, diagnostics, OpenAPI, service contracts, and the admin UI.
- Do not pass natural language, arbitrary RPC methods, or transaction parameters through the operator API into the wallet boundary.

## 3. Capabilities explicitly not implemented

The following are not part of this working tree and must remain unavailable through APIs, the database, or model context:

- Private keys, seed phrases, signer services, or key custody.
- Transaction construction, nonce, gas policy, broadcast, or transaction state machines.
- eth_sendTransaction, eth_sendRawTransaction, transfers, approvals, swaps, or payments.
- Recipient allowlists, payment orders, refunds, disputes, or automatic economic mode.
- Model-supplied arbitrary URLs, RPC methods, calldata, or amounts.

Real payment remains gated on an independent signer, an idempotent payment ledger, limits, gas controls, failure recovery, emergency pause, and small testnet exercises.

## 4. Current verification evidence

The current working tree was reviewed with:

| Gate | Result |
| --- | --- |
| Wallet foundation, RPC, acquisition, integrity, and admin API focused regression | 164 passed |
| Stage 3 inherited full regression | 1092 passed, 8 skipped, 251 subtests passed |
| ruff check | passed |
| ruff format --check | passed |
| mypy src tests | passed |
| compileall | passed |
| node --check src/noyra/web/admin.js | passed |
| git diff --check | passed; only the existing service.py CRLF conversion warning remains |

The 164 focused tests cover wallet registration, input limits, migration idempotence, RPC response bounds, budgets, lease recovery, retry, cancellation, cross-subject export, integrity tampering, and HTTP route behavior.

## 5. Acceptance and next-step rules

- The accepted Stage 4-A changes are recorded in one dedicated commit; do not split or rewrite the Stage 3 tag.
- The next task should cover one medium-sized wallet read-only module, preferably bounded balance observation/history queries or an equivalent read-only audit boundary. It must remain outside signing and payments.
