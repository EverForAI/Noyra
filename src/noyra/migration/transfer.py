"""Bounded, resumable artifact transfer with content-addressed chunks."""

from __future__ import annotations

import hashlib
import os
import secrets
import struct
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

MAX_ARTIFACT_BYTES = 10 * 1024 * 1024 * 1024
MAX_CHUNKS = 2_000_000
_ENCRYPTED_MAGIC = b"NOYRA-ENC1"


@dataclass(frozen=True)
class TransferReceipt:
    source: str
    destination: str
    byte_size: int
    chunk_bytes: int
    next_chunk: int
    chunk_hashes: tuple[str, ...]
    artifact_hash: str
    complete: bool


@dataclass(frozen=True)
class EncryptedTransferReceipt:
    source: str
    destination: str
    byte_size: int
    chunk_bytes: int
    chunk_count: int
    chunk_hashes: tuple[str, ...]
    artifact_hash: str
    manifest_digest: str
    nonce_prefix: bytes
    complete: bool = True


class TransferSession:
    def __init__(self, *, chunk_bytes: int = 1024 * 1024):
        if not 4096 <= chunk_bytes <= 64 * 1024 * 1024:
            raise ValueError("chunk size is outside safety bounds")
        self.chunk_bytes = chunk_bytes

    def send(
        self, source: Path | str, destination: Path | str, *, stop_after_chunks: int | None = None
    ) -> TransferReceipt:
        source_input = Path(source).expanduser()
        destination_input = Path(destination).expanduser()
        if ".." in destination_input.parts:
            raise ValueError("destination path cannot contain parent traversal")
        self._reject_symlink(source_input, "source")
        self._reject_symlink(destination_input, "destination")
        source_path = source_input.resolve()
        destination_path = destination_input.resolve()
        self._validate_paths(source_path, destination_path)
        if not source_path.is_file():
            raise ValueError("source must be a regular file")
        size = source_path.stat().st_size
        if size > MAX_ARTIFACT_BYTES:
            raise ValueError("artifact exceeds size limit")
        if stop_after_chunks is not None and (
            type(stop_after_chunks) is not int or stop_after_chunks < 1
        ):
            raise ValueError("stop chunk count is invalid")
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        if destination_path.exists() and destination_path.is_symlink():
            raise ValueError("destination cannot be a symlink")
        with source_path.open("rb") as source_stream, destination_path.open("wb") as target_stream:
            chunk_hashes: list[str] = []
            artifact = hashlib.sha256()
            index = 0
            while True:
                chunk = source_stream.read(self.chunk_bytes)
                if not chunk:
                    break
                digest = hashlib.sha256(chunk).hexdigest()
                chunk_hashes.append(digest)
                artifact.update(chunk)
                target_stream.write(chunk)
                index += 1
                if index > MAX_CHUNKS:
                    raise ValueError("artifact has too many chunks")
                if stop_after_chunks is not None and index >= stop_after_chunks:
                    target_stream.flush()
                    os.fsync(target_stream.fileno())
                    return TransferReceipt(
                        str(source_path), str(destination_path), size, self.chunk_bytes,
                        index, tuple(chunk_hashes), "", False,
                    )
            target_stream.flush()
            os.fsync(target_stream.fileno())
        return TransferReceipt(
            str(source_path), str(destination_path), size, self.chunk_bytes,
            index, tuple(chunk_hashes), artifact.hexdigest(), True,
        )

    def resume(self, receipt: TransferReceipt) -> TransferReceipt:
        source_input = Path(receipt.source).expanduser()
        destination_input = Path(receipt.destination).expanduser()
        self._reject_symlink(source_input, "source")
        self._reject_symlink(destination_input, "destination")
        source_path = source_input.resolve()
        destination_path = destination_input.resolve()
        self._validate_paths(source_path, destination_path)
        if (
            not source_path.is_file()
            or type(receipt.next_chunk) is not int
            or receipt.next_chunk < 0
            or receipt.next_chunk != len(receipt.chunk_hashes)
            or receipt.next_chunk > MAX_CHUNKS
        ):
            raise ValueError("transfer source is unavailable")
        if source_path.stat().st_size != receipt.byte_size:
            raise ValueError("transfer resume prefix is invalid")
        expected_prefix = receipt.next_chunk * receipt.chunk_bytes
        if not destination_path.is_file() or destination_path.stat().st_size != expected_prefix:
            raise ValueError("transfer resume prefix is invalid")
        with (
            source_path.open("rb") as source_prefix,
            destination_path.open("rb") as destination_prefix,
        ):
            for expected_hash in receipt.chunk_hashes:
                chunk = source_prefix.read(receipt.chunk_bytes)
                if hashlib.sha256(chunk).hexdigest() != expected_hash:
                    raise ValueError("transfer resume prefix is invalid")
                if destination_prefix.read(len(chunk)) != chunk:
                    raise ValueError("transfer resume prefix is invalid")
        with source_path.open("rb") as source_stream:
            source_stream.seek(expected_prefix)
            with destination_path.open("ab") as target_stream:
                chunk_hashes = list(receipt.chunk_hashes)
                artifact = hashlib.sha256()
                with source_path.open("rb") as full_stream:
                    while chunk := full_stream.read(receipt.chunk_bytes):
                        artifact.update(chunk)
                while chunk := source_stream.read(receipt.chunk_bytes):
                    if len(chunk_hashes) >= MAX_CHUNKS:
                        raise ValueError("artifact has too many chunks")
                    chunk_hashes.append(hashlib.sha256(chunk).hexdigest())
                    target_stream.write(chunk)
                target_stream.flush()
                os.fsync(target_stream.fileno())
        return replace(
            receipt,
            next_chunk=len(chunk_hashes),
            chunk_hashes=tuple(chunk_hashes),
            artifact_hash=artifact.hexdigest(),
            complete=True,
        )

    def verify(self, receipt: TransferReceipt) -> None:
        path = Path(receipt.destination).resolve()
        if not receipt.complete or not path.is_file():
            raise ValueError("transfer is incomplete")
        if path.stat().st_size != receipt.byte_size:
            raise ValueError("transfer byte size mismatch")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != receipt.artifact_hash:
            raise ValueError("transfer artifact hash mismatch")

    @staticmethod
    def _validate_paths(source: Path, destination: Path) -> None:
        if ".." in destination.parts:
            raise ValueError("destination path cannot contain parent traversal")
        if source == destination:
            raise ValueError("source and destination must differ")
        if destination.name in {"", ".", ".."}:
            raise ValueError("destination path is invalid")

    @staticmethod
    def _reject_symlink(path: Path, label: str) -> None:
        current = path
        while current != current.parent:
            if current.is_symlink():
                raise ValueError(f"{label} cannot be a symlink")
            current = current.parent


class EncryptedTransferSession:
    """Encrypt each bounded artifact chunk with an in-memory AEAD key."""

    def __init__(self, key: bytes, *, chunk_bytes: int = 1024 * 1024):
        if not isinstance(key, bytes) or len(key) != 32:
            raise ValueError("encrypted transfer key must be 32 bytes")
        if not 4096 <= chunk_bytes <= 64 * 1024 * 1024:
            raise ValueError("chunk size is outside safety bounds")
        self._cipher = ChaCha20Poly1305(key)
        self.chunk_bytes = chunk_bytes

    def send(
        self, source: Path | str, destination: Path | str, *, manifest_digest: str
    ) -> EncryptedTransferReceipt:
        self._validate_digest(manifest_digest)
        source_input = Path(source).expanduser()
        destination_input = Path(destination).expanduser()
        TransferSession._reject_symlink(source_input, "source")
        TransferSession._reject_symlink(destination_input, "destination")
        source_path = source_input.resolve()
        destination_path = destination_input.resolve()
        TransferSession._validate_paths(source_path, destination_path)
        if not source_path.is_file():
            raise ValueError("source must be a regular file")
        size = source_path.stat().st_size
        if size > MAX_ARTIFACT_BYTES:
            raise ValueError("artifact exceeds size limit")
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        nonce_prefix = secrets.token_bytes(8)
        hashes: list[str] = []
        artifact = hashlib.sha256()
        with source_path.open("rb") as source_stream, destination_path.open("wb") as target:
            target.write(_ENCRYPTED_MAGIC)
            target.write(struct.pack(">I", self.chunk_bytes))
            target.write(nonce_prefix)
            index = 0
            while chunk := source_stream.read(self.chunk_bytes):
                if index >= MAX_CHUNKS:
                    raise ValueError("artifact has too many chunks")
                hashes.append(hashlib.sha256(chunk).hexdigest())
                artifact.update(chunk)
                nonce = nonce_prefix + index.to_bytes(4, "big")
                aad = f"{manifest_digest}:{index}".encode()
                encrypted = self._cipher.encrypt(nonce, chunk, aad)
                target.write(struct.pack(">I", len(encrypted)))
                target.write(encrypted)
                index += 1
            target.flush()
            os.fsync(target.fileno())
        return EncryptedTransferReceipt(
            str(source_path), str(destination_path), size, self.chunk_bytes, index,
            tuple(hashes), artifact.hexdigest(), manifest_digest, nonce_prefix,
        )

    def receive(
        self,
        receipt: EncryptedTransferReceipt,
        destination: Path | str,
        *,
        manifest_digest: str,
    ) -> None:
        self._validate_digest(manifest_digest)
        if not receipt.complete or receipt.manifest_digest != manifest_digest:
            raise ValueError("encrypted transfer manifest mismatch")
        if receipt.chunk_count != len(receipt.chunk_hashes) or receipt.chunk_count > MAX_CHUNKS:
            raise ValueError("encrypted transfer receipt is invalid")
        encrypted_path = Path(receipt.destination).expanduser()
        destination_input = Path(destination).expanduser()
        TransferSession._reject_symlink(encrypted_path, "encrypted source")
        TransferSession._reject_symlink(destination_input, "destination")
        encrypted_path = encrypted_path.resolve()
        destination_path = destination_input.resolve()
        if not encrypted_path.is_file() or encrypted_path.stat().st_size > MAX_ARTIFACT_BYTES * 2:
            raise ValueError("encrypted transfer source is unavailable")
        TransferSession._validate_paths(encrypted_path, destination_path)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        artifact = hashlib.sha256()
        count = 0
        try:
            with encrypted_path.open("rb") as source, destination_path.open("wb") as target:
                if source.read(len(_ENCRYPTED_MAGIC)) != _ENCRYPTED_MAGIC:
                    raise ValueError("encrypted transfer header is invalid")
                chunk_bytes = struct.unpack(">I", source.read(4))[0]
                if chunk_bytes != receipt.chunk_bytes:
                    raise ValueError("encrypted transfer chunk size mismatch")
                if source.read(8) != receipt.nonce_prefix:
                    raise ValueError("encrypted transfer nonce mismatch")
                while count < receipt.chunk_count:
                    raw_length = source.read(4)
                    if len(raw_length) != 4:
                        raise ValueError("encrypted transfer is truncated")
                    length = struct.unpack(">I", raw_length)[0]
                    if length < 16 or length > chunk_bytes + 16:
                        raise ValueError("encrypted transfer chunk length is invalid")
                    encrypted = source.read(length)
                    nonce = receipt.nonce_prefix + count.to_bytes(4, "big")
                    chunk = self._cipher.decrypt(
                        nonce, encrypted, f"{manifest_digest}:{count}".encode()
                    )
                    if hashlib.sha256(chunk).hexdigest() != receipt.chunk_hashes[count]:
                        raise ValueError("encrypted transfer chunk hash mismatch")
                    artifact.update(chunk)
                    target.write(chunk)
                    count += 1
                if source.read(1):
                    raise ValueError("encrypted transfer has trailing data")
                target.flush()
                os.fsync(target.fileno())
        except Exception as error:
            with suppress(OSError):
                destination_path.unlink()
            if isinstance(error, ValueError) and "manifest" in str(error):
                raise
            raise ValueError("encrypted transfer decrypt failed") from error
        if count != receipt.chunk_count or destination_path.stat().st_size != receipt.byte_size:
            raise ValueError("encrypted transfer byte size mismatch")
        if artifact.hexdigest() != receipt.artifact_hash:
            raise ValueError("encrypted transfer artifact hash mismatch")

    @staticmethod
    def _validate_digest(value: str) -> None:
        if not isinstance(value, str) or len(value) != 64 or any(
            character not in "0123456789abcdef" for character in value
        ):
            raise ValueError("manifest digest is invalid")
