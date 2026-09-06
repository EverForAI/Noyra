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
`api_keys`, and optional per-group budget/pricing/retry values.
