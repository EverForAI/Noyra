# M7 Equal Interaction and Public Projection

M7 treats human messages as invitations to communicate. Receiving a message creates an event and
an `offered` interaction; it does not create a goal, action, model call, or permission. The subject
may accept, defer, reject, or remain silent. Only the subject actor can make that decision.

Subject-initiated messages and help requests are separate outgoing interaction records. They are
idempotent, durable, and still require a later channel adapter to deliver them. M7 does not turn a
message into an external side effect.

## Privacy boundary

The anonymous `PublicProjection.state()` contract is the versioned `PublicSubjectStateV1` schema.
It contains only a stable subject identifier, one display name, a coarse lifecycle state, an
explicitly allowlisted online signal, and the public diary count. It deliberately omits lifecycle
reasons, versions, timestamps, fatigue, plans, projects, missions, consciousness, model resources,
and all private cognition details. `GET /api/state/details` is the read-authenticated route for
detailed operational state. Public diary entries, safe behavior-log fields, and outgoing public
interactions remain separate projections. No public projection method accepts a mutation request.

## Public diary

Diary publication is subject-only and requires a completed sleep run. When a sleep reflection has a
public candidate, publication must match that exact candidate. Entries are immutable and cannot be
deleted through the API or by a normal SQLite write. This separates a private reflection candidate
from a deliberate public act.

M7 provides storage and projection contracts rather than a web server. HTTP, WebSocket, and channel
adapters belong to the deployment and tools modules; they must call these contracts and must never
grant an incoming message command semantics.
