# 2026-10-22 Independent Audit Remediation

This record documents the fixes for the five findings in
`2026-10-22-post-remediation-independent-readonly-audit.md`. Changes were made
on top of the existing uncommitted worktree; no pre-existing change or audit
evidence was reverted. This work did not commit, push, deploy, or access a
production host, real provider credential, model API, or payment service.

## Outcome

| Finding | Risk | Remediation | Residual / change risk |
|---|---|---|---|
| R1 Windows package inventory and strict-signature bypass | P1 | Verify exact package file inventory and copy only verified entries | Medium: authentic Windows signing/release gate still required |
| R2 failed one-shot retry has no safe operator closure | P2 | Add explicit evidence-bound, risk-accepting closure while retaining quarantine by default | High: closure intentionally permits a new business generation after a human override |
| R3 embedding vectors collide across distinct spaces | P2 | Fingerprint normalized endpoint, model, protocol and configured dimensions | Medium: provider-side alias changes without config changes cannot be inferred locally |
| R4 managed embedding status is confused with runtime state | P2 | Report stored desired resource separately from the effective running resource; require restart explicitly | Low: hot switching is intentionally not attempted |
| R5 retry-fence lookup scales with unrelated audit history | P2 | Schema 101 adds a partial expression index used by every authorization lookup | Low to medium: additional index storage and audit-write cost |

All five findings are addressed in code and have focused regression coverage.
This is not a claim that the complete repository or real release/deployment
gates have passed.

## Changes

### R1: Windows package inventory

The installer now walks the package tree without descending through reparse
points, rejects reparse points in ancestors and package entries, normalizes and
validates manifest-relative paths, checks duplicate/case-colliding paths and
SHA-256 syntax, and requires the manifest inventory to match every packaged
file exactly (apart from the manifest itself). Installation copies only the
verified manifest entries into staging, pins the already-verified manifest,
and verifies the staged tree again before publishing it. `start` re-verifies
the installed package before selecting a launcher, so the fixed launcher name
cannot be used to select an undeclared file. A command-line strict-signature
request is retained in the staged manifest and cannot be lost when rollback or
later startup verifies the installed package.

The package builder deliberately remains able to create a source-only package
for development; such a package can install but cannot start unless it contains
a listed `noyra.exe` or `noyra-desktop.exe`. This preserves the existing build
contract without weakening launcher verification. Regression cases use inert
text fixtures only; they never execute a fixture launcher.

Residual: local PowerShell tests do not provide an Authenticode certificate or
a clean Windows VM. Release signing, clean-VM install/upgrade/rollback, and
crash-recovery remain external gates. This fix closes the audited unlisted-file
path; it does not claim to replace publisher identity verification for a real
release channel.

### R2: Unknown retry closure

A one-shot retry that returns a known failure remains quarantined by default:
the failure of the replay cannot prove that the original unknown request was
not executed or billed. The operator may close the retry grant only by
providing a nonblank, bounded provider-resolution reference and explicitly
setting `accept_duplicate_risk=true`. Without both fields, the request returns
HTTP 409 and the route remains fenced. The database stores only a hash of the
resolution reference, never the free-form reference itself, and appends a
closure record and operator audit. The closed event is idempotent under the
database transaction; duplicate close requests do not create another closure.

The original call, attempts, costs, and unknown outcome are not rewritten.
After an explicitly recorded closure, the stable business-round identity can
advance; the next provider request uses normal resource routing, and its own
failure/unknown state is independently recorded. The same rules are exercised
for `world_cognition`, `sleep_reflection`, and `interaction_cognition`.

Residual: this is an explicit operator override, not a technical proof of the
provider's internal execution. The request requires deliberate evidence and
risk acceptance, retains a durable audit hash, and never auto-closes. Operators
must inspect provider-side records before using it.

### R3: Embedding-space identity

The OpenAI-compatible embedding provider identity now includes a SHA-256 space
fingerprint over the normalized HTTPS endpoint, model alias, configured output
dimensions, and protocol version. It excludes API credentials, so rotating a
key for the same configured space does not invalidate the vector corpus.
Environment-derived resource IDs now include configured dimensions as well.
Index writes, stale/missing detection, and similarity selection share this
provider identity, so endpoint or dimension changes cannot reuse vectors merely
because the model label is unchanged. Existing rows are detected as stale and
rebuilt through the existing bounded rebuild path rather than eagerly making a
large paid embedding request during startup.

Residual: if a provider silently changes the vector space behind an unchanged
endpoint/model alias and the caller omits dimensions, no local configuration
fingerprint can discover that remote mutation. Provider contract monitoring or
an operator-supplied model/version change is still needed for that case.

### R4: Desired versus effective embedding resource

The service exposes a runtime status derived from the actual `CognitionCycle`
embedding gateway and the database's currently selected active managed
resource. Diagnostics and the embedding resource list distinguish
`applied`, `restart_required`, `not_configured`, `cognition_disabled`, and
`unavailable`; configure/enable/disable/revoke responses include the same
runtime projection. The admin runtime summary displays whether the embedding
resource is actually applied or merely saved for the next restart.

No partial hot swap is attempted. `CognitionCycle`, its gateway, index, memory
store references, background rebuild path, and executor lifecycle remain one
consistent runtime graph. A managed resource selected after process startup is
reported as pending restart; after a fresh service construction it is reported
as effective. This is intentionally more conservative than replacing only a
gateway pointer.

### R5: Audit lookup scaling

Schema 101 adds `idx_audit_subject_action_call`, a partial expression index on
`(subject_id, action, json_extract(payload_json, '$.call_id'))` guarded by
`json_valid(payload_json)`. The route-fence correlated authorization and
cancellation checks, bulk audit fetch, explicit retry-closure checks, and
operator lookup use the index. The JSON validity predicate retains compatibility
with malformed legacy payload rows without making migration/index creation fail
on those rows. Schema 100 is unchanged; schema 101 has new DDL and structural
fingerprints, and runtime-export ownership inherits schema 100 because the
migration adds no exported data table.

Regression coverage upgrades a schema-100 database containing malformed audit
JSON, verifies the new schema contract and query plan, and invokes the actual
business route-fence query before and after adding 10,000 unrelated audit rows.
It asserts the route remains quarantined and SQLite VM-step work stays bounded.

## Verification

Focused runs completed during this remediation:

| Area | Result |
|---|---:|
| Windows package and process identity | 13 passed |
| Model unknown recovery, operator controls, retry fence and audit-integrity suites | 39 passed |
| Embedding resources, embedding resilience, bounded retrieval and private erasure | 55 passed |
| Managed embedding runtime state and related service checks | 9 passed |
| Schema/export migration checks | 16 passed |
| Frontend admin tests | 79 passed |
| HTTP operator retry-resolution contract regression | 1 passed |
| Ruff check / format, Mypy on touched Python files, Python compileall | passed |

Counts are per reported pytest invocation and can overlap; they are not summed
as unique tests. Final targeted reruns are recorded in the remediation session
and take precedence over any earlier red/green iterations. No full-repository
pytest run or external production gate is claimed.

## Deployment and operation

Schema 100 databases migrate forward to 101 through the normal migration path;
no existing schema fingerprint or historical migration was edited. Managed
embedding changes made while the service is running require a controlled
service restart. If an unknown model call used an explicitly authorized retry
and that retry failed, keep it quarantined unless provider evidence has been
reviewed and the operator explicitly accepts residual duplicate-execution
risk. The remediation has not been deployed to any server.
