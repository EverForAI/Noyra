from __future__ import annotations

import io
import logging

import httpx
import pytest

from noyra.core.runtime_logging import install_log_safety


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
