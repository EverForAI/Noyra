# Second Freeze Remediation Contract

Baseline: `ba7281475d8a467cfae47d8fde803ae4a1634836`.
Scope: FR2-01 through FR2-05 from the second freeze review. This record supersedes
the corresponding boundaries in `2026-09-07-p1-remediation.md`; it does not enable
payments, authorize publication or replace an existing preview tag.

## Payment Invariants

- An attempt rejection is not evidence that an earlier broadcast was cancelled.
  Retrying an unknown payment retains the prior known transaction hash. Rejection,
  transport failure and malformed responses remain unknown, retaining principal,
  native fees and nonce ownership. Restart recovery retains the same evidence.
- Every attempt keeps the original immutable nonce and transfer envelope. Receipt
  lookup examines the bounded attempt history (at most 32), verifies attempt
  hashes and validates receipts against the appropriate transaction identity.
  Success or revert of an earlier attempt can settle the payment exactly once.
- Native and token execution, including retries, share the native fee budget.
  A local observation predating settlement cannot establish available funds for
  either asset. New observations retain the existing conservative pending-fee
  deductions; they do not infer actual gas usage from envelope limits.
- Refund is allowed only after failed payment resolution, never merely unknown.
  A first request definitively rejected before broadcasting remains refundable.
  Receipt-only reconciliation remains available while policy disables new sends.
- Pre-admission rejects legacy terminal records produced by the old rejected-
  retry / unknown-refund paths if prior broadcasts remain unresolved. The check
  applies before optional-observation handling and blocks the affected subject's
  network, including refunds. A newer balance alone does not clear it. No historical
  status or journal is silently rewritten; affected operators must keep payment
  execution disabled and perform separately reviewed chain/journal reconciliation.
- Missing native observations for legacy native-only wallets retain their
  previous behavior. Token execution still requires a current native observation.
  These compatibility rules are not approval for real-funds deployment.

## Filesystem Invariants

- One operation charges exactly one capability use and then revalidates that
  exact integrity-checked grant, including expiry and legacy approval state.
  Rechecks do not charge another use or reapply the rolling rate limit. Another
  overlapping grant cannot silently replace the charged grant mid-operation.
- Reads revalidate after file-handle acquisition and before reading, then again
  after reading/decoding before reporting success. Observed revocation discards
  content. Revocation after the final check may overlap completion: this is not
  a promise to retract already returned bytes or linearizable cancellation.
- Absolute equivalent roots such as `authorized/../authorized` are normalized at
  use time without rewriting persisted integrity hashes. A root whose actual
  resolution differs from its lexical normalization is denied. No-follow traversal
  from the filesystem anchor remains the actual I/O boundary.
- Windows retains no-delete-shared ancestor handles and handle-based publication.
- POSIX writes require each opened directory to be owned by the effective service
  UID or trusted root, and deny group/other entry-write permission before creating
  child directories or staging payloads. A root-owned sticky ancestor such as
  `/tmp` is permitted only above the grant; the grant and publication namespace
  themselves must not be shared writable. Existing permissions are never changed
  automatically. POSIX ACL write masks are represented by the group mode bits;
  filesystems/ACL systems that do not enforce these POSIX permissions are outside
  the supported write deployment contract. Permissive DrvFS modes may be denied.
- Run the service under a dedicated OS account. Same-UID hostile processes,
  privileged actors, malicious filesystems/mounts and concurrent changes by trusted
  owners are not isolated by this in-process tool. A same-UID monkeypatch can still
  replace a staging entry; tests must not portray it as an OS isolation guarantee.
  Supporting that threat model would require separate OS/process isolation.

## Verification Checklist

New regressions extend the existing targeted gate suites:

- Unknown first send with/without hash, rejected retry, retained fee reservation,
  rejected refund, delayed success/revert and integrity after settlement.
- Earlier-attempt receipts after successful retry; restart during retry; generic
  transport/malformed response; receipt reconciliation with payments disabled.
- Native payment after token success/revert; native retry after another settlement;
  denial leaves attempt count unchanged; current snapshots allow correct execution.
- Valid legacy failed-retry/refunded-unknown records block subsequent spending
  even after a new observation, without mutation or loss of historical evidence.
- Revocation after open/read; revoked charged grant plus expired overlap; equivalent
  root read/write with a one-use rate limit; exactly one use per operation.
- POSIX shared-write and foreign-owner denial before staging/child creation;
  existing directory-swap, publication, disk-full, revocation and bounded-read tests.

The first six wallet regression cases, four Windows filesystem cases and six
POSIX permission cases failed on the pre-fix implementation before changes. The
earlier-attempt receipt and legacy-state tests also failed before their repairs.

Required final gates on one clean local commit:

```text
python scripts/audit-wallet-stage4b4.py --scope targeted --profile smoke
python scripts/audit-wallet-stage4b4.py --scope targeted --profile pressure
python scripts/audit-wallet-stage4b4.py --scope full --profile smoke
```

Also run Linux targeted coverage and Linux-platform Mypy. Final acceptance is
conditional on actual results under `artifacts/release/stage4b4/<commit>/<run>/`
and the independent freeze review, not on the presence of this document. No full
suite or remote GitHub success is implied by a targeted test result.

The old public allowlist/checksums and `preview-2026.09.07` stay untouched. Any new
preview still needs a newly reviewed sanitized snapshot, checksums and remote CI.
