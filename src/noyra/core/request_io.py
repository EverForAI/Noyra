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
    ):
        self._reader = reader
        self._connection = connection
        self._timeout = timeout
        self._remaining_bytes = max_bytes

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
