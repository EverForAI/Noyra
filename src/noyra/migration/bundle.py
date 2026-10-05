"""Recipient-encrypted, task-bound migration artifact envelopes."""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import secrets
import stat
from collections.abc import Mapping
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from noyra.core.types import canonical_json

BUNDLE_FORMAT = "noyra-migration-bundle/v1"
_CHUNK_BYTES = 1024 * 1024
_MAX_BUNDLE_BYTES = 10 * 1024 * 1024 * 1024
_MANIFEST_FIELDS = frozenset(
    {
        "format",
        "task_id",
        "target_id",
        "source_epoch",
        "recipient_key_fingerprint",
        "ephemeral_public_key",
        "salt",
        "nonce",
        "plaintext_size",
        "plaintext_sha256",
        "ciphertext_size",
        "ciphertext_sha256",
        "artifact_id",
        "subject_id",
        "schema_version",
        "artifact_format",
        "byte_size",
        "artifact_sha256",
        "payload_format",
        "database_sha256",
        "inventory_sha256",
    }
)
_REQUIRED_MANIFEST_FIELDS = _MANIFEST_FIELDS - {
    "artifact_id",
    "subject_id",
    "schema_version",
    "artifact_format",
    "byte_size",
    "artifact_sha256",
    "payload_format",
    "database_sha256",
    "inventory_sha256",
}


def encrypt_bundle(
    source: Path | str,
    destination: Path | str,
    *,
    recipient_public_key: X25519PublicKey,
    context: Mapping[str, str],
) -> dict[str, Any]:
    """Encrypt a file to an enrolled target key without buffering the file in memory."""
    source_path = Path(source).expanduser()
    destination_path = Path(destination).expanduser()
    _validate_context(context)
    if not isinstance(recipient_public_key, X25519PublicKey):
        raise ValueError("recipient public key is invalid")
    if source_path.is_symlink() or not source_path.is_file():
        raise ValueError("migration bundle source must be a regular file")
    plaintext_size = source_path.stat().st_size
    if not 0 < plaintext_size <= _MAX_BUNDLE_BYTES:
        raise ValueError("migration bundle size is invalid")
    if destination_path.exists() or destination_path.is_symlink():
        raise ValueError("migration bundle destination already exists")

    plaintext_digest = _file_digest(source_path)
    ephemeral_private = X25519PrivateKey.generate()
    ephemeral_public = ephemeral_private.public_key().public_bytes_raw()
    recipient_public = recipient_public_key.public_bytes_raw()
    recipient_fingerprint = hashlib.sha256(recipient_public).hexdigest()
    salt = os.urandom(32)
    nonce = os.urandom(12)
    metadata = {
        "format": BUNDLE_FORMAT,
        **dict(context),
        "recipient_key_fingerprint": recipient_fingerprint,
        "ephemeral_public_key": _encode(ephemeral_public),
        "salt": _encode(salt),
        "nonce": _encode(nonce),
        "plaintext_size": plaintext_size,
        "plaintext_sha256": plaintext_digest,
    }
    aad = canonical_json(metadata).encode("utf-8")
    key = _derive_key(ephemeral_private.exchange(recipient_public_key), salt, context)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(aad)

    parent_fd, destination_name = _open_private_parent(destination_path)
    temporary = _temporary_path(destination_path)
    temporary_name = temporary.name
    ciphertext_hash = hashlib.sha256()
    encrypted_plaintext_hash = hashlib.sha256()
    written = 0
    plaintext_read = 0
    try:
        with ExitStack() as stack:
            input_stream = stack.enter_context(os.fdopen(_open_regular_source(source_path), "rb"))
            output_stream = stack.enter_context(
                os.fdopen(
                    _open_new_private_file(parent_fd, destination_path.parent, temporary_name),
                    "wb",
                )
            )
            while chunk := input_stream.read(_CHUNK_BYTES):
                encrypted_plaintext_hash.update(chunk)
                plaintext_read += len(chunk)
                encrypted = encryptor.update(chunk)
                output_stream.write(encrypted)
                ciphertext_hash.update(encrypted)
                written += len(encrypted)
            final = encryptor.finalize()
            output_stream.write(final)
            ciphertext_hash.update(final)
            tag = encryptor.tag
            output_stream.write(tag)
            ciphertext_hash.update(tag)
            written += len(final) + len(tag)
            if (
                plaintext_read != plaintext_size
                or encrypted_plaintext_hash.hexdigest() != plaintext_digest
            ):
                raise ValueError("migration bundle source changed while encrypting")
            output_stream.flush()
            os.fsync(output_stream.fileno())
        _publish_new_file(
            temporary_name,
            destination_name,
            parent_fd,
            destination_path.parent,
        )
    except Exception:
        _unlink_private_file(temporary_name, parent_fd, destination_path.parent)
        raise
    finally:
        if parent_fd is not None:
            os.close(parent_fd)
    return {
        **metadata,
        "ciphertext_size": written,
        "ciphertext_sha256": ciphertext_hash.hexdigest(),
    }


def decrypt_bundle(
    source: Path | str,
    destination: Path | str,
    *,
    recipient_private_key: X25519PrivateKey,
    manifest: Mapping[str, Any],
    expected_context: Mapping[str, str],
) -> None:
    """Authenticate and decrypt a bundle to a private temporary file, then publish it."""
    source_path = Path(source).expanduser()
    destination_path = Path(destination).expanduser()
    _validate_context(expected_context)
    values = _validate_manifest(manifest)
    if not isinstance(recipient_private_key, X25519PrivateKey):
        raise ValueError("recipient private key is invalid")
    if any(values.get(key) != value for key, value in expected_context.items()):
        raise ValueError("migration bundle context does not match")
    recipient_public = recipient_private_key.public_key().public_bytes_raw()
    if hashlib.sha256(recipient_public).hexdigest() != values["recipient_key_fingerprint"]:
        raise ValueError("migration bundle recipient key does not match")
    if source_path.is_symlink() or not source_path.is_file():
        raise ValueError("migration bundle source must be a regular file")
    if destination_path.exists() or destination_path.is_symlink():
        raise ValueError("migration bundle destination already exists")
    if source_path.stat().st_size != values["ciphertext_size"]:
        raise ValueError("migration bundle ciphertext size mismatch")

    ephemeral_public = X25519PublicKey.from_public_bytes(
        _decode(values["ephemeral_public_key"], 32, "ephemeral public key")
    )
    salt = _decode(values["salt"], 32, "salt")
    nonce = _decode(values["nonce"], 12, "nonce")
    context = {
        key: str(values[key])
        for key in values
        if key
        not in {
            "format",
            "recipient_key_fingerprint",
            "ephemeral_public_key",
            "salt",
            "nonce",
            "plaintext_size",
            "plaintext_sha256",
            "ciphertext_size",
            "ciphertext_sha256",
            "byte_size",
            "artifact_sha256",
        }
    }
    metadata = {
        key: values[key]
        for key in values
        if key not in {"ciphertext_size", "ciphertext_sha256", "byte_size", "artifact_sha256"}
    }
    aad = canonical_json(metadata).encode("utf-8")
    key = _derive_key(recipient_private_key.exchange(ephemeral_public), salt, context)
    decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).decryptor()
    decryptor.authenticate_additional_data(aad)

    parent_fd, destination_name = _open_private_parent(destination_path)
    temporary = _temporary_path(destination_path)
    temporary_name = temporary.name
    ciphertext_hash = hashlib.sha256()
    plaintext_hash = hashlib.sha256()
    remaining = values["ciphertext_size"] - 16
    plaintext_written = 0
    try:
        with ExitStack() as stack:
            input_stream = stack.enter_context(os.fdopen(_open_regular_source(source_path), "rb"))
            output_stream = stack.enter_context(
                os.fdopen(
                    _open_new_private_file(parent_fd, destination_path.parent, temporary_name),
                    "wb",
                )
            )
            while remaining:
                chunk = input_stream.read(min(_CHUNK_BYTES, remaining))
                if not chunk:
                    raise ValueError("migration bundle ciphertext is truncated")
                remaining -= len(chunk)
                ciphertext_hash.update(chunk)
                plaintext = decryptor.update(chunk)
                plaintext_hash.update(plaintext)
                plaintext_written += len(plaintext)
                output_stream.write(plaintext)
            tag = input_stream.read(16)
            if len(tag) != 16 or input_stream.read(1):
                raise ValueError("migration bundle authentication tag is invalid")
            ciphertext_hash.update(tag)
            final = decryptor.finalize_with_tag(tag)
            plaintext_hash.update(final)
            plaintext_written += len(final)
            output_stream.write(final)
            if ciphertext_hash.hexdigest() != values["ciphertext_sha256"]:
                raise ValueError("migration bundle ciphertext digest mismatch")
            if (
                plaintext_written != values["plaintext_size"]
                or plaintext_hash.hexdigest() != values["plaintext_sha256"]
            ):
                raise ValueError("migration bundle plaintext digest mismatch")
            output_stream.flush()
            os.fsync(output_stream.fileno())
        _publish_new_file(
            temporary_name,
            destination_name,
            parent_fd,
            destination_path.parent,
        )
    except (InvalidTag, OSError, ValueError) as error:
        _unlink_private_file(temporary_name, parent_fd, destination_path.parent)
        if isinstance(error, InvalidTag):
            raise ValueError("migration bundle authentication failed") from error
        raise
    except Exception:
        _unlink_private_file(temporary_name, parent_fd, destination_path.parent)
        raise
    finally:
        if parent_fd is not None:
            os.close(parent_fd)


def _validate_context(context: Mapping[str, str]) -> None:
    required = {"task_id", "target_id", "source_epoch"}
    if (
        not isinstance(context, Mapping)
        or not required.issubset(context)
        or any(not isinstance(key, str) or not key for key in context)
        or any(
            not isinstance(value, str) or not value or len(value) > 128
            for value in context.values()
        )
    ):
        raise ValueError("migration bundle context is invalid")


def _validate_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(manifest, Mapping) or not _REQUIRED_MANIFEST_FIELDS.issubset(manifest):
        raise ValueError("migration bundle manifest is invalid")
    if set(manifest) - _MANIFEST_FIELDS:
        raise ValueError("migration bundle manifest is invalid")
    values = dict(manifest)
    if values["format"] != BUNDLE_FORMAT:
        raise ValueError("migration bundle format is unsupported")
    if any(key in values for key in ("payload_format", "database_sha256", "inventory_sha256")):
        from .subject_payload import PAYLOAD_FORMAT

        if values.get("payload_format") != PAYLOAD_FORMAT:
            raise ValueError("migration subject payload format is unsupported")
        for key in ("database_sha256", "inventory_sha256"):
            digest = values.get(key)
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)
            ):
                raise ValueError("migration subject payload digest is invalid")
    _validate_context({key: values[key] for key in ("task_id", "target_id", "source_epoch")})
    if (
        type(values["plaintext_size"]) is not int
        or not 0 < values["plaintext_size"] <= _MAX_BUNDLE_BYTES
        or type(values["ciphertext_size"]) is not int
        or values["ciphertext_size"] != values["plaintext_size"] + 16
    ):
        raise ValueError("migration bundle size is invalid")
    for field in ("recipient_key_fingerprint", "plaintext_sha256", "ciphertext_sha256"):
        if (
            not isinstance(values[field], str)
            or len(values[field]) != 64
            or any(character not in "0123456789abcdef" for character in values[field])
        ):
            raise ValueError("migration bundle digest is invalid")
    _decode(values["ephemeral_public_key"], 32, "ephemeral public key")
    _decode(values["salt"], 32, "salt")
    _decode(values["nonce"], 12, "nonce")
    if "byte_size" in values and values["byte_size"] != values["ciphertext_size"]:
        raise ValueError("migration bundle transfer size is invalid")
    if "artifact_sha256" in values and values["artifact_sha256"] != values["ciphertext_sha256"]:
        raise ValueError("migration bundle transfer digest is invalid")
    if "artifact_id" in values and (
        not isinstance(values["artifact_id"], str) or not values["artifact_id"]
    ):
        raise ValueError("migration bundle artifact id is invalid")
    if "subject_id" in values and (
        not isinstance(values["subject_id"], str) or not values["subject_id"]
    ):
        raise ValueError("migration bundle subject is invalid")
    if "schema_version" in values and (
        not (
            (type(values["schema_version"]) is int and values["schema_version"] >= 1)
            or (isinstance(values["schema_version"], str) and values["schema_version"].isdigit())
        )
    ):
        raise ValueError("migration bundle schema version is invalid")
    return values


def _derive_key(shared_secret: bytes, salt: bytes, context: Mapping[str, str]) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=BUNDLE_FORMAT.encode() + b"\n" + canonical_json(dict(context)).encode("utf-8"),
    ).derive(shared_secret)


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: Any, expected_length: int, label: str) -> bytes:
    if not isinstance(value, str) or not value or "=" in value:
        raise ValueError(f"migration bundle {label} is invalid")
    try:
        encoded = value.encode("ascii")
        decoded = base64.b64decode(
            encoded + b"=" * (-len(encoded) % 4), altchars=b"-_", validate=True
        )
    except (binascii.Error, UnicodeEncodeError, ValueError, TypeError) as error:
        raise ValueError(f"migration bundle {label} is invalid") from error
    if len(decoded) != expected_length or _encode(decoded) != value:
        raise ValueError(f"migration bundle {label} is invalid")
    return decoded


def _temporary_path(destination: Path) -> Path:
    return destination.with_name(f".{destination.name}.{secrets.token_hex(12)}.tmp")


def _open_private_parent(destination: Path) -> tuple[int | None, str]:
    """Open an existing private directory without following POSIX symlinks."""
    if destination.name in {"", ".", ".."}:
        raise ValueError("migration bundle destination name is invalid")
    parent = destination.parent
    if ".." in parent.parts:
        raise ValueError("migration bundle destination path is invalid")
    _reject_symlink_components(parent)
    try:
        details = parent.stat()
    except OSError as error:
        raise ValueError("migration bundle destination parent is unavailable") from error
    if not stat.S_ISDIR(details.st_mode):
        raise ValueError("migration bundle destination parent is invalid")
    if os.name == "nt":
        return None, destination.name
    get_effective_uid = getattr(os, "geteuid", lambda: details.st_uid)
    if details.st_uid != get_effective_uid() or stat.S_IMODE(details.st_mode) & 0o077:
        raise ValueError("migration bundle destination parent must be private directory")

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    absolute_parent = parent.absolute()
    descriptor = os.open(absolute_parent.anchor, flags)
    try:
        for component in absolute_parent.parts[1:]:
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or opened.st_uid != get_effective_uid()
            or stat.S_IMODE(opened.st_mode) & 0o077
        ):
            raise ValueError("migration bundle destination parent must be private directory")
        return descriptor, destination.name
    except Exception:
        os.close(descriptor)
        raise


def _reject_symlink_components(path: Path) -> None:
    """Reject symlink/reparse-point path components before opening the directory."""
    absolute = path.absolute()
    cursor = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        cursor = cursor / component
        try:
            details = cursor.lstat()
        except FileNotFoundError as error:
            raise ValueError("migration bundle destination parent is unavailable") from error
        attributes = getattr(details, "st_file_attributes", 0)
        reparse = bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        if stat.S_ISLNK(details.st_mode) or reparse:
            raise ValueError("migration bundle destination path contains a symlink")


def _open_regular_source(source: Path) -> int:
    _reject_symlink_components(source.parent)
    if source.is_symlink():
        raise ValueError("migration bundle source must be a regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError as error:
        raise ValueError("migration bundle source must be a regular file") from error
    details = os.fstat(descriptor)
    if not stat.S_ISREG(details.st_mode):
        os.close(descriptor)
        raise ValueError("migration bundle source must be a regular file")
    return descriptor


def _open_new_private_file(parent_fd: int | None, parent: Path, name: str) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    if parent_fd is not None:
        return os.open(name, flags, 0o600, dir_fd=parent_fd)
    return os.open(parent / name, flags, 0o600)


def _unlink_private_file(name: str, parent_fd: int | None, parent: Path) -> None:
    try:
        if parent_fd is not None:
            os.unlink(name, dir_fd=parent_fd)
        else:
            (parent / name).unlink(missing_ok=True)
    except FileNotFoundError:
        pass


def _publish_new_file(
    temporary: str,
    destination: str,
    parent_fd: int | None,
    parent: Path,
) -> None:
    """Atomically publish without replacing a path created by another process."""
    if parent_fd is not None:
        os.link(
            temporary,
            destination,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            follow_symlinks=False,
        )
        os.unlink(temporary, dir_fd=parent_fd)
        os.fsync(parent_fd)
    else:
        os.link(parent / temporary, parent / destination, follow_symlinks=False)
        (parent / temporary).unlink()
        if os.name != "nt":
            descriptor = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
