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
   hourly limits; model discovery uses the model ledger and daily research-call limit. An empty or
   unavailable API/model route can try the lightweight search surface once per run, retaining both
   attempts and their outcomes. An unknown action is never automatically reissued.
6. When another round is allowed, ask the model whether the first result set is sufficient and
   permit a switch to a different available method.
7. Register distinct canonical HTTPS URLs as candidates with initial trust `0.35`, reusing an
   existing source identity and respecting operator blocks. Search does not require any existing
   active source. Model-suggested URLs and search snippets remain unverified candidates.
8. Trial-read candidates using the existing public reading capability. Trial permission is explicit:
   the ordinary reader and observation API still require active sources. A trial needs an active,
   subject-owned autonomous goal, rechecks authorization and the runtime lease before dispatch and
   evidence commit, and records the actual page as an observation before settling its action.
9. After successful reads, Noyra may follow useful sources, hold one-off evidence, abandon a
   candidate, or select a bounded set of links actually present in the pages. Following is an
   evidence-backed source revision; reading alone never enables ongoing polling. A subject can
   also stop following a source it previously chose; operator-controlled revisions take precedence.
10. Read selected links at most one level deep, sharing the same per-run read budget with search
    candidates. No JavaScript browser, topic whitelist, default domain whitelist, or per-page human
    approval is added. Optional host-scoped operator grants still apply to every request.
11. Append an immutable research record containing method choices, discovery counts, read
    attempts, observation/action receipts and attention decisions. New runs use
    `noyra-research-reading/v1`; accepted source IDs require clean actual-read receipts. Both
    research integrity checks and project acceptance validate those links. Historical records keep
    their original discovery-only contract and are not rewritten or presented as fetched evidence.
    Raw queries remain private and are represented durably by hashes.

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

- Only absolute public HTTPS URLs are accepted. Trial reads use the same DNS pinning, public
  address validation, redirect rejection, media/response limits, and injection classification as
  regular reads. Extracted links are candidates and receive the same checks when actually read.
- Injection-signaled pages can be recorded as untrusted observations, but cannot be promoted,
  supply continuation links, or count as accepted research evidence. Model attention choices may
  name only the sources and links in the supplied actual reads.
- At most `NOYRA_MAX_RESEARCH_READS_PER_RUN` requests are admitted for trial reading. Capability
  rate limits, project action envelopes, the existing research daily model-call limit, and the
  gateway's token/cost budgets remain in effect. Reading assessment uses bounded local excerpts;
  identical extracts are labeled as duplicates rather than independent corroboration.
- Empty or failed reads never become accepted evidence. Unchanged content updates the poll
  heartbeat without a new observation, reading assessment, or productive research result. Failed
  checks cool down; backward clock movement keeps the cooldown closed.
- Restart recovery uses action-linked observations and model ledger results. A terminal trial does
  not send another request; an interrupted trial remains unresolved/unknown rather than retrying.
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
- `NOYRA_MAX_RESEARCH_READS_PER_RUN` (default 4, including chosen page links)

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

## Verification boundary

The B1 regression suite uses a fixed synthetic web world to exercise no-API search, cross-domain
reading, explicit source following, one-level limits, forged choices, capability revocation, blocked
sources, private DNS, injection, empty/unchanged content, restart recovery, project evidence and
runtime exports. It does not register external accounts or claim that live search availability,
production deployment, or long-running internet exploration has been verified. Search providers
and ordinary websites can still be unavailable or reject automated requests.
