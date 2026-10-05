# External release gates

The release workflow only accepts an independently produced, signed evidence
record for the exact commit being released. Local tests and a successful build
do not satisfy these gates. If any gate is missing, stale, unsigned, or tied to
another commit, the release job stops before publishing artifacts.

The required gate IDs are:

| Gate | Required evidence |
| --- | --- |
| `ubuntu_systemd` | Clean Ubuntu install/upgrade, systemd lifecycle, readiness and rollback checks |
| `encrypted_volume` | Real encrypted data volume, permissions, key availability, and restart/restore checks |
| `backup_restore` | Backup creation, integrity verification, clean-host restore, and hash/schema proof |
| `migration_fence` | Two-host migration, source write fence, target restore/health, cutover and rollback |
| `signer_kms` | Independent signer/KMS binding, denied signer access, rotation, and recovery checks |
| `reorg_nonce` | Real testnet nonce conflict, broadcast-unknown, confirmation timeout, and reorg reconciliation |
| `https_proxy` | HTTPS reverse proxy, trusted proxy headers, public/admin auth and rate-limit checks |
| `soak` | A bounded 24/72-hour run with storage, WAL, provider, wallet, and lifecycle telemetry |

Each gate entry must include a passed status, the operator that executed it,
an independent reviewer, start and finish timestamps, and non-secret evidence
references. The top-level record must contain the release commit SHA, review
time, reviewer identity, and an Ed25519 signature. The verifier rejects fields
whose names indicate secrets (tokens, credentials, API keys, private keys,
passwords, seeds, or mnemonics), so logs and credentials must be stored outside
the JSON record and referenced by opaque evidence IDs.

To publish evidence for a release, create the record from the real environment,
sign it with the release evidence key, base64-encode the signed JSON, then
manually dispatch `.github/workflows/external-gates.yml` from the release tag
(or the exact commit ref) with both:

```text
release_sha=<the exact 40-character commit SHA>
external_gates_json_base64=<base64 of the signed record>
```

The protected `external-gates` environment must require an independent
reviewer with self-review disabled. The workflow checks out `release_sha`,
verifies the signature and all gate IDs, and uploads an immutable artifact named
`external-gates-<release_sha>`. The release workflow looks up a successful run
for that same SHA and verifies the downloaded artifact again. Evidence older
than 72 hours is rejected.

Do not generate a `passed` record in GitHub Actions and do not reuse a record
from another commit. Until the real environment has produced and independently
reviewed this artifact, the external release gate remains open and production
automatic payment, unattended migration, and long-lived public deployment
must remain disabled or in manual-approval mode.

## Final-Commit Preparation

After all repair commits are complete, verify an isolated clone of that commit:

```bash
python scripts/verify-committed.py --commit HEAD --scope all --output output/committed-verification.json
python scripts/prepare-external-validation.py --commit HEAD --output output/external-gates-pending.json
```

The first command tests committed files and preserves unrelated local files. Its
report includes the exact SHA and executed commands; local success is not an
external-gate pass. The second creates an unsigned **pending** template which
the verifier intentionally rejects. Never change its statuses to passed without
executing and reviewing the actual tests. Recreate it after any source change.

Use the eight-gate matrix above and the migration gate runbook, with two
disposable Ubuntu/LUKS hosts, an isolated signer or KMS, a test chain/RPC and
HTTPS proxy. Record source/target installed SHA, dependency lock hashes, host
class and UTC windows. Require denied/wrong-key/tamper/replay/interruption
cases, not just a happy-path cutover. Run both local-wallet opt-in and external
signer binding scenarios; local mode remains supported by the product.

During acceptance, use explicit development-main on staging hosts if no stable
release exists. Keep production automation disabled until signed evidence for
that final SHA is provisioned. Evidence is signed independently with the key
whose public half is preconfigured on both deployment hosts and the protected
GitHub environment. The runtime checks SHA/signature on activation and new
automatic authorization, while 72-hour freshness is checked at publication.
An old accepted SHA does not stop merely because 72 hours have elapsed.
