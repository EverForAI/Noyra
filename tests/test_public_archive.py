from __future__ import annotations

import base64
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from noyra.core import SubjectKernel
from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.event_archive import EventPayloadArchive
from noyra.core.events import EventStore
from noyra.core.types import content_hash
from noyra.interaction.public_archive import PublicArchive, PublicationConflict


@pytest.fixture
def archive(tmp_path: Path) -> Iterator[tuple[PublicArchive, SubjectKernel]]:
    kernel = SubjectKernel(tmp_path / "data.sqlite3", "archive-test", content_hash("archive-test"))
    kernel.boot()
    kernel.orient()
    kernel.activate()
    yield PublicArchive(kernel.database), kernel
    kernel.close()


def publication(**changes: Any) -> dict[str, Any]:
    return {
        "publication_id": "public-example-01",
        "kind": "goal",
        "title": "公开目标",
        "summary": "仅公开审核摘要",
        "body": "公开说明",
        "details": {"status": "active"},
        "expected_revision": 0,
        "confirm_public": True,
        **changes,
    }


@pytest.mark.parametrize("kind", ["goal", "project", "prediction", "research", "sleep", "update"])
def test_explicit_publication_and_no_private_auto_copy(
    archive: tuple[PublicArchive, SubjectKernel], kind: str
) -> None:
    store, kernel = archive
    assert store.admin(kernel.subject_id) == []
    item = store.publish(kernel.subject_id, publication(kind=kind), actor="web-operator")
    assert item["provenance"] == "operator_summary"
    assert item["revision"] == 1
    assert set(item) == {
        "id",
        "kind",
        "title",
        "summary",
        "body",
        "status",
        "details",
        "revision",
        "occurred_at",
        "updated_at",
        "provenance",
    }


def test_retry_revision_conflict_and_withdrawal(
    archive: tuple[PublicArchive, SubjectKernel],
) -> None:
    store, kernel = archive
    first = store.publish(kernel.subject_id, publication(), actor="operator")
    assert store.publish(kernel.subject_id, publication(), actor="operator") == first
    with pytest.raises(PublicationConflict):
        store.publish(kernel.subject_id, publication(title="changed"), actor="operator")
    updated = store.publish(
        kernel.subject_id, publication(title="changed", expected_revision=1), actor="operator"
    )
    assert updated["occurred_at"] == first["occurred_at"]
    assert updated["revision"] == 2
    with pytest.raises(PublicationConflict):
        store.withdraw(kernel.subject_id, first["id"], 1, actor="operator")
    store.withdraw(kernel.subject_id, first["id"], 2, actor="operator")
    assert store.admin(kernel.subject_id)[0]["status"] == "withdrawn"
    with kernel.database.connection() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM public_publication_revisions").fetchone()[0]
            == 3
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"confirm_public": False},
        {"expected_revision": True},
        {"private_reason": "secret"},
        {"details": {"token": "secret"}},
        {"title": "\ud800"},
        {"kind": "prediction", "details": {"probability": float("nan")}},
        {"kind": "prediction", "details": {"probability": True}},
        {
            "kind": "research",
            "details": {"sources": [{"title": "secret", "url": "https://user:pass@example.com"}]},
        },
    ],
)
def test_rejects_unapproved_or_malformed_fields(
    archive: tuple[PublicArchive, SubjectKernel], changes: dict[str, Any]
) -> None:
    store, kernel = archive
    with pytest.raises((ValueError, UnicodeError)):
        store.publish(kernel.subject_id, publication(**changes), actor="operator")
    assert store.admin(kernel.subject_id) == []


def test_source_subject_and_record_isolation(archive: tuple[PublicArchive, SubjectKernel]) -> None:
    store, kernel = archive
    with pytest.raises(NotFoundError):
        store.publish(kernel.subject_id, publication(source_id="unowned-goal"), actor="operator")
    item = store.publish(kernel.subject_id, publication(), actor="operator")
    assert store.admin("another-subject") == []
    with pytest.raises(NotFoundError):
        store.withdraw("another-subject", item["id"], 1, actor="operator")
    with pytest.raises(NotFoundError):
        store.publish("another-subject", publication(), actor="operator")


def test_public_schema_has_fixed_reviewed_fingerprints(
    archive: tuple[PublicArchive, SubjectKernel],
) -> None:
    _, kernel = archive
    with kernel.database.connection() as connection:
        assert (
            kernel.database.schema_ddl_fingerprint(connection)
            == "8968045531bd80f2f1d487cb9a4b01e2b84767b10b8a85fc4ce85d0c956022bb"
        )
        assert (
            kernel.database.schema_structure_fingerprint(connection)
            == "bd4f12a88b407fc6f3fe4ab713401afb9c1a4ba73d79146927898a2ca8211b36"
        )


def test_page_filters_keyset_and_withdrawal(archive: tuple[PublicArchive, SubjectKernel]) -> None:
    store, kernel = archive
    for index in range(6):
        store.publish(
            kernel.subject_id,
            publication(publication_id=f"publication-{index}", title=f"公开标题{index}"),
            actor="operator",
        )
    first = store.page(kernel.subject_id, kind="goal", limit=2)
    second = store.page(kernel.subject_id, kind="goal", limit=2, cursor=first["next_cursor"])
    assert len(first["items"]) == len(second["items"]) == 2
    assert not {r["id"] for r in first["items"]} & {r["id"] for r in second["items"]}
    with pytest.raises(ValueError):
        store.page("another-subject", kind="goal", cursor=first["next_cursor"])
    with pytest.raises(ValueError):
        store.page(kernel.subject_id, kind="project", cursor=first["next_cursor"])
    assert len(store.page(kernel.subject_id, kind="goal", search="标题1")["items"]) == 1
    record = first["items"][0]
    store.withdraw(kernel.subject_id, record["id"], 1, actor="operator")
    assert store.page(kernel.subject_id, kind="goal", identifier=record["id"])["items"] == []
    assert len(store.page(kernel.subject_id)["items"]) >= 5


def test_lifecycle_projects_only_state_and_not_reason(
    archive: tuple[PublicArchive, SubjectKernel],
) -> None:
    store, kernel = archive
    kernel.lifecycle.transition("paused", "PRIVATE-SENTINEL", actor="operator")
    page = store.page(kernel.subject_id, kind="lifecycle")
    assert page["items"][0]["details"] == {"from": "active", "to": "paused"}
    assert "PRIVATE-SENTINEL" not in str(page)
    overview = store.overview(kernel.subject_id)
    assert set(overview["lifecycle"]) == {"state", "changed_at"}
    assert "PRIVATE-SENTINEL" not in str(overview)


def test_management_cursor_keeps_withdrawn_rows_and_subject_isolation(
    archive: tuple[PublicArchive, SubjectKernel],
) -> None:
    store, kernel = archive
    for index in range(5):
        store.publish(
            kernel.subject_id, publication(publication_id=f"manage-{index:04}"), actor="operator"
        )
    store.withdraw(kernel.subject_id, "manage-0002", 1, actor="operator")
    first = store.admin_page(kernel.subject_id, limit=2)
    second = store.admin_page(kernel.subject_id, limit=2, cursor=first["next_cursor"])
    assert len(first["items"]) == len(second["items"]) == 2
    assert any(row["status"] == "withdrawn" for row in first["items"])
    assert not {r["id"] for r in first["items"]} & {r["id"] for r in second["items"]}
    with pytest.raises(ValueError):
        store.admin_page("another-subject", cursor=first["next_cursor"])
    with pytest.raises(ValueError):
        store.admin_page(kernel.subject_id, cursor="bad!")


def test_archive_lists_use_excerpts_and_detail_fetches_full_public_body(
    archive: tuple[PublicArchive, SubjectKernel],
) -> None:
    store, kernel = archive
    item = store.publish(kernel.subject_id, publication(body="公开正文" * 4000), actor="operator")
    assert store.page(kernel.subject_id, kind="goal")["items"][0]["body"] == ""
    assert (
        store.page(kernel.subject_id, kind="goal", identifier=item["id"])["items"][0]["body"]
        == "公开正文" * 4000
    )
    with pytest.raises(ValueError):
        store.page(kernel.subject_id, identifier="")


def test_optional_summary_status_uses_the_displayed_default_for_filtering(
    archive: tuple[PublicArchive, SubjectKernel],
) -> None:
    store, kernel = archive
    default = store.publish(
        kernel.subject_id, publication(kind="update", details={}), actor="operator"
    )
    active = store.publish(
        kernel.subject_id,
        publication(publication_id="active-update", kind="update"),
        actor="operator",
    )
    assert [
        row["id"]
        for row in store.page(kernel.subject_id, kind="update", status="published")["items"]
    ] == [default["id"]]
    assert [
        row["id"] for row in store.page(kernel.subject_id, kind="update", status="active")["items"]
    ] == [active["id"]]


def test_status_filter_and_management_identity_preserve_legacy_legal_values(
    archive: tuple[PublicArchive, SubjectKernel],
) -> None:
    store, kernel = archive
    item = store.publish(
        kernel.subject_id, publication(details={"status": "Custom Status 1"}), actor="operator"
    )
    assert (
        store.page(kernel.subject_id, kind="goal", status="Custom Status 1")["items"][0]["id"]
        == item["id"]
    )
    assert store.admin_item(kernel.subject_id, item["id"])["revision"] == 1
    assert store.admin_item("other-subject", item["id"]) is None


def test_existing_public_records_keep_private_sources_out_of_v2(
    archive: tuple[PublicArchive, SubjectKernel],
) -> None:
    from noyra.interaction import (
        InteractionStore,
        PublicDiaryStore,
        PublicPostInput,
        PublicPostStore,
    )
    from noyra.sleep import SleepEngine, SleepReflectionPlan

    store, kernel = archive
    subject = kernel.subject_id
    messages = InteractionStore(kernel.database)
    public = messages.send(subject, "public:local", "PRIVATE-COUNTERPARTY", "Public outgoing")
    messages.send(subject, "local", "PRIVATE-COUNTERPARTY", "PRIVATE-OUTGOING")
    messages.receive(subject, "public:local", "PRIVATE-COUNTERPARTY", "PRIVATE-INCOMING")
    action = kernel.action_ledger.prepare(
        subject, "observe", "local-test", "PRIVATE-TARGET", {"input": "PRIVATE-INPUT"}
    )
    kernel.action_ledger.start(action.action_id)
    kernel.action_ledger.finish(
        action.action_id,
        "succeeded",
        {"output": "PRIVATE-RESULT"},
        public_explanation="Public activity",
    )
    posts = PublicPostStore(kernel.database)
    approved = posts.create(
        subject,
        PublicPostInput(kind="post", title="Approved", content="Public post"),
        author_provenance="operator",
    )
    posts.create(
        subject,
        PublicPostInput(kind="post", title="PRIVATE-PENDING", content="PRIVATE-POST"),
        author_provenance="operator",
    )
    posts.moderate(
        approved.post_id,
        subject_id=subject,
        status="published",
        actor="operator",
        reason="PRIVATE-MODERATION",
    )
    sleep = SleepEngine(kernel.database, subject)
    run = sleep.start("subject_choice", "PRIVATE-SLEEP-REASON")
    sleep.begin_reflection(run.sleep_id)
    diary_body = "公开日记" * 4_000
    sleep.commit_reflection(
        run.sleep_id,
        SleepReflectionPlan(
            summary="PRIVATE-REFLECTION",
            public_diary_candidate=diary_body,
        ),
    )
    sleep.enter_deep_sleep(run.sleep_id)
    sleep.wake(run.sleep_id, "PRIVATE-WAKE", force=True)
    sleep.complete_wake(run.sleep_id)
    diary = PublicDiaryStore(kernel.database).publish(
        subject, run.sleep_id, "Public diary", diary_body
    )
    timeline = store.page(subject, limit=100)
    assert "PRIVATE-" not in str(timeline)
    assert "PRIVATE-" not in str(store.overview(subject))
    assert {"diary", "behavior", "interaction", "post", "lifecycle"} <= {
        item["kind"] for item in timeline["items"]
    }
    assert all(item["body"] == "" for item in timeline["items"])
    assert (
        store.page(subject, kind="diary", identifier=diary.entry_id)["items"][0]["body"]
        == diary_body
    )
    assert store.page(subject, kind="interaction")["items"][0]["id"] == public.interaction_id
    assert store.page(subject, kind="post")["items"][0]["id"] == approved.post_id


def test_public_interactions_verify_hashes_and_case_sensitive_namespace(
    archive: tuple[PublicArchive, SubjectKernel],
) -> None:
    from noyra.interaction import InteractionStore

    store, kernel = archive
    messages = InteractionStore(kernel.database)
    upper = messages.send(kernel.subject_id, "PUBLIC:local", "peer", "must stay private")
    lower = messages.send(kernel.subject_id, "public:local", "peer", "approved")
    assert [item["id"] for item in store.page(kernel.subject_id, kind="interaction")["items"]] == [
        lower.interaction_id
    ]
    with kernel.database.transaction() as connection:
        connection.execute(
            "UPDATE interactions SET content=? WHERE interaction_id=?",
            ("tampered", lower.interaction_id),
        )
    with pytest.raises(IntegrityError):
        store.page(kernel.subject_id, kind="interaction")
    assert upper.interaction_id != lower.interaction_id


def test_public_lifecycle_remains_readable_after_verified_cold_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel = SubjectKernel(
        tmp_path / "archive.sqlite3", "lifecycle-archive", content_hash("lifecycle")
    )
    kernel.boot()
    kernel.orient()
    kernel.activate()
    try:
        event = EventStore(kernel.database).append(
            kernel.subject_id,
            "lifecycle_transition",
            "test",
            {"from": "active", "to": "paused"},
            privacy_level="public_status",
            occurred_at="2020-01-01T00:00:00+00:00",
        )
        monkeypatch.setenv(
            "NOYRA_ARCHIVE_ENCRYPTION_KEY", base64.urlsafe_b64encode(b"A" * 32).decode()
        )
        monkeypatch.setenv("NOYRA_ARCHIVE_ENCRYPTION_KEY_ID", "public-lifecycle-test")
        # Subject-scoped archive roots follow the production resolver contract.
        root = tmp_path / "subject" / "cold"
        EventPayloadArchive(kernel.database, root).archive_cold(
            kernel.subject_id, older_than_days=30
        )
        page = PublicArchive(kernel.database).page(
            kernel.subject_id, kind="lifecycle", until="2020-01-02T00:00:00Z"
        )
        assert page["items"][0]["id"] == event.event_id
        assert page["items"][0]["details"] == {"from": "active", "to": "paused"}
        monkeypatch.delenv("NOYRA_ARCHIVE_ENCRYPTION_KEY")
        with pytest.raises(IntegrityError):
            PublicArchive(kernel.database).page(
                kernel.subject_id, kind="lifecycle", until="2020-01-02T00:00:00Z"
            )
    finally:
        kernel.close()


def test_lifecycle_filtered_pagination_scans_until_matching_page(
    archive: tuple[PublicArchive, SubjectKernel],
) -> None:
    store, kernel = archive
    events = EventStore(kernel.database)
    ids = []
    for index in range(8):
        record = events.append(
            kernel.subject_id,
            "lifecycle_transition",
            "test",
            {"from": "active", "to": "paused" if index % 3 == 0 else "active"},
            privacy_level="public_status",
            occurred_at=f"2020-01-0{index + 1}T00:00:00Z",
        )
        if index % 3 == 0:
            ids.append(record.event_id)
    first = store.page(kernel.subject_id, kind="lifecycle", status="paused", limit=1)
    second = store.page(
        kernel.subject_id, kind="lifecycle", status="paused", limit=1, cursor=first["next_cursor"]
    )
    assert first["items"][0]["id"] == ids[-1]
    assert second["items"][0]["id"] == ids[-2]
    events.append(
        kernel.subject_id,
        "lifecycle_transition",
        "test",
        {"from": "future_state", "to": "active"},
        privacy_level="public_status",
    )
    with pytest.raises(IntegrityError):
        store.page(kernel.subject_id, kind="lifecycle", limit=1)


def test_behavior_checks_current_action_before_public_response(
    archive: tuple[PublicArchive, SubjectKernel],
) -> None:
    store, kernel = archive
    ledger = kernel.action_ledger
    action = ledger.prepare(kernel.subject_id, "observe", "test", "private-target", {})
    ledger.start(action.action_id)
    ledger.finish(action.action_id, "succeeded", {}, public_explanation="approved explanation")
    assert store.page(kernel.subject_id, kind="behavior")["items"]
    with kernel.database.transaction() as connection:
        connection.execute(
            "UPDATE actions SET result_json=? WHERE action_id=?",
            ('{"damaged":true}', action.action_id),
        )
    with pytest.raises(IntegrityError):
        store.page(kernel.subject_id, kind="behavior")


@pytest.mark.parametrize("timestamp", ["9999-12-31T23:59:59-01:00", "0001-01-01T00:00:00+01:00"])
def test_boundary_timezone_conversion_is_invalid_input(
    archive: tuple[PublicArchive, SubjectKernel], timestamp: str
) -> None:
    store, kernel = archive
    with pytest.raises(ValueError):
        store.page(kernel.subject_id, since=timestamp)
    with pytest.raises(ValueError):
        store.publish(
            kernel.subject_id,
            publication(kind="sleep", details={"started_at": timestamp}),
            actor="operator",
        )


def test_query_scan_and_byte_budgets_reset_after_failure(
    archive: tuple[PublicArchive, SubjectKernel], monkeypatch: pytest.MonkeyPatch
) -> None:
    import noyra.interaction.public_archive as module
    from noyra.interaction import InteractionStore

    store, kernel = archive
    item = InteractionStore(kernel.database).send(
        kernel.subject_id, "public:local", "peer", "test text"
    )
    with kernel.database.transaction() as connection:
        template = dict(
            connection.execute(
                "SELECT * FROM interactions WHERE interaction_id=?", (item.interaction_id,)
            ).fetchone()
        )
        for index in range(300):
            row = {
                **template,
                "interaction_id": f"budget-{index}",
                "idempotency_key": f"budget-key-{index}",
            }
            connection.execute(
                f"INSERT INTO interactions({','.join(row)}) VALUES({','.join('?' for _ in row)})",
                tuple(row.values()),
            )
    with monkeypatch.context() as patch:
        patch.setattr(module, "MAX_PUBLIC_SQL_STEPS", 1000)
        with pytest.raises(module.PublicQueryBudgetExceeded):
            store.page(kernel.subject_id, kind="interaction", search="not present")
    with monkeypatch.context() as patch:
        patch.setattr(module, "MAX_PUBLIC_READ_BYTES", 1)
        with pytest.raises(module.PublicQueryBudgetExceeded):
            store.page(kernel.subject_id, kind="interaction")
    assert store.page(kernel.subject_id, kind="interaction", limit=1)["items"]


def test_revision_capacity_retains_idempotency_and_withdrawal_reserve(
    archive: tuple[PublicArchive, SubjectKernel], monkeypatch: pytest.MonkeyPatch
) -> None:
    import noyra.interaction.public_archive as module

    store, kernel = archive
    monkeypatch.setattr(module, "MAX_PUBLIC_REVISION_ROWS", 4)
    first = store.publish(kernel.subject_id, publication(), actor="operator")
    assert store.publish(kernel.subject_id, publication(), actor="operator") == first
    for revision in (1, 2):
        store.publish(
            kernel.subject_id,
            publication(title=f"edit-{revision}", expected_revision=revision),
            actor="operator",
        )
    with pytest.raises(module.PublicationCapacityError):
        store.publish(
            kernel.subject_id, publication(title="edit-3", expected_revision=3), actor="operator"
        )
    store.withdraw(kernel.subject_id, first["id"], 3, actor="operator")
    assert store.admin(kernel.subject_id)[0]["status"] == "withdrawn"
    with pytest.raises(module.PublicationCapacityError):
        store.publish(kernel.subject_id, publication(expected_revision=4), actor="operator")


def test_empty_search_counts_bytes_and_preserves_sqlite_case_semantics(
    archive: tuple[PublicArchive, SubjectKernel], monkeypatch: pytest.MonkeyPatch
) -> None:
    import noyra.interaction.public_archive as module
    from noyra.interaction import InteractionStore

    store, kernel = archive
    InteractionStore(kernel.database).send(
        kernel.subject_id, "public:local", "peer", "ASCII 范围 Ä"
    )
    assert store.page(kernel.subject_id, kind="interaction", search="ascii")["items"]
    assert store.page(kernel.subject_id, kind="interaction", search="范围")["items"]
    assert not store.page(kernel.subject_id, kind="interaction", search="ä")["items"]
    with monkeypatch.context() as patch:
        patch.setattr(module, "MAX_PUBLIC_READ_BYTES", 1)
        with pytest.raises(module.PublicQueryBudgetExceeded):
            store.page(kernel.subject_id, kind="interaction", search="no matching text")
    assert store.page(kernel.subject_id, kind="interaction", search="ASCII")["items"]


def test_revision_byte_cap_and_legacy_over_cap_allow_withdrawal(
    archive: tuple[PublicArchive, SubjectKernel], monkeypatch: pytest.MonkeyPatch
) -> None:
    import noyra.interaction.public_archive as module

    store, kernel = archive
    item = store.publish(kernel.subject_id, publication(), actor="operator")
    monkeypatch.setattr(module, "MAX_PUBLIC_REVISION_BYTES", 1)
    with pytest.raises(module.PublicationCapacityError):
        store.publish(
            kernel.subject_id, publication(title="change", expected_revision=1), actor="operator"
        )
    store.withdraw(kernel.subject_id, item["id"], 1, actor="operator")
    assert store.admin(kernel.subject_id)[0]["revision"] == 2


def test_schema92_upgrade_preserves_private_history_and_publishes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import noyra.core.database as schema
    from noyra.core import EventStore, IdentityStore

    with monkeypatch.context() as previous:
        previous.setattr(schema, "CURRENT_SCHEMA_VERSION", 92)
        old = schema.Database(tmp_path / "old.sqlite3")
        IdentityStore(old).ensure("historical-subject", content_hash("historical"))
        EventStore(old).append("historical-subject", "private_note", "test", {"content": "private"})
    upgraded = schema.Database(old.path)
    with upgraded.connection() as connection:
        assert (
            connection.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()[0]
            == "93"
        )
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM public_publications").fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
