from __future__ import annotations

import json
import secrets
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from pydantic import SecretStr

from noyra.core import Database, IdentityStore
from noyra.core.types import content_hash
from noyra.interaction import (
    PublicPostCapacityError,
    PublicPostInput,
    PublicPostQueueFullError,
    PublicPostRateLimitError,
    PublicPostStore,
)
from noyra.service import NoyraService, ServiceSettings

REAL_CHOICE = secrets.choice


@pytest.mark.parametrize("dimension", ["storage", "queue", "rate"])
def test_lowered_saved_controls_override_inflight_snapshot(
    tmp_path: Path,
    dimension: str,
) -> None:
    db = Database(tmp_path / "db")
    subject = "public-stale-controls"
    IdentityStore(db).ensure(subject, content_hash(subject))
    store = PublicPostStore(db)
    proposal = PublicPostInput(kind="post", title="first", content="x" * 500)
    original = store.create(
        subject, proposal, idempotency_key="first", author_provenance="operator"
    )
    store.configure_controls(
        subject,
        rate_limit_per_hour=1,
        queue_cap=1 if dimension == "queue" else 100,
        storage_cap_bytes=1000 if dimension == "storage" else 10000,
        captcha_ttl_seconds=300,
        captcha_max_attempts=5,
        captcha_mode="digits",
        actor="operator",
    )
    # Lowering limits must still permit replay of the exact accepted post.
    assert (
        store.create(
            subject,
            proposal,
            idempotency_key="first",
            author_provenance="operator",
            queue_cap=100,
            storage_cap_bytes=10000,
        ).post_id
        == original.post_id
    )
    expected = {
        "storage": PublicPostCapacityError,
        "queue": PublicPostQueueFullError,
        "rate": PublicPostRateLimitError,
    }[dimension]
    if dimension == "rate":
        with db.transaction() as connection:
            store._record_rate_event(connection, subject, store._client_bucket("192.0.2.1"))
    with pytest.raises(expected):
        store.create(
            subject,
            PublicPostInput(kind="post", title="second", content="y" * 600),
            queue_cap=100,
            storage_cap_bytes=10000,
            rate_limit_per_hour=100,
            author_provenance="visitor" if dimension == "rate" else "operator",
            client_ip="192.0.2.1",
        )
    assert store.get(original.post_id, subject_id=subject).content == proposal.content


def test_post_configuration_tampering_is_rejected_inside_write_transaction(tmp_path: Path) -> None:
    from noyra.core.errors import IntegrityError

    db = Database(tmp_path / "db")
    subject = "public-controls-integrity"
    IdentityStore(db).ensure(subject, content_hash(subject))
    store = PublicPostStore(db)
    store.configure_controls(
        subject,
        rate_limit_per_hour=10,
        queue_cap=10,
        captcha_ttl_seconds=300,
        captcha_max_attempts=5,
        captcha_mode="digits",
        actor="operator",
    )
    with db.transaction() as connection:
        connection.execute("UPDATE public_post_controls SET queue_cap=100")
    with pytest.raises(IntegrityError):
        store.create(
            subject,
            PublicPostInput(kind="post", title="test", content="test"),
            author_provenance="operator",
            queue_cap=100,
        )


def fixed_choice(sequence: Sequence[Any]) -> Any:
    return "2" if isinstance(sequence, str) else REAL_CHOICE(sequence)


@pytest.mark.parametrize("cap", [1_000, 999_999, 1_000_000])
def test_saved_capacity_is_used_by_real_http_submission(tmp_path: Path, cap: int) -> None:
    service = NoyraService(
        ServiceSettings(
            data_dir=tmp_path / "data",
            subject_id="public-capacity-http",
            genesis_hash=content_hash("capacity"),
            host="127.0.0.1",
            port=0,
            integrity_mode="off",
            operator_token=SecretStr("synthetic-public-capacity-operator-token"),
        )
    )
    service.boot()
    service.http.start()
    try:
        _, port = service.http.address

        def http(path: str, data: Any = None) -> Any:
            try:
                response_context = urlopen(
                    Request(
                        f"http://127.0.0.1:{port}" + path,
                        data=None if data is None else json.dumps(data).encode(),
                        headers={
                            "Content-Type": "application/json",
                            **(
                                {"Authorization": "Bearer synthetic-public-capacity-operator-token"}
                                if path.startswith("/api/config/") or path.startswith("/api/admin/")
                                else {}
                            ),
                        },
                    ),
                    timeout=10,
                )
            except HTTPError:
                raise
            with response_context as response:
                return json.loads(response.read())

        http(
            "/api/config/public-post-controls",
            {
                "rate_limit_per_hour": 100,
                "queue_cap": 100,
                "captcha_ttl_seconds": 300,
                "captcha_max_attempts": 5,
                "captcha_issue_limit_per_hour": 30,
                "captcha_global_rate_per_minute": 300,
                "captcha_mode": "digits",
                "storage_cap_bytes": cap,
                "max_content_chars": 10,
            },
        )
        assert http("/api/admin/public-post-controls")["storage_cap_bytes"] == cap
        # Rendering noise also uses choice; one allowed glyph keeps the answer
        # deterministic without exposing any production CAPTCHA or secret.
        with patch("noyra.interaction.posts.secrets.choice", side_effect=fixed_choice):
            challenge = http("/api/public-posts/captcha", {})
        result = http(
            "/api/public-posts",
            {
                "kind": "post",
                "title": "title",
                "content": "中文测试",
                "captcha_id": challenge["challenge_id"],
                "captcha_answer": "222222",
            },
        )
        assert result["status"] == "pending_review"
        with pytest.raises(HTTPError) as caught:
            http(
                "/api/public-posts",
                {
                    "kind": "post",
                    "title": "long",
                    "content": "中" * 11,
                    "captcha_id": challenge["challenge_id"],
                    "captcha_answer": "222222",
                },
            )
        assert json.loads(caught.value.read())["error"] == "public_post_too_long"
    finally:
        service.http.close()
        service.kernel.close()


def test_utf8_capacity_and_concurrent_posts_preserve_existing_content(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.sqlite3")
    subject = "public-capacity-store"
    IdentityStore(db).ensure(subject, content_hash(subject))
    store = PublicPostStore(db)
    # 4 title bytes + 990 Chinese UTF-8 bytes + 6 default author bytes = 1000.
    proposal = PublicPostInput(kind="post", title="test", content="中" * 330)
    record = store.create(subject, proposal, storage_cap_bytes=1_000, author_provenance="operator")
    assert store.usage(subject)["byte_size"] == 1_000
    with pytest.raises(PublicPostCapacityError):
        store.create(
            subject,
            PublicPostInput(kind="post", title="extra", content="x"),
            storage_cap_bytes=1_000,
            author_provenance="operator",
        )
    assert store.get(record.post_id, subject_id=subject).content == proposal.content

    other = "public-capacity-race"
    IdentityStore(db).ensure(other, content_hash(other))

    def submit(index: int) -> str:
        try:
            store.create(
                other,
                PublicPostInput(kind="post", title=str(index), content="中" * 200),
                storage_cap_bytes=1_000,
                author_provenance="operator",
            )
            return "created"
        except PublicPostCapacityError:
            return "full"

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(submit, range(2))) == ["created", "full"]
    assert store.usage(other)["byte_size"] <= 1_000
