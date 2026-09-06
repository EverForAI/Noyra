# M26: Explicit Project Execution

M26 connects autonomous project phases to existing authorized cognition
capabilities. It does not add shell execution or silently grant permissions.

## Execution identity

Every phase attempt receives an append-only execution record keyed by
`project_id`, `phase_id`, and the phase attempt number. The execution ledger
tracks workflow, status, durable result references, acceptance evidence, and
hash-checked revisions. Repeating a phase attempt after restart resolves to the
same execution record and cannot create a duplicate tool intent.

Research and search actions now carry explicit project and phase references.
Legacy unbound research remains supported; project resource reconciliation only
uses the old goal/time fallback for those legacy records.

## First adapter

`research_note` and `knowledge_collection` phases dispatch to the existing
autonomous research planner. The dispatcher validates that the project and
phase are owned by the subject, active, and tied to the selected autonomous
goal. API search, browser search, and model fallback remain separate resource
pools and retain their existing authorization and rate limits.

The model cannot complete a phase by assertion. A successful research execution
requires a durable accepted research record with at least one result and an
accepted source. Empty, rejected, unavailable, or unknown outcomes are stored
as failed or blocked execution states and are not retried blindly.

## Recovery and visibility

Prepared, executing, and terminal execution states are durable. Existing action
recovery continues to quarantine interrupted external reads as unknown. Public
project projections expose only execution count and the latest safe status;
authenticated runtime exports include the complete execution ledger, hashes,
and revisions with existing secret redaction.

The remaining phase adapters are intentionally staged: prediction records,
bounded software prototypes, self-experiments, and collaboration requests
must each define their own durable acceptance evidence before they can advance
a phase.
