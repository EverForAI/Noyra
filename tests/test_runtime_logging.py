from __future__ import annotations

import io
import logging
from importlib import import_module
from typing import Any

import httpx
import pytest

from noyra.core.runtime_logging import install_cloud_history_safety, install_log_safety


@pytest.mark.parametrize("level", [logging.INFO, logging.DEBUG])
def test_actual_httpx_logs_keep_status_without_url_secrets(
    level: int, caplog: pytest.LogCaptureFixture
) -> None:
    install_log_safety()
    caplog.set_level(level)
    urls = [
        "https://api.weixin.qq.com/cgi-bin/token?secret=wechat-app-private",
        "https://api.weixin.qq.com/cgi-bin/message/custom/send?access_token=wechat-access-private",
        "https://api.telegram.org/bottelegram-private/sendMessage",
        "https://www.bing.com/search?q=private-research-query",
        "https://hooks.example/opaque-private-path?opaque=private-query",
        "https://username:password-private@hooks.example/secret-private",
    ]
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("httpx")
    logger.addHandler(handler)
    try:
        with httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(503))
        ) as client:
            for url in urls:
                client.get(url)
    finally:
        logger.removeHandler(handler)
    for output in (caplog.text, stream.getvalue(), repr([r.__dict__ for r in caplog.records])):
        assert "private" not in output
        assert "HTTP/1.1 503" in output
        assert "api.telegram.org" in output
        assert "www.bing.com" in output


def test_debug_headers_and_exception_payloads_never_reach_handlers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    install_log_safety()
    caplog.set_level(logging.DEBUG)
    logger = logging.getLogger("httpcore.http11")
    logger.debug("receive_response_headers.complete return_value=%r", [(b"cookie", b"raw-private")])
    logger.debug("new_unknown_event payload=%r", {"opaque": "unknown-private"})
    try:
        raise httpx.ReadError("raw-private-provider-error")
    except httpx.ReadError:
        logger.exception("receive_response_headers.failed exception=%r", "raw-private-exception")
    assert "receive_response_headers.complete" in caplog.text
    assert "error_type=ReadError" in caplog.text
    assert "private" not in repr([r.__dict__ for r in caplog.records])


def test_application_exception_urls_are_sanitized_without_losing_frames(
    caplog: pytest.LogCaptureFixture,
) -> None:
    install_log_safety()
    caplog.set_level(logging.INFO)
    try:
        raise ValueError("unstructured-private-provider-payload")
    except ValueError:
        logging.getLogger("noyra.service").exception(
            "request failed https://example.com/path-private?q=query-private app_secret=%s",
            "credential-private",
        )
    assert "error_type=ValueError" in caplog.text
    assert "example.com" in caplog.text
    assert "private" not in repr([r.__dict__ for r in caplog.records])


def test_log_safety_install_is_idempotent_and_unrelated_logs_unchanged() -> None:
    install_log_safety()
    before = logging.getLogRecordFactory()
    install_log_safety()
    assert logging.getLogRecordFactory() is before
    record = before("unrelated", logging.INFO, __file__, 1, "value=%s", ("original",), None)
    assert record.getMessage() == "value=original"


@pytest.mark.parametrize("name", ["botocore.auth", "boto3", "urllib3.connectionpool", "s3transfer"])
def test_cloud_raw_payloads_and_exceptions_do_not_reach_handlers(
    name: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    install_log_safety()
    caplog.set_level(logging.DEBUG)
    try:
        raise RuntimeError("synthetic-private-secret")
    except RuntimeError:
        logging.getLogger(name).exception("Signature:\n%s", "synthetic-private-signature")
    assert "cloud_sdk_event_details_withheld" in caplog.text
    assert "error_type=RuntimeError" in caplog.text
    assert "synthetic-private" not in repr([r.__dict__ for r in caplog.records])


@pytest.mark.parametrize("status", [200, 403])
def test_actual_s3_signing_endpoint_logs_and_history_are_safe(
    status: int,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("boto3")
    from noyra.core.archive import S3ArchiveProvider

    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "synthetic-private-access")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "synthetic-private-secret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "synthetic-private-session")
    monkeypatch.setenv("NOYRA_ARCHIVE_S3_BUCKET", "synthetic-private-bucket")
    monkeypatch.setenv("NOYRA_ARCHIVE_S3_REGION", "us-east-1")
    monkeypatch.setenv("NOYRA_ARCHIVE_S3_ACCOUNT_ID", "synthetic")
    monkeypatch.setenv("NOYRA_ARCHIVE_S3_ENDPOINT", "https://s3.example")
    # A fresh recorder may be enabled and handlers attached after installation.
    install_cloud_history_safety()
    history = import_module("botocore.history")
    recorder = history.get_global_history_recorder()
    captures: list[Any] = []

    class HistoryHandler:
        def emit(self, event_type: str, payload: Any, source: str) -> None:
            captures.append((event_type, payload, source))

    monkeypatch.setattr(recorder, "_handlers", [HistoryHandler()])
    monkeypatch.setattr(recorder, "_enabled", True)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logging.getLogger("botocore").addHandler(handler)
    sends: list[int] = []
    provider = S3ArchiveProvider.from_env()
    response_type = import_module("botocore.awsrequest").AWSResponse

    class Raw:
        def stream(self, *args: Any, **kwargs: Any) -> Any:
            yield (
                b""
                if status == 200
                else b"<Error><Code>AccessDenied</Code>"
                b"<Message>synthetic-private-error</Message></Error>"
            )

    def respond(request: Any, **kwargs: Any) -> Any:
        sends.append(1)
        assert "Authorization" in request.headers
        return response_type(request.url, status, {}, Raw())

    provider.client.meta.events.register("before-send.s3.PutObject", respond)
    try:
        if status == 200:
            provider.client.put_object(
                Bucket=provider.bucket,
                Key="synthetic-private-object",
                Body=b"synthetic-private-body",
            )
        else:
            with pytest.raises(import_module("botocore.exceptions").ClientError):
                provider.client.put_object(
                    Bucket=provider.bucket,
                    Key="synthetic-private-object",
                    Body=b"synthetic-private-body",
                )
    finally:
        provider.client.close()
        logging.getLogger("botocore").removeHandler(handler)
    assert sends == [1]
    assert captures
    assert any(payload.get("status_code") == status for _, payload, _ in captures)
    for output in (
        caplog.text,
        stream.getvalue(),
        repr(captures),
        repr([r.__dict__ for r in caplog.records]),
    ):
        assert "synthetic-private" not in output
        assert "Signature=" not in output
