# Noyra Subject Migration Design

## Goal

Allow a Noyra subject to propose and execute a verified migration to a trusted
target host while preserving subject identity, encrypted state, auditability,
single-writer safety, and operator control.

## Scope

The first release includes target registration, resource discovery from registered
targets, deterministic trust admission, cognitive migration proposals, configurable
approval modes, rejection cooldowns, encrypted transfer and restore, single-active
fencing, manual cutover, rollback, credential rebinding, and optional local-wallet
transfer. Cloud-provider provisioning is an adapter boundary; the core never scans
the public Internet or receives arbitrary SSH credentials.

## Safety Defaults

- Migration is disabled by default.
- Enabling migration selects `manual` approval by default.
- Automatic approval requires an explicit `policy_auto` setting and bounded policy.
- Emergency recovery is a separate setting and is disabled by default.
- External signer/KMS rebinding is the preferred wallet mode.
- Local private-key transfer is disabled unless explicitly enabled by a separate
  wallet policy.
- Provider credentials, operator tokens, tunnel tokens, and wallet secrets are not
  included in ordinary migration payloads.
- A target must be registered and prove possession of its enrollment key before it
  can receive state.
- Only one subject epoch may accept mutations at a time.

## Approval Modes

`disabled` prevents candidate discovery, proposals, and migration execution.
`manual` permits proposals but requires an operator approval bound to the exact
target, manifest, policy revision, and expiry. `policy_auto` permits execution only
when all configured hard gates pass. `emergency_recovery` permits restoration to a
pre-registered standby after source failure; it is not a general migration mode.

The policy stores bounded values for resource thresholds, allowed target IDs,
regions, maximum cost, maximum downtime, maintenance windows, trust level, proposal
cooldown after rejection, proposal expiry, and wallet mode. Policy revisions are
monotonic and are included in every proposal and approval.

## Candidate and Trust Model

A candidate consists of a target ID, public key, enrollment generation, endpoint,
resource observations, region, Noyra release SHA, OS/architecture, encrypted-volume
attestation, last health timestamp, and target-agent capabilities. The target agent
answers a nonce challenge signed by its enrollment key. The source verifies the
signature, freshness, enrollment generation, release compatibility, storage
capacity, encryption requirement, and absence of another active subject epoch.

The cognitive evaluator may rank candidates and explain the expected benefit, but it
cannot waive hard gates. A proposal requires a current resource opportunity, a
current source need, a trusted target, a positive benefit-versus-risk decision, a
valid maintenance window, and no active cooldown or conflicting work.

## Migration State Machine

The durable states are `planned`, `preflight`, `awaiting_approval`, `approved`,
`preparing`, `transferring`, `restoring`, `validating`, `cutover`, `committed`,
`rolling_back`, `rolled_back`, `cancelled`, and `failed`.

Every task has a migration ID, idempotency key, source epoch, target ID, policy
revision, manifest digest, backup artifact ID, target release SHA, expiry, bounded
logs, error code, and append-only audit events. Repeated requests with the same
idempotency key return the same task. Stale approvals, changed policy revisions,
changed target keys, and changed manifests are rejected.

## Data and Cutover Protocol

The source first pauses cognition, search, messaging, exports, and wallet execution,
then acquires the existing process lock and creates an authenticated encrypted
backup. The target restores into a new encrypted data root and verifies the manifest,
SQLite quick check, schema version, subject identity, genesis hash, event-chain tip,
archive references, and configured release SHA.

Before cutover, the source publishes a new epoch lease. The target must acquire that
lease before accepting mutations. The source then becomes read-only or stops. DNS,
Cloudflare, or reverse-proxy routing changes are performed only after target health
passes. Failure before commit leaves the source authoritative; failure after the
target lease is acquired enters the explicit rollback protocol. The old host is
never allowed to resume writes without a newer epoch.

## Wallet and Credential Modes

`external_signer_rebind` is the preferred mode. The migration carries only signer
identity and binding metadata; the target obtains credentials from its configured
KMS/signer enrollment. `local_wallet_transfer` is an explicit high-risk mode. It
uses a one-time encrypted channel and a separately approved key-transfer operation,
verifies the destination address and chain configuration, and keeps the source key
until commit. If a signer/KMS is unavailable and local transfer is not enabled, the
migration completes without wallet execution and reports that the wallet must be
rebound manually.

Model, search, proxy, tunnel, operator, and export credentials are re-bound from
protected files or system credentials. They are never placed in the migration
manifest, task status, logs, SQLite audit payloads, or ordinary exports.

## HTTP and Admin Surface

The operator API exposes policy read/update, target registration and revocation,
candidate discovery, proposal listing, proposal detail, approve, reject, cancel,
start, status, cutover, rollback, and emergency-recovery controls. All mutation
routes require the operator session and CSRF protection. The admin UI shows the
current mode, policy revision, trust failures, active epoch, cooldown, proposal
reason, target details, data/key plan, and rollback state.

## Failure Handling

All stages are resumable or fail closed. Transfer retries are bounded and keyed by
chunk hash. A corrupted or incomplete artifact is discarded. A target that fails
validation is quarantined and cannot be retried until its enrollment generation is
rotated. A denied proposal is retained for audit and cannot be re-proposed until the
configured cooldown expires. Emergency recovery requires a verified backup and a
pre-registered standby; it never searches for an arbitrary host.

## Acceptance Criteria

1. A fresh installation cannot discover or migrate until migration is explicitly
   enabled.
2. Enabling migration defaults to manual approval and records a policy revision.
3. A proposal includes target, reason, evidence, benefit, risk, key plan, downtime,
   expiry, and rollback details.
4. A denied proposal obeys the configured cooldown and cannot bypass it by changing
   text or idempotency keys.
5. Unregistered, stale, unencrypted, incompatible, or already-active targets are
   rejected before data transfer.
6. A restored target has the same subject identity and verified event-chain tip.
7. The old source cannot mutate after target epoch acquisition.
8. External signer rebinding works without copying private keys; local wallet transfer
   is impossible unless separately enabled and approved.
9. No secret appears in migration manifests, status, logs, exports, or audit payloads.
10. Interrupted transfer, restart, source failure, target failure, cutover failure,
    duplicate request, stale approval, and rollback are covered by automated tests.
