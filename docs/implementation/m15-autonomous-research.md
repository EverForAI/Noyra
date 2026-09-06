# M15 - Autonomous Research and Source Discovery

## Scope

M15 lets Noyra autonomously identify an information gap in an active non-human goal, choose a
search method, inspect the returned candidates, and optionally switch method for one bounded
follow-up round. Operator-provided search APIs are capabilities only. They do not create goals,
queries, priorities, or instructions.

The supported configured resources are Brave Search, Bing Web Search, Tavily, and Serper. If no
search API is configured, the planner is shown only the model and browser fallback methods. The
browser method uses a lightweight public Bing RSS search surface, so it requires neither an API key
nor an extra model call. It discovers candidate links only and does not claim that a returned page
was fetched.

## Runtime Flow

1. Select active goals whose origin is not `human_proposal`.
2. Build a private context from goal state, recent non-interaction events, known sources, recent
   search hashes, and the metadata of active search resources.
3. Ask the remote model to choose `api`, `model`, `browser`, or `wait`.
4. Validate the selected goal, evidence IDs, provider ID, and duplicate-query boundary locally.
5. Execute a bounded search round. Configured API and browser searches use the action ledger and
   hourly limits; model discovery uses the model ledger and daily research-call limit.
6. When another round is allowed, ask the model whether the first result set is sufficient and
   permit a switch to a different available method.
7. Register distinct HTTPS hosts as candidate world sources with initial trust `0.35`. A candidate
   is not fetched evidence and must later pass normal source activation and observation controls.
8. Append an immutable research record containing hashes, method choices, result counts, actions,
   and accepted source IDs. Raw queries remain private and are represented durably by hashes.

## Configuration Panel

Authenticated operators can use the `配置` tab or these endpoints:

- `GET /api/config/search-providers`
- `POST /api/config/search-providers`
- `POST /api/config/search-providers/{config_id}/revoke`

Responses never include API keys or secret-file references. Keys are stored under
`NOYRA_DATA_DIR/secrets/search` as restricted files; SQLite stores only a reference and fingerprint.
Replacing a label revokes the previous configuration in the same database transaction. Revocation
removes secret access while retaining the immutable resource history.

## Safety and Recovery

- Only absolute public HTTPS URLs are accepted.
- API and browser search responses are streamed under a 64 KB header-field limit, a 2 MB identity-
  encoded body limit, a total deadline, disabled redirects, and bounded connection pools. Malformed
  JSON is treated as a failed audited action.
- API and browser search actions are idempotent. Successful replays restore stored normalized
  results without repeating the network request.
- Interrupted executing actions are quarantined by the existing action-ledger recovery path.
- A successful planner call that was not committed after a process failure is reused instead of
  spending another model call.
- Provider uses, provider revisions, and research runs are append-only. Unique planner-call and
  action-use indexes prevent duplicate durable commits.
- Human messages and human-proposed goals are excluded from research planning context.

## Limits

The operator controls frequency and resource ceilings through:

- `NOYRA_RESEARCH_INTERVAL_SECONDS`
- `NOYRA_MAX_RESEARCH_MODEL_CALLS_PER_DAY`
- `NOYRA_MAX_RESEARCH_CONTEXT_CHARS`
- `NOYRA_MAX_SEARCH_ROUNDS_PER_RUN`
- `NOYRA_MAX_SEARCH_RESULTS_PER_ROUND`
- `NOYRA_MAX_DISCOVERED_SOURCES_PER_RUN`
- `NOYRA_MAX_BROWSER_SEARCHES_PER_HOUR`

Exhausted model budget contributes directly to fatigue. Search API usage is additionally bounded by
the hourly limit saved with each configured resource.

## Audit

Windows:

```powershell
./scripts/audit-autonomous-research.ps1
```

Ubuntu:

```bash
./scripts/audit-autonomous-research.sh
```
