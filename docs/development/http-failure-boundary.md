# Unexpected HTTP failure boundary

Each handler-owned HTTP request attempt receives a fresh, server-generated
32-character hexadecimal `X-Request-ID`. Incoming headers cannot choose it.
Normal final responses include that ID; a controlled unexpected error includes
the same ID in its JSON body so an operator can locate the corresponding log.
Request IDs, header buffers and response state reset for every request,
including a reused keepalive handler.

## Responses before and after sending

Unexpected failures before response headers are written produce a fixed JSON
error: `internal_error` with HTTP 500, or `service_unavailable` with HTTP 503 for
storage/integrity/runtime availability failures. The response includes only
that error code and the request ID, uses `no-store` and security headers, and
closes the connection. It does not retry a mutation or consume arbitrary
remaining request data. Existing expected route errors retain their contracts.

A fallback discards any unsent headers from the failed response, including
cookies and success status. It does not call the route's JSON serialization or
business-header helper again. HEAD failures send headers without a body.

The handler marks response output as started before flushing a nonempty header
buffer. A partially written block counts as started. Once output has begun,
an unexpected failure closes the connection and records the failure without
appending a second status line, JSON error, or replacement document. A streamed
file can therefore remain an incomplete transfer; the server cannot retract
bytes already sent or claim that a completed write was rolled back.

The existing mutation, lifecycle, migration and recovery error paths also
observe this boundary. Known pre-response runtime errors keep their original
response payload; post-response invalidation records an aborted response and
closes instead. A completely written informational response such as HTTP/1.1
`100 Continue` still permits one final response. A failed partial informational
write remains conservatively started.
Connection resets, broken pipes and request timeouts continue to close the
connection without attempting a fresh error response.

## Diagnostics at the source

`http_failures.log_http_failure` passes only fixed method/route templates,
request ID, status/phase, a bounded exception-type identifier and bounded
module/function/line locations to logging handlers. It does not pass raw
exceptions, `exc_info`, request data, client addresses, exception messages,
source lines, locals or traceback filesystem paths. This is safe without
relying on the process-wide log-record redactor being installed first.

Setup and finish errors outside `handle_one_request` use the bounded server's
same source-safe diagnostic helper. They do not use socketserver's default
raw stderr traceback. An error while generating the fallback is logged once
and the connection closes; no recursive fallback is attempted. If logging
itself fails, it cannot escape into that raw traceback fallback.

This adds no authentication permission, proxy trust, retry authorization or
business-state mutation. A generic 500/503 alone does not prove that a prior
business operation was rolled back or is safe to repeat: existing idempotency
and reconciliation rules still determine that.

Local tests exercise buffered/partial responses, serialization and storage
faults, HEAD, unread POST/pipelines, keepalive, runtime leases, real state and
file routes, setup/finish, and a failing logger. Detailed audit evidence and
raw test output remain outside the public repository.
