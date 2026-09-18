# Selectable Wallet Modes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a selectable encrypted local EVM wallet signer while preserving the existing external HTTPS signer and disabled default, with arbitrary valid recipients and existing economic safety controls.

**Architecture:** Introduce a small keystore module backed by audited `eth-account` signing and strict private-root validation. Add a bounded JSON-RPC transaction adapter for chain reads, gas estimation, signing, and broadcast. Select the signer in `NoyraService.from_env()` using explicit `disabled`, `local`, or `external` mode, while retaining injected signers for tests and operators.

**Tech Stack:** Python 3.11+, Pydantic, httpx, cryptography, `eth-account`, existing Noyra SQLite/audit/at-rest helpers, pytest.

**Spec:** Existing approved conversation design: built-in encrypted wallet is selectable; external signer remains compatible; disabled is default; arbitrary valid recipients are allowed; limits, balances, nonce, gas, emergency pause, ledger, audit, and recovery remain enforced.

## Global Constraints

- Default wallet mode is `disabled` in fresh installs. Legacy endpoint-only configuration retains external mode; explicit disabled always overrides it.
- `local` private material is encrypted at rest, loaded only in memory, never logged, exported, placed in SQLite, or passed through model/API inputs.
- `external` keeps the current credential-free HTTPS endpoint contract and bearer-token handling.
- Recipient/network/asset allowlists are not used as payment admission gates; registration, validation, limits, balance, nonce, gas, ledger, audit, and emergency pause remain enforced.
- RPC access is public HTTPS only, bounded, no redirects, no arbitrary methods from business input, and chain ID is verified.
- No cloud deployment, secret handling, or live-funds test is performed in this worktree.

---

### Task 1: Define encrypted local wallet and RPC interfaces

**Files:**
- Create: `src/noyra/wallet/local.py`
- Modify: `pyproject.toml`
- Test: `tests/test_wallet_local.py`

**Interfaces:**
- `create_keystore(path, password, private_key=None)` returns public metadata.
- `load_account(path, password)` returns an in-memory eth-account account.
- `LocalWalletSigner.sign_and_broadcast(...)`, `get_receipt(...)`, `get_pending_nonce(...)`, `get_fee_quote(...)`
- `LocalWalletRPC` owns bounded JSON-RPC calls for `eth_chainId`, nonce, balance, gas estimate, fee data, send raw transaction, transaction receipt, and block identity.

- [x] Write lifecycle, wrong-password, tamper, permission, and no-plaintext tests.
- [x] Run focused local wallet tests and implement the required behavior.
- [x] Add `eth-account` runtime dependency and implement strict keystore JSON, atomic private file creation, password-derived encryption through `eth-account` keystore APIs, and local signing.
- [x] Implement fixed native/ERC-20 envelope checks, deterministic EIP-155 legacy transaction construction using the immutable fee ceiling as gasPrice, chain/source/nonce/fee/balance validation, bounded RPC response parsing, and classified errors. No fluctuating tip or mutable fee on retries.
- [x] Run focused tests and Ruff for the new module.

### Task 2: Integrate selectable signer configuration

**Files:**
- Modify: `src/noyra/service.py`
- Modify: `src/noyra/wallet/execution.py`
- Modify: `src/noyra/wallet/__init__.py`
- Modify: `.env.example`
- Modify: `deploy/noyra.env.example`
- Test: `tests/test_service_wallet_modes.py`

**Interfaces:**
- `NOYRA_WALLET_MODE=disabled|local|external` with disabled default.
- Local settings use private-root/keystore path plus password file, never a password command-line value.
- Endpoint-only legacy configuration maps to `external` for compatibility; explicit conflicting mode fails closed.

- [x] Add mode-selection, conflict, ownership/close, and default-disabled tests.
- [x] Run the tests and confirm the mode contracts.
- [x] Add explicit parsing and instantiate the local keystore signer or `HTTPSWalletSigner`, wire cleanup for both signer types, and preserve injected signer behavior.
- [x] Update module/protocol documentation and committed environment examples with safe comments.
- [x] Run service-focused tests, Ruff, and mypy for changed modules.

### Task 3: Remove allowlist admission dependency while retaining controls

**Files:**
- Modify: `src/noyra/wallet/economy.py`
- Modify: `src/noyra/wallet/execution.py`
- Test: `tests/test_wallet_economy.py`
- Test: `tests/test_wallet_execution.py`

- [x] Add coverage proving an otherwise valid unlisted network/asset recipient can pass policy admission while amount, balance, limits, and emergency pause still reject violations.
- [x] Remove only network/asset allowlist checks from `_policy_allows`; preserve all other checks and durable fields for schema compatibility.
- [x] Strengthen ERC-20 calldata envelope validation (zero padding and positive amount) before local signing and execution.
- [x] Run wallet economy/execution tests and the full wallet test subset.

### Task 4: CLI setup and operator documentation

**Files:**
- Modify: `src/noyra/__main__.py`
- Modify: `src/noyra/__main__.py`
- Modify: `docs/adr/0002-wallet-economic-execution.md`
- Modify: `docs/deployment/research-preview.md`
- Test: `tests/test_wallet_setup.py`

- [x] Add tests for setup output containing only public address/metadata and refusing unsafe paths/password arguments.
- [x] Implement `python -m noyra wallet-setup` using hidden password prompts or protected password-file input, with create/import, atomic permissions, and public-only JSON output.
- [x] Document local/external switching, password-file/systemd credential handling, backup/recovery, arbitrary recipient validation, and disabled defaults.
- [x] Run CLI/docs tests and lint.

### Task 5: Dependency locks and verification

**Files:**
- Modify: `requirements.lock`
- Modify: `requirements-dev.lock`
- Modify: `requirements-cloud.lock` only if resolver requires synchronized base pins
- Test: all affected tests

- [x] Regenerate hash-pinned locks with `uv pip compile` using existing constraints and the existing development audit toolchain.
- [x] Validate lock files with hash-checked pip dry runs; run focused tests, full pytest, Ruff, mypy, and pip check. `pip-audit` was attempted but the Windows environment's subprocess encoding failed before scanning.
- [x] Review the final diff for secret leakage, mode defaults, and deployment side effects.

---
