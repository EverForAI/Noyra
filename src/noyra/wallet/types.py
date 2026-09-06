from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SQLITE_INT64_MAX = 9_223_372_036_854_775_807
_EVM_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")
_SYMBOL = re.compile(r"[A-Z0-9._-]{1,32}\Z")
_BALANCE = re.compile(r"(?:0|[1-9][0-9]{0,255})\Z")
_SOURCE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")

WalletChainFamily = Literal["evm"]
WalletAssetType = Literal["native", "token"]
WalletAddressPurpose = Literal["treasury", "spending", "observation", "external"]


def canonical_evm_address(value: str) -> str:
    """Return the storage form for one EVM public address.

    This intentionally does not accept a private key, seed phrase, or any
    other signer material. Lowercase storage makes equivalent EIP-55 display
    variants collide before they can become separate read-only registrations.
    """

    candidate = value.strip()
    if _EVM_ADDRESS.fullmatch(candidate) is None:
        raise ValueError("wallet address must be a 20-byte EVM address")
    return candidate.lower()


def canonical_balance(value: str) -> str:
    """Validate a non-negative integer balance without integer coercion."""

    if _BALANCE.fullmatch(value) is None:
        raise ValueError("wallet balance must be a canonical decimal string")
    return value


def canonical_timestamp(value: str) -> str:
    """Normalize an explicit observation time to UTC milliseconds."""

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("wallet observation time must be ISO-8601") from error
    if parsed.tzinfo is None:
        raise ValueError("wallet observation time must include a timezone")
    return parsed.astimezone(UTC).isoformat(timespec="milliseconds")


def validate_timestamp(value: object) -> bool:
    """Return whether a persisted timestamp is a timezone-aware ISO value."""

    if not isinstance(value, str) or not value:
        return False
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


def _nonblank_text(value: str, *, field: str, maximum: int) -> str:
    candidate = value.strip()
    if not candidate or len(candidate) > maximum:
        raise ValueError(f"wallet {field} is invalid")
    return candidate


def _symbol(value: str, *, field: str) -> str:
    candidate = _nonblank_text(value, field=field, maximum=32).upper()
    if _SYMBOL.fullmatch(candidate) is None:
        raise ValueError(f"wallet {field} is invalid")
    return candidate


def _rpc_url(value: str) -> str:
    candidate = _nonblank_text(value, field="RPC URL", maximum=512)
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError as error:
        raise ValueError("wallet RPC URL is invalid") from error
    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port not in {None, 443}
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("wallet RPC URL must be a credential-free HTTPS origin")
    try:
        hostname = parsed.hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as error:
        raise ValueError("wallet RPC URL is invalid") from error
    if hostname == "localhost":
        raise ValueError("wallet RPC URL must use a public endpoint")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("wallet RPC URL must use a public endpoint")
    netloc = f"[{hostname}]" if ":" in hostname else hostname
    return f"https://{netloc}"


class WalletNetworkInput(BaseModel):
    """An operator-provided, public read-only chain registration."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    label: str = Field(min_length=1, max_length=128)
    chain_family: WalletChainFamily = "evm"
    chain_id: int = Field(ge=1, le=SQLITE_INT64_MAX)
    native_symbol: str = Field(min_length=1, max_length=32)
    rpc_url: str | None = Field(default=None, max_length=512)

    @field_validator("label")
    @classmethod
    def validate_label(cls, value: str) -> str:
        return _nonblank_text(value, field="network label", maximum=128)

    @field_validator("native_symbol")
    @classmethod
    def validate_native_symbol(cls, value: str) -> str:
        return _symbol(value, field="native symbol")

    @field_validator("rpc_url")
    @classmethod
    def validate_rpc_url(cls, value: str | None) -> str | None:
        return None if value is None else _rpc_url(value)


class WalletAssetInput(BaseModel):
    """A public native-asset or token registration on an existing network."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    network_id: str = Field(min_length=1, max_length=128)
    asset_type: WalletAssetType
    contract_address: str | None = Field(default=None, max_length=42)
    name: str = Field(min_length=1, max_length=128)
    symbol: str = Field(min_length=1, max_length=32)
    decimals: int = Field(ge=0, le=255)

    @field_validator("network_id")
    @classmethod
    def validate_network_id(cls, value: str) -> str:
        return _nonblank_text(value, field="network identifier", maximum=128)

    @field_validator("contract_address")
    @classmethod
    def validate_contract_address(cls, value: str | None) -> str | None:
        return None if value is None else canonical_evm_address(value)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _nonblank_text(value, field="asset name", maximum=128)

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        return _symbol(value, field="asset symbol")

    @model_validator(mode="after")
    def validate_asset_shape(self) -> WalletAssetInput:
        if self.asset_type == "native" and self.contract_address is not None:
            raise ValueError("native assets cannot have a contract address")
        if self.asset_type == "token" and self.contract_address is None:
            raise ValueError("token assets require a contract address")
        return self


class WalletAddressInput(BaseModel):
    """A public address registration; this is never signer enrollment."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    network_id: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=128)
    address: str = Field(min_length=42, max_length=42)
    purpose: WalletAddressPurpose = "observation"

    @field_validator("network_id")
    @classmethod
    def validate_network_id(cls, value: str) -> str:
        return _nonblank_text(value, field="network identifier", maximum=128)

    @field_validator("label")
    @classmethod
    def validate_label(cls, value: str) -> str:
        return _nonblank_text(value, field="address label", maximum=128)

    @field_validator("address")
    @classmethod
    def validate_address(cls, value: str) -> str:
        return canonical_evm_address(value)


class WalletBalanceSnapshotInput(BaseModel):
    """One externally observed balance, represented without precision loss."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    asset_id: str = Field(min_length=1, max_length=128)
    address_id: str = Field(min_length=1, max_length=128)
    balance: str = Field(min_length=1, max_length=256)
    source: str = Field(default="operator_observation", min_length=1, max_length=64)
    observed_at: str | None = Field(default=None, max_length=64)

    @field_validator("asset_id", "address_id")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        return _nonblank_text(value, field="balance identifier", maximum=128)

    @field_validator("balance")
    @classmethod
    def validate_balance(cls, value: str) -> str:
        return canonical_balance(value)

    @field_validator("source")
    @classmethod
    def validate_source(cls, value: str) -> str:
        candidate = _nonblank_text(value, field="balance source", maximum=64)
        if _SOURCE.fullmatch(candidate) is None:
            raise ValueError("wallet balance source is invalid")
        return candidate

    @field_validator("observed_at")
    @classmethod
    def validate_observed_at(cls, value: str | None) -> str | None:
        return None if value is None else canonical_timestamp(value)


@dataclass(frozen=True)
class WalletNetworkRecord:
    network_id: str
    subject_id: str
    label: str
    chain_family: str
    chain_id: int
    native_symbol: str
    rpc_url: str | None
    status: str
    created_at: str
    revoked_at: str | None
    revoke_reason: str | None


@dataclass(frozen=True)
class WalletAssetRecord:
    asset_id: str
    subject_id: str
    network_id: str
    asset_type: str
    contract_address: str | None
    name: str
    symbol: str
    decimals: int
    status: str
    created_at: str
    revoked_at: str | None
    revoke_reason: str | None


@dataclass(frozen=True)
class WalletAddressRecord:
    address_id: str
    subject_id: str
    network_id: str
    label: str
    address: str
    purpose: str
    status: str
    created_at: str
    revoked_at: str | None
    revoke_reason: str | None


@dataclass(frozen=True)
class WalletBalanceSnapshotRecord:
    snapshot_id: str
    subject_id: str
    network_id: str
    asset_id: str
    address_id: str
    balance: str
    source: str
    observed_at: str
    created_at: str


@dataclass(frozen=True)
class WalletBalanceHistoryPage:
    """One stable, bounded page of immutable balance observations."""

    items: tuple[WalletBalanceSnapshotRecord, ...]
    next_cursor: str | None
    has_more: bool
