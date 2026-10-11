"""Keep network payloads and secret-bearing URLs out of every log handler."""

from __future__ import annotations

import logging
import re
import threading
import traceback
from typing import Any
from urllib.parse import urlsplit

from .redaction import redact_secret_text

_INSTALL_LOCK = threading.Lock()
_URL = re.compile(r"(?i)\b(?:https?|smtps?)://[^\s\"'<>]+")
_NETWORK_EVENT = re.compile(r"[a-z_]+(?:\.[a-z_]+)*\.(?:started|complete|failed)\Z")
_CLOUD_LOGGERS = ("botocore", "boto3", "urllib3", "s3transfer")


def _cloud_logger(name: str) -> bool:
    return any(name == prefix or name.startswith(prefix + ".") for prefix in _CLOUD_LOGGERS)


def _origin(value: object) -> str:
    try:
        parsed = urlsplit(str(value))
        host = parsed.hostname
        if parsed.scheme not in {"http", "https", "smtp", "smtps"} or not host:
            return "[REDACTED_URL]"
        # Only the network origin is diagnostic. Paths can contain bot tokens,
        # arbitrary webhook secrets and private identifiers just like queries.
        if not re.fullmatch(r"[A-Za-z0-9.:[\]_-]+", host):
            return "[REDACTED_URL]"
        if ":" in host:
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port is not None else ""
        return f"{parsed.scheme}://{host}{port}/[REDACTED]"
    except (TypeError, ValueError):
        return "[REDACTED_URL]"


def _safe_message(record: logging.LogRecord) -> str:
    if _cloud_logger(record.name):
        # SDK messages have evolving positional/dict formats, and even their
        # apparent event strings can contain canonical requests or signatures.
        # Keep the logger category, level and exception type, never raw payload.
        return "cloud_sdk_event_details_withheld"
    network = record.name == "httpx" or record.name.startswith(("httpx.", "httpcore"))
    if network:
        if (
            record.name == "httpx"
            and record.msg == 'HTTP Request: %s %s "%s %d %s"'
            and isinstance(record.args, tuple)
            and len(record.args) == 5
        ):
            method, url, version, status, _reason = record.args
            if (
                method in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
                and version in {"HTTP/1.0", "HTTP/1.1", "HTTP/2", "HTTP/3"}
                and type(status) is int
                and 100 <= status <= 599
            ):
                return f'HTTP Request: {method} {_origin(url)} "{version} {status}"'
        # httpcore DEBUG includes raw headers, cookies, request objects and
        # exception reprs. An allowlisted event name is the complete contract;
        # don't try to redact an evolving third-party payload schema.
        event = str(record.msg).partition(" ")[0]
        return event if _NETWORK_EVENT.fullmatch(event) else "network_event_details_withheld"
    return redact_secret_text(_URL.sub(lambda match: _origin(match.group()), record.getMessage()))


def install_log_safety() -> None:
    """Sanitize at record creation, before existing or future handlers see data.

    Root logger filters do not filter records propagated by child loggers.
    Chaining the record factory protects caplog, journald, file and custom
    handlers equally. Only Noyra and its network/cloud libraries are changed.
    """
    with _INSTALL_LOCK:
        previous = logging.getLogRecordFactory()
        if getattr(previous, "_noyra_log_safety", False):
            return

        def safe_record(*args: Any, **kwargs: Any) -> logging.LogRecord:
            record = previous(*args, **kwargs)
            if not (
                record.name == "noyra"
                or record.name.startswith(("noyra.", "httpx", "httpcore"))
                or _cloud_logger(record.name)
            ):
                return record
            try:
                message = _safe_message(record)
            except Exception:
                message = "log_details_withheld"
            if record.exc_info:
                error_type, _error, trace = record.exc_info
                # Keep actionable frames, not exception text, chained provider
                # payloads, local values or source lines containing credentials.
                frames = traceback.extract_tb(trace) if trace is not None else []
                locations = " -> ".join(f"{frame.name}:{frame.lineno}" for frame in frames)
                kind = error_type.__name__ if error_type is not None else "Exception"
                message += f" error_type={kind} frames={locations}"
            record.msg = message
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
            return record

        safe_record._noyra_log_safety = True  # type: ignore[attr-defined]
        logging.setLogRecordFactory(safe_record)


def install_cloud_history_safety() -> None:
    """Protect botocore's independent, opt-in history handlers as well as logs.

    Install before constructing the production client, including credential
    discovery. Existing and later handlers receive only a fixed safe schema;
    enabling DEBUG or history subsequently cannot restore raw request capture.
    The optional SDK is imported only for the cloud profile.
    """
    from importlib import import_module

    install_log_safety()
    history = import_module("botocore.history")
    with _INSTALL_LOCK:
        recorder_type = history.HistoryRecorder
        previous = recorder_type.record
        if getattr(previous, "_noyra_history_safety", False):
            return

        def safe_record(
            recorder: Any, event_type: Any, payload: Any, source: Any = "BOTOCORE"
        ) -> None:
            safe: dict[str, Any] = {}
            event = (
                event_type
                if event_type in {"API_CALL", "HTTP_REQUEST", "HTTP_RESPONSE", "PARSED_RESPONSE"}
                else "SDK_EVENT"
            )
            if isinstance(payload, dict):
                method = payload.get("method")
                if isinstance(method, str) and method in {"GET", "HEAD", "PUT", "POST", "DELETE"}:
                    safe["method"] = method
                status = payload.get("status_code")
                if type(status) is int and 100 <= status <= 599:
                    safe["status_code"] = status
            # Object/bucket hostnames can themselves identify private data.
            # History needs no endpoint, headers, body or parameters.
            previous(recorder, event, safe, "BOTOCORE")

        safe_record._noyra_history_safety = True  # type: ignore[attr-defined]
        recorder_type.record = safe_record
