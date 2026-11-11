# Workflow publication upgrade to schema117

This procedure supplements the deployment's existing supported upgrade and recovery
workflow. It is for an operator-controlled maintenance window; it does not authorize
restoring a workflow archive as a running subject or replaying external actions.

## Before starting

1. Use a reviewed release with its required checks and release evidence. Record the
   current and candidate release identities, database schema, and backup identity in
   private maintenance notes. A local test pass is not production release approval.
2. Stop or drain the runtime using the supported deployment workflow and confirm that
   no other process can write the database or archive tree. Keep the storage unlocked
   only within the existing private/encrypted maintenance boundary.
3. Retain an independent, consistent pre-upgrade backup: the SQLite database including
   committed WAL state, its matching archive/object tree and required keyring/credential
   references, plus the matching old release and configuration. Use the supported backup
   tooling or SQLite backup API; copying only the live database file is insufficient.
   Protect backup material with the same privacy and permission controls as the subject.
4. Check the operator publication directories. Keep original request keys privately for
   unfinished maintenance work; do not invent replacement keys to recover an old intent.
   Publication metadata and diagnostic JSON exports do not replace a restore backup.

Automatic `.pre-migration` database images are temporary rollback state. Successful
initialization may clean them up; they are not the independent deployment backup.
A filesystem copy made without a consistent database/archive boundary is not a
substitute. Do not print keys or copy private backup evidence into the source repository.

## What the upgrade preserves

Schema117 changes publication triggers, not table layout or the v1 archive formats.
Historical schema115/116 fingerprints remain supported for reading accepted roots.
The upgrade checks existing publication metadata and receipt bindings before installing
new guards. Invalid evidence aborts migration; it is not repaired, rehashed or deleted.
Archived receipt hashes are checked without archive-file reads inside the migration
writer transaction. Normal runtime integrity and archive readers must still check
physical objects; successful migration alone does not prove archive availability.

Only the exact reviewed116-to117 transition permits an existing unfinished request to
resume across schemas. The controller must find its original immutable intent and
reconstruct unchanged business history under the actual new schema. It preserves the
original publication identity, source, parent reference and frozen manifest. New
increments still require a same-schema parent; create a new117 base when beginning
a new117 chain. Schema115 unfinished requests and unreviewed future transitions do
not gain implicit compatibility. Their metadata remains retained and requires separate
review; do not delete or relabel it to force completion.

On Windows, creating or continuing prepared file publication remains unsupported
because native directory durability is not confirmed. Check `creation_capability`.
Do not simulate successful fsync in deployment to bypass this boundary. Historical
metadata/read and already-published request behavior retain their documented contracts.

## After starting the candidate

- Confirm the installed release and schema117, the intended subject/genesis, service
  readiness and integrity results. A running process alone is insufficient.
- Compare pre/post workflow and publication identities, versions and statuses. Recheck
  representative accepted base/chain archives through the authenticated operator routes.
  This requires the usual owned, paused/drained runtime and usable archive key.
- Retry unfinished work only with its original request key and expected version. Unknown
  durability remains unknown until full readback and real durability checks succeed;
  accepted retries must not overwrite files or append duplicate acceptance evidence.
- Verify that diagnostic runtime export still classifies publication/request tables and
  excludes unrelated subjects. Confirm that no model/payment/remote action was replayed
  merely to finish the maintenance upgrade. Resume normal work through the existing controls
  only after the deployment's readiness and integrity requirements are satisfied.

Keep these checks private. Do not publish subject data, original request keys, credentials,
raw evidence or backup locations as release artifacts.

## Failure and rollback

If migration fails, the existing database migration backup/restore path attempts to
restore the original database. Keep the runtime stopped while investigating. Verify the
old schema and the matching old release before reopening it; do not repeatedly retry
against damaged evidence or manufacture a new state hash.

After successful migration, an older runtime must reject the newer database. Switching
only the application binary is not a database downgrade. Before using a pre-upgrade
snapshot, determine whether any subject, ledger, receipt or external-action evidence was
written after that snapshot. If it was, preserve the current state and use the supported
forward-repair/reconciliation process; blindly replacing it would discard evidence.
A matched old backup can be rehearsed in an isolated copy with the matched old release,
without overwriting the candidate's current state. Actual rollback must follow the
supported deployment recovery protocol and its write/ownership fences.

See [workflow publication protocol](../development/workflow-export-publications.md),
[base API](../api/workflow-exports.md), [increment API](../api/workflow-increments.md),
and the [Ubuntu upgrade procedure](ubuntu.md#upgrade-from-the-management-page).
