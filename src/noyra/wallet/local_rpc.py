"""Bounded JSON-RPC transport used exclusively by the built-in wallet signer."""

from __future__ import annotations

import time

import httpx

from noyra.core.http import (
    DEFAULT_MAX_HEADER_BYTES,
    PublicDNSHTTPTransport,
    SyncHTTPTimeoutHook,
    call_sync_http_with_deadline,
    read_bounded_sync_response,
    validate_response_headers,
)
from noyra.core.types import strict_json_loads

from .execution import WalletExecutionError
from .types import SQLITE_INT64_MAX, _rpc_url

_RPC_ID = "noyra-local-wallet-v1"
_METHODS = frozenset({
    "eth_chainId", "eth_getTransactionCount", "eth_getBalance", "eth_gasPrice",
    "eth_estimateGas", "eth_call", "eth_sendRawTransaction",
    "eth_getTransactionReceipt", "eth_getTransactionByHash",
    "eth_getBlockByNumber", "eth_blockNumber",
})


class LocalWalletRPC:
    def __init__(
        self, rpc_urls: dict[int, str], *, client: httpx.Client | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        if not rpc_urls or len(rpc_urls) > 64:
            raise ValueError("local wallet RPC configuration is invalid")
        self.urls: dict[int, str] = {}
        for chain, url in rpc_urls.items():
            if type(chain) is not int or not 1 <= chain <= SQLITE_INT64_MAX:
                raise ValueError("local wallet RPC chain is invalid")
            self.urls[chain] = _rpc_url(url)
        if type(timeout_seconds) not in {int, float} or not 0 < timeout_seconds <= 60:
            raise ValueError("local wallet timeout is invalid")
        self.timeout_seconds = timeout_seconds
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(timeout_seconds), follow_redirects=False, trust_env=False,
            transport=PublicDNSHTTPTransport(max_connections=2),
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def call(self, chain_id: int, method: str, params: list[object], *, deadline: float) -> object:
        if type(chain_id) is not int or chain_id not in self.urls or method not in _METHODS:
            raise WalletExecutionError("local wallet RPC operation is invalid")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WalletExecutionError("local wallet RPC deadline exceeded")
        hook = SyncHTTPTimeoutHook()

        def request() -> bytes:
            with self._client.stream(
                "POST", self.urls[chain_id],
                json={"jsonrpc": "2.0", "id": _RPC_ID, "method": method, "params": params},
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                    "Content-Type": "application/json",
                    "User-Agent": "Noyra-LocalWallet/0.1.0",
                },
                follow_redirects=False, timeout=httpx.Timeout(remaining),
            ) as response:
                hook.register(response)
                try:
                    validate_response_headers(response, max_header_bytes=DEFAULT_MAX_HEADER_BYTES)
                    if response.status_code != 200 or response.headers.get(
                        "content-type", ""
                    ).split(";", 1)[0].lower() != "application/json":
                        raise WalletExecutionError("local wallet RPC response is invalid")
                    return read_bounded_sync_response(
                        response, max_body_bytes=65_536,
                        max_header_bytes=DEFAULT_MAX_HEADER_BYTES,
                        total_timeout_seconds=remaining, deadline=deadline,
                    )
                finally:
                    hook.clear(response)

        try:
            raw = call_sync_http_with_deadline(request, timeout=remaining, on_timeout=hook.cancel)
            body = strict_json_loads(raw)
            if (
                not isinstance(body, dict) or body.get("jsonrpc") != "2.0"
                or body.get("id") != _RPC_ID or "error" in body or "result" not in body
            ):
                raise ValueError("invalid RPC response")
            return body["result"]
        except Exception:
            # Provider messages/transport exceptions can contain URLs or signed
            # payloads. Never propagate their text or exception chain.
            raise WalletExecutionError("local wallet RPC request failed") from None
