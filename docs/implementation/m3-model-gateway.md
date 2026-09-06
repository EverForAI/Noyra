# M3 Remote Model Gateway

## Boundary

The model is an untrusted, replaceable cognition component. It does not own Noyra's identity,
budgets, lifecycle, state commits, or action permissions. M3 contains remote HTTP provider code
only; it does not install or run a local model.

## Durable call protocol

1. Validate messages and the requested Pydantic output schema.
2. Create one logical call with a subject-scoped idempotency key.
3. Estimate a conservative input-token upper bound from UTF-8 bytes.
4. Authorize each physical HTTP attempt in one SQLite write transaction.
5. Record the attempt as executing before network I/O.
6. Validate provider JSON and local structured output.
7. Commit usage, cost, response hash, and the terminal call state.

Logical calls and physical attempts are separate records. Retries therefore consume the daily
call budget and retain their own token and cost accounting.

## Failure policy

- Connection failure before a request is sent may retry within the configured bound.
- HTTP 429 may retry and records zero token use for that attempt.
- HTTP 408 and 5xx may retry, but retain their full reservation because billing is uncertain.
- Read/write ambiguity, cancellation during I/O, and process interruption become `unknown` and
  are never retried automatically.
- Invalid structured output may retry only within the explicit bounded policy.
- A crash before network send cancels the reservation; a crash during I/O retains it.

## Privacy

API keys are represented by `SecretStr`, are inserted only into the HTTPS authorization header,
and are never written to SQLite. Provider response bodies and transport exception details are not
copied into public error codes. Model responses remain private state and are protected with a
content hash; no public API is introduced in M3.

## Budgets and fatigue input

Hard UTC-day limits cover physical attempts, input tokens, output tokens, and micro-US dollars.
Concurrent reservations are serialized by SQLite. Missing usage and ambiguous attempts retain
their conservative reservation. `BudgetStatus.pressure` exposes a normalized 0-1 resource signal
for the later fatigue and sleep module; it does not itself define an emotion.

## Deployment

The gateway uses HTTPX with redirects disabled, bounded connection pools, explicit timeouts, and
environment proxy trust disabled by default. Windows and Ubuntu run the same provider and ledger
tests. Ubuntu is the production target for continuous operation.
