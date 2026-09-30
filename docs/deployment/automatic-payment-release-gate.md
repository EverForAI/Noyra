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

That setting is an operator assertion, not a substitute for evidence. A
release must carry a signed `external-gates.json` with format
`noyra-external-gates/v1`, the exact release commit SHA, all fixed gate IDs,
reviewer identity, UTC test windows, and evidence references. The release job
rejects missing, stale, failed, incomplete, or unverifiable evidence.

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
