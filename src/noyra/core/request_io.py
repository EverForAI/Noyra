"""Request reads limited by an absolute header/body budget, including drip feeds."""

from __future__ import annotations

import io
import socket
from typing import Any

from .deadline import monotonic_time, remaining_seconds


class RequestHeadLimitError(OSError):
    """A head-byte rejection with framing phase, never attacker-supplied text."""

    def __init__(self, *, request_line: bool) -> None:
        super().__init__("request header byte limit exceeded")
        self.request_line = request_line


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
        self._head_limit = max_header_bytes
        self._request_line_bytes = 0
        self._request_line_complete = False

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        self._connection.settimeout(remaining_seconds(self._timeout))
        # read1 performs at most one raw read. BufferedReader.read/readline
        # repeatedly return here and consume the SAME remaining budget.
        size = len(buffer)
        if self._remaining_bytes is not None:
            size = min(size, max(1, self._remaining_bytes + 1))
        if self._header_remaining is not None:
            # Read at most one byte past the head budget, even if the outer
            # BufferedReader asks for a large buffer. Do not drain an attack.
            size = min(size, max(1, self._header_remaining + 1))
        data = self._reader.read1(size)
        if self._header_remaining is not None:
            # read-ahead may cross the request line before parse_request runs.
            # Classify from wire bytes, not the handler's current call frame.
            if not self._request_line_complete:
                newline = data.find(b"\n")
                self._request_line_bytes += len(data) if newline < 0 else newline + 1
                self._request_line_complete = newline >= 0
            scan = self._header_tail + data
            boundaries = [
                index + len(end)
                for end in (b"\r\n\r\n", b"\n\r\n", b"\n\n")
                if (index := scan.find(end)) >= 0
            ]
            header_size = min(boundaries) - len(self._header_tail) if boundaries else len(data)
            self._header_remaining -= header_size
            if self._header_remaining < 0:
                raise RequestHeadLimitError(
                    request_line=(
                        not self._request_line_complete
                        or (
                            self._head_limit is not None
                            and self._request_line_bytes > self._head_limit
                        )
                    )
                )
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

    def discard_pending(self, *, max_bytes: int, timeout_seconds: float) -> None:
        """Discard a bounded socket tail after rejecting an oversized head.

        Unread request bytes can cause TCP to reset and discard a response
        already sent to the client. The discarded bytes never reach the parser.
        """
        deadline = monotonic_time() + timeout_seconds
        while max_bytes > 0:
            remaining_time = deadline - monotonic_time()
            if remaining_time <= 0:
                return
            try:
                self._connection.settimeout(remaining_time)
                data = self._reader.read1(min(io.DEFAULT_BUFFER_SIZE, max_bytes))
            except OSError:
                return
            if not data:
                return
            max_bytes -= len(data)

    def close(self) -> None:
        try:
            self._reader.close()
        finally:
            super().close()
