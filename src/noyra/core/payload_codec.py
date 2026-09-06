from __future__ import annotations

import base64
import zlib

from .errors import IntegrityError, PayloadLimitError

PREFIX = "noyra-zlib-b64:"


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


def decompress_text(value: str | None, *, max_bytes: int | None = None) -> str | None:
    if max_bytes is not None and max_bytes < 1:
        raise ValueError("decompression limit must be positive")
    if value is None:
        return value
    if not value.startswith(PREFIX):
        if max_bytes is not None and len(value.encode("utf-8")) > max_bytes:
            raise PayloadLimitError("persisted text exceeds the configured byte limit")
        return value
    try:
        encoded = value[len(PREFIX) :]
        if max_bytes is not None:
            # A valid zlib stream cannot be materially larger than its output.
            # Reject oversized base64 before allocating its decoded form.
            max_encoded = 4 * ((max_bytes + 1_024 + 2) // 3)
            if len(encoded) > max_encoded:
                raise PayloadLimitError("compressed text exceeds the configured byte limit")
        compressed = base64.b64decode(encoded, altchars=b"-_", validate=True)
        if max_bytes is None:
            raw = zlib.decompress(compressed)
        else:
            raw = decompress_bytes(compressed, max_bytes=max_bytes)
        return raw.decode("utf-8")
    except PayloadLimitError:
        raise
    except (ValueError, UnicodeDecodeError, zlib.error) as error:
        raise IntegrityError("compressed model payload is invalid") from error
