# Social proposal rejection and recovery

Social cognition keeps the existing hourly, recipient-route-bound business
purpose. Choosing contact, requesting help, waiting, and respecting distance
continues to use the same relationship, invitation, privacy, and route checks.
This protocol adds no per-message approval requirement or recipient allowlist.

## Deterministic rejection

A provider-successful proposal can still fail local business validation. That
is a business rejection, not a provider failure or an unknown outcome. The
successful model call, usage, and cost remain unchanged.

Each such call receives one immutable, private `relationship_social_rejected`
event. Its deterministic event ID binds the call. The receipt records request
and response hashes, an evaluated-context hash, a bounded validation reason,
and the next consideration time. It does not copy message bodies or prompts.
Normal event hashing and encrypted archive reads apply to these receipts.

The context fingerprint covers the actual bounded context, relationship
revisions, and the frozen contact route. Rejection events themselves do not
enter social context, so recording a rejection cannot create its own new input.

Within that hour and route, unchanged evaluated evidence returns
`relationship_social_waiting`; it does not repeatedly validate the rejected
proposal, append another rejection, or invoke a provider. Changed evidence may
start a new proposal after the configured social review interval, or at the
next hour, whichever comes first. A new hourly round may reconsider unchanged
evidence. Existing daily call and model budget limits still apply. A rejected
response is never rehabilitated as a new proposal.

## Paid success and explicit retries

Uncommitted successes and explicitly prepared retries on the same route retain
their original business purpose across an hour change. Valid successful results
can be committed without requiring another daily model-call allowance. Current
lifecycle, evidence, relationship, withdrawal, and contact-route validation
still applies; recovery cannot move a proposal to another room. Explicitly
reconciled success evidence may contain only the proposal content. Recovery
verifies its stored hash and schema without inventing usage/cost fields or
settling the underlying unknown attempt reservations.

Pre-upgrade successes need no database migration or fabricated historical
rejection. They retain current-evidence validation; an actual rejection during
recovery creates its first receipt at that time. Historical prompts are not
reconstructed. Legacy unresolved calls without enough route information stay
fenced until their normal reconciliation path resolves them.

New requests use the shared stable business-request identity and router fence.
Every unresolved social call or active attempt is checked across hourly rounds
and routes. Preparation checks again inside the model ledger transaction, and
a per-subject process lock serializes social workers. The new physical call may
pass its own preparation check, while any other prepared call stays fenced.
An authorized retry cannot move from its original physical call to a different
gateway or resource merely because the gateway configuration changed.

No unknown call, attempt, fee reservation, or external delivery is automatically
settled, released, or resent. Recovery neither sends a message directly nor
changes delivery reconciliation. Already dispatched provider work cannot be
undone. The lane lock is not a global lock over every model caller or over
external network operations.

## Verification

Use local provider fixtures to cover repeated rejection, changed evidence and
cooldown, hourly rollover, daily limits, paid-result crashes, old keys, explicit
retry and closure, gateway changes, concurrent workers, preparation races,
withdrawals, real restart, encrypted event archiving, and corrupted evidence.
Both direct and routed gateways must preserve their retry boundaries. Detailed
audit evidence and raw test output remain outside the public source tree.
