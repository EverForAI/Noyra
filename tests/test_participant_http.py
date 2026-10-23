from __future__ import annotations

import json
from collections.abc import Iterator
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

import pytest
from pydantic import SecretStr

from noyra.core import SubjectKernel
from noyra.core.types import content_hash
from noyra.interaction.accounts import AccountStore
from noyra.interaction.store import InteractionStore
from noyra.service import NoyraHTTPServer, ServiceSettings
from test_wallet_economy import _bounty, _fixture, _goal


@pytest.fixture
def http(tmp_path: Path) -> Iterator[tuple[NoyraHTTPServer, str]]:
    kernel = SubjectKernel(
        tmp_path / "data" / "noyra.sqlite3", "participant-http", content_hash("participant-http")
    )
    kernel.boot()
    kernel.orient()
    kernel.activate()
    owner = NoyraHTTPServer(
        kernel,
        ServiceSettings(
            data_dir=tmp_path / "data",
            subject_id=kernel.subject_id,
            genesis_hash=content_hash("participant-http"),
            host="127.0.0.1",
            port=0,
            admin_token=SecretStr("admin-test-key-with-enough-entropy"),
        ),
    )
    owner.start()
    try:
        yield owner, "http://127.0.0.1:" + str(owner.address[1])
    finally:
        owner.close()
        kernel.close()


class Client:
    def __init__(self, base: str):
        self.base = base
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))
        self.csrf = ""

    def request(
        self, path: str, payload: dict[str, Any] | None = None, **headers: str
    ) -> tuple[int, Any]:
        request = Request(
            self.base + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={
                "Content-Type": "application/json",
                "X-Participant-CSRF": self.csrf,
                **headers,
            },
        )
        try:
            response = self.opener.open(request, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read())

    def login(self, name: str) -> None:
        code, data = self.request(
            "/api/participants/login", {"username": name, "password": "correct horse battery"}
        )
        assert code == 200, data
        self.csrf = data["csrf_token"]


def test_http_session_csrf_cross_origin_and_operator_separation(
    http: tuple[NoyraHTTPServer, str],
) -> None:
    owner, base = http
    store = AccountStore(owner.kernel.database, owner.kernel.subject_id)
    store.register("alice", "correct horse battery", "Alice")
    alice = Client(base)
    alice.login("alice")
    assert alice.request("/api/participants/session")[1]["participant"]["entity_type"] == "human"
    assert alice.request("/api/state")[0] == 200
    assert alice.request("/api/admin/public-publications")[0] == 401
    assert Client(base).request("/api/participants/messages")[0] == 401
    assert (
        alice.request(
            "/api/participants/messages",
            {"content": "hello", "idempotency_key": "one"},
            Origin="https://evil.test",
        )[0]
        == 403
    )
    csrf = alice.csrf
    alice.csrf = ""
    assert (
        alice.request("/api/participants/messages", {"content": "hello", "idempotency_key": "one"})[
            0
        ]
        == 403
    )
    alice.csrf = csrf
    first = alice.request(
        "/api/participants/messages", {"content": "hello", "idempotency_key": "one"}
    )
    assert first[0] == 200, first
    assert (
        alice.request("/api/participants/messages", {"content": "hello", "idempotency_key": "one"})
        == first
    )
    assert (
        alice.request(
            "/api/participants/messages",
            {"content": "hello", "idempotency_key": "one", "entity_id": "someone-else"},
        )[0]
        == 400
    )
    assert alice.request("/api/participants/logout", {})[0] == 200
    assert alice.request("/api/participants/messages")[0] == 401


def test_private_reply_isolation_and_bearer_rotation(http: tuple[NoyraHTTPServer, str]) -> None:
    owner, base = http
    store = AccountStore(owner.kernel.database, owner.kernel.subject_id)
    store.register("alice", "correct horse battery", "Same")
    store.register("bob", "correct horse battery", "Same", kind="noyra")
    alice, bob = Client(base), Client(base)
    alice.login("alice")
    bob.login("bob")
    status, first = alice.request(
        "/api/participants/messages", {"content": "private Alice", "idempotency_key": "shared"}
    )
    assert status == 200
    status, second = bob.request(
        "/api/participants/messages", {"content": "private Bob", "idempotency_key": "shared"}
    )
    assert status == 200
    assert first["interaction_id"] != second["interaction_id"]
    record = InteractionStore(owner.kernel.database).get(first["interaction_id"])
    InteractionStore(owner.kernel.database).send(
        owner.kernel.subject_id,
        "participant",
        record.counterparty,
        "Reply for Alice",
        related_interaction_id=record.interaction_id,
    )
    assert "Reply for Alice" in str(alice.request("/api/participants/messages"))
    assert "private Alice" not in str(bob.request("/api/participants/messages"))
    assert "Reply for Alice" not in str(bob.request("/api/participants/messages"))
    notices = alice.request("/api/participants/notifications")[1]["items"]
    assert len(notices) == 1
    assert (
        bob.request(
            "/api/participants/notification-read",
            {"notification_id": notices[0]["notification_id"]},
        )[0]
        == 404
    )
    assert (
        alice.request(
            "/api/participants/notification-read",
            {"notification_id": notices[0]["notification_id"]},
        )[0]
        == 200
    )
    token = alice.request("/api/participants/credential", {})[1]["api_key"]
    machine = Client(base)
    assert machine.request("/api/participants/messages", Authorization="Bearer " + token)[0] == 200
    assert (
        machine.request("/api/participants/credential", {}, Authorization="Bearer " + token)[0]
        == 403
    )
    assert (
        machine.request(
            "/api/participants/messages", Authorization="Bearer admin-test-key-with-enough-entropy"
        )[0]
        == 401
    )
    alice.request("/api/participants/credential", {"revoke": True})
    assert machine.request("/api/participants/messages", Authorization="Bearer " + token)[0] == 401


def test_authenticated_collaboration_derives_owner_not_client_label(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from noyra.interaction.collaboration import ParticipantCollaboration
    from noyra.wallet.workflow import WalletRewardWorkflow

    db, subject, _wallet, economy, network, asset = _fixture(tmp_path)
    bounty = _bounty(economy, subject, network, asset, _goal(db, subject))
    economy.publish_bounty(bounty.bounty_id, subject, actor="operator")
    accounts = AccountStore(db, subject)
    accounts.register("alice", "correct horse battery", "Alice")
    accounts.register("bob", "correct horse battery", "Bob")
    a = accounts.authenticate(accounts.login("alice", "correct horse battery")[0])
    b = accounts.authenticate(accounts.login("bob", "correct horse battery")[0])
    assert a and b
    owner = SimpleNamespace(
        kernel=SimpleNamespace(database=db, subject_id=subject),
        wallet_economy=economy,
        wallet_rewards=WalletRewardWorkflow(db),
    )
    collaboration = ParticipantCollaboration(owner)
    assert collaboration.opportunities()[0]["bounty_id"] == bounty.bounty_id
    apply = {"bounty_id": bounty.bounty_id, "content": "I can help", "idempotency_key": "apply-one"}
    first = collaboration.write("/api/participants/apply", apply, a, "127.0.0.1")
    assert collaboration.write("/api/participants/apply", apply, a, "127.0.0.1") == first
    assert collaboration.read("/api/participants/applications", {}, b)["items"] == []
    with pytest.raises(LookupError):
        collaboration.write(
            "/api/participants/withdraw",
            {"kind": "application", "id": first["application_id"]},
            b,
            "127.0.0.1",
        )
    payload = {
        "bounty_id": bounty.bounty_id,
        "content": "Evidence",
        "evidence": ["https://example.test/result"],
        "recipient_address": "0x" + "1" * 40,
        "idempotency_key": "submission-one",
        "consent_version": 1,
    }
    submission = collaboration.write("/api/participants/submit", payload, a, "127.0.0.1")
    assert collaboration.write("/api/participants/submit", payload, a, "127.0.0.1") == submission
    assert collaboration.read("/api/participants/submissions", {}, b)["items"] == []
    with db.connection() as c:
        assert (
            c.execute("SELECT counterparty FROM wallet_bounty_submissions").fetchone()[0]
            == a.entity_id
        )
        assert c.execute("SELECT COUNT(*) FROM wallet_payment_orders").fetchone()[0] == 0
    with pytest.raises(ValueError):
        collaboration.write(
            "/api/participants/submit", {**payload, "counterparty": b.entity_id}, a, "127.0.0.1"
        )


def test_registration_uses_existing_captcha_and_failed_post_counts_commit(
    http: tuple[NoyraHTTPServer, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, base = http
    captured = []
    original = owner.public_posts._captcha_image

    def image(answer: str) -> str:
        captured.append(answer)
        return original(answer)

    monkeypatch.setattr(owner.public_posts, "_captcha_image", image)
    visitor = Client(base)
    status, challenge = visitor.request("/api/public-posts/captcha", {"presentation": "image"})
    assert status == 200
    status, registered = visitor.request(
        "/api/participants/register",
        {
            "username": "new-user",
            "display_name": "A human",
            "password": "correct horse battery",
            "captcha_id": challenge["challenge_id"],
            "captcha_answer": captured[-1],
        },
    )
    assert status == 201, registered
    assert "recovery_code" in registered
    visitor.login("new-user")
    status, challenge = visitor.request("/api/public-posts/captcha", {"presentation": "image"})
    payload = {
        "kind": "post",
        "title": "A thought",
        "content": "Public contribution",
        "idempotency_key": "public-one",
        "captcha_id": challenge["challenge_id"],
        "captcha_answer": "WRONG",
    }
    assert visitor.request("/api/participants/posts", payload)[0] == 400
    with owner.kernel.database.connection() as c:
        assert (
            c.execute(
                "SELECT attempts FROM public_post_captcha_challenges WHERE challenge_id=?",
                (challenge["challenge_id"],),
            ).fetchone()[0]
            == 1
        )
    payload["captcha_answer"] = captured[-1]
    status, post = visitor.request("/api/participants/posts", payload)
    assert status == 200, post
    assert visitor.request("/api/participants/posts", payload) == (status, post)
    mine = visitor.request("/api/participants/posts")[1]["items"]
    assert [row["post_id"] for row in mine] == [post["post_id"]]
    assert Client(base).request("/api/participants/posts")[0] == 401
