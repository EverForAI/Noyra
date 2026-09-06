# Restricted research preview

Project and intended repository: `Noyra`, `EverForAI/Noyra`. Maintainer: Jaxon Grey.
This profile is for disposable local research, not production use or real funds.
Payments, tips, bounties, independent signing and authorized file operations
remain project goals; their temporary disablement does not remove their code.

## Required boundaries

| Setting or resource | Preview requirement |
| --- | --- |
| Data | A new, empty or absent directory; never reuse a development or personal subject database |
| Listener | `NOYRA_HOST=127.0.0.1`; do not expose the service publicly |
| Cognition | `NOYRA_COGNITION_ENABLED=false` for this offline configuration check |
| Automatic payments | `NOYRA_WALLET_AUTOMATION_ENABLED=false` |
| Automatic publishing | `NOYRA_WALLET_AUTOMATION_AUTO_PUBLISH=false` |
| Wallet signing | Empty `NOYRA_WALLET_SIGNER_ENDPOINT`, `NOYRA_WALLET_SIGNER_ID`, `NOYRA_WALLET_SIGNER_BEARER_TOKEN`; no programmatically injected signer |
| File capabilities | No file read/write grants; no grants copied from an older database |
| Economic state | No real wallet, spending address, payment order or funds |
| External providers | No live model, embedding, search, S3 or communication credentials |
| Authentication | Fresh independent random tokens; never reuse example tokens or personal deployment keys |

Noyra consumes process environment variables. Merely editing `.env.example` does
not clear inherited environment variables or change an existing service. Docker
Compose loads `.env`, which is a different, private deployment file. Do not use
the production Compose or desktop installation guide as an audited preview launcher.

Remove inherited `NOYRA_*` settings from the isolated test process before applying
the reviewed example and disposable overrides. In particular, an inherited signer
endpoint can activate a signer even if a newly copied example omits that field.
Disabling automation does not revoke existing durable permissions or erase old
orders. The fresh-data requirement is mandatory, not a convenience.

At-rest `development` mode is allowed only for disposable test state. Real private
state requires validated encrypted storage and recovery arrangements. A file-tool
grant is distinct from the service's necessary writes to its own database, logs
and generated secrets; this profile does not claim that the entire process never
writes files. Common-knowledge or storage keys created during initialization are
not wallet signers and must still remain private.

## Reproducible offline check

From a checkout with development dependencies installed:

```powershell
python -m pytest -q tests/test_public_preview_profile.py
```

The test parses both committed environment examples, clears inherited Noyra
configuration, uses a new temporary data root, generates ephemeral authentication
and archive keys, binds an ephemeral loopback port, runs the real startup gate,
and checks that signer/execution/automation/cognition are absent and capability,
address, order and transport tables are empty. It rejects outbound socket
connections and closes the service before returning. It is not a persistent
server, a live network demonstration or a full production deployment test.

The same module validates the five committed historical SQLite fixtures against
their hashes and generated identity/event content. Fixture data is not a copy of
an actual subject's conversations or psychological state.

AUD-12 and the fee-admission portion of AUD-05 remain open. These configuration
restrictions reduce exposure but do not fix either issue. See
[SECURITY](../../SECURITY.md) and the
[audit correction](../audit/2026-09-05-comprehensive-audit-remediation.md).
