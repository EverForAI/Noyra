"""Built-in signing of fixed native and ERC-20 transfers.

The private account stays in this module's memory. Deterministic EIP-155 legacy
transactions preserve the exact nonce, fee and hash across retries and restarts.
No secret or signed raw transaction is written to the database or audit log.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Literal, cast

import httpx
from eth_account.signers.local import LocalAccount
from eth_utils import to_checksum_address  # type: ignore[attr-defined]

from noyra.core.types import content_hash, utc_now

from .execution import (
    WalletBroadcastResult,
    WalletBroadcastUnknownError,
    WalletExecutionError,
    WalletReceipt,
    WalletSignerError,
    WalletUnsignedTransfer,
)
from .local_rpc import LocalWalletRPC
from .types import SQLITE_INT64_MAX, canonical_evm_address

_QUANTITY = re.compile(r"0x(?:0|[1-9a-f][0-9a-f]{0,63})\Z")
_HASH = re.compile(r"0x[0-9a-fA-F]{64}\Z")
_WORD = re.compile(r"0x[0-9a-fA-F]{64}\Z")
_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _quantity(value: object, *, maximum: int = 2**256 - 1) -> int:
    if not isinstance(value, str) or _QUANTITY.fullmatch(value) is None:
        raise WalletExecutionError("local wallet RPC quantity is invalid")
    result = int(value[2:], 16)
    if result > maximum:
        raise WalletExecutionError("local wallet RPC quantity exceeds bounds")
    return result


def _hash(value: object) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise WalletExecutionError("local wallet RPC hash is invalid")
    return value.lower()


class LocalWalletSigner:
    """An in-process signer; it is not a security boundary against host compromise."""

    def __init__(
        self,
        account: LocalAccount,
        rpc_urls: dict[int, str],
        *,
        client: httpx.Client | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        if not isinstance(account, LocalAccount):
            raise ValueError("local wallet account is invalid")
        self._account: LocalAccount | None = account
        self.address = canonical_evm_address(account.address)
        self.signer_id = f"local:{self.address}"
        self._rpc = LocalWalletRPC(rpc_urls, client=client, timeout_seconds=timeout_seconds)
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            self._account = None
            self._rpc.close()

    def _chain(self, chain_id: int, deadline: float) -> None:
        observed = _quantity(self._rpc.call(chain_id, "eth_chainId", [], deadline=deadline))
        if observed != chain_id:
            raise WalletSignerError("local wallet chain id mismatch")

    def _validate(self, transfer: WalletUnsignedTransfer) -> WalletUnsignedTransfer:
        if self._account is None:
            raise WalletSignerError("local wallet is closed")
        try:
            # Pydantic's model_copy/model_construct can bypass validation.
            checked = WalletUnsignedTransfer.model_validate(transfer.model_dump())
            if checked.source_address != self.address or int(checked.max_fee_per_gas) <= 0:
                raise ValueError("invalid source or fee")
            recipient, _ = self._effect(checked)
            if recipient == self.address or recipient == "0x" + "0" * 40:
                raise ValueError("invalid recipient")
            if checked.asset_type == "token" and checked.gas_limit < 50_000:
                raise ValueError("invalid token gas")
            return checked
        except Exception:
            raise WalletSignerError("local wallet transfer is invalid") from None

    @staticmethod
    def _effect(transfer: WalletUnsignedTransfer) -> tuple[str, str]:
        if transfer.asset_type == "native":
            return transfer.to_address, transfer.value
        word = transfer.data[10:74]
        if word[:24] != "0" * 24 or int(transfer.data[-64:], 16) == 0:
            raise WalletSignerError("local wallet token calldata is invalid")
        return "0x" + word[24:], str(int(transfer.data[-64:], 16))

    def _signed(self, transfer: WalletUnsignedTransfer) -> tuple[str, str]:
        if self._account is None:
            raise WalletSignerError("local wallet is closed")
        try:
            signed = self._account.sign_transaction(
                cast(
                    Any,
                    {
                        "chainId": transfer.chain_id,
                        "nonce": transfer.nonce,
                        "gas": transfer.gas_limit,
                        "gasPrice": int(transfer.max_fee_per_gas),
                        "to": to_checksum_address(transfer.to_address),
                        "value": int(transfer.value),
                        "data": transfer.data,
                    },
                )
            )
            return "0x" + bytes(signed.raw_transaction).hex(), "0x" + bytes(signed.hash).hex()
        except Exception:
            raise WalletSignerError("local wallet could not sign transfer") from None

    @staticmethod
    def _rpc_transfer(transfer: WalletUnsignedTransfer) -> dict[str, object]:
        return {
            "from": transfer.source_address,
            "to": transfer.to_address,
            "value": hex(int(transfer.value)),
            "data": transfer.data,
        }

    def _admit(self, transfer: WalletUnsignedTransfer, tx_hash: str, deadline: float) -> None:
        self._chain(transfer.chain_id, deadline)
        nonce = _quantity(
            self._rpc.call(
                transfer.chain_id,
                "eth_getTransactionCount",
                [self.address, "pending"],
                deadline=deadline,
            ),
            maximum=SQLITE_INT64_MAX,
        )
        if nonce > transfer.nonce:
            # A prior attempt may have mined while its response was lost.
            raise WalletBroadcastUnknownError(tx_hash=tx_hash)
        if nonce < transfer.nonce:
            raise WalletSignerError("local wallet nonce is ahead of chain")
        gas = _quantity(
            self._rpc.call(
                transfer.chain_id,
                "eth_estimateGas",
                [self._rpc_transfer(transfer)],
                deadline=deadline,
            ),
            maximum=30_000_000,
        )
        fee = _quantity(self._rpc.call(transfer.chain_id, "eth_gasPrice", [], deadline=deadline))
        if not 21_000 <= gas <= transfer.gas_limit or not 0 < fee <= int(transfer.max_fee_per_gas):
            raise WalletSignerError("local wallet gas or fee exceeds authorized envelope")
        balance = _quantity(
            self._rpc.call(
                transfer.chain_id, "eth_getBalance", [self.address, "pending"], deadline=deadline
            )
        )
        if balance < int(transfer.value) + transfer.gas_limit * int(transfer.max_fee_per_gas):
            raise WalletSignerError("local wallet native balance is insufficient")
        if transfer.asset_type == "token":
            result = self._rpc.call(
                transfer.chain_id,
                "eth_call",
                [
                    {
                        "to": transfer.contract_address,
                        "data": "0x70a08231" + self.address[2:].rjust(64, "0"),
                    },
                    "pending",
                ],
                deadline=deadline,
            )
            if not isinstance(result, str) or not _WORD.fullmatch(result):
                raise WalletSignerError("local wallet token balance is invalid")
            if int(result, 16) < int(transfer.data[-64:], 16):
                raise WalletSignerError("local wallet token balance is insufficient")

    def sign_and_broadcast(
        self,
        transfer: WalletUnsignedTransfer,
        *,
        request_id: str,
    ) -> WalletBroadcastResult:
        if type(request_id) is not str or not 1 <= len(request_id) <= 256:
            raise WalletSignerError("local wallet request identity is invalid")
        with self._lock:
            checked = self._validate(transfer)
            raw, tx_hash = self._signed(checked)
            deadline = time.monotonic() + self._rpc.timeout_seconds
            try:
                self._admit(checked, tx_hash, deadline)
            except WalletBroadcastUnknownError:
                raise
            except WalletSignerError:
                raise
            except WalletExecutionError:
                raise WalletSignerError("local wallet preflight unavailable") from None
            try:
                result = _hash(
                    self._rpc.call(
                        checked.chain_id, "eth_sendRawTransaction", [raw], deadline=deadline
                    )
                )
                if result != tx_hash:
                    raise WalletExecutionError("transaction hash mismatch")
            except Exception:
                # Even JSON-RPC errors like already-known/nonce-too-low are
                # ambiguous after submission. Never release a possible payment.
                raise WalletBroadcastUnknownError(tx_hash=tx_hash) from None
            return WalletBroadcastResult(tx_hash, checked.chain_id, checked.nonce, utc_now())

    def get_pending_nonce(self, address: str, *, chain_id: int) -> int:
        if canonical_evm_address(address) != self.address or self._account is None:
            raise WalletSignerError("local wallet source address is invalid")
        deadline = time.monotonic() + self._rpc.timeout_seconds
        self._chain(chain_id, deadline)
        return _quantity(
            self._rpc.call(
                chain_id,
                "eth_getTransactionCount",
                [self.address, "pending"],
                deadline=deadline,
            ),
            maximum=SQLITE_INT64_MAX,
        )

    def get_fee_quote(self, transfer: WalletUnsignedTransfer) -> tuple[int, str]:
        checked = self._validate(transfer)
        deadline = time.monotonic() + self._rpc.timeout_seconds
        self._chain(checked.chain_id, deadline)
        gas = _quantity(
            self._rpc.call(
                checked.chain_id,
                "eth_estimateGas",
                [self._rpc_transfer(checked)],
                deadline=deadline,
            ),
            maximum=30_000_000,
        )
        fee = _quantity(self._rpc.call(checked.chain_id, "eth_gasPrice", [], deadline=deadline))
        # Quote headroom is frozen into the authorized envelope before signing.
        gas = max(50_000 if checked.asset_type == "token" else 21_000, (gas * 120 + 99) // 100)
        if gas > 30_000_000 or fee <= 0 or fee * 2 >= 2**256:
            raise WalletExecutionError("local wallet fee quote exceeds bounds")
        return gas, str(fee * 2)

    def _receipt_transfer(self, tx: dict[str, Any], chain_id: int) -> WalletUnsignedTransfer:
        data = tx.get("input")
        if not isinstance(data, str):
            raise WalletExecutionError("local wallet transaction input is invalid")
        token = data != "0x"
        if _quantity(tx.get("chainId")) != chain_id or _quantity(tx.get("type", "0x0")) != 0:
            raise WalletExecutionError("local wallet transaction chain or type is invalid")
        return self._validate(
            WalletUnsignedTransfer(
                chain_id=chain_id,
                nonce=_quantity(tx.get("nonce"), maximum=SQLITE_INT64_MAX),
                gas_limit=_quantity(tx.get("gas")),
                max_fee_per_gas=str(_quantity(tx.get("gasPrice"))),
                source_address=tx["from"],
                to_address=tx["to"],
                value=str(_quantity(tx.get("value"))),
                data=data,
                asset_type="token" if token else "native",
                contract_address=tx["to"] if token else None,
            )
        )

    @staticmethod
    def _token_effect(
        logs: object,
        transfer: WalletUnsignedTransfer,
        tx_hash: str,
        block_hash: str,
        block: int,
    ) -> None:
        if not isinstance(logs, list):
            raise WalletExecutionError("local wallet receipt logs are invalid")
        recipient, amount = LocalWalletSigner._effect(transfer)
        topics = [
            _TRANSFER_TOPIC,
            "0x" + transfer.source_address[2:].rjust(64, "0"),
            "0x" + recipient[2:].rjust(64, "0"),
        ]
        matched_amounts: list[int] = []
        # Require exactly the authorized outgoing transfer. Fee-on-transfer or
        # unusual ERC-20 implementations remain unconfirmed for investigation.
        for log in logs:
            if not isinstance(log, dict):
                raise WalletExecutionError("local wallet receipt log is invalid")
            if str(log.get("address", "")).lower() != transfer.contract_address:
                continue
            actual_topics = log.get("topics")
            if not isinstance(actual_topics, list) or len(actual_topics) != 3:
                continue
            if [str(t).lower() for t in actual_topics][:2] != topics[:2]:
                continue
            if (
                [str(t).lower() for t in actual_topics] != topics
                or log.get("removed") is not False
                or _hash(log.get("transactionHash")) != tx_hash
                or _hash(log.get("blockHash")) != block_hash
                or _quantity(log.get("blockNumber")) != block
                or not isinstance(log.get("data"), str)
                or not _WORD.fullmatch(log["data"])
            ):
                raise WalletExecutionError("local wallet token effect differs from transfer")
            matched_amounts.append(int(log["data"], 16))
        if matched_amounts != [int(amount)]:
            raise WalletExecutionError("local wallet token effect is missing or ambiguous")

    def get_receipt(self, tx_hash: str, *, chain_id: int) -> WalletReceipt | None:
        with self._lock:
            deadline = time.monotonic() + self._rpc.timeout_seconds
            requested = _hash(tx_hash)
            self._chain(chain_id, deadline)
            raw = self._rpc.call(
                chain_id,
                "eth_getTransactionReceipt",
                [requested],
                deadline=deadline,
            )
            if raw is None:
                return None
            try:
                if not isinstance(raw, dict) or _hash(raw.get("transactionHash")) != requested:
                    raise ValueError("invalid receipt")
                block = _quantity(raw.get("blockNumber"), maximum=SQLITE_INT64_MAX)
                block_hash = _hash(raw.get("blockHash"))
                status = _quantity(raw.get("status"), maximum=1)
                tx = self._rpc.call(
                    chain_id,
                    "eth_getTransactionByHash",
                    [requested],
                    deadline=deadline,
                )
                if not isinstance(tx, dict) or _hash(tx.get("hash")) != requested:
                    raise ValueError("invalid transaction")
                transfer = self._receipt_transfer(tx, chain_id)
                # Reconstruct our deterministic signature after restart, binding
                # the RPC transaction fields to its actual transaction hash.
                _, expected_hash = self._signed(transfer)
                if (
                    expected_hash != requested
                    or _hash(tx.get("blockHash")) != block_hash
                    or _quantity(tx.get("blockNumber")) != block
                    or canonical_evm_address(raw["from"]) != transfer.source_address
                    or canonical_evm_address(raw["to"]) != transfer.to_address
                ):
                    raise ValueError("transaction identity mismatch")
                canonical = self._rpc.call(
                    chain_id, "eth_getBlockByNumber", [hex(block), False], deadline=deadline
                )
                if (
                    not isinstance(canonical, dict)
                    or _hash(canonical.get("hash")) != block_hash
                    or _quantity(canonical.get("number")) != block
                ):
                    raise ValueError("receipt block is not canonical")
                latest = _quantity(
                    self._rpc.call(chain_id, "eth_blockNumber", [], deadline=deadline),
                    maximum=SQLITE_INT64_MAX,
                )
                if latest < block:
                    raise ValueError("chain tip precedes receipt")
                if status == 1 and transfer.asset_type == "token":
                    self._token_effect(raw.get("logs"), transfer, requested, block_hash, block)
                recipient, amount = self._effect(transfer)
                effect_hash = (
                    None
                    if status == 0
                    else "0x"
                    + content_hash(
                        {
                            "tx_hash": requested,
                            "chain_id": chain_id,
                            "asset_type": transfer.asset_type,
                            "contract_address": transfer.contract_address,
                            "recipient_address": recipient,
                            "amount": amount,
                        }
                    )
                )
                return WalletReceipt(
                    requested,
                    chain_id,
                    cast(Literal[0, 1], status),
                    block,
                    block_hash,
                    latest - block + 1,
                    effect_hash,
                )
            except Exception:
                raise WalletExecutionError("local wallet receipt validation failed") from None
