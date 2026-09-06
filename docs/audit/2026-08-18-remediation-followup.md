# 2026-08-18 M42 Remediation Follow-up

This is a follow-up to `docs/audit/2026-08-15-full-readonly-audit.md`; it does not rewrite the
historical findings.

## Results

- Full pytest: 746 passed, 3 platform-specific skips, 104 subtests passed.
- M41 smoke: 274 passed.
- Deployment/service audit: 194 passed, 1 platform-specific skip, 70.29% focused coverage.
- Ruff, format, strict Mypy, compileall, pip check, UTF-8 pip-audit, and diff check passed.
- Windows and WSL2 Ubuntu 24.04 each ran the full suite with 746 passed; complementary
  P2-13/P2-18 platform traversal contracts passed.
- P1-09 through P1-12 and P2-04 through P2-13, P2-16 through P2-18 have executable contracts and are marked
  `verified` in `tests/contracts/remediation_acceptance.json`.
- P2-14 and P3-04 now have real temporary-volume or upstream-dataset evidence and are marked
  `implemented`; P2-15 is now `verified` after both Docker profile builds. The acceptance matrix
  is now `34 verified`, `3 implemented`, and `2 open`.
- P3-01, P3-02, P3-06, and P3-07 have executable contracts and are marked `verified`.

## Deliberately Unclosed

The following remain `implemented` or `open` because the required evidence was not available:

- P2-14: temporary real BitLocker and dm-crypt volumes passed the required guard, backup,
  rotation, restore, and historical-key-loss checks; a clean production-host restore drill and
  key custody record are still required for `verified`.
- P3-03: the fixed-history ablation and durable report pass, and a deterministic 10,000-snapshot
  stability soak reported no envelope violations; no multi-day calibration soak exists yet.
- P3-04: full LoCoMo `locomo10` evidence retrieval and LongMemEval Oracle retrieval now have
  pinned upstream revisions, hashes, and reports; LongMemEval S/M, answer-level QA scoring, and
  corpus-size trend curves remain open.
- P3-05: PowerShell package install/upgrade/rollback/hash tests pass, but no signed MSI/MSIX,
  clean VM, or crash-recovery run exists.
- P3-08: the release workflow now signs and verifies `SHA256SUMS` with pinned cosign and emits
  SBOM/provenance attestations, but no real tag release was executed.

These limitations are recorded as status, not treated as passing evidence.
