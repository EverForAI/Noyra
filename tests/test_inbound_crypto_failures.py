from __future__ import annotations

import builtins
import json
import logging
import re
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from cryptography.exceptions import UnsupportedAlgorithm
from pydantic import SecretStr

from noyra.core.types import content_hash
from noyra.interaction import InboundAuthenticationError, TransportInput, inbound
from noyra.service import NoyraService, ServiceSettings
from test_inbound_replay_boundaries import TOKEN, Wire, wire

PRIVATE = "M18-private-key-message-and-backend-details"


@contextmanager
def fail_import(module: str) -> Iterator[None]:
    original = builtins.__import__

    def unavailable(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == module:
            raise ModuleNotFoundError(PRIVATE)
        return original(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=unavailable):
        yield


@pytest.mark.parametrize("operation", ["key", "verify", "challenge"])
def test_missing_ed25519_backend_is_not_reported_as_bad_credentials(operation: str) -> None:
    callback = wire("qq")
    if operation == "challenge":
        # HMAC authenticates first; the legacy address response still needs
        # Ed25519. Neither path can sign when that backend is unavailable.
        callback = Wire(
            "qq",
            b'{"op":13,"d":{"plain_token":"synthetic","event_ts":"1"}}',
            {},
            {"secret": TOKEN, "signing_key": TOKEN},
        )
        import hashlib
        import hmac

        timestamp = str(int(time.time()))
        callback.headers.update(
            {
                "X-Noyra-Timestamp": timestamp,
                "X-Noyra-Nonce": "n",
                "X-Noyra-Signature": hmac.new(
                    TOKEN.encode(), f"{timestamp}.n.".encode() + callback.body, hashlib.sha256
                ).hexdigest(),
            }
        )
    with (
        fail_import("cryptography.hazmat.primitives.asymmetric.ed25519"),
        pytest.raises(RuntimeError, match="crypto backend unavailable") as failure,
    ):
        if operation == "key":
            inbound._qq_private_key(TOKEN)
        else:
            callback.parse("synthetic-transport")
    assert type(failure.value).__name__ == "CryptoBackendUnavailableError"
    assert PRIVATE not in str(failure.value)


@pytest.mark.parametrize("kind", ["qq", "feishu-encrypted", "wechat"])
@pytest.mark.parametrize("failure", ["missing", "unsupported"])
def test_crypto_backend_failures_stay_distinct_from_invalid_ciphertext(
    kind: str, failure: str
) -> None:
    callback = wire(kind)
    module = (
        "cryptography.hazmat.primitives.asymmetric.ed25519"
        if kind == "qq"
        else "cryptography.hazmat.primitives.ciphers"
    )
    target = (
        module + ".Ed25519PrivateKey.from_private_bytes" if kind == "qq" else module + ".Cipher"
    )
    injection = (
        fail_import(module)
        if failure == "missing"
        else patch(target, side_effect=UnsupportedAlgorithm(PRIVATE))
    )
    with injection, pytest.raises(RuntimeError, match="crypto backend unavailable") as caught:
        callback.parse("synthetic-transport")
    assert type(caught.value).__name__ == "CryptoBackendUnavailableError"
    assert PRIVATE not in str(caught.value)


@pytest.mark.parametrize("timestamp", ["stale", "invalid", "overlong", "non-ascii"])
def test_qq_timestamp_failure_is_not_reclassified_by_its_own_valueerror_handler(
    timestamp: str,
) -> None:
    callback = wire("qq")
    value = {
        "stale": str(int(time.time()) - 301),
        "invalid": "not-a-time",
        "overlong": "0" * 40,
        "non-ascii": str(int(time.time())).translate(
            str.maketrans("0123456789", "".join(chr(0xFF10 + index) for index in range(10)))
        ),
    }[timestamp]
    callback.headers["X-Signature-Timestamp"] = value
    with pytest.raises(
        InboundAuthenticationError,
        match="qq signature is stale" if timestamp == "stale" else "timestamp is invalid",
    ):
        callback.parse("synthetic-transport")


@pytest.mark.parametrize("signature", ["bad-hex", "short", "wrong"])
def test_qq_bad_signatures_remain_authentication_failures(signature: str) -> None:
    callback = wire("qq")
    callback.headers["X-Signature-Ed25519"] = {
        "bad-hex": "not-hex",
        "short": "aa",
        "wrong": "00" * 64,
    }[signature]
    with pytest.raises(InboundAuthenticationError, match="qq signature is invalid"):
        callback.parse("synthetic-transport")


def broken_key() -> Any:
    def broken(*args: Any) -> None:
        raise RuntimeError(PRIVATE)

    return SimpleNamespace(public_key=lambda: SimpleNamespace(verify=broken))


def test_unexpected_crypto_failure_is_not_silenced_as_an_attacker_signature() -> None:
    callback = wire("qq")
    with (
        patch.object(inbound, "_qq_private_key", return_value=broken_key()),
        pytest.raises(RuntimeError, match=PRIVATE),
    ):
        callback.parse("synthetic-transport")


def test_unsupported_verify_operation_is_a_local_backend_failure() -> None:
    def unsupported(*args: Any) -> None:
        raise UnsupportedAlgorithm(PRIVATE)

    callback = wire("qq")
    key = SimpleNamespace(public_key=lambda: SimpleNamespace(verify=unsupported))
    with (
        patch.object(inbound, "_qq_private_key", return_value=key),
        pytest.raises(RuntimeError, match="crypto backend unavailable"),
    ):
        callback.parse("synthetic-transport")


@pytest.mark.parametrize("unsupported", [False, True])
def test_qq_authenticated_challenge_signing_failure_cannot_return_a_signature(
    unsupported: bool,
) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    timestamp = str(int(time.time()))
    body = json.dumps({"op": 13, "d": {"plain_token": "synthetic", "event_ts": timestamp}}).encode()
    source = TOKEN.encode()
    while len(source) < 32:
        source += source
    real_key = Ed25519PrivateKey.from_private_bytes(source[:32])
    callback = Wire(
        "qq",
        body,
        {
            "X-Signature-Timestamp": timestamp,
            "X-Signature-Ed25519": real_key.sign(timestamp.encode() + body).hex(),
        },
        {"secret": TOKEN},
    )

    def cannot_sign(*args: Any) -> bytes:
        if unsupported:
            raise UnsupportedAlgorithm(PRIVATE)
        raise RuntimeError(PRIVATE)

    fake = SimpleNamespace(public_key=real_key.public_key, sign=cannot_sign)
    with (
        patch.object(inbound, "_qq_private_key", return_value=fake),
        pytest.raises(RuntimeError, match="crypto backend unavailable" if unsupported else PRIVATE),
    ):
        callback.parse("synthetic-transport")


@pytest.fixture
def site(tmp_path: Path) -> Iterator[tuple[NoyraService, str]]:
    service = NoyraService(
        ServiceSettings(
            data_dir=tmp_path / "data",
            subject_id="M18-Synthetic",
            genesis_hash=content_hash("M18"),
            host="127.0.0.1",
            port=0,
            integrity_mode="off",
        )
    )
    service.boot()
    transport = service.http.transports.configure(
        service.kernel.subject_id,
        TransportInput(
            channel="qq",
            label="Synthetic crypto errors",
            endpoint="https://api.sgroup.qq.com",
            credentials={"app_id": SecretStr("synthetic-app"), "app_secret": SecretStr(TOKEN)},
        ),
        actor="operator",
    )
    service.http.inbound.bind(
        service.kernel.subject_id,
        transport.transport_id,
        external_account_id="synthetic-app",
        external_sender_id="synthetic-sender",
        role="participant",
    )
    service.http.start()
    try:
        yield service, transport.transport_id
    finally:
        service.http.close()
        service.kernel.close()


@pytest.mark.parametrize("prefix", ["/webhooks/", "/api/v1/webhooks/"])
@pytest.mark.parametrize("failure", ["missing", "unsupported", "unexpected", "wrong-signature"])
def test_http_failure_classification_is_safe_before_global_redaction_and_recovers(
    site: tuple[NoyraService, str],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    prefix: str,
    failure: str,
) -> None:
    service, transport_id = site
    callback = wire("qq")
    headers = dict(callback.headers)
    if failure == "wrong-signature":
        headers["X-Signature-Ed25519"] = "00" * 64
    _, port = service.http.address
    url = f"http://127.0.0.1:{port}{prefix}qq/{transport_id}?secret={PRIVATE}"
    headers["X-Private-Test"] = PRIVATE
    # Keep private exception messages observable to an unsafe handler, so a
    # passing test proves the HTTP diagnostic does not pass them to handlers.
    monkeypatch.setattr(logging, "_logRecordFactory", logging.LogRecord)
    caplog.set_level(logging.ERROR, logger="noyra.service")
    injection: AbstractContextManager[Any]
    if failure == "missing":
        injection = fail_import("cryptography.hazmat.primitives.asymmetric.ed25519")
    elif failure == "unsupported":
        injection = patch(
            "cryptography.hazmat.primitives.asymmetric.ed25519.Ed25519PrivateKey.from_private_bytes",
            side_effect=UnsupportedAlgorithm(PRIVATE),
        )
    elif failure == "unexpected":
        injection = patch.object(inbound, "_qq_private_key", return_value=broken_key())
    else:
        injection = nullcontext()
    status = (
        503 if failure in {"missing", "unsupported"} else 500 if failure == "unexpected" else 401
    )
    with injection:
        with pytest.raises(HTTPError) as denied:
            urlopen(Request(url, data=callback.body, headers=headers, method="POST"), timeout=5)
        with denied.value as response:
            assert response.code == status
            body = response.read()
            payload = json.loads(body)
            assert PRIVATE.encode() not in body
            if status != 401:
                assert payload["error"] == (
                    "service_unavailable" if status == 503 else "internal_error"
                )
                assert re.fullmatch(r"[0-9a-f]{32}", payload["request_id"])
                assert response.headers["X-Request-ID"] == payload["request_id"]
    with service.kernel.database.read_transaction() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM interaction_inbound_events").fetchone()[0] == 0
        )
    if status != 401:
        failures = [
            record
            for record in caplog.records
            if "http event=handler_failure" in record.getMessage()
        ]
        assert len(failures) == 1
        assert payload["request_id"] in failures[0].getMessage()
        assert (
            "error_type=CryptoBackendUnavailableError" in caplog.text
            if status == 503
            else "error_type=RuntimeError" in caplog.text
        )
    assert PRIVATE not in repr([record.__dict__ for record in caplog.records])
    assert all(record.exc_info is None and record.exc_text is None for record in caplog.records)
    # Removing the local failure is sufficient; no credential replacement or
    # operator write is needed, and the original business event can be accepted.
    with urlopen(
        Request(url, data=callback.body, headers=callback.headers, method="POST"), timeout=5
    ) as response:
        assert json.loads(response.read()) == {"op": 12, "d": 0}
    with service.kernel.database.read_transaction() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM interaction_inbound_events").fetchone()[0] == 1
        )


@pytest.mark.parametrize("prefix", ["/webhooks/", "/api/v1/webhooks/"])
@pytest.mark.parametrize("logger_fails", [False, True])
def test_post_ingest_audit_failure_preserves_ack_without_raw_exception_logging(
    site: tuple[NoyraService, str],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    prefix: str,
    logger_fails: bool,
) -> None:
    service, transport_id = site
    callback = wire("qq")
    _, port = service.http.address
    url = f"http://127.0.0.1:{port}{prefix}qq/{transport_id}?secret={PRIVATE}"
    monkeypatch.setattr(logging, "_logRecordFactory", logging.LogRecord)
    caplog.set_level(logging.ERROR, logger="noyra.service")
    observed: list[dict[str, Any]] = []

    class Observer(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            observed.append(dict(record.__dict__))
            if logger_fails:
                raise RuntimeError(PRIVATE + "-logger")

    logger = logging.getLogger("noyra.service")
    observer = Observer()
    logger.addHandler(observer)
    try:
        with patch.object(service.http, "audit_admin_event", side_effect=RuntimeError(PRIVATE)):
            for _ in range(2):
                with urlopen(
                    Request(url, data=callback.body, headers=callback.headers, method="POST"),
                    timeout=5,
                ) as response:
                    assert response.status == 200
                    assert json.loads(response.read()) == {"op": 12, "d": 0}
        assert len(observed) == 2
        assert all(
            record.get("exc_info") is None and record.get("exc_text") is None for record in observed
        )
        assert PRIVATE not in repr(observed)
        if not logger_fails:
            diagnostics = [
                record.getMessage()
                for record in caplog.records
                if "inbound_acceptance_audit_failure" in record.getMessage()
            ]
            assert len(diagnostics) == 2
            assert all(
                "status=200" in line and "phase=after_ingestion" in line for line in diagnostics
            )
            assert all("error_type=RuntimeError" in line for line in diagnostics)
            ids = [
                match.group(1)
                for line in diagnostics
                if (match := re.search(r"request_id=([a-f0-9]{32})", line))
            ]
            assert len(ids) == 2 and len(set(ids)) == 2
        with service.kernel.database.read_transaction() as connection:
            assert (
                connection.execute("SELECT COUNT(*) FROM interaction_inbound_events").fetchone()[0]
                == 1
            )
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM interactions WHERE direction='incoming'"
                ).fetchone()[0]
                == 1
            )
    finally:
        logger.removeHandler(observer)
