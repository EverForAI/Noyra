from __future__ import annotations

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch
from urllib.request import HTTPCookieProcessor

import pytest

from noyra.interaction.accounts import AccountError, AccountStore
from noyra.service import NoyraHTTPServer
from test_participant_http import Client
from test_participant_http import http as participant_http_fixture


@pytest.fixture
def participant_http(tmp_path: Path) -> Iterator[tuple[NoyraHTTPServer, str]]:
    yield from cast(Any, participant_http_fixture).__wrapped__(tmp_path)


@pytest.mark.parametrize("revocation", ["recover", "password", "logout"])
@pytest.mark.parametrize("revoke_key", [False, True])
def test_credential_operation_cannot_cross_session_revocation(
    participant_http: tuple[NoyraHTTPServer, str],
    revocation: str,
    revoke_key: bool,
) -> None:
    owner, base = participant_http
    store = AccountStore(owner.kernel.database, owner.kernel.subject_id)
    registration = store.register("alice", "correct horse battery", "Alice")
    client = Client(base)
    client.login("alice")
    arrived, release = threading.Event(), threading.Event()
    throttle = AccountStore.throttle

    def controlled(self: AccountStore, client_id: str, operation: str, limit: int = 12) -> None:
        throttle(self, client_id, operation, limit)
        if operation == "writes":
            arrived.set()
            assert release.wait(15)

    with (
        patch.object(AccountStore, "throttle", controlled),
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        pending = pool.submit(
            client.request, "/api/participants/credential", {"revoke": revoke_key}
        )
        try:
            assert arrived.wait(15)
            if revocation == "recover":
                store.reset("alice", registration["recovery_code"], "new correct horse battery")
            elif revocation == "password":
                token, csrf, _ = store.login("alice", "correct horse battery")
                who = store.authenticate(token, csrf=csrf)
                assert who
                store.change_password(who, "correct horse battery", "new correct horse battery")
            else:
                old_token = next(
                    cookie.value
                    for handler in cast(Any, client.opener).handlers
                    if isinstance(handler, HTTPCookieProcessor)
                    for cookie in handler.cookiejar
                    if cookie.name == "noyra_participant"
                )
                assert isinstance(old_token, str)
                store.logout(old_token)
            # Simulate a fresh, legitimately authorized credential minted after
            # recovery. A stale revoke must not remove it either.
            password = (
                "correct horse battery" if revocation == "logout" else "new correct horse battery"
            )
            token, csrf, _ = store.login("alice", password)
            who = store.authenticate(token, csrf=csrf)
            assert who
            fresh = store.api_key(who, session_token=token, csrf=csrf)
            assert fresh
        finally:
            release.set()
        status, body = pending.result(timeout=15)
    assert status == 401 and body == {"error": "invalid_credentials"}
    assert store.authenticate(fresh, api=True)
    with store.database.connection() as c:
        assert c.execute("SELECT COUNT(*) FROM participant_api_credentials").fetchone()[0] == 1


def test_credential_transaction_checks_expiry_csrf_account_and_disabled_entity(
    participant_http: tuple[NoyraHTTPServer, str],
) -> None:
    owner, _ = participant_http
    store = AccountStore(owner.kernel.database, owner.kernel.subject_id)
    store.register("alice", "correct horse battery", "Alice")
    store.register("bob", "correct horse battery", "Bob")
    token, csrf, _ = store.login("alice", "correct horse battery")
    other, other_csrf, _ = store.login("bob", "correct horse battery")
    who = store.authenticate(token, csrf=csrf)
    assert who
    for candidate, code in [(token, "wrong"), (other, other_csrf)]:
        with pytest.raises(AccountError, match="invalid_credentials"):
            store.api_key(who, session_token=candidate, csrf=code)
    key = store.api_key(who, session_token=token, csrf=csrf)
    assert key and store.authenticate(key, api=True)
    with store.database.transaction() as c:
        c.execute(
            "UPDATE participant_sessions SET expires_at=0 WHERE subject_id=?", (store.subject,)
        )
    with pytest.raises(AccountError, match="invalid_credentials"):
        store.api_key(who, session_token=token, csrf=csrf)
    token, csrf, _ = store.login("alice", "correct horse battery")
    with store.database.transaction() as c:
        c.execute(
            "UPDATE participant_entities SET status='disabled' WHERE entity_id=?", (who.entity_id,)
        )
    with pytest.raises(AccountError, match="invalid_credentials"):
        store.api_key(who, session_token=token, csrf=csrf)


def test_credential_committed_before_reset_is_revoked(
    participant_http: tuple[NoyraHTTPServer, str],
) -> None:
    owner, _ = participant_http
    store = AccountStore(owner.kernel.database, owner.kernel.subject_id)
    registration = store.register("alice", "correct horse battery", "Alice")
    token, csrf, _ = store.login("alice", "correct horse battery")
    who = store.authenticate(token, csrf=csrf)
    assert who
    key = store.api_key(who, session_token=token, csrf=csrf)
    assert key and store.authenticate(key, api=True)
    store.reset("alice", registration["recovery_code"], "new correct horse battery")
    assert store.authenticate(key, api=True) is None
