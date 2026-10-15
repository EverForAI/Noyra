# Self-Hosted Signer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a loopback-only, independently confined Ethereum signer service that implements the existing HTTPS wallet signer protocol with durable idempotency.

**Architecture:** Add a focused `noyra.signer_service` module containing bounded HTTP handling, keystore loading, a small allow-listed JSON-RPC adapter, and SQLite request state. The systemd template runs it as `noyra-signer` with `LoadCredential`; Noyra remains an HTTPS client and never receives private key material.

**Tech Stack:** Python 3.11+, `http.server`, `sqlite3`, `eth-account`, `httpx`, `pydantic`, systemd, pytest, Ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-10-15-self-hosted-signer-design.md`

## Global Constraints

- Signer binds only to `127.0.0.1:8780`.
- Wallet keystore and wallet password are readable only by `noyra-signer`.
- All signer routes require the exact bearer credential and bounded JSON.
- Only native ETH Sepolia transfer envelopes are enabled in this implementation.
- A request ID is durable and idempotent; an ambiguous broadcast is never retried automatically.
- Secrets, raw transactions, RPC URLs, and bearer tokens never enter logs or ordinary exports.
- The implementation must preserve the existing `HTTPSWalletSigner` response shapes.

### Task 1: Protocol models and RPC adapter

**Files:**
- Create: `src/noyra/signer_service.py`
- Test: `tests/test_signer_service.py`

**Interfaces:**
- Produce strict request/response parsing helpers, `SignerConfig`, `SignerRPC`, and `SignerError`.
- Consume `WalletUnsignedTransfer`, `canonical_evm_address`, and existing wallet signing conventions.

- [ ] **Step 1: Write failing tests for strict parsing and RPC allow-list**

```python
def test_transfer_parser_rejects_extra_fields() -> None:
    payload = valid_transfer_payload()
    payload["unexpected"] = True
    with pytest.raises(SignerError, match="invalid transfer"):
        parse_transfer(payload)


def test_rpc_adapter_only_allows_chain_and_wallet_methods() -> None:
    rpc = FakeRPC()
    with pytest.raises(SignerError):
        rpc.call("debug_traceTransaction", [])
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `python -m pytest tests/test_signer_service.py -q`

Expected: collection fails because `noyra.signer_service` does not exist.

- [ ] **Step 3: Implement strict models and the allow-listed adapter**

Use `WalletUnsignedTransfer.model_validate` followed by an explicit re-check
of the source address, non-zero native value, `0x` data, and Sepolia chain ID.
Implement `SignerRPC.call()` with only `eth_chainId`,
`eth_getTransactionCount`, `eth_getBalance`, `eth_gasPrice`,
`eth_estimateGas`, `eth_sendRawTransaction`, `eth_getTransactionReceipt`,
`eth_getTransactionByHash`, `eth_getBlockByNumber`, and `eth_blockNumber`.
Map RPC failures to safe `SignerError` reason codes and never include provider
messages or URLs in exceptions.

- [ ] **Step 4: Run focused tests and commit**

Run: `python -m pytest tests/test_signer_service.py -q`.

Commit: `git add src/noyra/signer_service.py tests/test_signer_service.py && git commit -m "test: define self-hosted signer protocol"`

### Task 2: Signing engine and durable idempotency

**Files:**
- Modify: `src/noyra/signer_service.py`
- Test: `tests/test_signer_service.py`

**Interfaces:**
- Produce `SignerEngine`, `SignerState`, and `SignerState.open()` with a private SQLite schema.
- `SignerEngine.transfer(request_id: str, transfer: WalletUnsignedTransfer) -> WalletBroadcastResult`.
- `SignerEngine.receipt(tx_hash: str, chain_id: int) -> WalletReceipt | None`.

- [ ] **Step 1: Write failing tests for signing, duplicate IDs, and unknown broadcast**

```python
def test_duplicate_request_id_returns_original_result_without_second_broadcast(engine):
    first = engine.transfer("request-1", native_transfer())
    second = engine.transfer("request-1", native_transfer())
    assert second == first
    assert engine.rpc.sent_raw_transactions == [first.tx_hash]


def test_ambiguous_broadcast_is_persisted_and_never_retried(engine):
    engine.rpc.raise_after_send = TimeoutError()
    with pytest.raises(SignerError, match="broadcast unknown"):
        engine.transfer("request-2", native_transfer())
    engine.rpc.raise_after_send = None
    with pytest.raises(SignerError, match="broadcast unknown"):
        engine.transfer("request-2", native_transfer())
    assert len(engine.rpc.sent_raw_transactions) == 1
```

- [ ] **Step 2: Run tests and verify failure**

Run: `python -m pytest tests/test_signer_service.py -q`.

Expected: the engine and durable state are undefined.

- [ ] **Step 3: Implement the minimal durable engine**

Load the account from `keystore.load_account()` using the credential file.
Use the same legacy transaction fields as `LocalWalletSigner`, calculate the
deterministic raw transaction hash before admission, and persist only request
ID, transfer hash, envelope metadata, state, transaction hash, nonce, and
timestamps. Use a transaction lock around read/admit/sign/broadcast state
changes. Refuse a second transfer for `unknown`; return the original result
for `broadcast` or `confirmed`. Validate receipt identity and canonical block
hash using the same checks as the in-process signer.

- [ ] **Step 4: Run tests and commit**

Run: `python -m pytest tests/test_signer_service.py -q`.

Commit: `git add src/noyra/signer_service.py tests/test_signer_service.py && git commit -m "feat: add durable signer engine"`

### Task 3: HTTP service and CLI entry point

**Files:**
- Modify: `src/noyra/signer_service.py`
- Modify: `src/noyra/__main__.py`
- Test: `tests/test_signer_service.py`

**Interfaces:**
- Produce `SignerHTTPServer`, `signer_main()`, and `python -m noyra signer`.
- The server exposes `/migration-identity`, `/v1/nonce`, `/v1/fee-quote`, `/v1/transfer`, and `/v1/receipt`.

- [ ] **Step 1: Write failing HTTP tests**

```python
def test_http_requires_bearer_and_returns_identity(server):
    assert post(server, "/v1/nonce", valid_nonce_payload()).status_code == 401
    response = post(server, "/migration-identity", identity_payload(), token=TOKEN)
    assert response.status_code == 200
    assert set(response.json()) == {"signature"}
```

- [ ] **Step 2: Run focused tests and verify failure**

Run: `python -m pytest tests/test_signer_service.py -q`.

Expected: `SignerHTTPServer` and CLI dispatch are undefined.

- [ ] **Step 3: Implement bounded HTTP handling**

Use `ThreadingHTTPServer` bound to the configured host/port. Enforce a body
limit, reject duplicate or missing JSON fields, compare bearer tokens using
`secrets.compare_digest`, and map errors to stable JSON status codes without
secret-bearing messages. Add `signer` dispatch in `__main__.py` with explicit
paths for keystore, credential directory, state database, host, port, and
chain ID; default to `127.0.0.1:8780` and chain `11155111`.

- [ ] **Step 4: Run tests and commit**

Run: `python -m pytest tests/test_signer_service.py -q` and `python -m ruff check src/noyra/signer_service.py src/noyra/__main__.py tests/test_signer_service.py`.

Commit: `git add src/noyra/signer_service.py src/noyra/__main__.py tests/test_signer_service.py && git commit -m "feat: expose self-hosted signer HTTP service"`

### Task 4: systemd confinement and contract tests

**Files:**
- Create: `deploy/systemd/noyra-signer.service`
- Test: `tests/test_signer_service.py`
- Modify: `deploy/systemd/noyra-credentials.conf.example`

**Interfaces:**
- Produce a unit that starts `python -m noyra signer` as `noyra-signer` and injects credentials through `%d`.

- [ ] **Step 1: Write failing unit contract tests**

```python
def test_signer_unit_is_loopback_only_and_credential_backed() -> None:
    text = (ROOT / "deploy/systemd/noyra-signer.service").read_text()
    assert "User=noyra-signer" in text
    assert "127.0.0.1" in text and "8780" in text
    assert "LoadCredential=" in text
    assert "ProtectSystem=strict" in text
```

- [ ] **Step 2: Run the test and verify failure**

Run: `python -m pytest tests/test_signer_service.py -q`.

Expected: the unit file is missing.

- [ ] **Step 3: Add the confined unit and credential drop-in example**

Use `User=noyra-signer`, `Group=noyra-signer`, `WorkingDirectory=/var/lib/noyra-signer`,
`LoadCredential=wallet-password:/etc/noyra-signer/credentials/wallet-password`,
`LoadCredential=sepolia-rpc-url:/etc/noyra-signer/credentials/sepolia-rpc-url`,
and `LoadCredential=signer-token:/etc/noyra/credentials/staging-signer-token`.
Set `NoNewPrivileges`, `PrivateTmp`, `PrivateDevices`, `ProtectSystem=strict`,
`ProtectHome`, `ReadWritePaths=/var/lib/noyra-signer`, and restart limits.

- [ ] **Step 4: Run contract/static checks and commit**

Run: `python -m pytest tests/test_signer_service.py -q`, `python -m ruff check src/noyra/signer_service.py src/noyra/__main__.py tests/test_signer_service.py`, and `git diff --check`.

Commit: `git add deploy/systemd/noyra-signer.service deploy/systemd/noyra-credentials.conf.example tests/test_signer_service.py && git commit -m "ops: confine self-hosted signer with systemd"`

### Task 5: Full verification and deployment handoff

**Files:**
- Modify only files listed above if a verification failure identifies a concrete defect.

- [ ] **Step 1: Run focused tests and static checks**

Run: `python -m pytest tests/test_signer_service.py tests/test_wallet_execution.py tests/test_wallet_local.py -q`.

- [ ] **Step 2: Run repository checks**

Run: `python -m ruff check src tests/test_signer_service.py`, `python -m mypy src/noyra/signer_service.py`, `python -m compileall -q src`, and `git diff --check`.

- [ ] **Step 3: Inspect the diff**

Confirm no private key, password, token, RPC URL, or raw signed transaction appears in tracked files, tests, logs, or error strings.

- [ ] **Step 4: Prepare host deployment commands**

Build/install the release on the staging host, install the unit, run `systemctl daemon-reload`, enable/start the signer, configure the Cloudflare hostname route, set the Noyra external signer variables, and run the exploratory Sepolia acceptance sequence. Record the final commit SHA and keep third-party KMS marked out of scope.
