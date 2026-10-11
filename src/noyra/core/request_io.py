"""Request reads limited by an absolute header/body budget, including drip feeds."""

from __future__ import annotations

import io
import socket
from typing import Any

from .deadline import remaining_seconds


class DeadlineRequestReader(io.RawIOBase):
    def __init__(self, reader: Any, connection: socket.socket, timeout: float):
        self._reader = reader
        self._connection = connection
        self._timeout = timeout

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        self._connection.settimeout(remaining_seconds(self._timeout))
        # read1 performs at most one raw read. BufferedReader.read/readline
        # repeatedly return here and consume the SAME remaining budget.
        data = self._reader.read1(len(buffer))
        remaining_seconds(self._timeout)
        buffer[: len(data)] = data
        return len(data)

    def close(self) -> None:
        try:
            self._reader.close()
        finally:
            super().close()
