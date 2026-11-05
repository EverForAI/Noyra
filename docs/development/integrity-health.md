# Integrity health and read-only diagnosis

`/health/live` remains process liveness. `/health` and the detailed authenticated
readiness endpoint expose separate integrity and integrity-watchdog components.
The controller reports observed findings, evidence availability and accumulated
coverage. The main-service watchdog snapshot reports `not_checked`, `ok`,
`backoff`, `escalated` or `evidence_unavailable` without asking the HTTP thread
to load the watchdog database state.

A current watchdog/evidence transition invalidates a cached health result.
An old green cache cannot hide a newly failed checker or persist after its
recovery. Public health remains a compact service/status response; check details
and private error metadata stay behind the existing operator authentication.
The management overview includes current controller status and coverage without
mutating the observer's cached data.

Readiness now returns 503 when an enabled checker has no completed observation,
is rebuilding evidence, has findings, or its watchdog is unavailable/in backoff.
It returns 200 when the existing storage/encryption/cloud gates and observed
integrity checks are ready. Partial registered coverage is shown explicitly;
clean startup coverage is not described as a complete deep audit. A watchdog
that has not entered the loop yet is shown as `not_checked` while a valid startup
controller result can still satisfy readiness. `off` remains explicitly disabled.

This is an operational health contract, not a change to action policy:
`alert` does not automatically pause for findings and `pause` still quarantines.
The existing loop protection on checker exceptions remains fail-closed. A health
request does not reopen admission, retry model calls, create goals or send
anything. In pause mode incomplete evidence reconstruction also prevents
operator resume/wake and boot completion until the required checks finish.
A deployment using readiness for routing should account for the more accurate
503 response even in alert mode; management diagnostics remain accessible.

## Local evidence diagnosis

Use the installed application Python from the same release:

```sh
python scripts/diagnose-integrity-evidence.py SUBJECT_INTEGRITY_DIRECTORY
```

This validates the current state and latest derived view, returns bounded
metadata and counts, and performs no SQLite access in evidence-only mode.
It neither repairs nor deletes evidence and has no resume/clear-quarantine path.
A missing or unreadable derived view may still produce a diagnostic exit code
of 2 while the verified state commit remains authoritative for the controller.

To perform a limited independent core check, provide an explicitly sealed,
journal-free SQLite copy and its subject ID:

```sh
python scripts/diagnose-integrity-evidence.py SUBJECT_INTEGRITY_DIRECTORY \
  --snapshot SEALED_DATABASE_COPY --subject SUBJECT_ID
```

The copy must contain all committed data and have no concurrent writer. Do not
copy only a live main file while its WAL is active. Snapshot mode refuses visible
SQLite journal sidecars and is opened read-only/immutable without initialization.
The five selected checks cover schema contract, SQLite quick-check, foreign
keys, identity continuity and event-chain tail, with fixed time/row/byte limits.
It does not run provider/network checks or the complete deep registry. The output
marks this scope and omits private payloads/exception text. Existing live evidence
and lifecycle are unchanged, including when a real schema trigger is damaged.
A successful snapshot diagnosis does not clear a finding on the running instance.
Resolve the cause and let its owned controller complete the required audit.

The same-file commit is compatible with verified legacy sidecars on read.
Rollback to older writers needs coordinated handling of evidence and runtime
state: an old writer can rewrite a new envelope without updating its marker.
New readers will reject that mismatch rather than treat an old green report
as current evidence. Do not edit checksums or remove findings to force readiness.
