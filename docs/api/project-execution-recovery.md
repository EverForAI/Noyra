# Project execution review recovery

The management console provides **运行防护 → 项目执行恢复** for interrupted local
`research_note`, `knowledge_collection` and `software_prototype` executions.
It returns the incomplete attempt to Noyra's own project review. It does not
certify an output, complete a phase, instruct a new attempt or resume the runtime.
Prediction, publication, delivery and payment workflows retain their own recovery
contracts.

## Operator flow

1. Pause the runtime through the existing lifecycle control and let active work
   finish. Inspect and independently reconcile uncertain provider calls, model
   attempts, actions and message deliveries using their existing evidence rules.
   A free-form recovery explanation cannot settle any of those records.
2. Refresh the project execution recovery panel. Check the project, phase,
   original status and whether the attempt is still current. Unsupported or stale
   attempts remain diagnosis-only. Migration fencing, integrity quarantine,
   unavailable protected storage and missing ownership prevent recovery.
3. Select **交还 Noyra 自主审查** and record why the incomplete local attempt can
   be discarded after its external effects have been checked. This creates an
   immutable private receipt and operator audit record in one transaction.
4. Resume the runtime separately when ready. Normal review rules, resource
   budgets, daily quotas, dependencies and retry backoff still apply. Noyra may
   continue, wait, pause, abandon or request help. An ambiguous execution and its
   receipt cannot satisfy successful phase acceptance.

The original execution, artifacts, provider histories and fee reservations remain
unchanged. Releasing a local attempt never resends an unknown request. If Noyra
subsequently chooses to continue, that is a separate phase attempt subject to
normal admission; it does not rewrite or replay the original execution identity.
New ambiguous work after release still blocks an advancing review commit.

## HTTP contract

Both `/api/admin/...` and `/api/v1/admin/...` are supported. These endpoints
require the existing operator credential or an operator session. Session writes
also require the existing CSRF header. They are not available to read-only roles.

`GET /api/v1/admin/project-execution-recovery?limit=50` returns at most 100
records (default 50), a truncation flag, runtime conditions and external blocker
flags. Each blocker value indicates presence, not an exact backlog count.
Records contain IDs, original status, output type, current attempt, a snapshot
fingerprint, result-reference presence, release status and eligibility. Model
text, artifact paths and recovery explanations are excluded.

`POST /api/v1/admin/project-execution-recovery/release-review` accepts exactly:

```json
{
  "execution_id": "<execution ID from the current catalog>",
  "expected_fingerprint": "<64 lowercase hex characters from the same record>",
  "reason": "<nonempty operator explanation, at most 2000 characters>"
}
```

The write rechecks ownership, pause, drain, boundaries, current project/phase/
attempt, snapshot and lower-layer ambiguity. The authenticated operator supplies
the actor; callers cannot set it in the request. A changed snapshot returns 409;
refresh and inspect again. Repeating an already accepted request with its original
fingerprint returns the existing receipt with `idempotent: true`. The receipt
never applies to another execution or phase attempt. Request conflicts and
unavailable evidence do not modify the execution.

Receipts remain valid through supported encrypted event archiving and runtime
restart. The private operator explanation is retained for accountability, while
the review prompt receives only a fixed review-only marker. It receives no
operator-written instruction from this recovery operation.

The API inventory and response statuses are maintained in `openapi.yaml` and
`src/noyra/service_contract.py`.
