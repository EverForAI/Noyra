# Migration Binding Proofs (2026-10-04)

The first three migration security contracts are implemented in the source tree:

1. A target registers an independent X25519 recipient key. The source verifies
   the target's encrypted proof-of-possession before it creates or fences a
   migration artifact. The target rejects a binding request whose fingerprint
   is not its registered recipient key.
2. `SQLiteArtifactProvider` creates a task-bound X25519/HKDF/AES-GCM bundle.
   The source transfers the ciphertext through bounded, authenticated chunks.
   The target verifies the manifest and volume preflight, assembles the
   ciphertext atomically, decrypts it with its recipient private key, checks
   the plaintext digest and context, and restores only the authenticated
   plaintext into a task-specific private directory.
3. Target-side credential references, fingerprints, wallet mode, signer/KMS
   identity, local-wallet approval, encrypted-volume evidence, and target
   generation are returned as a signed binding record. The source verifies the
   record and carries only proof digests into activation and the durable
   execution receipt. Private keys, passwords, API keys, and bearer tokens are
   rejected from binding requests and are never copied into the bundle.

Binding records are task-bound, signed by the enrolled target key, stored with
exclusive private-file creation, and idempotent for an identical retry. A
changed task, manifest, recipient fingerprint, credential set, wallet binding,
or local-wallet approval is rejected. Local-wallet approvals are one-time,
address-bound, task-bound, time-limited, and checked again at activation;
external signer mode carries only the signer identity and address.

The automated evidence for this module is the migration test suite, including
target proof signatures, recipient mismatch, binding-record tampering,
idempotent retry, local-wallet approval replay, encrypted bundle round trips,
chunk transfer, restore, health, activation, rollback, and end-to-end executor
flows. Static checks include Ruff, compileall, and the repository's configured
Mypy checks where the installed type environment is compatible.

This closes the code portion of items 1 through 3. It does not claim that a
real deployment has passed the external release gates. Migration remains
disabled by default until the Linux/LUKS/systemd two-host rehearsal, real KMS
or signer checks, restart/fence/rollback evidence, and long-running soak have
been recorded for the exact release SHA.
