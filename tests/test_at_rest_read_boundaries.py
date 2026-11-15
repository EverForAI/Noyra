from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import pytest
from pydantic import SecretStr

from noyra.core import SubjectKernel
from noyra.core.at_rest import AtRestConfig, AtRestError, AtRestGuard, BackupKeyring
from noyra.core.types import content_hash
from noyra.interaction import TransportInput, participant_http
from noyra.interaction.accounts import AccountStore
from noyra.service import NoyraHTTPServer, ServiceSettings

ADMIN = "synthetic-m06-operator-token-with-enough-entropy"
PRIVATE_REASON = "M06-private-boundary-diagnostic-must-not-escape"


@dataclass
class Boundary:
    ready: bool = True
    required: bool = True
    checks: int = 0

    def require_request_ready(self) -> dict[str, object]:
        self.checks += 1
        if not self.ready:
            raise AtRestError(PRIVATE_REASON)
        return self.request_health()

    def require_ready(self) -> dict[str, object]:
        raise AssertionError("HTTP reads must use the bounded request check")

    def request_health(self) -> dict[str, object]:
        return {"mode": "required", "enforced": self.required, "ready": self.ready}

    def health(self) -> dict[str, object]:
        return self.request_health()


@dataclass
class Site:
    owner: NoyraHTTPServer
    base: str
    boundary: Boundary
    article: str
    partition: str

    def request(
        self,
        path: str,
        *,
        operator: bool = False,
        payload: dict[str, object] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        headers = {"Authorization": f"Bearer {ADMIN}"} if operator else {}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            self.base + path,
            headers=headers,
            data=None if payload is None else json.dumps(payload).encode(),
        )
        response: Any
        try:
            response = urlopen(request, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            return response.status, dict(response.headers.items()), response.read()


@pytest.fixture
def site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Site]:
    with ExitStack() as cleanup:
        kernel = SubjectKernel(tmp_path / "data.sqlite3", "m06-http", content_hash("m06-http"))
        cleanup.callback(kernel.close)
        kernel.boot()
        kernel.orient()
        kernel.activate()
        owner = NoyraHTTPServer(
            kernel,
            ServiceSettings(
                data_dir=tmp_path,
                subject_id=kernel.subject_id,
                genesis_hash=content_hash("m06-http"),
                host="127.0.0.1",
                port=0,
                admin_token=SecretStr(ADMIN),
                public_site_url="https://public.example",
            ),
        )
        cleanup.callback(owner.close)
        owner.public_archive.publish(
            kernel.subject_id,
            {
                "publication_id": "m06-public-item",
                "kind": "goal",
                "title": "M06 approved public title",
                "summary": "M06 approved summary",
                "body": "M06 approved public body",
                "details": {},
                "expected_revision": 0,
                "confirm_public": True,
            },
            actor="operator",
        )
        boundary = Boundary()
        monkeypatch.setattr(owner, "at_rest", boundary)
        owner.start()
        # HTTP startup binds the durable sitemap keyring; take tokens afterwards.
        partitions = owner.public_archive.sitemap_partitions(kernel.subject_id)
        assert partitions
        yield Site(
            owner,
            f"http://127.0.0.1:{owner.address[1]}",
            boundary,
            "/public/goal/m06-public-item",
            "/sitemap.xml?part=" + quote(partitions[0], safe=""),
        )


@pytest.mark.parametrize("document", ["article", "article-query", "index", "partition", "home"])
def test_public_documents_stop_before_archive_reads_and_recover(site: Site, document: str) -> None:
    paths = {
        "article": site.article,
        "article-query": site.article + "?lang=en&theme=dark&return=timeline",
        "index": "/sitemap.xml",
        "partition": site.partition,
        "home": "/sitemap.xml?part=home",
    }
    path = paths[document]
    status, _headers, normal_body = site.request(path)
    assert status == 200
    site.boundary.ready = False
    archive = site.owner.public_archive
    with (
        patch.object(archive, "page", wraps=archive.page) as page,
        patch.object(archive, "sitemap_partitions", wraps=archive.sitemap_partitions) as partitions,
        patch.object(archive, "sitemap_partition", wraps=archive.sitemap_partition) as partition,
    ):
        status, headers, body = site.request(path)
        assert status == 503
        assert json.loads(body) == {"error": "at_rest_boundary_unavailable"}
        assert headers["Cache-Control"] == "no-store"
        assert headers["X-Noyra-Public-Contract"] == "public-contract-v2"
        assert int(headers["Retry-After"]) > 0
        assert PRIVATE_REASON.encode() not in body
        page.assert_not_called()
        partitions.assert_not_called()
        partition.assert_not_called()
    site.boundary.ready = True
    status, headers, restored_body = site.request(path)
    assert status == 200 and restored_body == normal_body
    assert headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    ("route", "normal_status"),
    [
        ("policy", 200),
        ("targets", 200),
        ("candidates", 200),
        ("proposals", 200),
        ("proposals/missing", 404),
        ("tasks", 200),
        ("tasks/missing", 404),
        ("tasks/missing/self-attestation", 404),
    ],
)
def test_early_migration_reads_authenticate_then_check_storage_before_access(
    site: Site, route: str, normal_status: int
) -> None:
    for prefix in ("/api/", "/api/v1/"):
        path = prefix + "admin/migration/" + route
        assert site.request(path, operator=True)[0] == normal_status
        site.boundary.ready = False
        checks = site.boundary.checks
        status, _headers, body = site.request(path)
        assert status == 401 and json.loads(body) == {"error": "unauthorized"}
        assert site.boundary.checks == checks
        database = site.owner.kernel.database
        runtime = site.owner.migration_runtime
        with (
            patch.object(database, "connection", wraps=database.connection) as connection,
            patch.object(
                runtime, "refresh_candidates", wraps=runtime.refresh_candidates
            ) as refresh,
        ):
            status, headers, body = site.request(path, operator=True)
            assert status == 503
            assert json.loads(body) == {"error": "at_rest_boundary_unavailable"}
            assert headers["Cache-Control"] == "no-store"
            assert PRIVATE_REASON.encode() not in body
            connection.assert_not_called()
            refresh.assert_not_called()
        site.boundary.ready = True
        assert site.request(path, operator=True)[0] == normal_status


@pytest.mark.parametrize("prefix", ["/api/", "/api/v1/"])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_participant_routes_already_fail_closed_and_recover(
    site: Site, prefix: str, method: str
) -> None:
    payload: dict[str, object] | None = None
    route = "session"
    if method == "POST":
        AccountStore(site.owner.kernel.database, site.owner.kernel.subject_id).register(
            "m06-user", "synthetic correct horse battery", "M06 tester"
        )
        route = "login"
        payload = {"username": "m06-user", "password": "synthetic correct horse battery"}
    path = prefix + "participants/" + route
    assert site.request(path, payload=payload)[0] == 200
    site.boundary.ready = False
    with patch.object(participant_http, "AccountStore", wraps=AccountStore) as store:
        status, headers, body = site.request(path, payload=payload)
        assert status == 503
        assert json.loads(body) == {"error": "at_rest_boundary_unavailable"}
        assert headers["Cache-Control"] == "no-store"
        assert PRIVATE_REASON.encode() not in body
        store.assert_not_called()
    site.boundary.ready = True
    assert site.request(path, payload=payload)[0] == 200


@pytest.mark.parametrize("prefix", ["/webhooks/", "/api/v1/webhooks/"])
def test_wechat_challenge_already_checks_storage_before_secrets_and_recovers(
    site: Site, prefix: str
) -> None:
    secret = "synthetic-m06-wechat-token"
    transport = site.owner.transports.configure(
        site.owner.kernel.subject_id,
        TransportInput(
            channel="wechat",
            label="M06 synthetic callback",
            endpoint="https://api.weixin.qq.com",
            credentials={"webhook_secret": SecretStr(secret)},
        ),
        actor="operator",
    )
    timestamp = str(int(time.time()))
    nonce = "m06-test-nonce"
    signature = hashlib.sha1("".join(sorted((secret, timestamp, nonce))).encode()).hexdigest()
    path = (
        prefix
        + "wechat/"
        + transport.transport_id
        + "?"
        + urlencode(
            {"signature": signature, "timestamp": timestamp, "nonce": nonce, "echostr": "m06-echo"}
        )
    )
    assert site.request(path)[::2] == (200, b"m06-echo")
    site.boundary.ready = False
    transports = site.owner.transports
    with (
        patch.object(transports, "get", wraps=transports.get) as get,
        patch.object(transports, "secret", wraps=transports.secret) as read_secret,
    ):
        status, _headers, body = site.request(path)
        assert status == 503
        # Preserve the callback's existing quarantine error contract.
        assert json.loads(body) == {"error": "integrity_quarantine"}
        assert PRIVATE_REASON.encode() not in body and b"m06-echo" not in body
        get.assert_not_called()
        read_secret.assert_not_called()
    site.boundary.ready = True
    assert site.request(path)[::2] == (200, b"m06-echo")


def test_static_assets_and_degraded_health_remain_available(site: Site) -> None:
    site.boundary.ready = False
    for path in (
        "/",
        "/admin",
        "/app.js",
        "/participant.js",
        "/styles.css",
        "/robots.txt",
        "/health/live",
    ):
        status, _headers, body = site.request(path)
        assert status == 200, (path, body)
    assert site.request("/favicon.ico")[0] == 204
    assert site.request("/admin/session")[0] == 200
    status, _headers, body = site.request("/health")
    assert status == 503 and json.loads(body)["status"] == "degraded"
    assert PRIVATE_REASON.encode() not in body
    for path in ("/health/ready", "/api/admin/readiness", "/api/v1/admin/readiness"):
        status, _headers, body = site.request(path, operator=True)
        assert status == 503
        payload = json.loads(body)
        assert payload["status"] == "degraded"
        assert payload["reason_code"] == "at_rest_unavailable"
        assert PRIVATE_REASON.encode() not in body
    assert site.request("/api/admin/readiness")[0] == 401
    for path in ("/api/state", "/api/v1/public/overview"):
        status, _headers, body = site.request(path)
        assert status == 503 and json.loads(body) == {"error": "at_rest_boundary_unavailable"}


@pytest.mark.parametrize("mode", ["absent", "development", "required-ready"])
def test_existing_available_boundary_modes_keep_public_and_migration_reads(
    site: Site, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if mode == "absent":
        monkeypatch.setattr(site.owner, "at_rest", None)
    elif mode == "development":
        site.boundary.required = False
        site.boundary.ready = False
    for path in (site.article, "/sitemap.xml", site.partition, "/api/v1/admin/migration/policy"):
        assert site.request(path, operator=True)[0] == 200
    assert (site.boundary.checks > 0) == (mode == "required-ready")


@pytest.mark.parametrize("loss", ["pending", "expired", "unsafe"])
def test_real_request_guard_snapshot_failure_blocks_early_reads_and_recovers(
    site: Site, loss: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard = AtRestGuard(AtRestConfig(data_root=site.owner.settings.data_dir, mode="required"))
    key = Mock(spec=BackupKeyring)
    key.metadata.return_value = {}
    # Simulate host encryption/key availability, keeping the real bounded guard.
    guard._prepared = True
    monkeypatch.setattr(guard, "_require_volume", lambda: None)
    monkeypatch.setattr(guard, "_require_keyring", lambda: key)
    monkeypatch.setattr(guard, "_monitor_permissions", lambda: None)
    monkeypatch.setattr(site.owner, "at_rest", guard)
    signature = guard._permission_root_signature()
    try:
        for path in (site.article, "/sitemap.xml", site.partition, "/api/admin/migration/policy"):
            guard._permission_snapshot = (signature, time.monotonic(), None)
            assert site.request(path, operator=True)[0] == 200
            guard._permission_snapshot = (
                None
                if loss == "pending"
                else (
                    signature,
                    time.monotonic() - (10 if loss == "expired" else 0),
                    PRIVATE_REASON if loss == "unsafe" else None,
                )
            )
            status, _headers, body = site.request(path, operator=True)
            assert status == 503
            assert json.loads(body) == {"error": "at_rest_boundary_unavailable"}
            assert PRIVATE_REASON.encode() not in body
            guard._permission_snapshot = (signature, time.monotonic(), None)
            assert site.request(path, operator=True)[0] == 200
    finally:
        guard.close()
