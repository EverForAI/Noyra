from __future__ import annotations

import re
import threading
import time
from types import TracebackType
from typing import Self

import httpx

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
from noyra.core.types import strict_json_loads

from .store import WalletStore
from .types import (
    WalletAddressRecord,
    WalletAssetRecord,
    WalletBalanceSnapshotInput,
    WalletBalanceSnapshotRecord,
)

WALLET_RPC_MAX_RESPONSE_BYTES = 65_536
WALLET_RPC_MAX_HEADER_BYTES = DEFAULT_MAX_HEADER_BYTES
WALLET_RPC_DEFAULT_TIMEOUT_SECONDS = 10.0
WALLET_RPC_DEFAULT_MAX_CONCURRENT_REQUESTS = 2
_RPC_REQUEST_ID = "noyra-wallet-balance-v1"
_EVM_QUANTITY = re.compile(r"0x(?:0|[1-9a-f][0-9a-f]{0,63})\Z")
_EVM_UINT256_DATA = re.compile(r"0x[0-9a-f]{64}\Z")


class WalletRPCError(RuntimeError):
    """A sanitized failure from the bounded, read-only wallet RPC boundary."""

    def __init__(self, code: str, *, request_count: int = 0):
        super().__init__(code)
        self.code = code
        self.request_count = request_count


class _WalletRPCStatusError(RuntimeError):
    def __init__(self, status_code: int):
        self.status_code = status_code


class WalletRPCBalanceAcquirer:
    """Acquire EVM balances through registered public HTTPS RPC origins only.

    The acquirer only emits ``eth_getBalance`` and token ``eth_call`` requests.
    It receives no signer material and has no transaction, transfer, or payment
    operation. Successful results are persisted through ``WalletStore`` as
    immutable observations.
    """

    def __init__(
        self,
        store: WalletStore,
        *,
        client: httpx.Client | None = None,
        timeout_seconds: float = WALLET_RPC_DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes: int = WALLET_RPC_MAX_RESPONSE_BYTES,
        max_concurrent_requests: int = WALLET_RPC_DEFAULT_MAX_CONCURRENT_REQUESTS,
    ):
        if not isinstance(store, WalletStore):
            raise TypeError("wallet RPC store is invalid")
        if not 0 < timeout_seconds <= 60:
            raise ValueError("wallet RPC timeout is invalid")
        if not 1_024 <= max_response_bytes <= WALLET_RPC_MAX_RESPONSE_BYTES:
            raise ValueError("wallet RPC response limit is invalid")
        if not 1 <= max_concurrent_requests <= 8:
            raise ValueError("wallet RPC concurrency limit is invalid")
        self.store = store
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.max_concurrent_requests = max_concurrent_requests
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            transport=PublicDNSHTTPTransport(max_connections=max_concurrent_requests),
        )
        self._slots = threading.BoundedSemaphore(max_concurrent_requests)

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

    def acquire_balance(
        self,
        subject_id: str,
        *,
        asset_id: str,
        address_id: str,
        actor: str,
    ) -> WalletBalanceSnapshotRecord:
        """Read one active registered target and persist one immutable snapshot.

        This compatibility entry point performs a direct read.  Scheduled or
        retried reads should use :class:`WalletBalanceAcquisitionRunner`, which
        wraps the same ``read_balance`` operation in the durable run ledger.
        """

        result, asset, address = self.read_balance(
            subject_id,
            asset_id=asset_id,
            address_id=address_id,
            actor=actor,
        )
        return self.store.record_balance_snapshot(
            subject_id,
            WalletBalanceSnapshotInput(
                asset_id=asset.asset_id,
                address_id=address.address_id,
                balance=result,
                source="evm_rpc",
            ),
            actor=actor,
        )

    def read_balance(
        self,
        subject_id: str,
        *,
        asset_id: str,
        address_id: str,
        actor: str,
    ) -> tuple[str, WalletAssetRecord, WalletAddressRecord]:
        """Read a balance without writing it, using only registered metadata.

        The tuple contains the canonical balance followed by the validated
        asset and address records.  It is intentionally not a generic RPC
        client: callers cannot provide an endpoint, method, data, or block tag.
        """

        WalletStore._require_operator(actor, "acquire a wallet balance")
        validate_subject_id(subject_id)
        asset = self.store.get_asset(asset_id, subject_id=subject_id)
        address = self.store.get_address(address_id, subject_id=subject_id)
        if (
            asset.status != "active"
            or address.status != "active"
            or asset.network_id != address.network_id
        ):
            raise ValueError("wallet balance references are not active on one network")
        network = self.store.get_network(asset.network_id, subject_id=subject_id)
        if network.status != "active":
            raise ValueError("wallet network is not active")
        rpc_url = network.rpc_url
        if network.chain_family != "evm" or not rpc_url:
            raise WalletRPCError("wallet_rpc_not_configured")

        method, params, abi_encoded = self._balance_request(
            asset.asset_type, asset.contract_address, address.address
        )
        started = time.monotonic()
        if not self._slots.acquire(timeout=self.timeout_seconds):
            raise WalletRPCError("wallet_rpc_capacity_exhausted")
        request_count = 0

        def bounded_request(
            request_method: str,
            request_params: list[object],
            *,
            request_abi_encoded: bool,
        ) -> str:
            nonlocal request_count
            try:
                remaining = self._remaining_timeout(started)
            except WalletRPCError as error:
                raise WalletRPCError(error.code, request_count=request_count) from error
            request_count += 1
            try:
                return self._request(
                    rpc_url,
                    request_method,
                    request_params,
                    abi_encoded=request_abi_encoded,
                    timeout_seconds=remaining,
                )
            except WalletRPCError as error:
                raise WalletRPCError(error.code, request_count=request_count) from error

        try:
            observed_chain_id = bounded_request("eth_chainId", [], request_abi_encoded=False)
            if observed_chain_id != str(network.chain_id):
                raise WalletRPCError("wallet_rpc_chain_id_mismatch", request_count=request_count)
            result = bounded_request(method, params, request_abi_encoded=abi_encoded)
        finally:
            self._slots.release()
        return result, asset, address

    def _remaining_timeout(self, started: float) -> float:
        remaining = self.timeout_seconds - (time.monotonic() - started)
        if remaining <= 0:
            raise WalletRPCError("wallet_rpc_timeout")
        return remaining

    def _request(
        self,
        endpoint: str,
        method: str,
        params: list[object],
        *,
        abi_encoded: bool,
        timeout_seconds: float,
    ) -> str:
        timeout_hook = SyncHTTPTimeoutHook()
        payload = {
            "jsonrpc": "2.0",
            "id": _RPC_REQUEST_ID,
            "method": method,
            "params": params,
        }

        def perform_request() -> bytes:
            deadline = time.monotonic() + timeout_seconds
            with self._client.stream(
                "POST",
                endpoint,
                json=payload,
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                    "Content-Type": "application/json",
                    "User-Agent": "Noyra-Wallet-ReadOnly/0.1.0",
                },
                follow_redirects=False,
                timeout=httpx.Timeout(timeout_seconds),
            ) as response:
                timeout_hook.register(response)
                try:
                    validate_response_headers(
                        response,
                        max_header_bytes=WALLET_RPC_MAX_HEADER_BYTES,
                    )
                    if response.status_code != 200:
                        raise _WalletRPCStatusError(response.status_code)
                    media_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                    if media_type != "application/json":
                        raise WalletRPCError("wallet_rpc_content_type")
                    return read_bounded_sync_response(
                        response,
                        max_body_bytes=self.max_response_bytes,
                        max_header_bytes=WALLET_RPC_MAX_HEADER_BYTES,
                        total_timeout_seconds=timeout_seconds,
                        deadline=deadline,
                    )
                finally:
                    timeout_hook.clear(response)

        try:
            body_bytes = call_sync_http_with_deadline(
                perform_request,
                timeout=timeout_seconds,
                on_timeout=timeout_hook.cancel,
            )
        except WalletRPCError:
            raise
        except _WalletRPCStatusError as error:
            raise WalletRPCError(f"wallet_rpc_http_{error.status_code}") from error
        except HTTPResponseLimitError as error:
            raise WalletRPCError("wallet_rpc_response_too_large") from error
        except (TimeoutError, httpx.TimeoutException) as error:
            raise WalletRPCError("wallet_rpc_timeout") from error
        except httpx.ConnectError as error:
            raise WalletRPCError("wallet_rpc_connect_failed") from error
        except httpx.HTTPError as error:
            raise WalletRPCError("wallet_rpc_transport_failed") from error

        return self._parse_quantity(body_bytes, abi_encoded=abi_encoded)

    @staticmethod
    def _balance_request(
        asset_type: str,
        contract_address: str | None,
        address: str,
    ) -> tuple[str, list[object], bool]:
        if asset_type == "native":
            return "eth_getBalance", [address, "latest"], False
        if asset_type == "token" and contract_address is not None:
            encoded_address = address.removeprefix("0x")
            data = "0x70a08231" + encoded_address.rjust(64, "0")
            return "eth_call", [{"to": contract_address, "data": data}, "latest"], True
        raise ValueError("wallet asset configuration is invalid")

    @staticmethod
    def _parse_quantity(body_bytes: bytes, *, abi_encoded: bool) -> str:
        try:
            body = strict_json_loads(body_bytes)
        except (TypeError, ValueError) as error:
            raise WalletRPCError("wallet_rpc_invalid_json") from error
        if not isinstance(body, dict):
            raise WalletRPCError("wallet_rpc_invalid_response")
        if body.get("jsonrpc") != "2.0" or body.get("id") != _RPC_REQUEST_ID:
            raise WalletRPCError("wallet_rpc_invalid_response")
        if "error" in body:
            raise WalletRPCError("wallet_rpc_remote_error")
        result = body.get("result")
        pattern = _EVM_UINT256_DATA if abi_encoded else _EVM_QUANTITY
        if not isinstance(result, str) or pattern.fullmatch(result) is None:
            raise WalletRPCError("wallet_rpc_invalid_quantity")
        return str(int(result[2:], 16))
