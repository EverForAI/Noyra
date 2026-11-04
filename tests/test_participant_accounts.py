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
    api = store.api_key(principal, session_token=token, csrf=csrf)
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


def test_password_change_requires_current_secret_and_revokes_all_credentials(
    tmp_path: Path,
) -> None:
    store = accounts(tmp_path)
    first = store.register("alice", "correct horse battery", "Alice")
    token, csrf, _ = store.login("alice", "correct horse battery")
    who = store.authenticate(token)
    assert who
    api = store.api_key(who, session_token=token, csrf=csrf)
    assert api
    with pytest.raises(AccountError):
        store.change_password(who, "not the old password", "another correct password")
    assert store.authenticate(token)
    recovery = store.change_password(who, "correct horse battery", "another correct password")
    assert recovery != first["recovery_code"]
    assert store.authenticate(token) is None
    assert store.authenticate(api, api=True) is None
    assert store.login("alice", "another correct password")[0]


@pytest.mark.parametrize("failure_count", [9, 10, 15])
def test_successful_logins_do_not_increment_account_failure_window(
    tmp_path: Path, failure_count: int
) -> None:
    store = accounts(tmp_path)
    store.register("alice", "correct horse battery", "Alice")
    for _ in range(failure_count):
        store.login("alice", "correct horse battery")
    assert not store.login_captcha_required("alice")
    with store.database.connection() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM participant_auth_limits WHERE subject_id=?", (store.subject,)
        ).fetchone()[0]
    assert count == 0


def test_repeated_failures_require_captcha_but_success_is_not_locked_out(tmp_path: Path) -> None:
    store = accounts(tmp_path)
    store.register("alice", "correct horse battery", "Alice")
    for _ in range(10):
        with pytest.raises(AccountError, match="invalid_credentials"):
            store.login("alice", "incorrect password")
    assert store.login_captcha_required("alice")
    with pytest.raises(AccountError, match="invalid_credentials"):
        store.login("alice", "correct horse battery")
    token, _, _ = store.login("alice", "correct horse battery", captcha_verified=True)
    assert token and not store.login_captcha_required("alice")


def test_captcha_verified_wrong_password_keeps_account_failure_challenge(tmp_path: Path) -> None:
    store = accounts(tmp_path)
    store.register("alice", "correct horse battery", "Alice")
    for _ in range(10):
        with pytest.raises(AccountError):
            store.login("alice", "incorrect password")
    with pytest.raises(AccountError, match="invalid_credentials"):
        store.login("alice", "still wrong", captcha_verified=True)
    assert store.login_captcha_required("alice")
    # The API keeps the same invalid-credentials response whether or not a
    # challenge is required, preventing account-state probing.
    with pytest.raises(AccountError, match="invalid_credentials"):
        store.login("alice", "correct horse battery")
    assert store.login("alice", "correct horse battery", captcha_verified=True)[0]
    assert not store.login_captcha_required("alice")


def test_unknown_user_failures_use_one_bounded_shared_bucket(tmp_path: Path) -> None:
    store = accounts(tmp_path)
    for index in range(10):
        with pytest.raises(AccountError, match="invalid_credentials"):
            store.login(f"unknown-{index}", "incorrect password")
    assert store.login_captcha_required("another-unknown")
    with pytest.raises(AccountError, match="invalid_credentials"):
        store.login("another-unknown", "incorrect password")
    store.register("alice", "correct horse battery", "Alice")
    assert not store.login_captcha_required("alice")


def test_auth_bucket_cleanup_and_capacity_are_scoped_to_current_subject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = accounts(tmp_path, "subject-a")
    second = accounts(tmp_path, "subject-b")
    now = [100_000.0]
    monkeypatch.setattr("noyra.interaction.accounts.time.time", lambda: now[0])
    first.throttle("expired", "login")
    first.throttle("expired", "other")
    with first.database.transaction() as connection:
        connection.executemany(
            "INSERT INTO participant_auth_limits(subject_id,bucket,window_start,attempts) "
            "VALUES(?,?,?,?)",
            [("subject-a", f"bucket-{index}", now[0], 1) for index in range(10_000 - 2)],
        )
    second.throttle("client", "login")
    with first.database.connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM participant_auth_limits WHERE subject_id='subject-a'"
            ).fetchone()[0]
            == 10_000
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM participant_auth_limits WHERE subject_id='subject-b'"
            ).fetchone()[0]
            == 1
        )
    now[0] += 3601
    second.throttle("next-client", "register")
    with first.database.connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM participant_auth_limits WHERE subject_id='subject-a'"
            ).fetchone()[0]
            == 10_000
        )
    first.throttle("next-client", "login")
    with first.database.connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM participant_auth_limits WHERE subject_id='subject-a'"
            ).fetchone()[0]
            == 1
        )


def test_concurrent_account_failures_are_durable_and_captcha_reopens_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    store = accounts(tmp_path)
    store.register("alice", "correct horse battery", "Alice")
    monkeypatch.setattr("noyra.interaction.accounts.password_matches", lambda *_: False)

    def attempt(index: int) -> str:
        try:
            store.login("alice", f"wrong-{index}")
        except AccountError as error:
            return str(error)
        raise AssertionError("wrong password unexpectedly authenticated")

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(attempt, range(12)))
    assert all(result in {"invalid_credentials", "login_captcha_required"} for result in results)
    assert results.count("invalid_credentials") >= 10
    assert store.login_captcha_required("alice")
    monkeypatch.setattr(
        "noyra.interaction.accounts.password_matches",
        lambda password, _encoded: password == "correct horse battery",
    )
    assert store.login("alice", "correct horse battery", captcha_verified=True)[0]
    assert not store.login_captcha_required("alice")


def test_success_clears_only_that_account_failure_bucket(tmp_path: Path) -> None:
    store = accounts(tmp_path)
    store.register("alice", "correct horse battery", "Alice")
    store.register("bob", "correct horse battery", "Bob")
    for username in ("alice", "bob"):
        for _ in range(10):
            with pytest.raises(AccountError, match="invalid_credentials"):
                store.login(username, "incorrect password")
    assert store.login_captcha_required("alice") and store.login_captcha_required("bob")
    store.login("alice", "correct horse battery", captcha_verified=True)
    assert not store.login_captcha_required("alice")
    assert store.login_captcha_required("bob")


def test_account_failure_challenge_expires_at_window_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    store = accounts(tmp_path)
    store.register("alice", "correct horse battery", "Alice")
    now = [100_000.0]
    monkeypatch.setattr("noyra.interaction.accounts.time.time", lambda: now[0])
    for _ in range(10):
        with pytest.raises(AccountError, match="invalid_credentials"):
            store.login("alice", "incorrect password")
    assert store.login_captcha_required("alice")
    now[0] += 3600.001
    assert not store.login_captcha_required("alice")
    with pytest.raises(AccountError, match="invalid_credentials"):
        store.login("alice", "incorrect password")
    assert not store.login_captcha_required("alice")


def test_challenged_account_uses_same_dummy_verifier_without_revealing_threshold(
    tmp_path: Path,
) -> None:
    from unittest.mock import patch

    store = accounts(tmp_path)
    store.register("alice", "correct horse battery", "Alice")
    for _ in range(10):
        with pytest.raises(AccountError):
            store.login("alice", "wrong password")
    seen = []

    def verifier(password: str, encoded: str) -> bool:
        seen.append(encoded)
        return False

    with patch("noyra.interaction.accounts.password_matches", side_effect=verifier):
        with pytest.raises(AccountError, match="invalid_credentials"):
            store.login("alice", "correct horse battery")
        with pytest.raises(AccountError, match="invalid_credentials"):
            store.login("unknown-name", "incorrect password")
    assert seen == ["00" * 16 + ":" + "00" * 64, "00" * 16 + ":" + "00" * 64]
