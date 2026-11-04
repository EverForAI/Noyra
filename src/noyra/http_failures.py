"""Source-safe diagnostics and fixed outcomes for unexpected HTTP failures."""

from __future__ import annotations

import logging
import re
import secrets
import sqlite3
from collections import deque
from http import HTTPStatus

from noyra.core.admission import OperationInvalidated
from noyra.core.at_rest import AtRestError
from noyra.core.errors import IntegrityError, RuntimeOwnershipError
from noyra.http_access_logging import http_log_fields

LOGGER = logging.getLogger("noyra.service")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,191}\Z")


def request_id() -> str:
    return secrets.token_hex(16)


def failure_status(error: BaseException) -> HTTPStatus:
    if isinstance(
        error,
        (
            sqlite3.Error,
            IntegrityError,
            AtRestError,
            OperationInvalidated,
            RuntimeOwnershipError,
            OSError,
        ),
    ):
        return HTTPStatus.SERVICE_UNAVAILABLE
    return HTTPStatus.INTERNAL_SERVER_ERROR


def _identifier(value: object) -> str:
    return value if isinstance(value, str) and _IDENTIFIER.fullmatch(value) else "unknown"


def log_http_failure(
    error: BaseException | None,
    correlation: str,
    *,
    method: object = None,
    target: object = None,
    started: bool = False,
    event: str = "handler_failure",
) -> None:
    """Never pass exceptions, source lines, paths or request text to a handler.

    Unlike exc_info-based logging this is safe even before main installs log
    redaction, and custom logging handlers cannot retrieve raw exception data.
    A failing logger is not allowed to escape back to stdlib's raw stderr hook.
    """
    try:
        safe_method, route, _ = http_log_fields(method, target, "-")
        frames: deque[str] = deque(maxlen=12)
        trace = error.__traceback__ if error is not None else None
        for _ in range(64):
            if trace is None:
                break
            frame = trace.tb_frame
            module = _identifier(frame.f_globals.get("__name__"))
            function = _identifier(frame.f_code.co_name)
            frames.append(f"{module}.{function}:{trace.tb_lineno}")
            trace = trace.tb_next
        safe_id = correlation if re.fullmatch(r"[0-9a-f]{32}", correlation) else "unavailable"
        LOGGER.error(
            "http event=%s request_id=%s method=%s route=%s status=%s "
            "phase=%s error_type=%s frames=%s",
            event
            if event in {"handler_failure", "server_failure", "error_response_failure"}
            else "handler_failure",
            safe_id,
            safe_method,
            route,
            "aborted" if started else int(failure_status(error)) if error is not None else 500,
            "after_headers" if started else "before_headers",
            _identifier(type(error).__name__) if error is not None else "unknown",
            " -> ".join(frames) or "unavailable",
        )
    except Exception:
        # No print/exception logging here: those would expose the very payload
        # that this terminal diagnostic boundary deliberately omitted.
        return
