# M42 Remediation Status

Updated 2026-08-18 after the interrupted remediation run.

## Local Verification

The following gates have been run against the current working tree:

- M41 smoke profile: 274 passed.
- Deployment/service audit: 194 passed, 1 Windows filesystem-symlink test skipped by platform.
- Focused P1/P2/P3 remediation suites: green, with only platform-specific symlink skips.
- Full static gates: Ruff check, Ruff format check, strict Mypy, compileall, pip check,
  pip-audit, and `git diff --check` pass.
- P1-12 execution and autonomous-project regression: 36 passed after replacing the stale
  one-call prediction fixture with calibration and measured target-time resolution.
- Windows package lifecycle smoke: install, upgrade, rollback, manifest hash rejection,
  and atomic staging pass through PowerShell.

- Full repository run: 746 passed, 3 platform-specific skips, and 104 subtests passed on
  Windows and in WSL2 Ubuntu 24.04. The platform skips are retained in each host's report.
  The skips are the POSIX symlink and encrypted-filesystem checks listed below; they are not
  converted into passes.

## Implementation State

P1-09 through P1-12, P2-04 through P2-09, P2-11, P2-12, P2-13, P2-15, P2-17, and P2-18 have
executable implementations, focused regression evidence, and a green full suite. P2-13 and P2-18
are now `verified` after complementary Windows and WSL2 Ubuntu traversal runs. P2-15 is now
`verified` after the Docker Hub connection recovered: both pinned base and cloud images built,
and their container imports passed. P2-14 remains `implemented` because a clean production host
and operational restore drill are still required.
P2-14 is `implemented`: both a temporary Windows BitLocker VHDX and a temporary WSL2 dm-crypt
LUKS volume passed the required guard, private-storage checks, encrypted backup, key rotation,
restore, and historical-key-loss fail-closed flow. A clean production host and operational restore
drill are still required before promoting it to `verified`.

P3-01, P3-02, P3-03, P3-06, and P3-07 now have bounded runtime implementations, durable or
replayable evidence, and focused tests. P3-04 now has real pinned runs over LoCoMo `locomo10`
evidence QA and LongMemEval Oracle; the upstream revisions, hashes, retrieval metrics, and
limits are recorded in `docs/audit/2026-08-18-p3-04-memory-benchmarks.md`. The committed fixture
remains reduced and license-safe, so LongMemEval S/M, answer-level QA scoring, and corpus-size
trend curves are still required before closure.
P3-03 also passed a deterministic 10,000-snapshot stability soak with no envelope violations;
multi-day calibration and drift evidence is still required before closure.
P3-05 provides an atomic Windows package/lifecycle foundation but not a signed MSI/MSIX,
clean-VM upgrade record, or crash-recovery soak. P3-08 now includes pinned cosign signing and
verification steps in the release workflow, but no real tag release or attestation verification
has run in this environment.

## External Gates Still Required

The following evidence cannot be manufactured locally and must remain open or implemented:

- A clean production-host BitLocker/LUKS restore drill and operational key custody record. The
  temporary encrypted-volume acceptance is recorded in `docs/audit/2026-08-18-p2-14-at-rest-acceptance.md`.
- Clean Windows VM, Authenticode/MSIX signing certificate, and upgrade/crash rollback run.
- Real LoCoMo/LongMemEval downloads with recorded upstream revisions and hashes.
- A real GitHub tag release with Sigstore certificate and offline verification.

No issue is promoted to `verified` solely because a happy-path unit test passed; the acceptance
matrix remains the authoritative closure record.
