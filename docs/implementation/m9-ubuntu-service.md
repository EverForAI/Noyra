# M9 Ubuntu Service and Public Dashboard

M9 turns the durable runtime into a deployable 24/7 process. `NoyraService` owns one subject kernel,
one bounded autonomy loop, and one local HTTP server. Startup recovers the previous lifecycle,
process termination releases the subject lock, and systemd or Docker restarts the process after a
failure.

The HTTP surface is intentionally small. Public GET endpoints expose health, the versioned
`PublicSubjectStateV1` identity/lifecycle/online projection, public diary entries, redacted behavior
logs, and explicitly public outgoing interactions. Fatigue and detailed operational state require
the authenticated `/api/state/details` read route. Authenticated POST requests create only private
incoming communication invitations.
The handler enforces a request-size ceiling, JSON content type, a constant-time bearer-token check,
strict input types, defensive response headers, and no browser storage.

The service supplies a deterministic fallback sleep reflection so an unattended subject does not
remain permanently in `winding_down`. It records counts and commits no model-proposed memory,
belief, goal, or personality changes. The sleep state machine then checkpoints, waits for the
configured deep-sleep deadline, and wakes. A richer model-backed reflection can replace the hook,
but deep sleep and waking remain model-free.

M9 does not yet choose world sources or call a model during active ticks. That cognition cycle is a
separate module so provider output cannot bypass observation provenance, capability grants, model
budgets, or Supervisor validation.
