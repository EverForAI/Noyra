"""Bounded wallet payment execution behind an independent signer boundary.

Only fixed native/ERC-20 transfer envelopes cross this boundary. No private key,
seed, mnemonic, arbitrary RPC method, URL, calldata, or model-controlled
transaction is accepted here. Production signers should implement the Protocol
in a separate process or service; MockSigner is test-only.
"""

from __future__ import annotations

import ipaddress
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime
from types import TracebackType
from typing import Any, Literal, Protocol, Self, cast
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from noyra.core.admission import OperationInvalidated, current_lease
from noyra.core.database import Database
from noyra.core.errors import IntegrityError, InvalidTransitionError, NotFoundError
from noyra.core.http import (
    DEFAULT_MAX_HEADER_BYTES,
    HTTPResponseLimitError,
    PublicDNSHTTPTransport,
    SyncHTTPTimeoutHook,
    call_sync_http_with_deadline,
    read_bounded_sync_response,
    validate_response_headers,
)
from noyra.core.identity import validate_subject_id
from noyra.core.types import content_hash, new_id, strict_json_loads, utc_now

from .economy import WalletEconomyStore
from .economy_types import PaymentOrderRecord, canonical_amount
from .types import SQLITE_INT64_MAX, canonical_evm_address

_UINT = re.compile(r"(?:0|[1-9][0-9]{0,77})\Z")
_HASH = re.compile(r"0x[0-9a-fA-F]{64}\Z")
_MAX_UINT256 = (1 << 256) - 1
_MAX_GAS_LIMIT = 30_000_000
_SIGNER_MAX_RESPONSE_BYTES = 16_384
_SIGNER_MAX_HEADER_BYTES = DEFAULT_MAX_HEADER_BYTES


class WalletExecutionError(RuntimeError):
    """Base class for classified execution failures."""


class WalletSignerError(WalletExecutionError):
    """The isolated signer rejected the fixed transfer."""


class WalletBroadcastUnknownError(WalletExecutionError):
    """Signing/broadcast may have succeeded but the response was lost."""

    def __init__(
        self, message: str = "wallet broadcast outcome is unknown", *, tx_hash: str | None = None
    ):
        super().__init__(message)
        self.tx_hash = tx_hash


class WalletChainReorganizationError(WalletExecutionError):
    """A previously confirmed receipt is no longer canonical."""


def _uint(value: str, label: str, *, positive: bool = False) -> str:
    if not isinstance(value, str) or _UINT.fullmatch(value) is None:
        raise ValueError(f"{label} must be a canonical unsigned integer string")
    parsed = int(value)
    if parsed > _MAX_UINT256 or (positive and parsed == 0):
        raise ValueError(f"{label} is out of range")
    return value


def _tx_hash(value: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise ValueError("wallet transaction hash is invalid")
    return value.lower()


class WalletTransferIntent(BaseModel):
    """Immutable structured input permitted from the business process."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    order_id: str = Field(min_length=1, max_length=128)
    subject_id: str = Field(min_length=1, max_length=128)
    network_id: str = Field(min_length=1, max_length=128)
    asset_id: str = Field(min_length=1, max_length=128)
    asset_type: Literal["native", "token"]
    contract_address: str | None = Field(default=None, min_length=42, max_length=42)
    source_address: str = Field(min_length=42, max_length=42)
    recipient_address: str = Field(min_length=42, max_length=42)
    amount: str = Field(min_length=1, max_length=78)
    chain_id: int = Field(ge=1, le=SQLITE_INT64_MAX)
    nonce: int = Field(ge=0, le=SQLITE_INT64_MAX)
    gas_limit: int = Field(ge=21_000, le=_MAX_GAS_LIMIT)
    max_fee_per_gas: str = Field(min_length=1, max_length=78)

    @field_validator("source_address", "recipient_address", "contract_address")
    @classmethod
    def address(cls, value: str | None) -> str | None:
        return None if value is None else canonical_evm_address(value)

    @field_validator("amount")
    @classmethod
    def amount_value(cls, value: str) -> str:
        return _uint(canonical_amount(value), "transfer amount", positive=True)

    @field_validator("max_fee_per_gas")
    @classmethod
    def fee_value(cls, value: str) -> str:
        return _uint(value, "max fee per gas", positive=True)

    @model_validator(mode="after")
    def shape(self) -> WalletTransferIntent:
        if self.asset_type == "native" and self.contract_address is not None:
            raise ValueError("native transfer cannot contain a token contract")
        if self.asset_type == "token" and self.contract_address is None:
            raise ValueError("token transfer requires its registered contract")
        if self.asset_type == "token" and self.gas_limit < 50_000:
            raise ValueError("token transfer gas limit is too low")
        if self.source_address == self.recipient_address:
            raise ValueError("wallet source and recipient must differ")
        return self


class WalletUnsignedTransfer(BaseModel):
    """Canonical EVM native transfer or ERC-20 transfer envelope."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    chain_id: int = Field(ge=1, le=SQLITE_INT64_MAX)
    nonce: int = Field(ge=0, le=SQLITE_INT64_MAX)
    gas_limit: int = Field(ge=21_000, le=_MAX_GAS_LIMIT)
    max_fee_per_gas: str = Field(min_length=1, max_length=78)
    source_address: str = Field(min_length=42, max_length=42)
    to_address: str = Field(min_length=42, max_length=42)
    value: str = Field(min_length=1, max_length=78)
    data: str = Field(min_length=2, max_length=2048)
    asset_type: Literal["native", "token"]
    contract_address: str | None = Field(default=None, min_length=42, max_length=42)

    @field_validator("source_address", "to_address", "contract_address")
    @classmethod
    def addresses(cls, value: str | None) -> str | None:
        return None if value is None else canonical_evm_address(value)

    @field_validator("max_fee_per_gas", "value")
    @classmethod
    def quantities(cls, value: str) -> str:
        return _uint(value, "transaction quantity")

    @field_validator("data")
    @classmethod
    def data_value(cls, value: str) -> str:
        if (
            not value.startswith("0x")
            or len(value) % 2
            or any(c not in "0123456789abcdefABCDEF" for c in value[2:])
        ):
            raise ValueError("transaction data is invalid")
        return value.lower()

    @model_validator(mode="after")
    def shape(self) -> WalletUnsignedTransfer:
        if self.asset_type == "native":
            if self.contract_address is not None or self.data != "0x" or self.value == "0":
                raise ValueError("native transfer envelope is invalid")
        else:
            if self.contract_address is None or self.to_address != self.contract_address:
                raise ValueError("token transfer target is invalid")
            if self.value != "0" or len(self.data) != 138 or not self.data.startswith("0xa9059cbb"):
                raise ValueError("token transfer calldata is not canonical")
        return self


@dataclass(frozen=True)
class WalletBroadcastResult:
    tx_hash: str
    chain_id: int
    nonce: int
    accepted_at: str


@dataclass(frozen=True)
class WalletReceipt:
    tx_hash: str
    chain_id: int
    status: Literal[0, 1]
    block_number: int | None = None
    block_hash: str | None = None
    confirmations: int = 0
    effect_hash: str | None = None


class HTTPSWalletSigner:
    """Call an isolated signer over one fixed HTTPS JSON endpoint.

    The signer service owns all key material. Noyra sends only the canonical
    transfer envelope and accepts only a bounded transaction result or receipt.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        signer_id: str,
        client: httpx.Client | None = None,
        timeout_seconds: float = 15.0,
        bearer_token: str | None = None,
    ):
        parsed = urlparse(endpoint.rstrip("/"))
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("wallet signer endpoint must be credential-free HTTPS")
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError("wallet signer endpoint must use a public host")
        if type(signer_id) is not str or not 1 <= len(signer_id.strip()) <= 256:
            raise ValueError("wallet signer identity is invalid")
        if type(timeout_seconds) not in {int, float} or not 0 < timeout_seconds <= 60:
            raise ValueError("wallet signer timeout is invalid")
        self.endpoint = parsed.geturl()
        self.signer_id = signer_id.strip()
        self.timeout_seconds = timeout_seconds
        if bearer_token is not None and (
            not isinstance(bearer_token, str) or not 1 <= len(bearer_token.strip()) <= 4096
        ):
            raise ValueError("wallet signer bearer token is invalid")
        self.bearer_token = None if bearer_token is None else bearer_token.strip()
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            transport=PublicDNSHTTPTransport(max_connections=2),
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _request(self, path: str, payload: dict[str, Any]) -> tuple[int, bytes]:
        timeout_hook = SyncHTTPTimeoutHook()

        def perform_request() -> tuple[int, bytes]:
            deadline = time.monotonic() + self.timeout_seconds
            with self._client.stream(
                "POST",
                self.endpoint + path,
                json=payload,
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                    "Content-Type": "application/json",
                    "User-Agent": "Noyra-Wallet-Signer/0.1.0",
                    **(
                        {"Authorization": f"Bearer {self.bearer_token}"}
                        if self.bearer_token is not None
                        else {}
                    ),
                },
                follow_redirects=False,
                timeout=httpx.Timeout(self.timeout_seconds),
            ) as response:
                timeout_hook.register(response)
                try:
                    validate_response_headers(response, max_header_bytes=_SIGNER_MAX_HEADER_BYTES)
                    body = read_bounded_sync_response(
                        response,
                        max_body_bytes=_SIGNER_MAX_RESPONSE_BYTES,
                        max_header_bytes=_SIGNER_MAX_HEADER_BYTES,
                        total_timeout_seconds=self.timeout_seconds,
                        deadline=deadline,
                    )
                    if response.status_code == 200:
                        media_type = response.headers.get("content-type", "").split(";", 1)[0]
                        if media_type.casefold() != "application/json":
                            raise WalletExecutionError("wallet signer content type is invalid")
                    return response.status_code, body
                finally:
                    timeout_hook.clear(response)

        return cast(
            tuple[int, bytes],
            call_sync_http_with_deadline(
                perform_request,
                timeout=self.timeout_seconds,
                on_timeout=timeout_hook.cancel,
            ),
        )

    def sign_and_broadcast(
        self, transfer: WalletUnsignedTransfer, *, request_id: str
    ) -> WalletBroadcastResult:
        if not isinstance(transfer, WalletUnsignedTransfer):
            raise WalletSignerError("wallet signer transfer is invalid")
        if type(request_id) is not str or not 1 <= len(request_id) <= 256:
            raise WalletSignerError("wallet signer request identity is invalid")
        try:
            status_code, response_body = self._request(
                "/v1/transfer",
                {"request_id": request_id, "transfer": transfer.model_dump(mode="json")},
            )
            if 400 <= status_code < 500:
                raise WalletSignerError("wallet signer rejected transfer")
            if status_code != 200:
                raise WalletBroadcastUnknownError()
            body = strict_json_loads(response_body)
            if not isinstance(body, dict) or set(body) != {
                "tx_hash",
                "chain_id",
                "nonce",
                "accepted_at",
            }:
                raise WalletBroadcastUnknownError()
            response_chain_id = body.get("chain_id")
            response_nonce = body.get("nonce")
            accepted_at = body.get("accepted_at")
            if (
                type(response_chain_id) is not int
                or not 1 <= response_chain_id <= SQLITE_INT64_MAX
                or type(response_nonce) is not int
                or not 0 <= response_nonce <= SQLITE_INT64_MAX
                or not isinstance(accepted_at, str)
                or not 1 <= len(accepted_at.strip()) <= 128
            ):
                raise WalletBroadcastUnknownError()
            return WalletBroadcastResult(
                _tx_hash(body["tx_hash"]),
                response_chain_id,
                response_nonce,
                accepted_at,
            )
        except (WalletSignerError, WalletBroadcastUnknownError):
            raise
        except (
            HTTPResponseLimitError,
            TimeoutError,
            httpx.HTTPError,
            ValueError,
            TypeError,
        ) as error:
            raise WalletBroadcastUnknownError() from error

    def get_receipt(self, tx_hash: str, *, chain_id: int) -> WalletReceipt | None:
        if type(chain_id) is not int or not 1 <= chain_id <= SQLITE_INT64_MAX:
            raise WalletExecutionError("wallet signer receipt chain id is invalid")
        try:
            status_code, response_body = self._request(
                "/v1/receipt", {"tx_hash": _tx_hash(tx_hash), "chain_id": chain_id}
            )
            if status_code == 404:
                return None
            if 400 <= status_code < 500:
                raise WalletSignerError("wallet signer receipt rejected")
            if status_code != 200:
                raise WalletExecutionError("wallet signer receipt lookup failed")
            body = strict_json_loads(response_body)
            required = {"tx_hash", "chain_id", "status"}
            allowed = required | {"block_number", "block_hash", "confirmations", "effect_hash"}
            if not isinstance(body, dict) or not required <= set(body) <= allowed:
                raise WalletExecutionError("wallet signer receipt response is invalid")
            response_chain_id = body.get("chain_id")
            status = body.get("status")
            block_number = body.get("block_number")
            block_hash = body.get("block_hash")
            confirmations = body.get("confirmations", 0)
            effect_hash = body.get("effect_hash")
            response_tx_value = body.get("tx_hash")
            if not isinstance(response_tx_value, str):
                raise WalletExecutionError("wallet signer receipt response is invalid")
            response_tx_hash = _tx_hash(response_tx_value)
            if response_tx_hash != _tx_hash(tx_hash) or response_chain_id != chain_id:
                raise WalletExecutionError("wallet signer receipt identity does not match request")
            if (
                type(response_chain_id) is not int
                or not 1 <= response_chain_id <= SQLITE_INT64_MAX
                or type(status) is not int
                or status not in {0, 1}
                or type(confirmations) is not int
                or not 0 <= confirmations <= SQLITE_INT64_MAX
                or (
                    block_number is not None
                    and (type(block_number) is not int or not 0 <= block_number <= SQLITE_INT64_MAX)
                )
                or (
                    block_hash is not None
                    and (not isinstance(block_hash, str) or _HASH.fullmatch(block_hash) is None)
                )
                or (
                    effect_hash is not None
                    and (not isinstance(effect_hash, str) or _HASH.fullmatch(effect_hash) is None)
                )
            ):
                raise WalletExecutionError("wallet signer receipt response is invalid")
            return WalletReceipt(
                response_tx_hash,
                response_chain_id,
                cast(Literal[0, 1], status),
                block_number,
                None if block_hash is None else block_hash.lower(),
                confirmations,
                None if effect_hash is None else effect_hash.lower(),
            )
        except WalletExecutionError:
            raise
        except (
            HTTPResponseLimitError,
            TimeoutError,
            httpx.HTTPError,
            ValueError,
            TypeError,
        ) as error:
            raise WalletExecutionError("wallet signer receipt lookup failed") from error

    def get_pending_nonce(self, address: str, *, chain_id: int) -> int:
        """Read the chain pending nonce from the isolated signer service."""
        try:
            canonical_address = canonical_evm_address(address)
            if type(chain_id) is not int or not 1 <= chain_id <= SQLITE_INT64_MAX:
                raise ValueError("wallet signer nonce chain id is invalid")
            status_code, response_body = self._request(
                "/v1/nonce", {"address": canonical_address, "chain_id": chain_id}
            )
            if status_code == 404:
                raise WalletExecutionError("wallet signer nonce route is unavailable")
            if 400 <= status_code < 500:
                raise WalletSignerError("wallet signer nonce request rejected")
            if status_code != 200:
                raise WalletExecutionError("wallet signer nonce lookup failed")
            body = strict_json_loads(response_body)
            if not isinstance(body, dict) or set(body) != {"nonce"}:
                raise WalletExecutionError("wallet signer nonce response is invalid")
            nonce = body.get("nonce")
            if type(nonce) is not int or not 0 <= nonce <= SQLITE_INT64_MAX:
                raise WalletExecutionError("wallet signer nonce response is invalid")
            return nonce
        except WalletExecutionError:
            raise
        except (
            HTTPResponseLimitError,
            TimeoutError,
            httpx.HTTPError,
            ValueError,
            TypeError,
        ) as error:
            raise WalletExecutionError("wallet signer nonce lookup failed") from error

    def get_fee_quote(self, transfer: WalletUnsignedTransfer) -> tuple[int, str]:
        """Ask the signer-side chain adapter for a bounded gas/fee quote."""
        if not isinstance(transfer, WalletUnsignedTransfer):
            raise WalletExecutionError("wallet signer fee quote transfer is invalid")
        try:
            status_code, response_body = self._request(
                "/v1/fee-quote", {"transfer": transfer.model_dump(mode="json")}
            )
            if 400 <= status_code < 500:
                raise WalletSignerError("wallet signer fee quote rejected")
            if status_code != 200:
                raise WalletExecutionError("wallet signer fee quote failed")
            body = strict_json_loads(response_body)
            if not isinstance(body, dict) or set(body) != {"gas_limit", "max_fee_per_gas"}:
                raise WalletExecutionError("wallet signer fee quote response is invalid")
            gas_limit = body.get("gas_limit")
            max_fee = body.get("max_fee_per_gas")
            if type(gas_limit) is not int or not 21_000 <= gas_limit <= _MAX_GAS_LIMIT:
                raise WalletExecutionError("wallet signer fee quote response is invalid")
            if not isinstance(max_fee, str):
                raise WalletExecutionError("wallet signer fee quote response is invalid")
            return gas_limit, _uint(max_fee, "wallet signer max fee per gas", positive=True)
        except WalletExecutionError:
            raise
        except (
            HTTPResponseLimitError,
            TimeoutError,
            httpx.HTTPError,
            ValueError,
            TypeError,
        ) as error:
            raise WalletExecutionError("wallet signer fee quote failed") from error


class WalletSigner(Protocol):
    """Independent signer process/service contract."""

    signer_id: str

    def sign_and_broadcast(
        self, transfer: WalletUnsignedTransfer, *, request_id: str
    ) -> WalletBroadcastResult:
        """Sign and broadcast one fixed transfer, or raise a classified error."""

    def get_receipt(self, tx_hash: str, *, chain_id: int) -> WalletReceipt | None:
        """Return a receipt, or None while confirmation is pending."""

    def get_pending_nonce(self, address: str, *, chain_id: int) -> int:
        """Return the chain pending nonce for the configured spending address."""

    def get_fee_quote(self, transfer: WalletUnsignedTransfer) -> tuple[int, str]:
        """Return current gas and max-fee bounds for a fixed transfer envelope."""


class MockSigner:
    """Deterministic signer double; it contains no key material."""

    def __init__(
        self,
        *,
        signer_id: str = "mock-signer",
        source_address: str | None = None,
        chain_id: int | None = None,
        lose_first_response: bool = False,
    ):
        if type(signer_id) is not str or not 1 <= len(signer_id.strip()) <= 256:
            raise ValueError("wallet signer identity is invalid")
        if chain_id is not None and (type(chain_id) is not int or chain_id < 1):
            raise ValueError("wallet signer chain id is invalid")
        self.signer_id = signer_id.strip()
        self.source_address = (
            None if source_address is None else canonical_evm_address(source_address)
        )
        self.chain_id = chain_id
        self.lose_first_response = lose_first_response
        self.requests: list[WalletUnsignedTransfer] = []
        self._broadcasts: dict[str, WalletBroadcastResult] = {}
        self._transfers: dict[str, WalletUnsignedTransfer] = {}
        self._receipts: dict[str, WalletReceipt] = {}
        self._lost_once = False
        self._pending_nonces: dict[tuple[str, int], int] = {}

    def sign_and_broadcast(
        self, transfer: WalletUnsignedTransfer, *, request_id: str
    ) -> WalletBroadcastResult:
        if self.source_address is not None and transfer.source_address != self.source_address:
            raise WalletSignerError("wallet signer source address mismatch")
        if self.chain_id is not None and transfer.chain_id != self.chain_id:
            raise WalletSignerError("wallet signer chain id mismatch")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 256:
            raise WalletSignerError("wallet signer request identity is invalid")
        result = self._broadcasts.get(request_id)
        if result is None:
            self.requests.append(transfer)
            tx_hash = "0x" + content_hash(
                {"request_id": request_id, "transfer": transfer.model_dump(mode="json")}
            )
            result = WalletBroadcastResult(tx_hash, transfer.chain_id, transfer.nonce, utc_now())
            self._broadcasts[request_id] = result
            self._transfers[tx_hash] = transfer
            key = (transfer.source_address, transfer.chain_id)
            self._pending_nonces[key] = max(self._pending_nonces.get(key, 0), transfer.nonce + 1)
        if self.lose_first_response and not self._lost_once:
            self._lost_once = True
            raise WalletBroadcastUnknownError(tx_hash=result.tx_hash)
        return result

    def get_receipt(self, tx_hash: str, *, chain_id: int) -> WalletReceipt | None:
        tx_hash = _tx_hash(tx_hash)
        if self.chain_id is not None and chain_id != self.chain_id:
            raise WalletSignerError("wallet signer receipt chain id mismatch")
        return self._receipts.get(tx_hash)

    def get_pending_nonce(self, address: str, *, chain_id: int) -> int:
        address = canonical_evm_address(address)
        if type(chain_id) is not int or chain_id < 1:
            raise WalletSignerError("wallet signer nonce chain id is invalid")
        if self.chain_id is not None and chain_id != self.chain_id:
            raise WalletSignerError("wallet signer nonce chain id mismatch")
        return self._pending_nonces.get((address, chain_id), 0)

    def get_fee_quote(self, transfer: WalletUnsignedTransfer) -> tuple[int, str]:
        if not isinstance(transfer, WalletUnsignedTransfer):
            raise WalletSignerError("wallet signer fee quote transfer is invalid")
        return transfer.gas_limit, transfer.max_fee_per_gas

    def set_receipt(
        self,
        tx_hash: str,
        *,
        chain_id: int,
        status: Literal[0, 1],
        block_number: int | None = 1,
        block_hash: str | None = None,
        confirmations: int | None = None,
        effect_hash: str | None = None,
    ) -> None:
        tx_hash = _tx_hash(tx_hash)
        if type(chain_id) is not int or chain_id < 1:
            raise ValueError("receipt chain id is invalid")
        if type(status) is not int or status not in {0, 1}:
            raise ValueError("receipt status is invalid")
        if block_number is not None and (
            type(block_number) is not int or not 0 <= block_number <= SQLITE_INT64_MAX
        ):
            raise ValueError("receipt block number is invalid")
        if block_hash is None and block_number is not None:
            block_hash = "0x" + content_hash(
                {"tx_hash": tx_hash, "block_number": block_number, "chain_id": chain_id}
            )
        if block_hash is not None and _HASH.fullmatch(block_hash) is None:
            raise ValueError("receipt block hash is invalid")
        if confirmations is None:
            confirmations = 1 if block_number is not None else 0
        if type(confirmations) is not int or not 0 <= confirmations <= SQLITE_INT64_MAX:
            raise ValueError("receipt confirmations are invalid")
        if effect_hash is None and status == 1 and tx_hash in self._transfers:
            transfer = self._transfers[tx_hash]
            effect_hash = "0x" + content_hash(
                {
                    "tx_hash": tx_hash,
                    "chain_id": transfer.chain_id,
                    "asset_type": transfer.asset_type,
                    "contract_address": transfer.contract_address,
                    "recipient_address": (
                        transfer.to_address
                        if transfer.asset_type == "native"
                        else "0x" + transfer.data[10:74][-40:]
                    ),
                    "amount": (
                        transfer.value
                        if transfer.asset_type == "native"
                        else str(int(transfer.data[-64:], 16))
                    ),
                }
            )
        if effect_hash is not None and _HASH.fullmatch(effect_hash) is None:
            raise ValueError("receipt effect hash is invalid")
        self._receipts[tx_hash] = WalletReceipt(
            tx_hash,
            chain_id,
            status,
            block_number,
            None if block_hash is None else block_hash.lower(),
            confirmations,
            None if effect_hash is None else effect_hash.lower(),
        )


class EVMTransferAdapter:
    """Build only native and registered ERC-20 transfer envelopes."""

    def build(self, intent: WalletTransferIntent) -> WalletUnsignedTransfer:
        if not isinstance(intent, WalletTransferIntent):
            raise TypeError("wallet transfer intent is invalid")
        if intent.asset_type == "native":
            return WalletUnsignedTransfer(
                chain_id=intent.chain_id,
                nonce=intent.nonce,
                gas_limit=intent.gas_limit,
                max_fee_per_gas=intent.max_fee_per_gas,
                source_address=intent.source_address,
                to_address=intent.recipient_address,
                value=intent.amount,
                data="0x",
                asset_type="native",
                contract_address=None,
            )
        assert intent.contract_address is not None
        recipient_word = intent.recipient_address.removeprefix("0x").rjust(64, "0")
        amount_word = format(int(intent.amount), "064x")
        return WalletUnsignedTransfer(
            chain_id=intent.chain_id,
            nonce=intent.nonce,
            gas_limit=intent.gas_limit,
            max_fee_per_gas=intent.max_fee_per_gas,
            source_address=intent.source_address,
            to_address=intent.contract_address,
            value="0",
            data="0xa9059cbb" + recipient_word + amount_word,
            asset_type="token",
            contract_address=intent.contract_address,
        )


@dataclass(frozen=True)
class WalletExecutionRecord:
    execution_id: str
    subject_id: str
    order_id: str
    network_id: str
    asset_id: str
    source_address: str
    recipient_address: str
    asset_type: str
    contract_address: str | None
    amount: str
    chain_id: int
    nonce: int
    gas_limit: int
    max_fee_per_gas: str
    request_id: str
    request_hash: str
    signer_id: str
    status: str
    tx_hash: str | None
    error_code: str | None
    receipt_status: int | None
    receipt_block_number: int | None
    receipt_block_hash: str | None
    receipt_confirmations: int | None
    receipt_effect_hash: str | None
    attempt_count: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class WalletExecutionAttemptRecord:
    attempt_id: str
    execution_id: str
    subject_id: str
    attempt_number: int
    request_id: str
    status: str
    tx_hash: str | None
    error_code: str | None
    started_at: str
    completed_at: str | None


class WalletPaymentExecutionEngine:
    """Crash-safe orchestrator for reserved payment orders."""

    def __init__(
        self,
        database: Database,
        signer: WalletSigner,
        *,
        economy: WalletEconomyStore | None = None,
        adapter: EVMTransferAdapter | None = None,
        max_fee_per_gas: str = "1000000000",
        min_confirmations: int = 1,
    ):
        self.database = database
        self.economy = economy or WalletEconomyStore(database)
        self.wallets = self.economy.wallets
        self.signer = signer
        signer_id = getattr(signer, "signer_id", None)
        if not isinstance(signer_id, str) or not 1 <= len(signer_id.strip()) <= 256:
            raise ValueError("wallet signer identity is invalid")
        self.signer_id = signer_id.strip()
        self.adapter = adapter or EVMTransferAdapter()
        self.max_fee_per_gas = _uint(max_fee_per_gas, "default max fee per gas", positive=True)
        if type(min_confirmations) is not int or not 1 <= min_confirmations <= SQLITE_INT64_MAX:
            raise ValueError("wallet minimum confirmations is invalid")
        self.min_confirmations = min_confirmations

    def execute_order(
        self,
        order_id: str,
        subject_id: str,
        *,
        actor: str,
        gas_limit: int | None = None,
        max_fee_per_gas: str | None = None,
        nonce: int | None = None,
    ) -> WalletExecutionRecord:
        actor = self.economy._operator(actor)
        execution, transfer, request_id = self._prepare(
            order_id,
            subject_id,
            actor=actor,
            gas_limit=gas_limit,
            max_fee_per_gas=max_fee_per_gas,
            nonce=nonce,
            retry_unknown=False,
        )
        try:
            result = self._sign_and_broadcast(transfer, request_id=request_id)
        except OperationInvalidated:
            raise
        except WalletBroadcastUnknownError as error:
            return self._mark_unknown(
                execution.execution_id,
                subject_id,
                actor=actor,
                tx_hash=error.tx_hash,
                code="broadcast_unknown",
            )
        except WalletSignerError as error:
            return self._mark_failed(
                execution.execution_id, subject_id, actor=actor, code=self._signer_error_code(error)
            )
        except Exception:
            return self._mark_unknown(
                execution.execution_id,
                subject_id,
                actor=actor,
                tx_hash=None,
                code="signer_transport_unknown",
            )
        if not self._valid_broadcast_result(result, transfer):
            return self._mark_unknown(
                execution.execution_id,
                subject_id,
                actor=actor,
                tx_hash=None,
                code="signer_response_invalid",
            )
        try:
            return self._mark_broadcast(
                execution.execution_id, subject_id, actor=actor, result=result
            )
        except WalletExecutionError:
            return self._mark_unknown(
                execution.execution_id,
                subject_id,
                actor=actor,
                tx_hash=result.tx_hash,
                code="signer_response_invalid",
            )

    def retry_unknown(
        self, order_id: str, subject_id: str, *, actor: str, reason: str
    ) -> WalletExecutionRecord:
        reason = reason.strip()
        if not reason or len(reason) > 2000:
            raise ValueError("unknown retry reason is invalid")
        actor = self.economy._operator(actor)
        existing = self._order_execution(order_id, subject_id)
        if existing is not None:
            receipt, _error = self._lookup_receipt(existing)
            if receipt is not None:
                if receipt.status == 1:
                    return self._mark_confirmed(
                        existing.execution_id,
                        subject_id,
                        actor=actor,
                        receipt=receipt,
                    )
                return self._mark_failed(
                    existing.execution_id,
                    subject_id,
                    actor=actor,
                    code="chain_receipt_failed",
                    receipt_tx_hash=receipt.tx_hash,
                    receipt_status=0,
                    receipt_block_number=receipt.block_number,
                    receipt_block_hash=receipt.block_hash,
                    receipt_confirmations=receipt.confirmations,
                    receipt_effect_hash=receipt.effect_hash,
                )
        execution, transfer, request_id = self._prepare(
            order_id,
            subject_id,
            actor=actor,
            gas_limit=None,
            max_fee_per_gas=None,
            nonce=None,
            retry_unknown=True,
            reason=reason,
        )
        try:
            result = self._sign_and_broadcast(transfer, request_id=request_id)
        except OperationInvalidated:
            raise
        except WalletBroadcastUnknownError as error:
            return self._mark_unknown(
                execution.execution_id,
                subject_id,
                actor=actor,
                tx_hash=error.tx_hash,
                code="broadcast_unknown",
            )
        except WalletSignerError as error:
            # Rejection of this attempt says nothing about an earlier broadcast.
            return self._mark_unknown(
                execution.execution_id,
                subject_id,
                actor=actor,
                tx_hash=execution.tx_hash,
                code=self._signer_error_code(error),
            )
        except Exception:
            return self._mark_unknown(
                execution.execution_id,
                subject_id,
                actor=actor,
                tx_hash=None,
                code="signer_transport_unknown",
            )
        if not self._valid_broadcast_result(result, transfer):
            return self._mark_unknown(
                execution.execution_id,
                subject_id,
                actor=actor,
                tx_hash=None,
                code="signer_response_invalid",
            )
        try:
            return self._mark_broadcast(
                execution.execution_id, subject_id, actor=actor, result=result
            )
        except WalletExecutionError:
            return self._mark_unknown(
                execution.execution_id,
                subject_id,
                actor=actor,
                tx_hash=result.tx_hash,
                code="signer_response_invalid",
            )

    def poll_receipt(
        self, execution_id: str, subject_id: str, *, actor: str
    ) -> WalletExecutionRecord:
        actor = self.economy._operator(actor)
        execution = self.get_execution(execution_id, subject_id)
        if execution.status not in {"broadcast", "unknown"}:
            return execution
        receipt, error = self._lookup_receipt(execution)
        if receipt is None and error is not None:
            if execution.status == "unknown":
                return execution
            return self._mark_unknown(
                execution_id,
                subject_id,
                actor=actor,
                tx_hash=execution.tx_hash,
                code=error,
            )
        if receipt is None:
            return execution
        if receipt.status == 1:
            return self._mark_confirmed(execution_id, subject_id, actor=actor, receipt=receipt)
        return self._mark_failed(
            execution_id,
            subject_id,
            actor=actor,
            code="chain_receipt_failed",
            receipt_tx_hash=receipt.tx_hash,
            receipt_status=0,
            receipt_block_number=receipt.block_number,
            receipt_block_hash=receipt.block_hash,
            receipt_confirmations=receipt.confirmations,
            receipt_effect_hash=receipt.effect_hash,
        )

    def verify_confirmed_receipt(self, execution_id: str, subject_id: str) -> WalletExecutionRecord:
        """Recheck a settled receipt without reversing append-only settlement."""
        execution = self.get_execution(execution_id, subject_id)
        if execution.status != "confirmed" or execution.tx_hash is None:
            raise InvalidTransitionError("wallet execution is not confirmed")
        try:
            receipt = self.signer.get_receipt(execution.tx_hash, chain_id=execution.chain_id)
        except Exception as error:
            raise WalletExecutionError("confirmed receipt lookup is unknown") from error
        if (
            receipt is None
            or not self._valid_receipt(receipt, execution, min_confirmations=self.min_confirmations)
            or receipt.status != 1
        ):
            raise WalletChainReorganizationError("confirmed wallet receipt is no longer canonical")
        if (
            receipt.block_number != execution.receipt_block_number
            or receipt.block_hash != execution.receipt_block_hash
            or (
                execution.receipt_confirmations is None
                or receipt.confirmations < execution.receipt_confirmations
            )
            or receipt.effect_hash != execution.receipt_effect_hash
        ):
            raise WalletChainReorganizationError("confirmed wallet receipt evidence changed")
        return execution

    def refund(
        self, order_id: str, subject_id: str, *, actor: str, reason: str
    ) -> PaymentOrderRecord:
        return self.economy.refund_order(order_id, subject_id, actor=actor, reason=reason)

    def recover_inflight(
        self, subject_id: str, *, actor: str, limit: int = 100
    ) -> list[WalletExecutionRecord]:
        return self.recover_database_inflight(
            self.database,
            subject_id,
            actor=actor,
            limit=limit,
            economy=self.economy,
        )

    @classmethod
    def recover_database_inflight(
        cls,
        database: Database,
        subject_id: str,
        *,
        actor: str,
        limit: int = 100,
        economy: WalletEconomyStore | None = None,
    ) -> list[WalletExecutionRecord]:
        """Fence signing rows as unknown after a process restart.

        This recovery path deliberately does not require a signer. It only
        changes durable state and therefore remains safe when the signer is
        unavailable or intentionally not configured.
        """
        validate_subject_id(subject_id)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("invalid recovery limit")
        econ = economy or WalletEconomyStore(database)
        actor = econ._operator(actor)
        with database.read_transaction() as c:
            rows = c.execute(
                "SELECT execution_id FROM wallet_payment_executions "
                "WHERE subject_id=? AND status='signing' "
                "ORDER BY created_at,execution_id LIMIT ?",
                (subject_id, limit),
            ).fetchall()
        recovered: list[WalletExecutionRecord] = []
        for row in rows:
            with database.transaction() as c:
                execution = c.execute(
                    "SELECT * FROM wallet_payment_executions WHERE execution_id=? AND subject_id=?",
                    (row["execution_id"], subject_id),
                ).fetchone()
                if execution is None or execution["status"] != "signing":
                    continue
                order = econ._order_row(c, execution["order_id"], subject_id)
                if order["status"] != "signing":
                    raise IntegrityError("wallet execution/order recovery mismatch")
                audit_id = econ._audit(
                    c,
                    subject_id,
                    "wallet_payment_unknown",
                    actor,
                    {"order_id": execution["order_id"], "error_code": "process_restarted"},
                )
                econ._transition_order(c, order, "unknown", actor, "process_restarted")
                now = utc_now()
                state_hash = cls._execution_hash_values_from_row(
                    execution,
                    "unknown",
                    execution["tx_hash"],
                    "process_restarted",
                    None,
                    None,
                    None,
                    None,
                    None,
                    int(execution["attempt_count"]),
                    now,
                )
                c.execute(
                    "UPDATE wallet_payment_executions SET status='unknown',"
                    "error_code='process_restarted',receipt_status=NULL,receipt_block_number=NULL,"
                    "receipt_block_hash=NULL,receipt_confirmations=NULL,receipt_effect_hash=NULL,"
                    "last_audit_id=?,updated_at=?,state_hash=? "
                    "WHERE execution_id=?",
                    (audit_id, now, state_hash, execution["execution_id"]),
                )
                attempt = c.execute(
                    "SELECT * FROM wallet_payment_execution_attempts "
                    "WHERE execution_id=? AND attempt_number=?",
                    (execution["execution_id"], int(execution["attempt_count"])),
                ).fetchone()
                if attempt is None:
                    raise IntegrityError("wallet execution attempt is missing")
                attempt_hash = cls._attempt_hash_values(
                    attempt["attempt_id"],
                    attempt["execution_id"],
                    attempt["subject_id"],
                    int(attempt["attempt_number"]),
                    attempt["request_id"],
                    "unknown",
                    execution["tx_hash"],
                    "process_restarted",
                    attempt["started_at"],
                    now,
                )
                c.execute(
                    "UPDATE wallet_payment_execution_attempts SET status='unknown',tx_hash=?,"
                    "error_code='process_restarted',completed_at=?,state_hash=? "
                    "WHERE attempt_id=?",
                    (execution["tx_hash"], now, attempt_hash, attempt["attempt_id"]),
                )
                recovered.append(
                    cls._execution_from_row(
                        c.execute(
                            "SELECT * FROM wallet_payment_executions WHERE execution_id=?",
                            (execution["execution_id"],),
                        ).fetchone()
                    )
                )
        return recovered

    def get_execution(self, execution_id: str, subject_id: str) -> WalletExecutionRecord:
        validate_subject_id(subject_id)
        with self.database.read_transaction() as c:
            row = c.execute(
                "SELECT * FROM wallet_payment_executions WHERE execution_id=? AND subject_id=?",
                (execution_id, subject_id),
            ).fetchone()
        if row is None:
            raise NotFoundError("wallet payment execution not found")
        return self._execution_from_row(row)

    def _order_execution(self, order_id: str, subject_id: str) -> WalletExecutionRecord | None:
        validate_subject_id(subject_id)
        with self.database.read_transaction() as c:
            row = c.execute(
                "SELECT * FROM wallet_payment_executions WHERE order_id=? AND subject_id=?",
                (order_id, subject_id),
            ).fetchone()
        return None if row is None else self._execution_from_row(row)

    def list_executions(self, subject_id: str, *, limit: int = 100) -> list[WalletExecutionRecord]:
        validate_subject_id(subject_id)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("invalid execution limit")
        with self.database.read_transaction() as c:
            rows = c.execute(
                "SELECT * FROM wallet_payment_executions WHERE subject_id=? "
                "ORDER BY created_at DESC,execution_id DESC LIMIT ?",
                (subject_id, limit),
            ).fetchall()
        return [self._execution_from_row(row) for row in rows]

    def list_attempts(
        self, execution_id: str, subject_id: str, *, limit: int = 100
    ) -> list[WalletExecutionAttemptRecord]:
        validate_subject_id(subject_id)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("invalid attempt limit")
        with self.database.read_transaction() as c:
            execution = c.execute(
                "SELECT 1 FROM wallet_payment_executions WHERE execution_id=? AND subject_id=?",
                (execution_id, subject_id),
            ).fetchone()
            if execution is None:
                raise NotFoundError("wallet payment execution not found")
            rows = c.execute(
                "SELECT * FROM wallet_payment_execution_attempts "
                "WHERE execution_id=? AND subject_id=? "
                "ORDER BY attempt_number DESC LIMIT ?",
                (execution_id, subject_id, limit),
            ).fetchall()
        return [self._attempt_from_row(row) for row in rows]

    def verify_integrity(self, subject_id: str) -> dict[str, int]:
        """Verify durable execution state without contacting the signer."""
        return self.verify_database_integrity(self.database, subject_id)

    @classmethod
    def verify_database_integrity(cls, database: Database, subject_id: str) -> dict[str, int]:
        """Verify execution rows using only the database boundary."""
        validate_subject_id(subject_id)
        with database.read_transaction() as c:
            execution_count = 0
            for row in c.execute(
                "SELECT execution_row.*,"
                "(SELECT COUNT(*) FROM wallet_payment_execution_attempts attempt "
                " WHERE attempt.execution_id=execution_row.execution_id "
                " AND attempt.subject_id=execution_row.subject_id) AS stored_attempt_count,"
                "(SELECT MIN(attempt_number) FROM wallet_payment_execution_attempts attempt "
                " WHERE attempt.execution_id=execution_row.execution_id "
                " AND attempt.subject_id=execution_row.subject_id) AS first_attempt_number,"
                "(SELECT MAX(attempt_number) FROM wallet_payment_execution_attempts attempt "
                " WHERE attempt.execution_id=execution_row.execution_id "
                " AND attempt.subject_id=execution_row.subject_id) AS last_attempt_number,"
                "(SELECT status FROM wallet_payment_execution_attempts attempt "
                " WHERE attempt.execution_id=execution_row.execution_id "
                " AND attempt.subject_id=execution_row.subject_id "
                " AND attempt.attempt_number=execution_row.attempt_count) AS latest_attempt_status "
                "FROM wallet_payment_executions execution_row WHERE execution_row.subject_id=?",
                (subject_id,),
            ):
                execution_count += 1
                if row["state_hash"] != cls._execution_hash(row):
                    raise IntegrityError(f"wallet execution hash mismatch: {row['execution_id']}")
                expected_attempts = int(row["attempt_count"])
                if (
                    int(row["stored_attempt_count"]) != expected_attempts
                    or row["first_attempt_number"] != 1
                    or row["last_attempt_number"] != expected_attempts
                    or row["latest_attempt_status"] != row["status"]
                ):
                    raise IntegrityError(
                        f"wallet execution attempt chain mismatch: {row['execution_id']}"
                    )
                order = c.execute(
                    "SELECT subject_id,status FROM wallet_payment_orders WHERE order_id=?",
                    (row["order_id"],),
                ).fetchone()
                if (
                    order is None
                    or order["subject_id"] != subject_id
                    or not (
                        order["status"] == row["status"]
                        or (
                            order["status"] == "refunded" and row["status"] in {"failed", "unknown"}
                        )
                    )
                ):
                    raise IntegrityError(f"wallet execution order mismatch: {row['execution_id']}")
            attempt_count = 0
            for row in c.execute(
                "SELECT * FROM wallet_payment_execution_attempts WHERE subject_id=?", (subject_id,)
            ):
                attempt_count += 1
                if row["state_hash"] != cls._attempt_hash(row):
                    raise IntegrityError(
                        f"wallet execution attempt hash mismatch: {row['attempt_id']}"
                    )
            return {
                "wallet_payment_executions": execution_count,
                "wallet_payment_execution_attempts": attempt_count,
            }

    @staticmethod
    def _valid_broadcast_result(
        result: WalletBroadcastResult, transfer: WalletUnsignedTransfer
    ) -> bool:
        return (
            isinstance(result, WalletBroadcastResult)
            and type(result.chain_id) is int
            and result.chain_id == transfer.chain_id
            and type(result.nonce) is int
            and result.nonce == transfer.nonce
            and isinstance(result.tx_hash, str)
            and _HASH.fullmatch(result.tx_hash) is not None
            and isinstance(result.accepted_at, str)
            and 1 <= len(result.accepted_at.strip()) <= 128
        )

    @staticmethod
    def _valid_receipt(
        receipt: WalletReceipt,
        execution: WalletExecutionRecord,
        *,
        min_confirmations: int = 1,
    ) -> bool:
        expected_effect = None
        if execution.tx_hash is not None:
            expected_effect = WalletPaymentExecutionEngine._expected_effect_hash(execution)
        return (
            isinstance(receipt, WalletReceipt)
            and isinstance(receipt.tx_hash, str)
            and _HASH.fullmatch(receipt.tx_hash) is not None
            and execution.tx_hash is not None
            and receipt.tx_hash.lower() == execution.tx_hash.lower()
            and type(receipt.chain_id) is int
            and receipt.chain_id == execution.chain_id
            and type(receipt.status) is int
            and receipt.status in {0, 1}
            and receipt.block_number is not None
            and type(receipt.block_number) is int
            and 0 <= receipt.block_number <= SQLITE_INT64_MAX
            and isinstance(receipt.block_hash, str)
            and _HASH.fullmatch(receipt.block_hash) is not None
            and type(receipt.confirmations) is int
            and receipt.confirmations >= min_confirmations
            and receipt.confirmations <= SQLITE_INT64_MAX
            and (
                execution.asset_type != "token"
                or receipt.status == 0
                or (
                    isinstance(receipt.effect_hash, str)
                    and _HASH.fullmatch(receipt.effect_hash) is not None
                    and receipt.effect_hash.lower() == expected_effect
                )
            )
        )

    @staticmethod
    def _expected_effect_hash(execution: WalletExecutionRecord) -> str:
        if execution.tx_hash is None:
            raise ValueError("wallet execution transaction hash is missing")
        return "0x" + content_hash(
            {
                "tx_hash": execution.tx_hash.lower(),
                "chain_id": execution.chain_id,
                "asset_type": execution.asset_type,
                "contract_address": execution.contract_address,
                "recipient_address": execution.recipient_address,
                "amount": execution.amount,
            }
        )

    @staticmethod
    def _signer_error_code(error: WalletSignerError) -> str:
        """Persist only a non-secret signer error classification."""
        del error
        return "signer_rejected"

    def _prepare(
        self,
        order_id: str,
        subject_id: str,
        *,
        actor: str,
        gas_limit: int | None,
        max_fee_per_gas: str | None,
        nonce: int | None,
        retry_unknown: bool,
        reason: str = "",
    ) -> tuple[WalletExecutionRecord, WalletUnsignedTransfer, str]:
        validate_subject_id(subject_id)
        if gas_limit is not None and (
            type(gas_limit) is not int or not 21_000 <= gas_limit <= _MAX_GAS_LIMIT
        ):
            raise ValueError("wallet gas limit is invalid")
        fee = (
            self.max_fee_per_gas
            if max_fee_per_gas is None
            else _uint(max_fee_per_gas, "max fee per gas", positive=True)
        )
        if nonce is not None and (type(nonce) is not int or not 0 <= nonce <= SQLITE_INT64_MAX):
            raise ValueError("wallet nonce is invalid")
        with self.database.transaction() as c:
            order = self.economy._order_row(c, order_id, subject_id)
            if retry_unknown:
                if order["status"] != "unknown":
                    raise InvalidTransitionError("only unknown orders can be retried")
                policy = self.economy._policy_row(c, subject_id)
                if (
                    policy is None
                    or int(policy["emergency_paused"])
                    or str(policy["mode"]) == "disabled"
                ):
                    raise ValueError("wallet payment execution is emergency paused or disabled")
                if not self.economy._policy_allows(c, order, policy, exclude_order_id=order_id):
                    raise ValueError("wallet payment policy no longer authorizes retry")
                execution_row = c.execute(
                    "SELECT * FROM wallet_payment_executions WHERE order_id=? AND subject_id=?",
                    (order_id, subject_id),
                ).fetchone()
                if execution_row is None:
                    raise IntegrityError("unknown order execution is missing")
                execution = self._execution_from_row(execution_row)
                if execution.signer_id != self.signer_id:
                    raise ValueError("unknown execution signer identity changed")
                attempt_number = execution.attempt_count + 1
                if attempt_number > 32:
                    raise ValueError("wallet execution retry limit reached")
                nonce_value, gas_value, fee_value = (
                    execution.nonce,
                    execution.gas_limit,
                    execution.max_fee_per_gas,
                )
                request_id = f"{order_id}:attempt:{attempt_number}"
                transfer = self.adapter.build(
                    WalletTransferIntent(
                        order_id=order_id,
                        subject_id=subject_id,
                        network_id=execution.network_id,
                        asset_id=execution.asset_id,
                        asset_type=cast(Literal["native", "token"], execution.asset_type),
                        contract_address=execution.contract_address,
                        source_address=execution.source_address,
                        recipient_address=execution.recipient_address,
                        amount=execution.amount,
                        chain_id=execution.chain_id,
                        nonce=nonce_value,
                        gas_limit=gas_value,
                        max_fee_per_gas=fee_value,
                    )
                )
                source = self.economy._spending_address(c, subject_id, execution.network_id)
                if source is None or source["address"] != execution.source_address:
                    raise WalletExecutionError("wallet retry spending address changed")
                self._check_observed_fee_budget(
                    c,
                    subject_id=subject_id,
                    network_id=execution.network_id,
                    asset_type=execution.asset_type,
                    source_address_id=source["address_id"],
                    min_balance=policy["min_balance"],
                    gas_limit=gas_value,
                    max_fee_per_gas=fee_value,
                    max_observation_age_seconds=int(policy["max_observation_age_seconds"]),
                    exclude_execution_id=execution.execution_id,
                )
                aid = self.economy._audit(
                    c,
                    subject_id,
                    "wallet_payment_execution_retry",
                    actor,
                    {"order_id": order_id, "attempt_number": attempt_number, "reason": reason},
                )
                self.economy._transition_order(c, order, "signing", actor, reason)
                now = utc_now()
                state = self._execution_hash_values_from_row(
                    execution_row,
                    "signing",
                    execution.tx_hash,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    attempt_number,
                    now,
                )
                c.execute(
                    "UPDATE wallet_payment_executions SET status='signing',"
                    "error_code=NULL,receipt_status=NULL,receipt_block_number=NULL,"
                    "receipt_block_hash=NULL,receipt_confirmations=NULL,receipt_effect_hash=NULL,"
                    "attempt_count=?,last_audit_id=?,updated_at=?,state_hash=? "
                    "WHERE execution_id=?",
                    (attempt_number, aid, now, state, execution.execution_id),
                )
                attempt_id = new_id("wallet_attempt")
                attempt_hash = self._attempt_hash_values(
                    attempt_id,
                    execution.execution_id,
                    subject_id,
                    attempt_number,
                    request_id,
                    "signing",
                    None,
                    None,
                    now,
                    None,
                )
                c.execute(
                    "INSERT INTO wallet_payment_execution_attempts("
                    "attempt_id,execution_id,subject_id,attempt_number,request_id,status,"
                    "tx_hash,error_code,started_at,completed_at,state_hash,created_audit_id"
                    ") VALUES(?,?,?, ?,?,'signing',NULL,NULL,?,?,?,?)",
                    (
                        attempt_id,
                        execution.execution_id,
                        subject_id,
                        attempt_number,
                        request_id,
                        now,
                        None,
                        attempt_hash,
                        aid,
                    ),
                )
                return (
                    self._execution_from_row(
                        c.execute(
                            "SELECT * FROM wallet_payment_executions WHERE execution_id=?",
                            (execution.execution_id,),
                        ).fetchone()
                    ),
                    transfer,
                    request_id,
                )
            if order["status"] != "reserved":
                raise InvalidTransitionError("only reserved orders can execute")
            policy = self.economy._policy_row(c, subject_id)
            if (
                policy is None
                or int(policy["emergency_paused"])
                or str(policy["mode"]) == "disabled"
            ):
                raise ValueError("wallet payment execution is emergency paused or disabled")
            if not self.economy._policy_allows(c, order, policy, exclude_order_id=order_id):
                raise ValueError("wallet payment execution is no longer authorized")
            asset = self.wallets._asset_row(c, order["asset_id"], subject_id=subject_id)
            network = self.wallets._network_row(c, order["network_id"], subject_id=subject_id)
            if (
                asset["status"] != "active"
                or network["status"] != "active"
                or asset["network_id"] != network["network_id"]
            ):
                raise ValueError("wallet execution references are not active")
            source = c.execute(
                "SELECT * FROM wallet_addresses WHERE subject_id=? AND network_id=? "
                "AND status='active' AND purpose='spending' "
                "ORDER BY created_at,address_id LIMIT 2",
                (subject_id, order["network_id"]),
            ).fetchall()
            if len(source) != 1:
                raise ValueError("wallet spending address must have exactly one active address")
            source_row = source[0]
            if source_row["address"] == order["recipient_address"]:
                raise ValueError("wallet source and recipient must differ")
            if nonce is not None:
                nonce_value = nonce
            else:
                nonce_provider = getattr(self.signer, "get_pending_nonce", None)
                if callable(nonce_provider):
                    try:
                        nonce_value = nonce_provider(
                            source_row["address"], chain_id=int(network["chain_id"])
                        )
                    except Exception as error:
                        raise WalletExecutionError("wallet nonce authority unavailable") from error
                    if type(nonce_value) is not int or not 0 <= nonce_value <= SQLITE_INT64_MAX:
                        raise WalletExecutionError("wallet nonce authority returned invalid nonce")
                    local_active_nonce = c.execute(
                        "SELECT MAX(nonce) AS nonce FROM wallet_payment_executions "
                        "WHERE subject_id=? AND network_id=? AND source_address=? "
                        "AND (status IN ('signing','broadcast','unknown','confirmed') "
                        "OR tx_hash IS NOT NULL)",
                        (subject_id, order["network_id"], source_row["address"]),
                    ).fetchone()["nonce"]
                    if local_active_nonce is not None and nonce_value <= int(local_active_nonce):
                        raise WalletExecutionError("wallet nonce authority is behind local history")
                else:
                    # A local MAX(nonce) is not authoritative: it cannot see
                    # transactions submitted by another process and advances
                    # even when a signer rejects a request before broadcast.
                    raise WalletExecutionError("wallet nonce authority unavailable")
            gas_value = gas_limit or (21_000 if asset["asset_type"] == "native" else 100_000)

            def build_transfer(current_gas: int, current_fee: str) -> WalletUnsignedTransfer:
                return self.adapter.build(
                    WalletTransferIntent(
                        order_id=order_id,
                        subject_id=subject_id,
                        network_id=order["network_id"],
                        asset_id=order["asset_id"],
                        asset_type=asset["asset_type"],
                        contract_address=asset["contract_address"],
                        source_address=source_row["address"],
                        recipient_address=order["recipient_address"],
                        amount=order["amount"],
                        chain_id=int(network["chain_id"]),
                        nonce=nonce_value,
                        gas_limit=current_gas,
                        max_fee_per_gas=current_fee,
                    )
                )

            transfer = build_transfer(gas_value, fee)
            # A production signer is expected to quote current chain fees. An
            # explicit operator override remains available for controlled
            # incident handling, while the normal workflow consumes the
            # signer-side quote rather than assuming a stale fixed fee.
            fee_quote = getattr(self.signer, "get_fee_quote", None)
            if gas_limit is None or max_fee_per_gas is None:
                if not callable(fee_quote):
                    raise WalletExecutionError("wallet fee authority unavailable")
                try:
                    quoted_gas, quoted_fee = fee_quote(transfer)
                    if type(quoted_gas) is not int or not 21_000 <= quoted_gas <= _MAX_GAS_LIMIT:
                        raise WalletExecutionError("wallet fee quote gas limit is invalid")
                    quoted_fee = _uint(
                        quoted_fee, "wallet fee quote max fee per gas", positive=True
                    )
                except WalletExecutionError:
                    raise
                except Exception as error:
                    raise WalletExecutionError("wallet fee quote unavailable") from error
                gas_value = gas_value if gas_limit is not None else quoted_gas
                fee = fee if max_fee_per_gas is not None else quoted_fee
                transfer = build_transfer(gas_value, fee)
            self._check_observed_fee_budget(
                c,
                subject_id=subject_id,
                network_id=order["network_id"],
                asset_type=asset["asset_type"],
                source_address_id=source_row["address_id"],
                min_balance=policy["min_balance"],
                gas_limit=gas_value,
                max_fee_per_gas=fee,
                max_observation_age_seconds=int(policy["max_observation_age_seconds"]),
            )
            request_id = f"{order_id}:attempt:1"
            execution_id = new_id("wallet_exec")
            now = utc_now()
            audit_id = self.economy._audit(
                c,
                subject_id,
                "wallet_payment_execution_signing",
                actor,
                {
                    "order_id": order_id,
                    "request_id": request_id,
                    "signer_id": self.signer_id,
                },
            )
            self.economy._transition_order(c, order, "signing", actor, "execution started")
            request_hash = content_hash(transfer.model_dump(mode="json"))
            state_hash = self._execution_hash_values(
                execution_id,
                subject_id,
                order_id,
                order["network_id"],
                order["asset_id"],
                source_row["address"],
                order["recipient_address"],
                asset["asset_type"],
                asset["contract_address"],
                order["amount"],
                int(network["chain_id"]),
                nonce_value,
                gas_value,
                fee,
                request_id,
                request_hash,
                self.signer_id,
                "signing",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                1,
                now,
                now,
            )
            c.execute(
                "INSERT INTO wallet_payment_executions("
                "execution_id,subject_id,order_id,network_id,asset_id,source_address,"
                "recipient_address,asset_type,contract_address,amount,chain_id,nonce,"
                "gas_limit,max_fee_per_gas,request_id,request_hash,signer_id,status,"
                "tx_hash,error_code,receipt_status,receipt_block_number,receipt_block_hash,"
                "receipt_confirmations,receipt_effect_hash,attempt_count,"
                "state_hash,created_audit_id,last_audit_id,created_at,updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'signing',"
                "NULL,NULL,NULL,NULL,NULL,NULL,NULL,1,?,?,?,?,?)",
                (
                    execution_id,
                    subject_id,
                    order_id,
                    order["network_id"],
                    order["asset_id"],
                    source_row["address"],
                    order["recipient_address"],
                    asset["asset_type"],
                    asset["contract_address"],
                    order["amount"],
                    int(network["chain_id"]),
                    nonce_value,
                    gas_value,
                    fee,
                    request_id,
                    request_hash,
                    self.signer_id,
                    state_hash,
                    audit_id,
                    audit_id,
                    now,
                    now,
                ),
            )
            attempt_id = new_id("wallet_attempt")
            attempt_hash = self._attempt_hash_values(
                attempt_id,
                execution_id,
                subject_id,
                1,
                request_id,
                "signing",
                None,
                None,
                now,
                None,
            )
            c.execute(
                "INSERT INTO wallet_payment_execution_attempts("
                "attempt_id,execution_id,subject_id,attempt_number,request_id,status,"
                "tx_hash,error_code,started_at,completed_at,state_hash,created_audit_id"
                ") VALUES(?,?,?, ?,?,'signing',NULL,NULL,?,?,?,?)",
                (
                    attempt_id,
                    execution_id,
                    subject_id,
                    1,
                    request_id,
                    now,
                    None,
                    attempt_hash,
                    audit_id,
                ),
            )
            return (
                self._execution_from_row(
                    c.execute(
                        "SELECT * FROM wallet_payment_executions WHERE execution_id=?",
                        (execution_id,),
                    ).fetchone()
                ),
                transfer,
                request_id,
            )

    def _check_observed_fee_budget(
        self,
        c: Any,
        *,
        subject_id: str,
        network_id: str,
        asset_type: str,
        source_address_id: str,
        min_balance: str,
        gas_limit: int,
        max_fee_per_gas: str,
        max_observation_age_seconds: int,
        exclude_execution_id: str | None = None,
    ) -> None:
        """Reserve native fees atomically with the execution, including retries.

        Token reservations promise token principal only; fee admission happens
        at signing. Legacy native-only wallets retain optional observations.
        Token execution always requires a bounded-age native observation.
        """
        self.economy._require_resolved_legacy_payments(c, subject_id, network_id)
        native_asset = c.execute(
            "SELECT asset_id FROM wallet_assets WHERE subject_id=? AND network_id=? "
            "AND asset_type='native' AND status='active' LIMIT 1",
            (subject_id, network_id),
        ).fetchone()
        if native_asset is None:
            raise WalletExecutionError("wallet native fee asset is not registered")
        latest = c.execute(
            "SELECT * FROM wallet_balance_snapshots WHERE subject_id=? AND network_id=? "
            "AND asset_id=? AND address_id=? ORDER BY observed_at DESC,snapshot_id DESC LIMIT 1",
            (subject_id, network_id, native_asset["asset_id"], source_address_id),
        ).fetchone()
        if latest is None:
            if asset_type == "token":
                raise WalletExecutionError("wallet native fee balance is unavailable")
            return
        observation = self.wallets._balance_from_row(latest)
        if asset_type == "token":
            from .store import WALLET_BALANCE_OBSERVATION_MAX_AGE_SECONDS

            age_limit = min(
                max_observation_age_seconds or WALLET_BALANCE_OBSERVATION_MAX_AGE_SECONDS,
                WALLET_BALANCE_OBSERVATION_MAX_AGE_SECONDS,
            )
            age = (
                datetime.fromisoformat(self.economy.clock())
                - datetime.fromisoformat(observation.observed_at)
            ).total_seconds()
            if not 0 <= age <= age_limit:
                raise WalletExecutionError("wallet native fee balance is not current")
        fee = int(max_fee_per_gas) * int(gas_limit)
        source_address = c.execute(
            "SELECT address FROM wallet_addresses WHERE address_id=? AND subject_id=?",
            (source_address_id, subject_id),
        ).fetchone()
        if source_address is None:
            raise IntegrityError("wallet spending address is missing")
        # Every payment spends this native budget, even a reverted token transfer.
        # Without block anchors, require a newer observation after settlement.
        settled_after_observation = c.execute(
            "SELECT 1 FROM wallet_payment_executions "
            "WHERE subject_id=? AND network_id=? AND source_address=? "
            "AND status IN ('confirmed','failed') AND tx_hash IS NOT NULL "
            "AND updated_at>=? LIMIT 1",
            (subject_id, network_id, source_address["address"], observation.observed_at),
        ).fetchone()
        if settled_after_observation is not None:
            raise WalletExecutionError("wallet native fee balance predates settlement")
        pending_fee = sum(
            int(row["gas_limit"]) * int(row["max_fee_per_gas"])
            for row in c.execute(
                "SELECT gas_limit,max_fee_per_gas FROM wallet_payment_executions "
                "WHERE subject_id=? AND network_id=? AND source_address=? "
                "AND status IN ('signing','broadcast','unknown') "
                "AND (? IS NULL OR execution_id<>?)",
                (
                    subject_id,
                    network_id,
                    source_address["address"],
                    exclude_execution_id,
                    exclude_execution_id,
                ),
            )
        )
        reserved_amount = sum(
            int(row["amount"])
            for row in c.execute(
                "SELECT amount FROM wallet_payment_orders WHERE subject_id=? AND network_id=? "
                "AND asset_id=? AND status IN ('reserved','signing','broadcast','unknown')",
                (subject_id, network_id, native_asset["asset_id"]),
            )
        )
        # min_balance is in the payment asset's atoms, not necessarily wei.
        native_minimum = int(min_balance) if asset_type == "native" else 0
        if int(observation.balance) - reserved_amount - pending_fee < fee + native_minimum:
            raise WalletExecutionError("observed native balance cannot cover wallet fees")

    def _mark_broadcast(
        self, execution_id: str, subject_id: str, *, actor: str, result: WalletBroadcastResult
    ) -> WalletExecutionRecord:
        with self.database.transaction() as c:
            execution = c.execute(
                "SELECT * FROM wallet_payment_executions WHERE execution_id=? AND subject_id=?",
                (execution_id, subject_id),
            ).fetchone()
            if execution is None:
                raise NotFoundError("wallet payment execution not found")
            order = self.economy._order_row(c, execution["order_id"], subject_id)
            if order["status"] != "signing":
                return self._execution_from_row(execution)
            tx = _tx_hash(result.tx_hash)
            if result.chain_id != int(execution["chain_id"]) or result.nonce != int(
                execution["nonce"]
            ):
                raise WalletExecutionError("signer response does not match fixed chain or nonce")
            collision = c.execute(
                "SELECT 1 FROM wallet_payment_executions WHERE subject_id=? AND network_id=? "
                "AND tx_hash=? AND execution_id<>?",
                (subject_id, execution["network_id"], tx, execution_id),
            ).fetchone()
            if collision is not None:
                raise WalletExecutionError("signer reused a transaction hash")
            aid = self.economy._audit(
                c,
                subject_id,
                "wallet_payment_broadcast",
                actor,
                {"order_id": execution["order_id"], "tx_hash": tx, "nonce": result.nonce},
            )
            self.economy._transition_order(
                c, order, "broadcast", actor, "signer broadcast accepted"
            )
            now = utc_now()
            state = self._execution_hash_values_from_row(
                execution,
                "broadcast",
                tx,
                None,
                None,
                None,
                None,
                None,
                None,
                int(execution["attempt_count"]),
                now,
            )
            c.execute(
                "UPDATE wallet_payment_executions SET status='broadcast',tx_hash=?,"
                "last_audit_id=?,updated_at=?,state_hash=? WHERE execution_id=?",
                (tx, aid, now, state, execution_id),
            )
            self._finish_attempt(c, execution, "broadcast", tx, None, now)
            return self._execution_from_row(
                c.execute(
                    "SELECT * FROM wallet_payment_executions WHERE execution_id=?", (execution_id,)
                ).fetchone()
            )

    def _sign_and_broadcast(
        self, transfer: WalletUnsignedTransfer, *, request_id: str
    ) -> WalletBroadcastResult:
        """Call the signer only while the current runtime lease is fenced."""
        lease = current_lease()
        if lease is None:
            return self.signer.sign_and_broadcast(transfer, request_id=request_id)
        with lease._gate.external_side_effect_scope(lease):
            return self.signer.sign_and_broadcast(transfer, request_id=request_id)

    def _mark_unknown(
        self, execution_id: str, subject_id: str, *, actor: str, tx_hash: str | None, code: str
    ) -> WalletExecutionRecord:
        with self.database.transaction() as c:
            execution = c.execute(
                "SELECT * FROM wallet_payment_executions WHERE execution_id=? AND subject_id=?",
                (execution_id, subject_id),
            ).fetchone()
            if execution is None:
                raise NotFoundError("wallet payment execution not found")
            order = self.economy._order_row(c, execution["order_id"], subject_id)
            if order["status"] not in {"signing", "broadcast"}:
                return self._execution_from_row(execution)
            if tx_hash is None:
                tx = execution["tx_hash"]
            else:
                try:
                    tx = _tx_hash(tx_hash)
                except ValueError:
                    tx = execution["tx_hash"]
                    code = "broadcast_unknown_invalid_hash"
            aid = self.economy._audit(
                c,
                subject_id,
                "wallet_payment_unknown",
                actor,
                {"order_id": execution["order_id"], "tx_hash": tx, "error_code": code},
            )
            self.economy._transition_order(c, order, "unknown", actor, code)
            now = utc_now()
            state = self._execution_hash_values_from_row(
                execution,
                "unknown",
                tx,
                code,
                None,
                None,
                None,
                None,
                None,
                int(execution["attempt_count"]),
                now,
            )
            c.execute(
                "UPDATE wallet_payment_executions SET status='unknown',tx_hash=?,"
                "error_code=?,last_audit_id=?,updated_at=?,state_hash=? WHERE execution_id=?",
                (tx, code, aid, now, state, execution_id),
            )
            self._finish_attempt(c, execution, "unknown", tx, code, now)
            return self._execution_from_row(
                c.execute(
                    "SELECT * FROM wallet_payment_executions WHERE execution_id=?", (execution_id,)
                ).fetchone()
            )

    def _lookup_receipt(
        self, execution: WalletExecutionRecord
    ) -> tuple[WalletReceipt | None, str | None]:
        # Attempts share one nonce/envelope. A newer attempt must not hide an
        # earlier transaction's receipt. The retry contract permits 32 attempts.
        hashes = dict.fromkeys([execution.tx_hash] if execution.tx_hash is not None else [])
        with self.database.read_transaction() as c:
            for row in c.execute(
                "SELECT * FROM wallet_payment_execution_attempts WHERE execution_id=? "
                "AND subject_id=? ORDER BY attempt_number DESC LIMIT 32",
                (execution.execution_id, execution.subject_id),
            ):
                if row["state_hash"] != self._attempt_hash(row):
                    raise IntegrityError("wallet execution attempt hash mismatch")
                if row["tx_hash"] is not None:
                    hashes[_tx_hash(row["tx_hash"])] = None
        error = None
        for tx in hashes:
            try:
                receipt = self.signer.get_receipt(tx, chain_id=execution.chain_id)
            except Exception:
                error = "receipt_lookup_unknown"
                continue
            if receipt is not None:
                if self._valid_receipt(
                    receipt,
                    replace(execution, tx_hash=tx),
                    min_confirmations=self.min_confirmations,
                ):
                    return receipt, None
                error = "receipt_invalid"
        return None, error

    def _mark_failed(
        self,
        execution_id: str,
        subject_id: str,
        *,
        actor: str,
        code: str,
        receipt_tx_hash: str | None = None,
        receipt_status: int | None = None,
        receipt_block_number: int | None = None,
        receipt_block_hash: str | None = None,
        receipt_confirmations: int | None = None,
        receipt_effect_hash: str | None = None,
    ) -> WalletExecutionRecord:
        with self.database.transaction() as c:
            execution = c.execute(
                "SELECT * FROM wallet_payment_executions WHERE execution_id=? AND subject_id=?",
                (execution_id, subject_id),
            ).fetchone()
            if execution is None:
                raise NotFoundError("wallet payment execution not found")
            order = self.economy._order_row(c, execution["order_id"], subject_id)
            if order["status"] not in {"signing", "broadcast", "unknown"}:
                return self._execution_from_row(execution)
            tx = execution["tx_hash"] if receipt_tx_hash is None else _tx_hash(receipt_tx_hash)
            aid = self.economy._audit(
                c,
                subject_id,
                "wallet_payment_failed",
                actor,
                {"order_id": execution["order_id"], "error_code": code},
            )
            self.economy._transition_order(c, order, "failed", actor, code)
            now = utc_now()
            state = self._execution_hash_values_from_row(
                execution,
                "failed",
                tx,
                code,
                receipt_status,
                receipt_block_number,
                receipt_block_hash,
                receipt_confirmations,
                receipt_effect_hash,
                int(execution["attempt_count"]),
                now,
            )
            c.execute(
                "UPDATE wallet_payment_executions SET status='failed',tx_hash=?,error_code=?,"
                "receipt_status=?,receipt_block_number=?,receipt_block_hash=?,"
                "receipt_confirmations=?,receipt_effect_hash=?,last_audit_id=?,updated_at=?,"
                "state_hash=? WHERE execution_id=?",
                (
                    tx,
                    code,
                    receipt_status,
                    receipt_block_number,
                    receipt_block_hash,
                    receipt_confirmations,
                    receipt_effect_hash,
                    aid,
                    now,
                    state,
                    execution_id,
                ),
            )
            self._finish_attempt(c, execution, "failed", tx, code, now)
            return self._execution_from_row(
                c.execute(
                    "SELECT * FROM wallet_payment_executions WHERE execution_id=?", (execution_id,)
                ).fetchone()
            )

    def _mark_confirmed(
        self, execution_id: str, subject_id: str, *, actor: str, receipt: WalletReceipt
    ) -> WalletExecutionRecord:
        with self.database.transaction() as c:
            execution = c.execute(
                "SELECT * FROM wallet_payment_executions WHERE execution_id=? AND subject_id=?",
                (execution_id, subject_id),
            ).fetchone()
            if execution is None:
                raise NotFoundError("wallet payment execution not found")
            order = self.economy._order_row(c, execution["order_id"], subject_id)
            if order["status"] == "confirmed":
                return self._execution_from_row(execution)
            if order["status"] not in {"broadcast", "unknown"}:
                raise InvalidTransitionError("order is not awaiting receipt")
            tx = _tx_hash(receipt.tx_hash)
            aid = self.economy._audit(
                c,
                subject_id,
                "wallet_payment_confirmed",
                actor,
                {
                    "order_id": execution["order_id"],
                    "tx_hash": tx,
                    "block_number": receipt.block_number,
                    "block_hash": receipt.block_hash,
                    "confirmations": receipt.confirmations,
                },
            )
            self.economy._transition_order(c, order, "confirmed", actor, "chain receipt confirmed")
            self.economy._post_order_journal(
                c, order, "settlement", ("reserved", "debit"), ("paid", "credit")
            )
            now = utc_now()
            state = self._execution_hash_values_from_row(
                execution,
                "confirmed",
                tx,
                None,
                1,
                receipt.block_number,
                receipt.block_hash,
                receipt.confirmations,
                receipt.effect_hash,
                int(execution["attempt_count"]),
                now,
            )
            c.execute(
                "UPDATE wallet_payment_executions SET status='confirmed',tx_hash=?,error_code=NULL,"
                "receipt_status=1,receipt_block_number=?,receipt_block_hash=?,"
                "receipt_confirmations=?,receipt_effect_hash=?,last_audit_id=?,updated_at=?,"
                "state_hash=? WHERE execution_id=?",
                (
                    tx,
                    receipt.block_number,
                    receipt.block_hash,
                    receipt.confirmations,
                    receipt.effect_hash,
                    aid,
                    now,
                    state,
                    execution_id,
                ),
            )
            self._finish_attempt(c, execution, "confirmed", tx, None, now)
            return self._execution_from_row(
                c.execute(
                    "SELECT * FROM wallet_payment_executions WHERE execution_id=?", (execution_id,)
                ).fetchone()
            )

    def _finish_attempt(
        self,
        c: Any,
        execution: Any,
        status: str,
        tx_hash: str | None,
        error_code: str | None,
        completed_at: str,
    ) -> None:
        attempt = c.execute(
            "SELECT * FROM wallet_payment_execution_attempts "
            "WHERE execution_id=? AND attempt_number=?",
            (execution["execution_id"], int(execution["attempt_count"])),
        ).fetchone()
        if attempt is None:
            raise IntegrityError("wallet execution attempt is missing")
        c.execute(
            "UPDATE wallet_payment_execution_attempts SET status=?,tx_hash=?,error_code=?,"
            "completed_at=?,state_hash=? WHERE attempt_id=?",
            (
                status,
                tx_hash,
                error_code,
                completed_at,
                self._attempt_hash_values(
                    attempt["attempt_id"],
                    attempt["execution_id"],
                    attempt["subject_id"],
                    int(attempt["attempt_number"]),
                    attempt["request_id"],
                    status,
                    tx_hash,
                    error_code,
                    attempt["started_at"],
                    completed_at,
                ),
                attempt["attempt_id"],
            ),
        )

    @staticmethod
    def _execution_hash_values(*values: Any) -> str:
        keys = (
            "execution_id",
            "subject_id",
            "order_id",
            "network_id",
            "asset_id",
            "source_address",
            "recipient_address",
            "asset_type",
            "contract_address",
            "amount",
            "chain_id",
            "nonce",
            "gas_limit",
            "max_fee_per_gas",
            "request_id",
            "request_hash",
            "signer_id",
            "status",
            "tx_hash",
            "error_code",
            "receipt_status",
            "receipt_block_number",
            "receipt_block_hash",
            "receipt_confirmations",
            "receipt_effect_hash",
            "attempt_count",
            "created_at",
            "updated_at",
        )
        return content_hash(dict(zip(keys, values, strict=True)))

    @classmethod
    def _execution_hash(cls, row: Any) -> str:
        return cls._execution_hash_values(
            *[
                row[key]
                for key in (
                    "execution_id",
                    "subject_id",
                    "order_id",
                    "network_id",
                    "asset_id",
                    "source_address",
                    "recipient_address",
                    "asset_type",
                    "contract_address",
                    "amount",
                    "chain_id",
                    "nonce",
                    "gas_limit",
                    "max_fee_per_gas",
                    "request_id",
                    "request_hash",
                    "signer_id",
                    "status",
                    "tx_hash",
                    "error_code",
                    "receipt_status",
                    "receipt_block_number",
                    "receipt_block_hash",
                    "receipt_confirmations",
                    "receipt_effect_hash",
                    "attempt_count",
                    "created_at",
                    "updated_at",
                )
            ]
        )

    @classmethod
    def _execution_hash_values_from_row(
        cls,
        row: Any,
        status: str,
        tx_hash: str | None,
        error_code: str | None,
        receipt_status: int | None,
        block_number: int | None,
        block_hash: str | None,
        confirmations: int | None,
        effect_hash: str | None,
        attempt_count: int,
        updated_at: str | None = None,
    ) -> str:
        values = [
            row[key]
            for key in (
                "execution_id",
                "subject_id",
                "order_id",
                "network_id",
                "asset_id",
                "source_address",
                "recipient_address",
                "asset_type",
                "contract_address",
                "amount",
                "chain_id",
                "nonce",
                "gas_limit",
                "max_fee_per_gas",
                "request_id",
                "request_hash",
                "signer_id",
            )
        ]
        values.extend(
            (
                status,
                tx_hash,
                error_code,
                receipt_status,
                block_number,
                block_hash,
                confirmations,
                effect_hash,
                attempt_count,
            )
        )
        values.extend((row["created_at"], row["updated_at"] if updated_at is None else updated_at))
        return cls._execution_hash_values(*values)

    @staticmethod
    def _attempt_hash_values(
        attempt_id: str,
        execution_id: str,
        subject_id: str,
        attempt_number: int,
        request_id: str,
        status: str,
        tx_hash: str | None,
        error_code: str | None,
        started_at: str,
        completed_at: str | None,
    ) -> str:
        return content_hash(
            {
                "attempt_id": attempt_id,
                "execution_id": execution_id,
                "subject_id": subject_id,
                "attempt_number": attempt_number,
                "request_id": request_id,
                "status": status,
                "tx_hash": tx_hash,
                "error_code": error_code,
                "started_at": started_at,
                "completed_at": completed_at,
            }
        )

    @classmethod
    def _attempt_hash(cls, row: Any) -> str:
        return cls._attempt_hash_values(
            row["attempt_id"],
            row["execution_id"],
            row["subject_id"],
            int(row["attempt_number"]),
            row["request_id"],
            row["status"],
            row["tx_hash"],
            row["error_code"],
            row["started_at"],
            row["completed_at"],
        )

    @staticmethod
    def _execution_from_row(row: Any) -> WalletExecutionRecord:
        return WalletExecutionRecord(
            row["execution_id"],
            row["subject_id"],
            row["order_id"],
            row["network_id"],
            row["asset_id"],
            row["source_address"],
            row["recipient_address"],
            row["asset_type"],
            row["contract_address"],
            row["amount"],
            int(row["chain_id"]),
            int(row["nonce"]),
            int(row["gas_limit"]),
            row["max_fee_per_gas"],
            row["request_id"],
            row["request_hash"],
            row["signer_id"],
            row["status"],
            row["tx_hash"],
            row["error_code"],
            None if row["receipt_status"] is None else int(row["receipt_status"]),
            None if row["receipt_block_number"] is None else int(row["receipt_block_number"]),
            row["receipt_block_hash"],
            None if row["receipt_confirmations"] is None else int(row["receipt_confirmations"]),
            row["receipt_effect_hash"],
            int(row["attempt_count"]),
            row["created_at"],
            row["updated_at"],
        )

    @staticmethod
    def _attempt_from_row(row: Any) -> WalletExecutionAttemptRecord:
        return WalletExecutionAttemptRecord(
            row["attempt_id"],
            row["execution_id"],
            row["subject_id"],
            int(row["attempt_number"]),
            row["request_id"],
            row["status"],
            row["tx_hash"],
            row["error_code"],
            row["started_at"],
            row["completed_at"],
        )
