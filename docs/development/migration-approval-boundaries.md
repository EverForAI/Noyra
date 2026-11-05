# Migration approval identities

Migration approval modes have separate meanings. `self_attested` keeps the
single-operator workflow and existing technical gates. Compatibility
`dual_review` counts different server bindings and explicitly reports
`legacy_bindings`; it does not establish independent accounts or people.
HTTP bearer and cookie approvals from the same credential role share one
binding, including across new browser sessions. A display label is not identity.

With `migration_require_independent_review`, `dual_review` reports
`independent_principals`. Each counted approval must have a fresh request-bound
principal from the configured authenticated proxy and an immutable provenance
record. Principal names themselves are not stored in that record. Two sessions
for one principal count once. Historical approvals without this provenance are
retained but cannot be promoted by inference. Already-approved legacy tasks
require a new proposal when switching to independent review.

The existing append-only migration audit owns these typed provenance records;
no history is rewritten and no schema migration is needed. Record hashes bind
the approval ID, proposal, policy revision, identity binding and kind. Strict
counting validates that binding when presenting approval counts and before
source fencing, manual preparation and cutover commit. The execution coordinator
receives the same strict setting as the HTTP approval manager.

Proxy trust still depends on the authenticated proxy deployment and the
request-bound principal signature. Independent accounts do not prove different
natural persons. Single-operator modes do not require that infrastructure.
Tests cover mixed legacy/principal reviews, repeat approvals, different labels,
bearer/cookie equivalence, strict configuration propagation and the existing
fresh principal signature contract. Production identity-provider integration
remains a deployment validation step.

## Local wallet export authority

A local wallet export requires the source database's explicit task approval;
`local_wallet_approval` in a private target configuration is no longer authority.
The approval audit binds subject, task/proposal, source epoch, policy revision,
wallet address, target enrollment generation, target signing-key fingerprint,
recipient encryption-key fingerprint and a bounded expiry. Legacy approval
payloads are retained but require an explicit new confirmation before export.

The existing authenticated `/api/admin/migration/tasks/{taskId}/wallet-approval`
endpoint issues the permit with `{"authorize_local_wallet": true}`. An identical
live retry returns the same permit. It also accepts
`{"revoke_local_wallet": true, "approval_id": "CURRENT_ID"}`; repeated revocation
is idempotent and a stale ID cannot revoke a newly issued permit. The permit ID
is returned to the operator, while secret keys and passwords remain private.
Task cancellation, policy disable/revision change, target re-enrollment/revocation,
wallet mismatch and expiry all invalidate its use.

Before reading the keystore/password, artifact generation revalidates the
source authority and the actual encryption recipient. One permit is bound to
one task/artifact identity; an identical retry is permitted, another artifact
is rejected. The target's existing signed binding, one-time approval marker,
same-task retry and activation expiry checks remain in place. Source checks
are repeated on managed/manual execution paths before proceeding with the
wallet binding. No historical permit is auto-replayed or inferred from config.

Source authorization is a checked boundary before material access, not remote
cancellation of bytes already exported. Revoking a permit cannot retract an
already delivered encrypted artifact. A failure after export or target receipt
still requires the existing explicit reconciliation/rollback protocol.
