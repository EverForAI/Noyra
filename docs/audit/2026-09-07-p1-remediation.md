# AUD-12 / AUD-05 Remediation

Second-freeze findings and updated contracts are addressed in
`2026-09-07-second-freeze-remediation.md`. In particular, POSIX publication needs
the enforced dedicated-account/trusted-directory boundary described there.

Baseline: `4aaa3ff` on `codex/candidate-20260822`.
Scope: the two findings reopened on 2026-09-06, plus their regression tests.
This is a local remediation record, not authorization to enable production
payments or to replace the published preview/tag.

## AUD-12: Authorized Filesystem I/O

Root cause: re-resolving a pathname does not bind a later open, mkdir, replace,
or cleanup to the checked directory. A deterministic Windows junction swap at
temporary-file creation left private bytes outside the grant even when final
publication failed. Both the direct parent and a higher ancestor reproduced it.

The capability tool now traverses every directory from the filesystem anchor
using relative no-follow opens. POSIX operations retain directory descriptors;
Windows reuses the prototype workspace's native relative-open/rename/delete API
with delete sharing disabled during capability I/O. Existing prototype callers
retain their previous sharing mode. Staging, publication, and error cleanup all
use the held directory/file handles, not a re-resolved cleanup pathname.

Reads reject non-regular and multi-link files and read at most max+1 bytes.
Writes retain exclusive staging, bounded writes, fsync, atomic publication,
pre-publication grant revalidation and conservative unknown error outcomes.
The regression suite also verifies revocation during a write, disk-full cleanup,
last-moment publication substitution, normal nested writes and read limits.

Boundary: POSIX authorization follows opened directory objects during an
operation, not an immutable pathname namespace. An actor with permission to move
an opened directory can change its name; descriptor-relative I/O never switches
to a replacement directory. Filesystem administrators, malicious filesystems,
mount replacement, and modification of already-authorized regular file contents
are not isolation guarantees provided by this tool. Use least-privilege OS
permissions. Windows handles prevent renames of the opened directory chain
while the operation is active. Paths containing reparse/symlink ancestors fail
closed; capability grants remain operator-controlled.

## AUD-05: Token Native-Fee Admission

Root cause: the native-fee check returned early for unregistered native assets
or missing snapshots, omitted native principal reservations for token transfers,
and reused token-denominated min_balance as native-denominated funds. Token
unknown retries bypassed the check entirely.

Token signing now requires an active native asset and a verified native balance
snapshot for the same subject/network/spending address. Future or stale native
observations fail closed. The maximum age is the tighter of the policy window
and the existing 24-hour observation ceiling; policy age=0 no longer disables
this requirement for token fees.

The existing BEGIN IMMEDIATE transaction deducts all native principal
reservations and signing/broadcast/unknown fee envelopes before admitting a new
token execution. A token retry revalidates its original spending address and
fee budget, excludes its own fee once, and preserves the original nonce and
envelope. Denial leaves the order, attempt count and signer untouched. Receipts
can still be reconciled without authorizing a new broadcast.

Terminal chain transactions, including reverts, require a newer native balance
observation before subsequent token signing. The schema has no block-anchored
balance snapshots, so this is conservative rather than guessing actual gas
consumption or automatically releasing budget against a stale observation.

Units: min_balance remains denominated in the payment asset. A token reserve is
not interpreted as wei. Reserved token orders promise token principal only;
native fees are admitted/reserved at signing, not when accepting submissions.
Normal execution still obtains a signer fee quote. The existing bounded,
explicit operator fee/gas override is preserved; the autonomous workflow does
not supply it. Legacy native-only optional-observation behavior is unchanged.

External balance changes, RPC freshness, signer correctness, real-chain gas
estimation and finality still require deployment/testnet validation. This change
does not claim that a timestamped local snapshot alone guarantees chain funds.

## Training Export CI Regression

The Windows CI failure occurred before race injection (`raced=False`). The test
compared an unnormalized temporary path to the exporter's resolved path. A
noncanonical spelling of the same directory deterministically reproduces the
missed injection. Normalize fixture paths and require that the race actually ran
even on the export-abort branch. The original foreign-byte rejection assertion
is retained. No training exporter implementation was changed.

## Verification And Release Boundary

New suites: `tests/test_capability_file_races.py` and
`tests/test_wallet_fee_admission.py`. Both are included in the clean-commit
wallet gate runner together with the workspace isolation suite.

Pre-freeze evidence: junction escape and nine token-admission regressions failed
on the old implementation; terminal-balance cases failed before the additional
settlement fence; all were rerun after the relevant changes. Ubuntu/Windows
targeted runs cover the platform-specific filesystem branches.

Required final validation runs against one clean local commit:

```text
python scripts/audit-wallet-stage4b4.py --scope targeted --profile smoke
python scripts/audit-wallet-stage4b4.py --scope targeted --profile pressure
python scripts/audit-wallet-stage4b4.py --scope full --profile smoke
```

Machine-readable outcomes, command logs and resource metrics are preserved under
`artifacts/release/stage4b4/<commit>/<run>/`. Final acceptance is conditional on
those run.json outcomes, not on this pre-run record.

The 2026-09-06 public allowlist/checksums describe the old frozen snapshot and
are deliberately not used to publish these changes. A subsequent preview must
review the new files, regenerate the allowlist/blob checksums, export a new
sanitized snapshot, and wait for remote CI. Do not push development history or
move `preview-2026.09.07`.
