"""Loopback-only HTTPS wallet signer service primitives.

The service deliberately keeps the private account behind a small protocol
boundary.  It is suitable for the staging self-hosted signer; it is not a
cloud KMS or an HSM.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import math
import secrets
import sqlite3
from collections.abc import Mapping
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import BoundedSemaphore, RLock
from typing import Any, Protocol

import httpx
from eth_account.messages import encode_defunct
from eth_account.signers.local import LocalAccount
from eth_utils import to_checksum_address  # type: ignore[attr-defined]

from noyra.core.deadline import monotonic_time, operation_deadline, remaining_seconds
from noyra.core.http import (
    PublicDNSHTTPTransport,
    SyncHTTPTimeoutHook,
    call_sync_http_with_deadline,
    read_bounded_sync_response,
)
from noyra.core.request_io import DeadlineRequestReader
from noyra.core.types import canonical_json, strict_json_loads, utc_now
from noyra.wallet.dispatch import WalletRequestFence
from noyra.wallet.execution import (
    WalletBroadcastResult,
    WalletReceipt,
    WalletUnsignedTransfer,
)
from noyra.wallet.types import SQLITE_INT64_MAX, canonical_evm_address

CHAIN_ID = 11_155_111
MAX_BODY_BYTES = 16_384
MAX_RPC_RESPONSE_BYTES = 262_144
MAX_SIGNER_WORKERS = 16
SIGNER_REQUEST_SECONDS = 30.0
_LOGGER = logging.getLogger(__name__)
ALLOWED_RPC_METHODS = frozenset(
    {
        "eth_chainId",
        "eth_getTransactionCount",
        "eth_getBalance",
        "eth_gasPrice",
        "eth_estimateGas",
        "eth_sendRawTransaction",
        "eth_getTransactionReceipt",
        "eth_getTransactionByHash",
        "eth_getBlockByNumber",
        "eth_blockNumber",
    }
)


class SignerError(RuntimeError):
    """Safe error that is suitable for returning to the signer client."""

    def __init__(self, message: str, *, code: str = "request_rejected") -> None:
        super().__init__(message)
        self.code = code


class SignerRPC(Protocol):
    def call(self, method: str, params: list[object]) -> object: ...


class HTTPRPC:
    """Small HTTPS JSON-RPC client with an explicit method allow-list."""

    def __init__(self, url: str, chain_id: int, *, timeout: float = 15.0) -> None:
        from noyra.wallet.types import _rpc_url

        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("rpc timeout is invalid")
        self.url = _rpc_url(url)
        self.chain_id = chain_id
        self.timeout = timeout
        self.client = httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            transport=PublicDNSHTTPTransport(
                max_connections=4, remaining=lambda: remaining_seconds(timeout)
            ),
        )

    def call(self, method: str, params: list[object]) -> object:
        if method not in ALLOWED_RPC_METHODS:
            raise SignerError("rpc operation is not allowed")
        hook = SyncHTTPTimeoutHook()
        try:
            budget = remaining_seconds(self.timeout)
            end = monotonic_time() + budget

            def request() -> bytes:
                # Authorization is enforced by the engine; this trace fence
                # prevents a timed-out worker from starting a late RPC send.
                fence = WalletRequestFence(end, hook, payment=False)
                try:
                    with operation_deadline(budget):
                        fence.check_deadline()
                        with self.client.stream(
                            "POST",
                            self.url,
                            json={
                                "jsonrpc": "2.0",
                                "id": "noyra-signer-v1",
                                "method": method,
                                "params": params,
                            },
                            headers={
                                "Accept": "application/json",
                                "Accept-Encoding": "identity",
                                "Content-Type": "application/json",
                            },
                            timeout=httpx.Timeout(remaining_seconds(budget)),
                            extensions={"trace": fence.trace},
                        ) as response:
                            hook.register(response)
                            try:
                                if response.status_code != 200:
                                    raise SignerError("rpc unavailable")
                                if (
                                    response.headers.get("Content-Encoding", "identity").lower()
                                    != "identity"
                                ):
                                    raise SignerError("rpc response encoding is invalid")
                                return read_bounded_sync_response(
                                    response,
                                    max_body_bytes=MAX_RPC_RESPONSE_BYTES,
                                    total_timeout_seconds=budget,
                                    deadline=end,
                                )
                            finally:
                                hook.clear(response)
                finally:
                    fence.close()

            body = strict_json_loads(
                call_sync_http_with_deadline(request, timeout=budget, on_timeout=hook.cancel)
            )
            if (
                not isinstance(body, dict)
                or body.get("jsonrpc") != "2.0"
                or body.get("id") != "noyra-signer-v1"
            ):
                raise SignerError("rpc response is invalid")
            if "error" in body or "result" not in body:
                raise SignerError("rpc request rejected")
            return body["result"]
        except SignerError:
            raise
        except Exception as exc:
            # The engine persists unknown for every sendRawTransaction failure.
            # This client never retries, including on deadline failure.
            raise SignerError("rpc unavailable") from exc

    def close(self) -> None:
        self.client.close()


def _quantity(value: object, *, maximum: int = 2**256 - 1) -> int:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise SignerError("rpc response is invalid")
    try:
        result = int(value, 16)
    except ValueError as exc:
        raise SignerError("rpc response is invalid") from exc
    if result < 0 or result > maximum:
        raise SignerError("rpc response is invalid")
    return result


def _hash(value: object) -> str:
    if not isinstance(value, str) or len(value) != 66 or not value.startswith("0x"):
        raise SignerError("receipt is invalid")
    try:
        int(value[2:], 16)
    except ValueError as exc:
        raise SignerError("receipt is invalid") from exc
    return value.lower()


def parse_transfer(payload: object) -> WalletUnsignedTransfer:
    """Parse the exact fixed transfer envelope used by Noyra."""
    if not isinstance(payload, Mapping):
        raise SignerError("invalid transfer")
    expected = {
        "chain_id",
        "nonce",
        "gas_limit",
        "max_fee_per_gas",
        "source_address",
        "to_address",
        "value",
        "data",
        "asset_type",
        "contract_address",
    }
    if set(payload) != expected:
        raise SignerError("invalid transfer")
    try:
        transfer = WalletUnsignedTransfer.model_validate(dict(payload), strict=True)
    except Exception as exc:
        raise SignerError("invalid transfer") from exc
    if transfer.chain_id != CHAIN_ID or transfer.asset_type != "native":
        raise SignerError("invalid transfer")
    return transfer


def _transfer_hash(transfer: WalletUnsignedTransfer) -> str:
    encoded = canonical_json(transfer.model_dump(mode="json"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class SignerState:
    """Durable request state; only non-secret envelope metadata is stored."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.lock = RLock()

    @classmethod
    def open(cls, path: str | Any) -> SignerState:
        connection = sqlite3.connect(str(path), check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS signer_requests ("
            "request_id TEXT PRIMARY KEY, transfer_hash TEXT NOT NULL, status TEXT NOT NULL,"
            "tx_hash TEXT, chain_id INTEGER NOT NULL, nonce INTEGER NOT NULL,"
            "gas_limit INTEGER NOT NULL, max_fee_per_gas TEXT NOT NULL, accepted_at TEXT,"
            "transfer_json TEXT, envelope_status TEXT NOT NULL DEFAULT 'legacy_unbound')"
        )
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(signer_requests)")}
        if "transfer_json" not in columns:
            connection.execute("ALTER TABLE signer_requests ADD COLUMN transfer_json TEXT")
        if "envelope_status" not in columns:
            # Rows created before transfer envelopes were persisted cannot be
            # reconstructed safely from a transaction hash.  Mark them
            # explicitly so reconciliation can distinguish an unavailable
            # historical contract from a missing receipt.
            connection.execute(
                "ALTER TABLE signer_requests ADD COLUMN envelope_status TEXT "
                "NOT NULL DEFAULT 'legacy_unbound'"
            )
        candidates = connection.execute(
            "SELECT * FROM signer_requests WHERE envelope_status IS NULL "
            "OR envelope_status!='bound'"
        )
        while rows := candidates.fetchmany(256):
            for row in rows:
                bound = False
                try:
                    transfer = parse_transfer(strict_json_loads(row["transfer_json"]))
                    bound = (
                        _transfer_hash(transfer) == row["transfer_hash"]
                        and transfer.chain_id == row["chain_id"]
                        and transfer.nonce == row["nonce"]
                        and transfer.gas_limit == row["gas_limit"]
                        and transfer.max_fee_per_gas == row["max_fee_per_gas"]
                    )
                except (SignerError, TypeError, ValueError):
                    pass
                connection.execute(
                    "UPDATE signer_requests SET envelope_status=? WHERE request_id=?",
                    ("bound" if bound else "legacy_unbound", row["request_id"]),
                )
        connection.commit()
        return cls(connection)

    def close(self) -> None:
        self.connection.close()


class SignerEngine:
    def __init__(
        self,
        *,
        account: LocalAccount,
        signer_id: str,
        chain_id: int = CHAIN_ID,
        rpc: SignerRPC,
        state: SignerState,
    ) -> None:
        if not isinstance(account, LocalAccount):
            raise ValueError("signer account is invalid")
        if not isinstance(signer_id, str) or not signer_id.strip():
            raise ValueError("signer identity is invalid")
        if chain_id != CHAIN_ID:
            raise ValueError("signer chain is invalid")
        self.account = account
        self.address = canonical_evm_address(account.address)
        self.signer_id = signer_id.strip()
        self.chain_id = chain_id
        self.rpc = rpc
        self.state = state

    def _check_chain(self) -> None:
        if _quantity(self.rpc.call("eth_chainId", []), maximum=SQLITE_INT64_MAX) != self.chain_id:
            raise SignerError("chain id mismatch")

    def _admit(self, transfer: WalletUnsignedTransfer) -> None:
        self._check_chain()
        nonce = _quantity(
            self.rpc.call("eth_getTransactionCount", [self.address, "pending"]),
            maximum=SQLITE_INT64_MAX,
        )
        if nonce != transfer.nonce:
            raise SignerError("nonce conflict", code="nonce_conflict")
        gas = _quantity(
            self.rpc.call("eth_estimateGas", [self._rpc_transfer(transfer)]), maximum=30_000_000
        )
        fee = _quantity(self.rpc.call("eth_gasPrice", []))
        if gas > transfer.gas_limit or fee <= 0 or fee > int(transfer.max_fee_per_gas):
            raise SignerError("gas or fee exceeds envelope")
        balance = _quantity(self.rpc.call("eth_getBalance", [self.address, "pending"]))
        if balance < int(transfer.value) + transfer.gas_limit * int(transfer.max_fee_per_gas):
            raise SignerError("insufficient balance")

    @staticmethod
    def _rpc_transfer(transfer: WalletUnsignedTransfer) -> dict[str, object]:
        return {
            "from": transfer.source_address,
            "to": transfer.to_address,
            "value": hex(int(transfer.value)),
            "data": transfer.data,
        }

    def _sign(self, transfer: WalletUnsignedTransfer) -> tuple[str, str]:
        try:
            transaction: Any = {
                "chainId": transfer.chain_id,
                "nonce": transfer.nonce,
                "gas": transfer.gas_limit,
                "gasPrice": int(transfer.max_fee_per_gas),
                "to": to_checksum_address(transfer.to_address),
                "value": int(transfer.value),
                "data": transfer.data,
            }
            signed = self.account.sign_transaction(transaction)
            raw = "0x" + bytes(signed.raw_transaction).hex()
            tx_hash = "0x" + bytes(signed.hash).hex()
            return raw, tx_hash
        except Exception as exc:
            raise SignerError("signing failed") from exc

    def prove_identity(self, signer_id: str, challenge: str) -> str:
        if (
            signer_id != self.signer_id
            or not isinstance(challenge, str)
            or not 1 <= len(challenge) <= 4096
        ):
            raise SignerError("identity rejected")
        try:
            context = strict_json_loads(challenge)
            fields = {
                "domain",
                "task_id",
                "target_id",
                "manifest_digest",
                "target_identity",
                "signer_id",
                "nonce",
            }
            if not isinstance(context, dict) or set(context) != fields:
                raise ValueError("identity context is invalid")
            if (
                context["domain"] != "noyra-migration-wallet/v1"
                or context["signer_id"] != self.signer_id
            ):
                raise ValueError("identity domain is invalid")
            for key in ("task_id", "target_id", "target_identity"):
                value = context[key]
                if (
                    not isinstance(value, str)
                    or not value
                    or len(value) > 512
                    or any(ord(c) < 32 for c in value)
                ):
                    raise ValueError("identity binding is invalid")
            for key in ("manifest_digest", "nonce"):
                value = context[key]
                if (
                    not isinstance(value, str)
                    or len(value) != 64
                    or any(c not in "0123456789abcdef" for c in value)
                ):
                    raise ValueError("identity digest is invalid")
            canonical = canonical_json(context)
            if challenge != canonical:
                raise ValueError("identity encoding is invalid")
            return "0x" + self.account.sign_message(encode_defunct(text=canonical)).signature.hex()
        except Exception as exc:
            raise SignerError("identity rejected") from exc

    def nonce(self, address: str, chain_id: int) -> int:
        if canonical_evm_address(address) != self.address or chain_id != self.chain_id:
            raise SignerError("nonce request rejected")
        self._check_chain()
        return _quantity(
            self.rpc.call("eth_getTransactionCount", [self.address, "pending"]),
            maximum=SQLITE_INT64_MAX,
        )

    def fee_quote(self, transfer: WalletUnsignedTransfer) -> tuple[int, str]:
        checked = parse_transfer(transfer.model_dump(mode="json"))
        if checked.source_address != self.address:
            raise SignerError("transfer source rejected")
        self._check_chain()
        gas = _quantity(
            self.rpc.call("eth_estimateGas", [self._rpc_transfer(checked)]), maximum=30_000_000
        )
        fee = _quantity(self.rpc.call("eth_gasPrice", []))
        gas = max(21_000, (gas * 120 + 99) // 100)
        if gas > 30_000_000 or fee <= 0:
            raise SignerError("fee quote unavailable")
        return gas, str(fee * 2)

    def transfer(self, request_id: str, transfer: WalletUnsignedTransfer) -> WalletBroadcastResult:
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 256:
            raise SignerError("request identity is invalid")
        checked = parse_transfer(transfer.model_dump(mode="json"))
        if checked.source_address != self.address:
            raise SignerError("transfer source rejected")
        transfer_hash = _transfer_hash(checked)
        with self.state.lock, self.state.connection:
            row = self.state.connection.execute(
                "SELECT * FROM signer_requests WHERE request_id=?", (request_id,)
            ).fetchone()
            if row is not None:
                if row["transfer_hash"] != transfer_hash:
                    raise SignerError("request identity is already bound", code="request_conflict")
                if row["status"] == "unknown":
                    raise SignerError("broadcast unknown", code="broadcast_unknown")
                if row["status"] == "pending":
                    self.state.connection.execute(
                        "UPDATE signer_requests SET status='unknown' WHERE request_id=?",
                        (request_id,),
                    )
                    self.state.connection.commit()
                    raise SignerError("broadcast unknown", code="broadcast_unknown")
                return WalletBroadcastResult(
                    row["tx_hash"], row["chain_id"], row["nonce"], row["accepted_at"]
                )
            raw, tx_hash = self._sign(checked)
            self._admit(checked)
            self.state.connection.execute(
                "INSERT INTO signer_requests("
                "request_id,transfer_hash,status,tx_hash,chain_id,nonce,gas_limit,"
                "max_fee_per_gas,accepted_at,transfer_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    request_id,
                    transfer_hash,
                    "pending",
                    tx_hash,
                    checked.chain_id,
                    checked.nonce,
                    checked.gas_limit,
                    checked.max_fee_per_gas,
                    None,
                    canonical_json(checked.model_dump(mode="json")),
                ),
            )
            self.state.connection.execute(
                "UPDATE signer_requests SET envelope_status='bound' WHERE request_id=?",
                (request_id,),
            )
            # Make the pending marker durable before any network side effect.
            # A process crash after broadcast must therefore recover as
            # unknown instead of issuing a second transaction.
            self.state.connection.commit()
            try:
                returned = self.rpc.call("eth_sendRawTransaction", [raw])
                if not isinstance(returned, str) or returned.lower() != tx_hash.lower():
                    raise SignerError("broadcast response is invalid")
            except Exception as exc:
                self.state.connection.execute(
                    "UPDATE signer_requests SET status='unknown' WHERE request_id=?", (request_id,)
                )
                self.state.connection.commit()
                if isinstance(exc, SignerError):
                    raise SignerError("broadcast unknown", code="broadcast_unknown") from exc
                raise SignerError("broadcast unknown", code="broadcast_unknown") from exc
            accepted_at = utc_now()
            self.state.connection.execute(
                "UPDATE signer_requests SET status='broadcast', accepted_at=? WHERE request_id=?",
                (accepted_at, request_id),
            )
            return WalletBroadcastResult(tx_hash, checked.chain_id, checked.nonce, accepted_at)

    def receipt(self, tx_hash: str, chain_id: int) -> WalletReceipt | None:
        if chain_id != self.chain_id:
            raise SignerError("receipt request rejected")
        requested = _hash(tx_hash)
        with self.state.lock:
            request = self.state.connection.execute(
                "SELECT transfer_hash,chain_id,nonce,status,transfer_json,envelope_status "
                "FROM signer_requests WHERE tx_hash=? AND chain_id=?",
                (requested, chain_id),
            ).fetchone()
        if request is None:
            return None
        if request["status"] not in {"pending", "unknown", "broadcast"}:
            raise SignerError("receipt request rejected")
        if request["envelope_status"] == "legacy_unbound":
            raise SignerError("receipt request is legacy_unbound", code="legacy_unbound")
        try:
            saved_payload = json.loads(request["transfer_json"])
            expected_transfer = parse_transfer(saved_payload)
        except (SignerError, TypeError, ValueError, KeyError) as error:
            raise SignerError("receipt request envelope is unavailable") from error
        if (
            _transfer_hash(expected_transfer) != request["transfer_hash"]
            or expected_transfer.chain_id != int(request["chain_id"])
            or expected_transfer.nonce != int(request["nonce"])
        ):
            raise SignerError("receipt request envelope is invalid")
        raw = self.rpc.call("eth_getTransactionReceipt", [requested])
        if raw is None:
            return None
        if not isinstance(raw, Mapping):
            raise SignerError("receipt is invalid")
        if _hash(raw.get("transactionHash")) != requested:
            raise SignerError("receipt is invalid")
        status = _quantity(raw.get("status"), maximum=1)
        block = _quantity(raw.get("blockNumber"), maximum=SQLITE_INT64_MAX)
        block_hash = _hash(raw.get("blockHash"))
        tx = self.rpc.call("eth_getTransactionByHash", [requested])
        if not isinstance(tx, Mapping) or _hash(tx.get("hash")) != requested:
            raise SignerError("receipt is invalid")
        if canonical_evm_address(tx.get("from", "")) != self.address:
            raise SignerError("receipt is invalid")
        if _quantity(tx.get("chainId"), maximum=SQLITE_INT64_MAX) != self.chain_id:
            raise SignerError("receipt is invalid")
        if (
            canonical_evm_address(tx.get("to", "")) != expected_transfer.to_address
            or _quantity(tx.get("nonce"), maximum=SQLITE_INT64_MAX) != expected_transfer.nonce
            or _quantity(tx.get("value")) != int(expected_transfer.value)
            or tx.get("input", tx.get("data")) != expected_transfer.data
        ):
            raise SignerError("receipt transaction does not match the broadcast request")
        canonical_block = self.rpc.call("eth_getBlockByNumber", [hex(block), False])
        if (
            not isinstance(canonical_block, Mapping)
            or _hash(canonical_block.get("hash")) != block_hash
            or _quantity(canonical_block.get("number"), maximum=SQLITE_INT64_MAX) != block
        ):
            raise SignerError("receipt is not canonical")
        latest = _quantity(self.rpc.call("eth_blockNumber", []), maximum=SQLITE_INT64_MAX)
        return WalletReceipt(
            requested,
            chain_id,
            1 if status == 1 else 0,
            block,
            block_hash.lower(),
            max(0, latest - block + 1),
        )


class SignerHTTPServer(ThreadingHTTPServer):
    def __init__(
        self, engine: SignerEngine, *, bearer_token: str, host: str = "127.0.0.1", port: int = 8780
    ):
        if host != "127.0.0.1":
            raise ValueError("signer host must be loopback")
        if (
            not isinstance(bearer_token, str)
            or not bearer_token
            or len(bearer_token) > 4096
            or not bearer_token.isascii()
        ):
            raise ValueError("signer token is invalid")
        self.engine = engine
        self.bearer_token = bearer_token
        self._worker_slots = BoundedSemaphore(MAX_SIGNER_WORKERS)
        super().__init__((host, port), self._handler())
        self.url = f"http://{host}:{self.server_port}"

    daemon_threads = True

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._worker_slots.acquire(blocking=False):
            with suppress(OSError):
                request.settimeout(0.1)
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\n"
                    b"Content-Length: 0\r\n\r\n"
                )
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._worker_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._worker_slots.release()

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "NoyraSigner/1"

            def setup(self) -> None:
                super().setup()
                self.connection.settimeout(SIGNER_REQUEST_SECONDS)
                self.rfile = io.BufferedReader(
                    DeadlineRequestReader(
                        self.rfile,
                        self.connection,
                        SIGNER_REQUEST_SECONDS,
                        max_bytes=MAX_BODY_BYTES + 16_384,
                    )
                )

            def handle_one_request(self) -> None:
                try:
                    with operation_deadline(SIGNER_REQUEST_SECONDS):
                        super().handle_one_request()
                except OSError:
                    self.close_connection = True

            def log_message(self, format: str, *args: object) -> None:
                del format, args

            def do_POST(self) -> None:
                authorization = self.headers.get("Authorization", "")
                if not authorization.isascii() or not secrets.compare_digest(
                    authorization, f"Bearer {owner.bearer_token}"
                ):
                    self._send(401, {"error": "unauthorized"})
                    return
                try:
                    if (
                        self.headers.get_all("Transfer-Encoding")
                        or len(self.headers.get_all("Content-Length", [])) != 1
                    ):
                        raise SignerError("request framing is invalid")
                    try:
                        length = int(self.headers.get("Content-Length", "-1"))
                    except ValueError as exc:
                        raise SignerError("request body is invalid") from exc
                    if length < 0 or length > MAX_BODY_BYTES:
                        raise SignerError("request body is invalid")
                    if (
                        self.headers.get("Content-Type", "").split(";", 1)[0].lower()
                        != "application/json"
                    ):
                        raise SignerError("request content type is invalid")
                    try:
                        body = self.rfile.read(length)
                    except OSError:
                        # A timed-out/reset request body must not reach dispatch or
                        # the generic 500 handler. Close without starting a reply,
                        # even if the socket timeout precedes the absolute budget.
                        self.close_connection = True
                        return
                    try:
                        if len(body) != length:
                            raise SignerError("request body is incomplete")
                        payload = strict_json_loads(body)
                    except (UnicodeDecodeError, ValueError) as exc:
                        raise SignerError("request body is invalid") from exc
                    status, result = self._dispatch(self.path, payload)
                    self._send(status, result)
                except SignerError as exc:
                    status = (
                        409
                        if exc.code
                        in {
                            "nonce_conflict",
                            "request_conflict",
                            "broadcast_unknown",
                            "legacy_unbound",
                        }
                        else 400
                    )
                    self._send(status, {"error": str(exc), "code": exc.code})
                except Exception as exc:
                    request_id = secrets.token_hex(8)
                    route = self.path.split("?", 1)[0][:128]
                    _LOGGER.exception(
                        "signer request failed request_id=%s route=%s exception=%s",
                        request_id,
                        route,
                        type(exc).__name__,
                    )
                    self._send(
                        500,
                        {"error": "signer_internal_error", "request_id": request_id},
                    )

            def _dispatch(self, path: str, payload: object) -> tuple[int, dict[str, object]]:
                if not isinstance(payload, Mapping):
                    raise SignerError("request body is invalid")
                if path == "/migration-identity":
                    if set(payload) != {"signer_id", "challenge"}:
                        raise SignerError("identity request is invalid")
                    return 200, {
                        "signature": owner.engine.prove_identity(
                            payload["signer_id"], payload["challenge"]
                        )
                    }
                if path == "/v1/nonce":
                    if set(payload) != {"address", "chain_id"}:
                        raise SignerError("nonce request is invalid")
                    return 200, {
                        "nonce": owner.engine.nonce(payload["address"], payload["chain_id"])
                    }
                if path == "/v1/fee-quote":
                    if set(payload) != {"transfer"}:
                        raise SignerError("fee request is invalid")
                    gas, fee = owner.engine.fee_quote(parse_transfer(payload["transfer"]))
                    return 200, {"gas_limit": gas, "max_fee_per_gas": fee}
                if path == "/v1/transfer":
                    if set(payload) != {"request_id", "transfer"}:
                        raise SignerError("transfer request is invalid")
                    result = owner.engine.transfer(
                        payload["request_id"], parse_transfer(payload["transfer"])
                    )
                    return 200, {
                        "tx_hash": result.tx_hash,
                        "chain_id": result.chain_id,
                        "nonce": result.nonce,
                        "accepted_at": result.accepted_at,
                    }
                if path == "/v1/receipt":
                    if set(payload) != {"tx_hash", "chain_id"}:
                        raise SignerError("receipt request is invalid")
                    receipt = owner.engine.receipt(payload["tx_hash"], payload["chain_id"])
                    if receipt is None:
                        return 404, {"error": "receipt not found"}
                    return 200, {
                        "tx_hash": receipt.tx_hash,
                        "chain_id": receipt.chain_id,
                        "status": receipt.status,
                        "block_number": receipt.block_number,
                        "block_hash": receipt.block_hash,
                        "confirmations": receipt.confirmations,
                    }
                return 404, {"error": "route not found"}

            def _send(self, status: int, body: dict[str, object]) -> None:
                raw = json.dumps(body, separators=(",", ":")).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.close_connection = True
                self.connection.settimeout(remaining_seconds(SIGNER_REQUEST_SECONDS))
                self.end_headers()
                self.wfile.write(raw)

        return Handler


__all__ = [
    "ALLOWED_RPC_METHODS",
    "CHAIN_ID",
    "SignerEngine",
    "SignerError",
    "SignerHTTPServer",
    "SignerState",
    "parse_transfer",
]
