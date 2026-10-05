from __future__ import annotations

import base64
import zlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from .errors import IntegrityError, PayloadLimitError

PREFIX = "noyra-zlib-b64:"
# Historical model I/O is bounded before JSON parsing in every consumer. A
# caller with a smaller integrity or export budget can tighten this value.
DEFAULT_TEXT_LIMIT = 64_000_000
_READ_BUDGET: ContextVar[tuple[int | Callable[[], int], Callable[[int], None]] | None] = ContextVar(
    "payload_read_budget", default=None
)


@contextmanager
def payload_read_budget(
    max_bytes: int | Callable[[], int], consume: Callable[[int], None]
) -> Iterator[None]:
    """Apply an audit's limit to nested consumers sharing this execution context."""
    token = _READ_BUDGET.set((max_bytes, consume))
    try:
        yield
    finally:
        _READ_BUDGET.reset(token)


def compress_text(value: str, *, minimum_bytes: int = 1_024) -> str:
    if value.startswith(PREFIX):
        return value
    raw = value.encode("utf-8")
    if len(raw) < minimum_bytes:
        return value
    compressed = base64.urlsafe_b64encode(zlib.compress(raw, level=6)).decode("ascii")
    encoded = PREFIX + compressed
    return encoded if len(encoded) < len(value) else value


def decompress_bytes(value: bytes, *, max_bytes: int) -> bytes:
    """Decompress one zlib stream without allowing an expansion past max_bytes."""
    if max_bytes < 1:
        raise ValueError("decompression limit must be positive")
    decompressor = zlib.decompressobj()
    raw = decompressor.decompress(value, max_bytes + 1)
    if len(raw) > max_bytes or decompressor.unconsumed_tail:
        raise PayloadLimitError("decompressed payload exceeds the configured byte limit")
    raw += decompressor.flush(max_bytes + 1 - len(raw))
    if len(raw) > max_bytes:
        raise PayloadLimitError("decompressed payload exceeds the configured byte limit")
    if not decompressor.eof or decompressor.unused_data:
        raise IntegrityError("compressed payload is invalid")
    return raw


def decompress_text(value: str | None, *, max_bytes: int = DEFAULT_TEXT_LIMIT) -> str | None:
    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError("decompression limit must be positive")
    budget = _READ_BUDGET.get()
    if budget is not None:
        available = budget[0]() if callable(budget[0]) else budget[0]
        max_bytes = min(max_bytes, available)
        if max_bytes < 1:
            raise PayloadLimitError("payload read budget exhausted")
    if value is None:
        return value
    if not value.startswith(PREFIX):
        if len(value.encode("utf-8")) > max_bytes:
            raise PayloadLimitError("persisted text exceeds the configured byte limit")
        return value
    try:
        encoded = value[len(PREFIX) :]
        # Reject oversized base64 before allocating its decoded form.
        max_encoded = 4 * ((max_bytes + 1_024 + 2) // 3)
        if len(encoded) > max_encoded:
            raise PayloadLimitError("compressed text exceeds the configured byte limit")
        compressed = base64.b64decode(encoded, altchars=b"-_", validate=True)
        raw = decompress_bytes(compressed, max_bytes=max_bytes)
        if budget is not None:
            budget[1](len(raw))
        return raw.decode("utf-8")
    except PayloadLimitError:
        raise
    except (ValueError, UnicodeDecodeError, zlib.error) as error:
        raise IntegrityError("compressed model payload is invalid") from error
