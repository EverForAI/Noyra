"""Operation-local receipt proofs keep archived metadata I/O out of writer scopes."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from .errors import IntegrityError
from .events import EventStore
from .types import canonical_json, content_hash, strict_json_loads

MAX_RECEIPTS = 128
MAX_RECEIPT_BYTES = 2 * 1024 * 1024


class PublicationEvents(EventStore):
    def __init__(self, database: Any) -> None:
        super().__init__(database)
        self._proofs: dict[tuple[str, str, str, str], str] = {}
        self._proof_bytes = 0
        self._writer_scope = False

    @contextmanager
    def no_archive_reads(self) -> Iterator[None]:
        previous = self._writer_scope
        self._writer_scope = True
        try:
            yield
        finally:
            self._writer_scope = previous

    def payload_from_row(
        self, row: Any, *, connection: Any | None = None, max_archive_bytes: int | None = None
    ) -> dict[str, Any]:
        if row["payload_archive_key"] is None:
            return super().payload_from_row(
                row, connection=connection, max_archive_bytes=max_archive_bytes
            )
        key = (
            str(row["subject_id"]),
            str(row["event_id"]),
            str(row["payload_hash"]),
            str(row["payload_archive_key"]),
        )
        if self._writer_scope:
            encoded = self._proofs.get(key)
            if encoded is None:
                raise IntegrityError("publication receipt changed or was not primed before writer")
            payload = strict_json_loads(encoded)
            if content_hash(payload) != row["payload_hash"]:
                raise IntegrityError("publication receipt proof differs")
            return dict(payload)
        payload = super().payload_from_row(
            row, connection=connection, max_archive_bytes=max_archive_bytes
        )
        if content_hash(payload) != row["payload_hash"]:
            raise IntegrityError("publication receipt digest differs")
        encoded = canonical_json(payload)
        if key not in self._proofs:
            size = len(encoded.encode())
            if len(self._proofs) >= MAX_RECEIPTS or self._proof_bytes + size > MAX_RECEIPT_BYTES:
                raise IntegrityError("publication receipt proof budget exceeded")
            self._proof_bytes += size
            self._proofs[key] = encoded
        return payload
