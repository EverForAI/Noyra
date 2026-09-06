# M41 Remediation Verification Foundation

M41 establishes the test contracts needed to remediate the findings in
`docs/audit/2026-08-15-full-readonly-audit.md`. Its baseline is `main` at `87e059a`.
It does not change any P1/P2 production behavior and does not close an audit finding.

## Verification assets

| Asset | Contract |
|---|---|
| `tests/fixtures/historical` | Authentic schema 6, 16, 21, 27, and 31 databases built from the commits that shipped those schemas; gzip and database SHA-256 values are pinned in the manifest |
| `tests/support/historical.py` | Expands fixtures only under a caller-owned temporary directory and rejects changed bytes |
| `tests/fixtures/synthetic/profiles.json` | Deterministic smoke, large, and soak profiles with 128, 10,000, and 100,000 events and memories |
| `tests/support/synthetic.py` | Builds valid event, training provenance, event-chain, memory, and memory-revision rows in bounded transactions |
| `tests/support/faults.py` | Named failpoints, explicit state-transition traces, deterministic thread gates, and real SQLite write-lock contention |
| `tests/contracts/remediation_acceptance.json` | Machine-checked issue origin, gate, closure state, independent subcontracts, and required evidence for all 39 findings |
| `scripts/audit-m41.py` | One Windows/Ubuntu entry point that redirects temporary data and tool caches under `.runtime/m41` |

The historical fixture builder is `scripts/build-historical-fixtures.py`. It uses Git history and
must be run deliberately when a fixture source changes. Normal test and audit runs only read the
compressed fixtures and materialize databases in a temporary directory.

The schema 31 fixture now migrates through schema 33. Migration startup preflights the marker before
current-schema DDL, rejects future markers without changing database bytes, and creates a verified
pre-migration backup for every older database. An injected migration failure restores the marker,
anchor evidence, and SQLite integrity check from that backup. The schema 32
`secret_cleanup_queue` migration and schema 33 `behavior_log_revisions` migration are idempotent
and checked by the optional-feature gate; schema 33 baselines legacy behavior rows in the same
migration transaction and refuses to auto-repair missing current-schema evidence.

## Repeatable entry points

Windows:

```powershell
./scripts/audit-m41.ps1 --scope targeted --profile smoke
./scripts/audit-m41.ps1 --scope full --profile smoke
./scripts/audit-m41.ps1 --scope targeted --profile large
```

Ubuntu:

```bash
bash ./scripts/audit-m41.sh --scope targeted --profile smoke
bash ./scripts/audit-m41.sh --scope full --profile smoke
bash ./scripts/audit-m41.sh --scope targeted --profile large
```

`soak` is opt-in because it creates 100,000 events. The runner sets `TEMP`, `TMP`, `TMPDIR`, Python,
Ruff, Mypy, and pip cache roots below `.runtime/m41` and deletes the per-run directory on exit. The
full scope runs the focused contracts, full pytest suite, Ruff check and format check, Mypy,
compileall, pip check, and pip-audit. Deployment audit remains a separate release gate because it
also validates Docker and systemd when those runtimes are present.

## Issue acceptance matrix

The JSON matrix contains the full claims and evidence types. This table is its human review index;
an issue stays open until every listed sub-contract is independently verified.

| Issue | Gate | Independent contracts |
|---|---|---|
| P1-01 | A | prepared-work disposition (`A`); honest sleep transition and progress (`B`) |
| P1-02 | C | production scheduling (`A`); complete registry and safe-pause policy (`B`) |
| P1-03 | C | valid archive verification (`A`); unavailable-key versus corruption classification (`B`) |
| P1-04 | B | explicit ownership/count reconciliation (`A`); child and revision completeness (`B`) |
| P1-05 | B | consent CAS/history (`A`); in-flight revocation at publication (`B`) |
| P1-06 | B | writer ownership through shutdown (`A`); post-cancel write prohibition (`B`) |
| P1-07 | B | bounded memory/bytes (`A`); temp placement and cleanup on every exit (`B`) |
| P1-08 | A | public allowlist (`A`); alias/nesting privacy diff (`B`) |
| P1-09 | D | bounded vector candidates (`A`); retrieval quality floor (`B`) |
| P1-10 | D | embedding budgets/ledger (`A`); timeout circuit and fatigue accounting (`B`) |
| P1-11 | D | keyring/rotation (`A`); cloud read-through and local GC (`B`) |
| P1-12 | C | execution/artifact digest chain (`A`); executable output validators (`B`) |
| P1-13 | A | no false per-use approval boundary (`A`); legacy flag fail-closed and ordinary use ledger (`B`) |
| P2-01 | B | subject workspace root (`A`); symlink/reparse containment (`B`) |
| P2-02 | D | append-only reconciliation (`A`); behavior revision integrity/export (`B`) |
| P2-03 | D | streaming search/browser bounds (`A`); transport-wide response limits (`B`) |
| P2-04 | D | SMTP DNS pinning (`A`); timeout/duplicate-delivery semantics (`B`) |
| P2-05 | D | provider lookup (`A`); authorized manual reconciliation (`B`) |
| P2-06 | D | live embedding revoke (`A`); secret repair queue (`B`) |
| P2-07 | D | model/embedding DNS pinning (`A`); embedding response bounds (`B`) |
| P2-08 | D | package collision rejection (`A`); exact envelope provenance (`B`) |
| P2-09 | D | advisory cognition integration (`A`); read-time trust verification (`B`) |
| P2-10 | D | reject future schema before writes (`A`); backup/failure/restore migration matrix (`B`) |
| P2-11 | D | thought outcome mapping (`A`); behavior replay simulation (`B`) |
| P2-12 | D | active-time clock (`A`); minimum duration/bounded abandonment (`B`) |
| P2-13 | D | atomic staged publication (`A`); platform no-follow operations (`B`) |
| P2-14 | D | at-rest boundary (`A`); key lifecycle and restore (`B`) |
| P2-15 | D | locked install profiles (`A`); configured-cloud fail-fast (`B`) |
| P2-16 | B | bounded read snapshots (`A`); asynchronous compatibility routes (`B`) |
| P2-17 | D | indexed cursor pagination (`A`); concurrent page stability (`B`) |
| P2-18 | D | subject slug ingress validation (`A`); independent storage-key containment (`B`) |
| P3-01 | Research | prototype validators (`A`); measured prediction/experiment outcomes (`B`) |
| P3-02 | Research | knowledge sync/revocation (`A`); explicit private-safe evaluation (`B`) |
| P3-03 | Research | affect ablation (`A`); long-run stability envelopes (`B`) |
| P3-04 | Research | pinned memory benchmarks (`A`); long-run quality/cost curves (`B`) |
| P3-05 | Research | Windows lifecycle (`A`); signing and rollback (`B`) |
| P3-06 | Research | safe operator controls (`A`); privacy-safe health surface (`B`) |
| P3-07 | Research | complete OpenAPI inventory (`A`); runtime drift rejection (`B`) |
| P3-08 | Research | immutable dependency inputs (`A`); signed release provenance (`B`) |

## Closure gate

Changing an issue to `verified` requires all of its contracts to have focused regression evidence,
the full test and static gates, and a read-only review of row counts, relevant integrity chains,
artifact hashes, and public API fields. P1 changes additionally require the applicable crash,
cancel, concurrency, large-data, restore, Windows, and Ubuntu evidence. A passing happy path cannot
close a compound issue.

M42 has verified the independent P1-01, P1-08, and P1-13 Gate A contracts, the P1-02 production
integrity-registry contract, the P1-03 archive-aware event-integrity contract, plus the P1-04 runtime
export ownership graph, P1-05 training consent CAS/publication boundary, P1-06 export worker
ownership/cancellation boundary, P1-07 training-export resource/storage boundary, P2-01 subject-scoped
workspace, P2-02 append-only behavior reconciliation, P2-03 bounded HTTP responses, P2-10
migration-safety contracts, and P2-16 export snapshot/request-worker contracts. P1-13 deliberately
keeps the product's autonomous-use semantics and does not add an approval schema. Future per-use
approval, if ever required, must be a new separately scoped contract rather than a revival of the
legacy string check.

## P1-02 production integrity implementation

P1-02 is `verified`. The built-in registry now contains 37 versioned checks and runs before startup
recovery, on the bounded periodic watchdog schedule, and through the manual operator path. Production
execution uses a spawned child process, an OS-enforced SQLite read-only URI, one shared read snapshot,
wall-clock deadlines, shutdown checkpoints, and bounded join/terminate/kill cleanup. A stalled checker
cannot keep the service writer or subject lock alive after the audit deadline.

Findings persist through the watchdog state and quarantine boundary. Alert mode records findings without
mutating lifecycle state. Pause mode converts proven P0 corruption into the durable safe-pause path;
restart, subject changes, partial reports, and registry-version upgrades cannot silently clear an
unresolved quarantine. P1 resource and availability outcomes remain distinct from P0 corruption.

The final table-coverage audit ran SQLite's authorizer over a healthy schema-33 manual registry run.
All 37 checks were `ok`, 109 tables were read, and all 11 previously missed durable tables were reached:
`audit_records`, the three autonomous-project resource/assistance/sleep tables, cognitive resource group
revisions and key events, fatigue transitions, observation content segments, and the three training
policy/record/export tables. Their owners now validate strict scalar types, canonical JSON, hashes,
subject ownership, history/current-state reconciliation, reverse foreign ownership, and orphan rows.
Epistemic reviews and search-provider uses also query from referenced subject evidence/actions so a
foreign row cannot hide by carrying another subject ID.

Evidence: 122 focused P1-02 tests; 183 related-domain tests; M41 smoke, large, and soak profiles with
274 tests each; the soak profile generated 100,000 events and 100,000 memories and required event
payloads, event chain, causal order, and mind state to remain `ok`; 600 full-suite pytest tests;
deployment audit 163 passed in both normal and coverage runs at 73.00% focused coverage; and passing
Ruff, format, Mypy (184 source files), compileall, pip check, UTF-8 pip-audit, and `git diff --check`.
Exact contracts are recorded in `docs/audit/2026-08-15-remediation-m42-p1-02.md`.

P1-12 remains `open`: the project-execution revision, result, artifact bytes, and acceptance evidence
still need one verified digest chain plus executable output-type validators. P1-02 therefore closes the
integrity-registry issue without restoring an overall `Complete` label to M40.

## P1-03 archive-aware event integrity implementation

P1-03 is `verified` after bounded archive/manifest fault injection, consistent-snapshot concurrency,
restore, static, deployment, dependency, and independent read-only gates. `LongRunResilience` now
checks events within one SQLite read transaction. Database-only manifest closure is verified before
the archive key is opened, so missing segment metadata, invalid counts, time ranges, tombstones, and
archive timestamps cannot be hidden by a missing key.

Encrypted segment reads are capped before allocation and decompressed under explicit per-segment and
total budgets. Segment and event cursors have fixed materialization limits, hot payloads have per-row
and total byte limits, and the archive lookup uses an indexed subject/key/time path. Current and legacy
segments reconcile manifest rows, payload hashes, event IDs, timestamps, and restored content without
changing event-chain roots. A concurrent archive commit is observed as one complete old or new snapshot,
never a mixed false P0.

Missing/wrong keys, temporary provider failures, and synchronous verification limits are `degraded`
P1 states. Authenticated corruption with matching key evidence, invalid manifests, missing objects,
bad tombstones, noncanonical JSON, or retained-hash mismatch is `corrupt` P0. Ambiguous fingerprint plus
ciphertext failure and unauthenticated data beyond the read envelope remain conservatively degraded;
P1-11 keyring history is required to disambiguate them.

Evidence: 35 focused P1-03 tests; M41 smoke and large profiles with 152 tests each; 425 full-suite
pytest tests; deployment audit 41 passed at 72.64% focused coverage; Ruff, format, Mypy (180 source
files), compileall, pip check, pip-audit, `git diff --check`, and independent final review. Exact
contracts are recorded in `docs/audit/2026-08-15-remediation-m42-p1-03.md`.

## P1-04 runtime-export implementation

The P1-04 implementation is `verified` after the static, deployment, and read-only release gates.
Schema 33 uses ownership graph version 1;
it has 98 direct `subject_id` tables, 28 revision/child tables joined through an explicit parent key,
two common-knowledge custom predicates (publisher/import reachability), a `schema_meta` allowlist that
exports only `schema_version`, and six rebuildable FTS tables marked `skipped`. The graph is executable:
missing or unexpected tables, a direct-subject classification mismatch, an undeclared parent foreign key,
or a parent that is not directly subject-owned fails closed.

Each exported table is cursor-read under the subject predicate and reconciled against a separate manifest
inventory entry. The manifest records expected and exported row counts, `referential_gaps`, ownership,
and skipped reasons; any count mismatch or cross-subject foreign-key reference aborts the archive before
the audit record is written. This prevents the old same-column `_id` collision from selecting another
table's revision set and avoids variable-sized `IN (...)` ownership lists.

Evidence: 11 focused P1-04 tests, 44 related regressions, M41 smoke and large profiles with 72 tests each,
345 full-suite pytest tests, deployment audit 40 passed at 72.07% focused coverage, and passing Ruff,
format, Mypy, compileall, pip check, pip-audit, and read-only graph/manifest review. The contracts include two-subject collision isolation,
all 28 parent families and common-knowledge reachability, explicit cross-subject rejection, a 1,100-row
parent/revision history, unregistered-table fail-closed behavior, and schema 6/16/21/27/31 historical
fixtures migrated to schema 33 with anchor and inventory reconciliation. Windows evidence is local; the
Ubuntu entry is reproducible, but no Ubuntu/WSL distribution or systemd runtime was available for a
host execution in this run.

## P2-16 export snapshot and request-worker implementation

P2-16 is `verified` after the isolated-backup, stale-image, WAL-checkpoint, asynchronous-route,
static, deployment, and read-only API gates. Runtime and training exporters close the live SQLite
source after a bounded backup and stream from a query-only same-volume image. A lock sidecar and
conservative stale-image scavenger protect active exports and recover crash leftovers; storage
quota accounting includes those images while they exist. Archived event and observation payload
metadata are resolved through the snapshot connection.

The explicit export-job API is canonical. Authenticated legacy runtime/training GET routes enqueue
the same bounded job and return `202` JSON, so a large archive cannot occupy an HTTP request worker.
Evidence and exact final gate counts are recorded in
`docs/audit/2026-08-15-remediation-m42-p2-16.md` and the machine-readable acceptance matrix.

## P1-05 training consent implementation

P1-05 is `verified` after deterministic concurrency, stale-version, publication/revocation,
pending-file failure, job visibility, append-only audit, static, deployment, and read-only gates.
Policy changes now read and update under one `BEGIN IMMEDIATE` transaction with a version predicate;
HTTP callers must supply `expected_version`, and a stale request returns `409` without a write.
Every committed version records complete before/after state in a database-enforced append-only audit
chain.

Training ZIPs remain hidden pending files until a final write transaction proves the entire consent
lease and version are unchanged. Revocation that linearizes first destroys the pending file, writes an
abort audit, and leaves no final artifact, success row, or downloadable job. Evidence is in
`docs/audit/2026-08-15-remediation-m42-p1-05.md`. The subsequently verified P1-07 contract adds
long-history resource and temporary-volume bounds without weakening this consent boundary.

## P1-06 export worker ownership implementation

P1-06 is `verified` after deterministic cancel/publish ordering, shutdown/lock sequencing,
pre-ownership restart, scoped file recovery, snapshot cancellation, static, deployment, and focused
review gates. Each background job now owns a cooperative `ExportControl`; its publication decision
lock serializes cancellation against the transaction that renames the artifact, completes the job,
and appends runtime/training success rows.

Service shutdown cancels and joins all export futures before releasing the subject process lock.
Restart recovery is no longer a constructor side effect: it runs for one subject only after
`SubjectKernel.boot()` owns the process lock, removes only interrupted job files, and preserves
completed/cross-subject/outside-root artifacts. Evidence and exact gate counts are recorded in
`docs/audit/2026-08-15-remediation-m42-p1-06.md`. The subsequently verified P1-07 contract reuses
these cancellation and ownership checkpoints for bounded long-history export work.

## P1-07 training export resource-bound implementation

P1-07 is `verified` after focused memory/disk contracts, M41 smoke and large profiles, the full
suite, static and deployment gates, and independent read-only review. The streaming path now uses
an exact disk-backed SQLite fingerprint index with fixed cache/mmap settings and actual file
high-water accounting. Episodes flush on event count, elapsed span, or serialized bytes. Every
JSONL record and shard has an explicit limit, and the v3 manifest records each shard's rows, bytes,
and SHA-256 digest.

The production work root is `data_root/exports/work`; any custom work root, final archive, or
pending archive is restricted to the managed `data_root/exports` tree. Work files, dedup pages, snapshot images, archive
growth, subject quota, and free-space headroom are checked while data is copied or written.
Stored event/model payloads and cold archive segments are rejected before unbounded materialization;
SQLite progress handlers preserve cancellation even when an indexed scan yields no matching rows.
Owned work, snapshot, dedup, and pending files are removed on success, cancellation, exception,
and ENOSPC. Ownership-gated restart cleanup removes work and pending archive orphans; snapshot
orphans remain governed by the P2-16 lock-aware stale-image scavenger and its grace period.

Evidence: 24 focused P1-07 tests; M41 smoke and large profiles with 117 tests each; 390 full-suite
pytest tests; deployment audit 41 passed at 72.64% focused coverage; and passing Ruff, format,
Mypy (179 source files), compileall, pip check, pip-audit, `git diff --check`, and read-only review.
Exact contracts and platform limits are recorded in
`docs/audit/2026-08-15-remediation-m42-p1-07.md`. Windows evidence was executed locally; no
Ubuntu/WSL distribution or systemd runtime was available, so that host gate remains assigned to
CI/release execution. P1-09/P1-10/P1-11/P1-12 and other adjacent findings remain independent and open.
