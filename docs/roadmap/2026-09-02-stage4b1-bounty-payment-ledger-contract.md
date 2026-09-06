# Stage 4-B-1 Execution Contract: Task Bounties, Payment Orders, and Ledger

Date: 2026-09-02
Status: ready for implementation after explicit user start
Frozen base: `44e78761dbd83ad081a3dffc434fed36bd3cf177`

## Objective

Implement the complete durable **pre-execution economic domain** required for
Noyra to publish structured tasks, accept human help, make a deterministic
completion/reward decision, and create an idempotent payment order backed by an
append-only ledger. Stage 4-B-1 stops before signer calls, transaction
construction, or chain broadcast.

## Existing components to reuse

- `autonomous_projects`, phases, assistance requests, and execution/review
  records provide goal provenance and internal project context.
- `interactions` and `public_posts(kind=help_request)` provide existing human
  communication and moderated public publication surfaces.
- Stage 4-A wallet networks, assets, addresses, balance observations, and
  acquisition diagnostics provide operator-managed public chain metadata.
- `audit_records`, behavior reconciliation, subject isolation, integrity
  checks, runtime export, service sessions/CSRF, and operator controls provide
  existing security and recovery conventions.

Stage 4-B-1 must link to these domains by durable identifiers and verified
state; it must not duplicate private project reasoning or treat arbitrary
public text as trusted completion evidence.

## Proposed aggregate boundaries

### 1. Bounty task

Required durable identity and policy fields:

- `bounty_id`, `subject_id`, `project_id`/`goal_id` provenance;
- public `title`, bounded `description`, structured acceptance criteria;
- `network_id`, `asset_id`, canonical integer `reward_amount` in smallest units;
- publication lifecycle and explicit `opens_at`/`expires_at`;
- maximum accepted submissions and reward slots;
- unique idempotency key, state hash, creation/update audit IDs and timestamps.

Lifecycle:

```text
draft → published → closed
                 ↘ cancelled
published/closed → expired (time-derived transition)
```

A published bounty is immutable except for append-only lifecycle transitions.
Changing reward, asset, criteria, provenance, or deadline requires a new bounty.

### 2. Human help submission

Required fields:

- `submission_id`, `bounty_id`, `subject_id`;
- bounded counterparty provenance and public submission/evidence references;
- canonical EVM recipient address plus network binding;
- idempotency key, submitted timestamp, state hash and audit link.

Lifecycle:

```text
submitted → accepted | rejected | withdrawn | expired
```

The model may recommend acceptance, but the stored transition must identify the
deterministic policy or authorized decision source. One submission cannot win
twice; identical provider/bounty/idempotency inputs replay the same record.

### 3. Reward decision and payment order

An accepted submission can create at most one reward decision and at most one
payment order. The order captures an immutable snapshot of bounty, recipient,
network, asset, integer amount, payment mode, policy version and idempotency
key. It never contains signer credentials or arbitrary calldata.

Pre-signer lifecycle for this stage:

```text
pending_policy
  → awaiting_confirmation   (conditional mode or policy escalation)
  → reserved                (automatic/confirmed and ledger funds reserved)
  → rejected | cancelled | expired
```

States needed only after a signer exists (`signing`, `broadcast`, `unknown`,
`confirmed`, `failed`, `refunded`) are reserved vocabulary and documented now,
but Stage 4-B-1 must not pretend to execute them.

### 4. Payment policy

Subject-scoped, versioned policy with modes:

- `disabled`;
- `conditional_confirmation`;
- `automatic`.

The initial migration defaults every subject to `disabled`. Required bounds:

- allow-listed network and reward asset;
- per-order, daily, and monthly integer amount limits;
- daily/monthly order-count limits;
- minimum observed spending-wallet balance and maximum observation age;
- automatic-mode maximum amount and anomaly blocking;
- global emergency pause and explicit version/CAS update.

No unbounded default, floating-point money, negative amount, or implicit mode
upgrade is permitted.

### 5. Append-only money ledger

Use immutable entries with integer smallest-unit amounts. Every reservation or
release posts a balanced journal under one subject/network/asset/order. At
minimum model the following accounts:

- `available`;
- `reserved`;
- `paid` (future signer settlement account);
- `released`/`cancelled` (audit/control account as required by the chosen
  balanced-entry representation).

The ledger must prove:

- sum of debits equals sum of credits per journal;
- one order reservation is posted at most once;
- cancellation/rejection releases a reservation exactly once;
- records cannot be updated or deleted;
- balances are derived, never directly overwritten;
- runtime export and integrity checks include every new table.

Because Stage 4-A observes balances rather than controlling keys, reservation
is an internal budget claim, not proof that chain funds were moved. The UI/API
must label it accordingly.

## API and UI scope

Operator-authenticated/CSRF-protected management endpoints:

- list/create/publish/close/cancel bounties;
- list and decide submissions;
- list payment orders and confirm/reject/cancel eligible orders;
- read/update payment policy with expected version;
- read bounded ledger journals and aggregate balances.

Public endpoints:

- bounded published-bounty list/detail with no private project reasoning;
- bounded submission endpoint with abuse controls, idempotency, canonical
  recipient address validation, and explicit consent/terms fields.

No public endpoint may read private cognition, internal acceptance reasoning,
operator identity, wallet RPC origin, treasury/spending address inventory,
ledger internals, or audit payloads.

## Required invariants and failure behavior

- Every record is subject-scoped; cross-subject foreign references fail closed.
- Money is a canonical base-10 integer string bounded to the chosen SQL/storage
  limit; never `float` or JSON number where precision can be lost.
- Reward asset decimals are snapshotted and must match the active registered
  asset at order creation.
- Concurrent acceptance/reservation yields one winner and one order.
- Idempotency replay returns the original equivalent record; key reuse with
  different inputs is a conflict.
- Revoked networks/assets/addresses, stale/unhealthy observations, disabled or
  paused policy, expired bounties, exhausted limits, and integrity degradation
  block reservations.
- Unknown external results do not exist yet in Stage 4-B-1; no code may claim
  that an order is signed, broadcast, or paid.
- Restore/runtime rollback cannot erase an externally meaningful order or
  journal in later stages; Stage 4-B-1 must make the append-only ledger suitable
  for the existing external-action reconciliation/export boundary.

## Schema and integration checklist

The implementation must update together:

- schema migration and migration idempotence/future-schema safety tests;
- database triggers, indexes, immutable identity/state hashes and audit actions;
- typed records, validators, store/service separation and error vocabulary;
- integrity registry/watchdog coverage;
- runtime export ownership graph and per-table reconciliation;
- storage pressure/long-history bounded reads and query-plan tests;
- service route inventory and OpenAPI schemas;
- admin UI/public projection and privacy/redaction tests;
- restart/recovery, concurrency and idempotency tests.

## Acceptance matrix

Stage 4-B-1 is complete only when all of the following pass:

1. Bounty provenance, publication lifecycle, immutability and expiry.
2. Submission abuse bounds, consent, idempotency, recipient/network validation,
   acceptance/rejection/withdrawal and cross-subject isolation.
3. One accepted submission produces at most one immutable reward decision and
   one payment order under concurrency.
4. Disabled, conditional and automatic policies behave exactly as configured;
   policy CAS, pause, amount/count limits, balance age and anomaly gates fail
   closed.
5. Ledger journals balance, are append-only, reserve/release exactly once and
   derive aggregates without precision loss.
6. API/OpenAPI/UI contracts distinguish `reserved` from `paid` and expose no
   signer or private cognition data.
7. Integrity tampering, migration interruption, restart, export ownership,
   long history and query plans are tested.
8. Focused suites, full pytest, Ruff, format, mypy, compileall, Node syntax,
   pip check and `git diff --check` pass.
9. The final Stage 4-B-1 commit contains no signer, private key, transaction
   construction, nonce/gas, RPC send method, or chain broadcast implementation.

## Non-goals for Stage 4-B-1

- private-key, seed, mnemonic, KMS, MPC or hardware-wallet enrollment;
- raw transaction construction/signing;
- `eth_sendTransaction` or `eth_sendRawTransaction`;
- nonce, Gas estimation or receipt polling;
- mainnet/testnet real-value transfer;
- refund broadcast, swap, approval, trading or investment;
- arbitrary model-selected URL, calldata, amount or recipient execution.

Those start only in Stage 4-B-2 after this ledger and policy domain is frozen.
