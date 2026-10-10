from __future__ import annotations

import io
from importlib import import_module

import pytest

from noyra.core.archive import S3ArchiveProvider


def test_locked_cloud_sdk_reads_and_checks_archive_without_network() -> None:
    boto3 = pytest.importorskip("boto3")
    config = import_module("botocore.config").Config
    stubber_type = import_module("botocore.stub").Stubber
    body_type = import_module("botocore.response").StreamingBody
    client = boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="synthetic",
        aws_secret_access_key="synthetic",
        endpoint_url="https://archive.example.invalid",
        config=config(
            connect_timeout=5, read_timeout=20, max_pool_connections=2, retries={"max_attempts": 0}
        ),
    )
    payload = b"synthetic archive"
    with stubber_type(client) as stubber:
        stubber.add_response(
            "get_object",
            {"Body": body_type(io.BytesIO(payload), len(payload))},
            {"Bucket": "test", "Key": "state"},
        )
        stubber.add_response("head_object", {}, {"Bucket": "test", "Key": "state"})
        provider = S3ArchiveProvider(client, bucket="test")
        assert provider.get("state", max_bytes=100) == payload
        assert provider.exists("state")
        stubber.assert_no_pending_responses()
    assert client.meta.config.connect_timeout == 5
    assert client.meta.config.read_timeout == 20
    assert client.meta.config.max_pool_connections == 2
    assert client.meta.config.retries["total_max_attempts"] == 1
    client.close()
