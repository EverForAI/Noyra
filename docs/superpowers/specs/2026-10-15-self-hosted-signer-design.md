# Self-Hosted HTTPS Signer Design

## Goal

Provide the staging wallet with a separately confined signer process on the
same Ubuntu host. The Noyra runtime keeps only the signer endpoint and bearer
credential; the signer process is the only process allowed to read the
encrypted Ethereum keystore and wallet password.

This is an operationally independent signer, not a cloud KMS or HSM. The
release evidence must call it `self-hosted signer` and must not claim hardware
key isolation.

## Boundary and deployment

`noyra.service` continues to run as `noyra` and listens on
`127.0.0.1:8765`. `noyra-signer.service` runs as `noyra-signer`, listens only
on `127.0.0.1:8780`, and reads the following credentials through systemd
`LoadCredential`:

- `wallet-password` for the Ethereum V3 keystore;
- `sepolia-rpc-url` for the chain adapter;
- `signer-token` for bearer authentication.

The keystore is root-created with mode `0600` and owned by `noyra-signer`.
The signer account, RPC URL, password, bearer token, and raw signed transaction
are never written to logs, SQLite, or ordinary evidence exports.

The public HTTPS endpoint is `https://signer-staging.hong168.win`, terminated
by the existing Cloudflare Tunnel and routed to `http://127.0.0.1:8780`.
The process itself never binds a public address.

## Protocol

Every POST requires `Authorization: Bearer <token>`, a JSON content type, a
bounded body, and an exact JSON object shape. Unknown routes and methods return
`404`/`405`; authentication failures return `401` without revealing whether a
request would otherwise be valid.

The fixed routes match `HTTPSWalletSigner`:

- `POST /migration-identity`: accepts `signer_id` and a bounded EIP-191
  challenge, and returns the account signature after checking the configured
  signer ID.
- `POST /v1/nonce`: accepts the configured address and chain ID, verifies the
  chain ID through `eth_chainId`, and returns the pending nonce.
- `POST /v1/fee-quote`: validates a complete `WalletUnsignedTransfer`, checks
  the source address and chain, and returns a bounded gas limit and fee.
- `POST /v1/transfer`: validates the complete envelope, performs nonce,
  gas, fee, and balance admission, signs a legacy EIP-155 transaction, and
  broadcasts it.
- `POST /v1/receipt`: validates a transaction hash and chain ID and returns a
  canonical receipt only when the transaction, receipt, and block agree.

The service supports native Sepolia ETH first. ERC-20 envelopes are rejected
until an implementation has the same calldata, balance, and receipt-effect
checks as the in-process signer.

## Idempotency and failure handling

The signer stores request state in a private SQLite database under
`/var/lib/noyra-signer`. A request ID is bound to a canonical transfer hash.
Repeating the same request returns the original broadcast result; reusing the
ID for a different transfer is rejected. The row is written before broadcast
with the deterministic transaction hash and envelope metadata, then updated
after a successful response.

If the RPC call times out or returns an ambiguous error after submission, the
request remains `unknown` and the signer never broadcasts that request again.
The caller must query `/v1/receipt` or reconcile it out of band. A restart
reopens the SQLite state and therefore preserves idempotency and receipt
lookups.

Nonce conflicts, wrong chain IDs, wrong addresses, invalid envelopes, RPC
errors, insufficient balance, and fee/gas violations fail closed with a safe
HTTP error and no secret material.

## Verification

Unit tests run the HTTP handler with a fake bounded JSON-RPC adapter and an
ephemeral keystore. They cover authentication, identity proof, envelope
validation, nonce and fee responses, idempotency, ambiguous broadcast state,
restart recovery, and receipt validation. Static checks cover Ruff, mypy,
compileall, and the systemd unit contract.

Host acceptance then installs the unit, configures the Cloudflare route, and
proves the Sepolia identity, small transfer, duplicate request behavior,
restart recovery, and receipt query. The acceptance artifact records the
commit SHA and labels the result `self-hosted signer`; third-party KMS remains
explicitly out of scope.
