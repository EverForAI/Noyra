# M24: Consciousness and Isolated Cognitive Routing

M24 adds a persistent consciousness frame chain and two independent remote model
resource pools. Economy and deep pools have separate budgets, provider groups,
API-key health, cooldowns, and failover. A pool never falls back into the other
pool. When a pool is unavailable, the route is recorded and a deduplicated
waiting task is persisted so the cognition loop can continue without crashing.

Consciousness frames are append-only, hash chained, and recovered after restart.
Repeated idle results do not create duplicate frames. Public state exposes only
safe aggregates and the latest frame summary; authenticated runtime export
contains detailed histories but excludes secret files and redacts credentials.

Environment groups use `NOYRA_ECONOMY_MODEL_GROUPS_JSON` and
`NOYRA_DEEP_MODEL_GROUPS_JSON`. Each entry contains `label`, `base_url`, `model`,
`api_keys`, and optional per-group budget/pricing/retry values. Production
deployments should load the JSON through `NOYRA_ECONOMY_MODEL_GROUPS_FILE` or
`NOYRA_DEEP_MODEL_GROUPS_FILE` (or the matching `*_CREDENTIAL` setting) and use
`api_key_files` or `api_key_credentials` for provider keys. Inline `api_keys`
remain only as a development compatibility path; imported keys are moved into
the protected runtime secret directory and are excluded from SQLite and exports.
