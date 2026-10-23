from __future__ import annotations

from pathlib import Path

import pytest

from noyra.core import Database, IdentityStore
from noyra.interaction.accounts import AccountError, AccountRateLimited, AccountStore


def accounts(tmp_path: Path, subject: str = "subject-a") -> AccountStore:
    db = Database(tmp_path / "db")
    IdentityStore(db).ensure(subject, "a" * 64)
    return AccountStore(db, subject)


def test_human_default_sessions_scope_reset_and_credential_revocation(tmp_path: Path) -> None:
    store = accounts(tmp_path)
    registration = store.register("alice", "correct horse battery", "Alice")
    token, csrf, who = store.login("ALICE", "correct horse battery")
    assert who["entity_type"] == "human"
    principal = store.authenticate(token, csrf=csrf)
    assert principal and principal.entity_id == registration["entity_id"]
    assert store.authenticate(token, csrf="wrong") is None
    assert accounts(tmp_path, "subject-b").authenticate(token) is None
    api = store.api_key(principal)
    assert api and store.authenticate(api, api=True)
    assert store.authenticate(api) is None
    assert store.authenticate(token, api=True) is None
    replacement = store.reset("alice", registration["recovery_code"], "new correct password")
    assert replacement != registration["recovery_code"]
    assert store.authenticate(token) is None
    assert store.authenticate(api, api=True) is None
    with pytest.raises(AccountError):
        store.reset("alice", registration["recovery_code"], "another new password")
    with pytest.raises(AccountError):
        store.login("alice", "correct horse battery")
    new_token, _, _ = store.login("alice", "new correct password")
    store.logout(new_token)
    assert store.authenticate(new_token) is None
    with store.database.connection() as c:
        stored = str([tuple(row) for row in c.execute("SELECT * FROM participant_credentials")])
        assert "new correct password" not in stored
        assert replacement not in stored


def test_renamed_and_shared_labels_never_merge_accounts(tmp_path: Path) -> None:
    store = accounts(tmp_path)
    first = store.register("first", "correct horse battery", "Same")
    second = store.register("second", "correct horse battery", "Same", kind="agent")
    assert first["entity_id"] != second["entity_id"]
    _, _, agent = store.login("second", "correct horse battery")
    assert agent["entity_type"] == "agent"
    with pytest.raises(AccountError):
        store.register("FIRST", "correct horse battery", "Other")
    for kind in ("operator", "admin"):
        with pytest.raises(AccountError):
            store.register("invalid", "correct horse battery", "Other", kind=kind)


def test_durable_auth_rate_limit_and_bounded_sessions(tmp_path: Path) -> None:
    store = accounts(tmp_path)
    for _ in range(3):
        store.throttle("client", "login", 3)
    with pytest.raises(AccountRateLimited):
        accounts(tmp_path).throttle("client", "login", 3)
    store.register("alice", "correct horse battery", "Alice")
    tokens = [store.login("alice", "correct horse battery")[0] for _ in range(9)]
    assert store.authenticate(tokens[0]) is None
    assert store.authenticate(tokens[-1])
