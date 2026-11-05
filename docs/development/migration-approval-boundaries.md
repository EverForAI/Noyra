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
