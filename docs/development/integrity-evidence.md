# Integrity evidence publication

The subject database remains the source of subject state. Integrity reports are
local evidence about the checks that ran, their snapshot and their scope; a
clean startup profile does not establish that every deep check has run.

## One verified state commit

`watchdog-state.json` is now the authoritative integrity-controller commit. It
contains the current report, active findings, scheduling state and coverage in
one file. A reserved `_noyra_integrity_commit` marker stores a version and the
hash of the remaining payload. The temporary file is flushed before one atomic
replacement; POSIX also flushes the parent directory. A failure before replace
leaves the old generation; process exit after replace leaves the new generation
readable without another file. Failed publication/readback makes evidence
unavailable and schedules reconstruction instead of announcing a clean result.

This hash detects mismatched or damaged evidence; it is not a signature or an
authentication boundary against someone who can rewrite the private evidence
and recompute its hash. Existing filesystem access and runtime ownership
boundaries still apply.

`latest-report.json` and per-run reports are derived views. They are published
only after the state commit. Failure to publish a view cannot replace the
current report with an older green report or erase its active findings.
History pruning remains bounded and does not prune the state commit.

Legacy files without the marker still require their matching `.sha256` sidecar.
Missing sidecars are unavailable evidence; invalid pairs are corrupt evidence.
Files with a marker must validate that marker and never fall back to a legacy
checksum. New writes produce compatibility sidecars where possible, but new
readers do not depend on their publication. A new reader can use a committed
file even if its sidecar is absent. Do not treat a missing legacy checksum as
permission to trust a legacy payload.

## Evidence, findings and coverage

The evidence reader distinguishes missing files, unreadable evidence and invalid
evidence. Invalid local evidence is not itself proof that subject data is
corrupt. A controller with no completed checks reports `not_checked`. Lost or
unusable state reports `evidence_unavailable` and a pending recheck. Valid
historical findings may be retained conservatively; a historical clean report
cannot declare that the current state is healthy.

An explicit `coverage` projection records which current check versions have a
non-incomplete result, and lists checks not yet covered. Coverage accumulates
across audit snapshots and is not a claim that all data passed simultaneously.
Known P0 findings survive unrelated clean shards and incomplete rechecks; only
valid results from the relevant checks can resolve them.

A registry upgrade or damaged/lost commit starts a guarded reconstruction.
The startup path attempts the complete current profile. If that attempt cannot
finish, persisted coverage and the cursor allow bounded remaining shards to
continue after restart rather than repeatedly starting the whole audit from
zero. An empty or partial registry selection cannot finish reconstruction.
Unknown findings from the previous registry are retired only after the current
registry is fully covered. Ordinary scheduling and its bounded incomplete-shard
retries remain in use.

The existing `off`, `alert` and `pause` action policies remain distinct. Evidence
reconstruction does not authorize automatic resumption, replay model calls,
send messages, or clear a proven corruption condition. Policy-driven pause
remains in effect until its required evidence is available.
