# Automatic payment release gate

Noyra's local deterministic tests prove the payment state machine, but they do
not prove signer isolation, real-chain receipts, backup recovery, or a long
running production process. Production automatic payment therefore remains
disabled until the external release evidence is independently reviewed.

The production profile requires this explicit opt-in before the service will
load autonomous wallet automation:

```text
NOYRA_WALLET_AUTOMATION_RELEASE_GATE=external_verified
```

That setting remains an operator opt-in. Production policy activation and each
new automatic payment authorization also verify a root-controlled Ed25519 key
at `/etc/noyra/release-evidence-public-key` and the current release's
`.noyra-external-gates.json`, bound to its `.noyra-source-sha`. An environment
assertion alone cannot enable production automation. Disabling and emergency
pause remain available when evidence is missing; receipt query/reconciliation
for already-broadcast transactions remains available. Container deployments instead bind
the image's `/opt/noyra/.noyra-source-sha` to an independently provisioned, read-only
`/run/noyra/release-assurance` directory containing `public-key` and `external-gates.json`;
see the optional Compose overlay in the Ubuntu deployment guide. A
release must carry a signed `external-gates.json` with format
`noyra-external-gates/v1`, the exact release commit SHA, all fixed gate IDs,
reviewer identity, UTC test windows, and evidence references. The release job
rejects missing, stale, failed, incomplete, or unverifiable evidence.

The evidence handoff uses the separate `external-gates` GitHub Actions workflow.
Before enabling releases, add the base64 raw Ed25519 verification key as the
repository secret `EXTERNAL_GATES_PUBLIC_KEY`. Create a GitHub environment named
`external-gates` with at least one required reviewer and **Prevent self-review**
enabled. The release workflow checks this environment policy before accepting a
run. For each release tag, prepare and independently sign the JSON record away
from the release job, then dispatch `external-gates` against that exact `v*` tag
and provide the base64-encoded JSON. The workflow verifies its signature and
commit before uploading the artifact; the release job locates the newest
successful run for the exact commit and downloads that artifact.

For a new release, first prepare and sign the record for the exact commit that
will be released. In GitHub Actions, run `external-gates` from `main` while
`main` points at that commit, approve the protected environment review, and
wait for the run to finish successfully. Only then create/push the `v*` tag
pointing at the same commit. This ordering prevents the tag-triggered release
from racing ahead of the evidence artifact. The release workflow selects the
newest external-gates run for its exact commit; a newer failed or pending run
blocks release rather than falling back to an older successful artifact. If a
tag already exists, dispatch the workflow from that tag, wait for success, and
then rerun the failed release job.

The dispatch input is visible in GitHub Actions metadata. Do not include API
keys, private evidence URLs, tokens, raw account identifiers, or other secrets
in the JSON. Use stable, non-secret evidence references. A successful upload
only proves the signed record passed validation and a separate environment
review; it does not itself perform the real-world tests listed below.

The required gates cover a disposable testnet transfer observed by a second
observer, signer timeout/response-loss/duplicate/restart behavior, signer/KMS
isolation, a 24-hour bounded soak, backup restore and rollback, and operator
approval of pause, incident, refund, and signer-rotation procedures. Evidence
must never contain private keys, seeds, mnemonics, bearer tokens, or raw
credentials.

The wallet policy still enforces the global automation switch, emergency
pause, single-payment cap, daily amount limit, balance and gas admission, and
durable reconciliation for unknown or reorged transactions. External evidence
does not weaken any of those runtime controls.

The 72-hour freshness window applies **at release publication**, not every
72 hours during operation. An older accepted release can restart offline with
the same valid signature and source SHA. Replacing the code SHA or trusted key
requires matching evidence. Production policy-auto migration consumes the same
contract; manual approval and explicitly opted-in local wallets remain supported.
