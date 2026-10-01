"""Bounded, resumable artifact transfer with content-addressed chunks."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, replace
from pathlib import Path

MAX_ARTIFACT_BYTES = 10 * 1024 * 1024 * 1024
MAX_CHUNKS = 2_000_000


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


class TransferSession:
    def __init__(self, *, chunk_bytes: int = 1024 * 1024):
        if not 4096 <= chunk_bytes <= 64 * 1024 * 1024:
            raise ValueError("chunk size is outside safety bounds")
        self.chunk_bytes = chunk_bytes

    def send(
        self, source: Path | str, destination: Path | str, *, stop_after_chunks: int | None = None
    ) -> TransferReceipt:
        source_path = Path(source).expanduser().resolve()
        destination_input = Path(destination).expanduser()
        if ".." in destination_input.parts:
            raise ValueError("destination path cannot contain parent traversal")
        destination_path = destination_input.resolve()
        self._validate_paths(source_path, destination_path)
        if not source_path.is_file() or source_path.is_symlink():
            raise ValueError("source must be a regular file")
        size = source_path.stat().st_size
        if size > MAX_ARTIFACT_BYTES:
            raise ValueError("artifact exceeds size limit")
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
        source_path = Path(receipt.source).resolve()
        destination_path = Path(receipt.destination).resolve()
        self._validate_paths(source_path, destination_path)
        if not source_path.is_file() or receipt.next_chunk < 0:
            raise ValueError("transfer source is unavailable")
        expected_prefix = receipt.next_chunk * receipt.chunk_bytes
        if not destination_path.is_file() or destination_path.stat().st_size != expected_prefix:
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
