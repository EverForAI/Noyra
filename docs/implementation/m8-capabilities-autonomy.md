# M8 Capabilities, Real Actions, and Autonomy Loop

M8 adds the boundary between an internal decision and a real external effect. A capability is an
operator-owned grant with an explicit type, resource scope, expiry, hourly rate limit, and side-effect
flag. The subject may choose whether to use a grant but cannot create or revoke the grant itself.
Per-use human approval is deliberately not part of this product contract; the operator grant and
its revocation are the authorization boundary. Legacy approval-required rows fail closed and must
be revoked and re-granted.

## Capability enforcement

Every use is recorded in an append-only ledger and may reference the action that consumed it.
Filesystem scopes use resolved root containment; public-web scopes use exact HTTPS host allowlists.
Rate-limit authorization and use insertion occur in one immediate SQLite transaction. Revocation
does not delete history.

The first tool surface deliberately excludes shell execution. It supports bounded UTF-8 reads,
atomic UTF-8 writes inside an authorized root, and public-source reads through M5's SSRF-resistant
`SafeWebReader`. Tool intent, start, result, resource cost, and privacy-filtered behavior log are all
committed through the existing action ledger. A failed authorization cancels the prepared action;
an uncertain write is marked `unknown` so it is never blindly repeated.

## Autonomy loop

`AutonomyLoop` provides a bounded async heartbeat suitable for a 24/7 supervisor. It serializes
ticks, respects lifecycle sleep states, requests sleep when fatigue requires it, applies a minimum
interval, and backs off after errors. With no active hook it records only a heartbeat; it does not
invent user tasks. A cognition/orchestration hook can later choose world observations, goals, or
interactions, but all resulting actions still pass through the same grant and action boundaries.

M8 supplies capability, filesystem, public-web, and scheduling primitives. Account-specific
publishing, email, social channels, wallets, and high-impact device control remain adapters with
their own grants; they are not implicitly enabled by this module.
