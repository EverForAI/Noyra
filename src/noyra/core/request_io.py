"""Request reads limited by an absolute header/body budget, including drip feeds."""

from __future__ import annotations

import io
import socket
from typing import Any

from .deadline import remaining_seconds


class DeadlineRequestReader(io.RawIOBase):
    def __init__(
        self,
        reader: Any,
        connection: socket.socket,
        timeout: float,
        *,
        max_bytes: int | None = None,
        max_header_bytes: int | None = None,
    ):
        self._reader = reader
        self._connection = connection
        self._timeout = timeout
        self._remaining_bytes = max_bytes
        self._header_remaining = max_header_bytes
        self._header_tail = b""

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        self._connection.settimeout(remaining_seconds(self._timeout))
        # read1 performs at most one raw read. BufferedReader.read/readline
        # repeatedly return here and consume the SAME remaining budget.
        size = len(buffer)
        if self._remaining_bytes is not None:
            size = min(size, max(1, self._remaining_bytes + 1))
        data = self._reader.read1(size)
        if self._header_remaining is not None:
            scan = self._header_tail + data
            boundaries = [
                index + len(end) for end in (b"\r\n\r\n", b"\n\n") if (index := scan.find(end)) >= 0
            ]
            header_size = min(boundaries) - len(self._header_tail) if boundaries else len(data)
            self._header_remaining -= header_size
            if self._header_remaining < 0:
                raise OSError("request header byte limit exceeded")
            if boundaries:
                self._header_remaining = None
                self._header_tail = b""
            else:
                self._header_tail = scan[-3:]
        if self._remaining_bytes is not None:
            self._remaining_bytes -= len(data)
            if self._remaining_bytes < 0:
                raise OSError("request byte limit exceeded")
        remaining_seconds(self._timeout)
        buffer[: len(data)] = data
        return len(data)

    def close(self) -> None:
        try:
            self._reader.close()
        finally:
            super().close()
