# Stage 4-A Read-only Wallet Freeze Review

Date: 2026-09-02
Branch: `codex/candidate-20260822`
Reviewed commit: `44e78761dbd83ad081a3dffc434fed36bd3cf177`
Parent: `3b86df46a082a431a72e3a8ab9d8e62970af6ebb`

## Decision

**Approved as the frozen read-only wallet observation baseline.** The reviewed
commit remains immutable. Economic wallet work starts from its descendant and
must not rewrite or squash this baseline.

## Scope review

The commit contains only:

- bounded wallet balance-observation health aggregation by network or newest
  observation source;
- subject-scoped filters, pagination limits, and ordered cursor consumption;
- operator-authenticated API and OpenAPI contract updates;
- redacted admin presentation;
- focused regression tests and this module's route documentation.

The reviewed diff does **not** add private-key or seed storage, signer access,
transaction construction, nonce/gas handling, transaction broadcast, transfer,
approval, swap, payment, payout, or arbitrary RPC behavior. It does not widen
model input into wallet parameters and does not expose balances, addresses,
RPC origins, leases, or claim tokens through the health projection.

## Contract checks

- Network/source dimensions and query filters are allow-listed and bounded.
- Health counters have consistency checks and severity ordering.
- Active-pair and group limits fail closed.
- History is consumed through a bounded ordered SQLite cursor; no unbounded
  result materialization is introduced.
- The service accepts only the fixed projection and degrades to a redacted
  unavailable result when the projection cannot be trusted.
- Subject isolation, lifecycle filtering, timestamp validation, API error
  mapping, compatibility routing, and UI escaping are covered by tests.

## Verification evidence

The focused freeze review regression passed:

```text
pytest -q tests/test_wallet.py tests/test_wallet_observation_health.py \
  tests/test_wallet_observation_breakdown.py tests/test_wallet_acquisition.py \
  tests/test_wallet_rpc.py tests/test_m42_p3_07_api_contract.py \
  tests/test_public_post_admin_ui.py
78 passed
```

The preceding commit validation also passed Ruff, format, mypy, compileall,
Node syntax checking, pip check, and `git diff --check`. A full repository
regression was started from this exact baseline; its final result is recorded
in the handoff report after completion.

## Findings

No freeze-blocking defect was found. The important finding is a **product-scope
gap**, not a regression in this commit: the repository currently has read-only
wallet registration, fixed RPC balance observation, acquisition scheduling,
history, integrity diagnostics, and health breakdowns, but it has no economic
execution domain. In particular, the following are intentionally absent and
must be designed in the next execution:

- structured autonomous bounty/task publication;
- helper submissions and completion evidence;
- reward policy and payment modes;
- payment orders and an append-only money ledger;
- an external signer boundary and transaction lifecycle.

These absences are not reasons to reject `44e7876`; they define the next
implementation boundary.

## Frozen boundary for the next execution

The next execution is **Stage 4-B-1: task bounty domain, payment orders, and
ledger**. It may add durable task/reward records and deterministic policy
checks, but it must not yet send a chain transaction. The following execution
will add an external signer and bounded chain execution. This ordering keeps
the ledger authoritative before any real economic side effect exists.

The final product target is not unrestricted model-controlled spending. It is:

```text
Noyra autonomous goal
→ structured task/bounty
→ human help submission
→ deterministic completion decision
→ idempotent reward order and ledger reservation
→ policy-gated signer execution
→ chain confirmation and audit
```

A production automatic payout mode may omit per-transaction human approval only
when the pre-authorized policy, task binding, limits, balance threshold, chain
allow-list, and emergency pause all pass. The model may propose structured
values; the Supervisor and payment policy must validate them before an external
side effect.

## Handoff

Before Stage 4-B-1 starts, the working tree must be clean and the exact
reviewed commit must remain in history. No wallet execution code is included in
this review commit.
