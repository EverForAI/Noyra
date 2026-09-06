# M5 World Observation, Prediction, and Genesis

## Purpose

M5 gives Noyra a read-only path into the public web, durable provenance for observations, a
revisioned world model, precommitted forecasts, and a deterministic genesis-orientation protocol.
Web content is evidence, never an instruction channel. This module does not yet schedule
observation cycles or perform the reflective sleep required to finish genesis; those belong to the
runtime and sleep modules.

## Network boundary

Only canonical HTTPS URLs on port 443 are accepted. Credentials, fragments, credential-like query
keys, literal private addresses, non-public DNS results, redirects, unsupported media, and decoded
responses over the configured limit are rejected. The default client ignores proxy environment
variables and checks the connected peer address. A custom transport must explicitly disable peer
verification, which is intended only for controlled tests.

DNS is resolved before each request and every returned address must be globally routable. The
actual connected peer is checked again to reduce DNS-rebinding exposure. The reader does not follow
redirects, execute JavaScript, submit forms, authenticate, or write to the remote service.

HTML scripts and non-content elements are removed. Prompt-injection patterns are recorded as risk
signals, but all extracted text remains untrusted observation data regardless of whether a pattern
matches.

## Provenance and world state

Every accepted document produces one private `world_observation` event and one immutable-content
observation. Canonical URL, source, event, content hash, media type, fetch metadata, and injection
signals are bound by a record hash. Duplicate content from the same source is idempotent.
Observation processing status is derived from an append-only transition history.

Sources have revisioned trust and lifecycle state. Claims require one or more observations from the
same subject and preserve each revision with its evidence. Causal links connect evidence to claims
and predictions. The integrity audit checks hashes, revision continuity, cross-subject boundaries,
status histories, current rows, and foreign keys.

## Forecast discipline

A prediction is recorded before its target time with a statement, explicit probability, target,
resolution criteria, evidence, and rationale. A false outcome cannot be recorded before the target
time. Resolution appends a review and calculates the Brier score; cancellation remains distinct
from an incorrect forecast. Every logical creation has a durable idempotency key.

## Genesis orientation

Genesis follows this state machine:

```text
created -> observing -> interpreting -> forecasting -> goal_seeding
              ^                                      |
              +--------------------------------------+
goal_seeding -> ready_for_sleep -> complete
```

Each committed cycle must cite new observations plus real appraisals and predictions owned by the
same subject. Optional goals can be included once they emerge. Completion is impossible until the
minimum cycle count is met and a later sleep module supplies a durable sleep reference. Transition
and cycle histories are append-only and version-checked.

## Current limitation

The HTTP client validates DNS answers and the connected peer, but it does not pin a TLS connection
to the pre-resolved IP. This is a defense-in-depth boundary, not a general-purpose secure browser.
Authenticated browsing, active tools, publishing, and other external side effects remain outside
M5.
