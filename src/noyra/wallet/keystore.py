"""Encrypted local Ethereum wallet keystore.

Only the encrypted Ethereum V3 document is persisted.  Decryption is an
explicit operation and the resulting account is kept in the caller's memory.
"""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

from eth_account import Account
from eth_account.signers.local import LocalAccount

from noyra.core.at_rest import (
    _harden_tree,
    _private_permission_error,
    validate_keyring_path,
    validate_private_file,
    validate_private_root,
)

MAX_KEYSTORE_BYTES = 16 * 1024
MAX_PASSWORD_BYTES = 4096
MIN_PASSWORD_CHARS = 8
KEYSTORE_FORMAT_VERSION = 3
# A fixed, bounded cost makes malformed files unable to induce unbounded work.
SCRYPT_N = 2**18
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32


def _error(message: str) -> ValueError:
    return ValueError(message)


def _password(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) < MIN_PASSWORD_CHARS
        or len(value.encode("utf-8")) > MAX_PASSWORD_BYTES
        or any(character in value for character in ("\x00", "\r", "\n"))
    ):
        raise _error("wallet password is invalid")
    return value


def _prepare_parent(path: Path) -> Path:
    configured = Path(path).expanduser()
    parent = configured.parent
    # validate_private_root rejects symlink/reparse ancestors and device crossings.
    try:
        if parent.exists():
            resolved = validate_private_root(parent, label="wallet directory")
            permission_error = _private_permission_error(resolved)
            if permission_error is not None:
                raise _error("wallet directory permissions are unsafe")
            return resolved
        # Resolve the nearest existing ancestor before creating anything.  New
        # components are private from their first mkdir and hardened again
        # after creation.
        ancestor = parent
        while not ancestor.exists() and ancestor.parent != ancestor:
            ancestor = ancestor.parent
        validate_private_root(ancestor, label="wallet directory parent")
        missing: list[Path] = []
        current = parent
        while not current.exists():
            missing.append(current)
            current = current.parent
        for directory in reversed(missing):
            directory.mkdir(mode=0o700)
            validate_private_root(directory, label="wallet directory")
            _harden_tree(directory)
        resolved = validate_private_root(parent, label="wallet directory")
        _harden_tree(resolved)
        permission_error = _private_permission_error(resolved)
        if permission_error is not None:
            raise _error("wallet directory permissions are unsafe")
        return resolved
    except ValueError:
        raise
    except Exception as exc:
        raise _error("wallet directory is unavailable") from exc


def _validate_existing_file(path: Path, *, label: str = "wallet keystore") -> Path:
    try:
        parent = validate_private_root(path.parent, label="wallet directory")
        candidate = validate_private_file(parent, path.name, allow_missing=False)
        candidate = validate_keyring_path(candidate)
        metadata = candidate.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise _error(f"{label} is unsafe")
        permission_error = _private_permission_error(parent)
        if permission_error is not None:
            raise _error(f"{label} permissions are unsafe")
        return candidate
    except ValueError:
        raise
    except Exception as exc:
        raise _error(f"{label} is unavailable") from exc


def _canonical_payload(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


def _write_exclusive(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.urandom(16).hex()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    descriptor = os.open(temporary, flags, 0o600)
    try:
        try:
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("wallet keystore write made no progress")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        with suppress(OSError):
            os.chmod(temporary, 0o600)
        # A hard-link installation is atomic and fails when the destination
        # already exists, so an existing wallet can never be replaced.
        os.link(temporary, path)
        os.unlink(temporary)
        with suppress(OSError):
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except Exception:
        with suppress(OSError):
            os.unlink(temporary)
        raise


def _validate_kdf(payload: dict[str, Any]) -> None:
    if set(payload) != {"address", "crypto", "id", "version"}:
        raise _error("wallet keystore structure is invalid")
    if type(payload["version"]) is not int or payload["version"] != KEYSTORE_FORMAT_VERSION:
        raise _error("wallet keystore version is unsupported")
    _hex_field(payload["address"], 20, "wallet keystore address")
    if not isinstance(payload["id"], str) or len(payload["id"]) > 128:
        raise _error("wallet keystore ID is invalid")
    try:
        uuid.UUID(payload["id"])
    except (ValueError, AttributeError, TypeError) as exc:
        raise _error("wallet keystore ID is invalid") from exc
    crypto = payload["crypto"]
    if not isinstance(crypto, dict) or set(crypto) != {
        "cipher",
        "cipherparams",
        "ciphertext",
        "kdf",
        "kdfparams",
        "mac",
    }:
        raise _error("wallet keystore crypto structure is invalid")
    if crypto["cipher"] != "aes-128-ctr" or crypto["kdf"] != "scrypt":
        raise _error("wallet keystore cipher is unsupported")
    cipherparams = crypto["cipherparams"]
    if not isinstance(cipherparams, dict) or set(cipherparams) != {"iv"}:
        raise _error("wallet keystore cipher parameters are invalid")
    _hex_field(cipherparams["iv"], 16, "wallet keystore IV")
    _hex_field(crypto["ciphertext"], 32, "wallet keystore ciphertext")
    _hex_field(crypto["mac"], 32, "wallet keystore MAC")
    params = crypto["kdfparams"]
    if not isinstance(params, dict) or set(params) != {"dklen", "n", "p", "r", "salt"}:
        raise _error("wallet keystore KDF parameters are invalid")
    if (
        type(params["dklen"]) is not int
        or params["dklen"] != SCRYPT_DKLEN
        or type(params["n"]) is not int
        or params["n"] != SCRYPT_N
        or type(params["r"]) is not int
        or params["r"] != SCRYPT_R
        or type(params["p"]) is not int
        or params["p"] != SCRYPT_P
    ):
        raise _error("wallet keystore KDF parameters are unsupported")
    _hex_field(params["salt"], 16, "wallet keystore salt")


def _hex_field(value: object, byte_length: int, label: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != byte_length * 2
        or re.fullmatch("[a-fA-F0-9]+", value) is None
    ):
        raise _error(f"{label} is invalid")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise _error("wallet keystore JSON is invalid")
        payload[key] = value
    return payload


def _read_private(path: Path, limit: int, label: str) -> bytes:
    candidate = _validate_existing_file(path, label=label)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        expected = candidate.lstat()
        descriptor = os.open(candidate, flags)
        try:
            actual = os.fstat(descriptor)
            if (
                not stat.S_ISREG(actual.st_mode)
                or actual.st_nlink != 1
                or (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino)
            ):
                raise _error(f"{label} changed while opening")
            if not 0 < actual.st_size <= limit:
                raise _error(f"{label} size is invalid")
            raw = bytearray()
            while len(raw) <= limit:
                chunk = os.read(descriptor, limit + 1 - len(raw))
                if not chunk:
                    break
                raw.extend(chunk)
            latest = candidate.lstat()
            if (
                len(raw) > limit
                or (actual.st_dev, actual.st_ino) != (latest.st_dev, latest.st_ino)
                or latest.st_nlink != 1
            ):
                raise _error(f"{label} changed while reading")
            return bytes(raw)
        finally:
            os.close(descriptor)
    except ValueError:
        raise
    except Exception as exc:
        raise _error(f"{label} is unavailable") from exc


def create_keystore(path: Path, password: str, *, private_key: str | None = None) -> str:
    """Create an encrypted V3 wallet file and return its lowercase address."""
    _password(password)
    configured = Path(path).expanduser()
    if ":" in configured.name or configured.name in {"", ".", ".."}:
        raise _error("wallet keystore path is invalid")
    parent = _prepare_parent(configured)
    target = parent / configured.name
    if target.exists() or target.is_symlink():
        raise _error("wallet keystore already exists")
    try:
        if private_key is None:
            account = Account.create()
        else:
            encoded = private_key.removeprefix("0x")
            _hex_field(encoded, 32, "wallet private key")
            account = Account.from_key(bytes.fromhex(encoded))
        payload = Account.encrypt(account.key, password, kdf="scrypt", iterations=SCRYPT_N)
        # Validate our assumptions about dependency output before persisting it.
        _validate_kdf(payload)
        _write_exclusive(target, _canonical_payload(payload))
    except ValueError as exc:
        message = str(exc)
        if "already exists" in message:
            raise
        raise _error("wallet private key is invalid") from exc
    except FileExistsError as exc:
        raise _error("wallet keystore already exists") from exc
    except Exception as exc:
        raise _error("wallet keystore could not be created") from exc
    return str(account.address).lower()


def load_account(path: Path, password: str) -> LocalAccount:
    """Decrypt and validate a wallet keystore into an in-memory account."""
    _password(password)
    raw = _read_private(Path(path).expanduser(), MAX_KEYSTORE_BYTES, "wallet keystore")
    if not raw or len(raw) > MAX_KEYSTORE_BYTES:
        raise _error("wallet keystore size is invalid")
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (UnicodeError, ValueError, RecursionError, json.JSONDecodeError) as exc:
        raise _error("wallet keystore JSON is invalid") from exc
    if not isinstance(payload, dict):
        raise _error("wallet keystore structure is invalid")
    _validate_kdf(payload)
    try:
        key = Account.decrypt(payload, password)
        account = Account.from_key(key)
    except Exception as exc:
        raise _error("wallet password is incorrect or keystore is invalid") from exc
    stored_address = str(payload["address"]).lower()
    if not stored_address.startswith("0x"):
        stored_address = "0x" + stored_address
    if account.address.lower() != stored_address:
        raise _error("wallet keystore address does not match its key")
    return account  # type: ignore[no-any-return]


def read_password_file(path: Path) -> str:
    """Read a private service password file, removing one terminal newline."""
    raw = _read_private(Path(path).expanduser(), MAX_PASSWORD_BYTES, "wallet password file")
    if not raw or len(raw) > MAX_PASSWORD_BYTES:
        raise _error("wallet password file is invalid")
    if raw.endswith(b"\r\n"):
        raw = raw[:-2]
    elif raw.endswith(b"\n"):
        raw = raw[:-1]
    try:
        password = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _error("wallet password file is invalid") from exc
    if not password or "\n" in password or "\r" in password:
        raise _error("wallet password file is invalid")
    _password(password)
    return password


class WalletKeyStore:
    """Small object API for callers that prefer a namespaced keystore."""

    create = staticmethod(create_keystore)
    load = staticmethod(load_account)


# Function aliases used by configuration adapters and older callers.
create_wallet = create_keystore
load_wallet = load_account
