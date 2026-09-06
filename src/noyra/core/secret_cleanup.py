from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .at_rest import AtRestError, _is_reparse_entry, validate_private_file, validate_private_root
from .database import Database
from .errors import IntegrityError, InvalidTransitionError, NotFoundError
from .types import new_id, utc_now

_RESOURCE_SPECS: dict[str, tuple[str, str, str, str]] = {
    # resource type: table, primary key, secret-reference column, revoked state
    "transport": ("interaction_transports", "transport_id", "secret_reference", "revoked"),
    "search": ("search_provider_configs", "config_id", "key_reference", "revoked"),
    "cognitive": ("cognitive_resource_keys", "key_id", "key_reference", "revoked"),
    "embedding": ("embedding_resources", "config_id", "__derived__", "revoked"),
}

_SECRET_INTENT_STATES = frozenset(
    {"prepared", "file_ready", "committed", "pending", "failed", "removed"}
)
_SECRET_INTENT_TRANSITIONS: dict[str, frozenset[str]] = {
    "prepared": frozenset({"prepared", "file_ready", "committed", "failed", "removed"}),
    "file_ready": frozenset({"file_ready", "committed", "failed", "removed"}),
    "committed": frozenset({"committed", "failed", "removed"}),
    "pending": frozenset({"pending", "failed", "removed"}),
    "failed": frozenset({"failed", "pending", "prepared", "removed"}),
    "removed": frozenset({"removed"}),
}


class SecretCleanupQueue:
    """Durably reconcile secret files and SQLite rows without storing values.

    A cleanup queue alone only covers the database-to-file direction (delete a
    revoked file).  ``secret_file_intents`` adds the inverse direction: a
    create is journalled before the file is published and committed with the
    durable resource row.  Startup reconciliation then handles crashes at any
    point in either sequence and removes unreferenced files.
    """

    def __init__(self, database: Database):
        self.database = database

    def enqueue(
        self,
        subject_id: str,
        resource_type: str,
        resource_id: str,
        secret_reference: str,
        error: BaseException,
    ) -> None:
        now = utc_now()
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO secret_cleanup_queue(
                    task_id, subject_id, resource_type, resource_id, secret_reference,
                    status, attempts, last_error, next_retry_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?)
                ON CONFLICT(resource_type, resource_id, secret_reference) DO UPDATE SET
                    status = 'pending', last_error = excluded.last_error,
                    next_retry_at = excluded.next_retry_at, updated_at = excluded.updated_at""",
                (
                    new_id("secret-cleanup"),
                    subject_id,
                    resource_type,
                    resource_id,
                    secret_reference,
                    type(error).__name__,
                    now,
                    now,
                    now,
                ),
            )

    def prepare_create(
        self,
        subject_id: str,
        resource_type: str,
        resource_id: str,
        secret_reference: str,
        fingerprint: str | None = None,
        *,
        connection: Any | None = None,
    ) -> str:
        """Persist a create intent before writing its secret file."""
        self._validate_intent_key(resource_type, resource_id, secret_reference)
        intent_id = new_id("secret-intent")
        now = utc_now()
        if connection is None:
            with self.database.transaction() as transaction:
                return self.prepare_create(
                    subject_id,
                    resource_type,
                    resource_id,
                    secret_reference,
                    fingerprint,
                    connection=transaction,
                )
        existing = connection.execute(
            "SELECT intent_id, subject_id, fingerprint, state FROM secret_file_intents "
            "WHERE resource_type = ? AND resource_id = ? AND secret_reference = ? "
            "AND operation = 'create'",
            (resource_type, resource_id, secret_reference),
        ).fetchone()
        if existing is not None:
            if existing["subject_id"] != subject_id:
                raise IntegrityError("secret create intent crosses a subject boundary")
            existing_id = str(existing["intent_id"])
            current = str(existing["state"])
            if current == "removed":
                raise InvalidTransitionError("removed secret create intent cannot be reopened")
            if current == "pending":
                raise IntegrityError("create secret intent has an invalid pending state")
            old_fingerprint = existing["fingerprint"]
            if (
                old_fingerprint is not None
                and fingerprint is not None
                and old_fingerprint != fingerprint
            ):
                raise IntegrityError("secret create intent fingerprint conflicts")
            if current == "failed":
                self.mark(existing_id, "prepared", connection=connection)
            if current in {"prepared", "failed"}:
                connection.execute(
                    "UPDATE secret_file_intents SET fingerprint = ?, updated_at = ? "
                    "WHERE intent_id = ?",
                    (
                        old_fingerprint if fingerprint is None else fingerprint,
                        now,
                        existing_id,
                    ),
                )
            return existing_id
        connection.execute(
            """INSERT INTO secret_file_intents(
                intent_id, subject_id, resource_type, resource_id, secret_reference,
                operation, fingerprint, state, last_error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'create', ?, 'prepared', NULL, ?, ?)
            ON CONFLICT(resource_type, resource_id, secret_reference, operation)
            DO UPDATE SET subject_id = excluded.subject_id, fingerprint = excluded.fingerprint,
                state = 'prepared', last_error = NULL, updated_at = excluded.updated_at""",
            (
                intent_id,
                subject_id,
                resource_type,
                resource_id,
                secret_reference,
                fingerprint,
                now,
                now,
            ),
        )
        row = connection.execute(
            "SELECT intent_id FROM secret_file_intents WHERE resource_type = ? "
            "AND resource_id = ? AND secret_reference = ? AND operation = 'create'",
            (resource_type, resource_id, secret_reference),
        ).fetchone()
        return str(row[0])

    def prepare_delete(
        self,
        subject_id: str,
        resource_type: str,
        resource_id: str,
        secret_reference: str,
        *,
        connection: Any | None = None,
    ) -> str:
        """Persist a delete intent atomically with a revoke transaction."""
        self._validate_intent_key(resource_type, resource_id, secret_reference)
        intent_id = new_id("secret-intent")
        now = utc_now()
        if connection is None:
            with self.database.transaction() as transaction:
                return self.prepare_delete(
                    subject_id,
                    resource_type,
                    resource_id,
                    secret_reference,
                    connection=transaction,
                )
        existing = connection.execute(
            "SELECT intent_id, subject_id, state FROM secret_file_intents "
            "WHERE resource_type = ? AND resource_id = ? AND secret_reference = ? "
            "AND operation = 'delete'",
            (resource_type, resource_id, secret_reference),
        ).fetchone()
        if existing is not None:
            if existing["subject_id"] != subject_id:
                raise IntegrityError("secret delete intent crosses a subject boundary")
            existing_id = str(existing["intent_id"])
            current = str(existing["state"])
            if current == "removed":
                return existing_id
            if current == "failed":
                self.mark(existing_id, "pending", connection=connection)
            elif current != "pending":
                raise InvalidTransitionError(f"cannot prepare delete intent in state {current}")
            else:
                self.mark(existing_id, "pending", connection=connection)
            return existing_id
        connection.execute(
            """INSERT INTO secret_file_intents(
                intent_id, subject_id, resource_type, resource_id, secret_reference,
                operation, fingerprint, state, last_error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'delete', NULL, 'pending', NULL, ?, ?)
            ON CONFLICT(resource_type, resource_id, secret_reference, operation)
            DO UPDATE SET subject_id = excluded.subject_id, state = 'pending',
                last_error = NULL, updated_at = excluded.updated_at""",
            (
                intent_id,
                subject_id,
                resource_type,
                resource_id,
                secret_reference,
                now,
                now,
            ),
        )
        row = connection.execute(
            "SELECT intent_id FROM secret_file_intents WHERE resource_type = ? "
            "AND resource_id = ? AND secret_reference = ? AND operation = 'delete'",
            (resource_type, resource_id, secret_reference),
        ).fetchone()
        return str(row[0])

    def mark(
        self,
        intent_id: str,
        state: str,
        *,
        error: BaseException | None = None,
        connection: Any | None = None,
    ) -> None:
        if not isinstance(state, str) or state not in _SECRET_INTENT_STATES:
            raise ValueError("secret intent state is invalid")
        now = utc_now()
        if connection is None:
            with self.database.transaction() as transaction:
                self.mark(intent_id, state, error=error, connection=transaction)
            return
        row = connection.execute(
            "SELECT operation, state FROM secret_file_intents WHERE intent_id = ?",
            (intent_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("secret intent not found")
        current = str(row["state"])
        operation = str(row["operation"])
        allowed = _SECRET_INTENT_TRANSITIONS.get(current)
        if allowed is None:
            raise IntegrityError(f"secret intent state is invalid: {current}")
        if state not in allowed:
            raise InvalidTransitionError(
                f"secret intent transition is invalid: {current} -> {state}"
            )
        if operation == "create" and state == "pending":
            raise InvalidTransitionError("create secret intent cannot enter pending state")
        if operation == "delete" and state in {"prepared", "file_ready", "committed"}:
            raise InvalidTransitionError("delete secret intent cannot enter create-only state")
        updated = connection.execute(
            "UPDATE secret_file_intents SET state = ?, last_error = ?, updated_at = ? "
            "WHERE intent_id = ?",
            (state, None if error is None else type(error).__name__, now, intent_id),
        )
        if updated.rowcount != 1:
            raise IntegrityError("secret intent update was not applied")

    def mark_file_ready(self, intent_id: str, *, connection: Any | None = None) -> None:
        self.mark(intent_id, "file_ready", connection=connection)

    def mark_committed(self, intent_id: str, *, connection: Any | None = None) -> None:
        self.mark(intent_id, "committed", connection=connection)

    def mark_removed(self, intent_id: str, *, connection: Any | None = None) -> None:
        self.mark(intent_id, "removed", connection=connection)

    def complete_delete(
        self,
        subject_id: str,
        resource_type: str,
        resource_id: str,
        secret_reference: str,
    ) -> None:
        """Mark an atomically-persisted delete intent as completed.

        Reusing ``prepare_delete`` makes this safe for legacy rows that were
        revoked before the intent journal existed.  The upsert and terminal
        transition share one transaction, so health never observes a newly
        prepared intent after the file has already been removed.
        """
        with self.database.transaction() as connection:
            intent_id = self.prepare_delete(
                subject_id,
                resource_type,
                resource_id,
                secret_reference,
                connection=connection,
            )
            self.mark_removed(intent_id, connection=connection)
            connection.execute(
                "UPDATE secret_cleanup_queue SET status = 'removed', last_error = NULL, "
                "next_retry_at = NULL, updated_at = ? WHERE subject_id = ? "
                "AND resource_type = ? AND resource_id = ? AND secret_reference = ?",
                (
                    utc_now(),
                    subject_id,
                    resource_type,
                    resource_id,
                    secret_reference,
                ),
            )

    def record_delete_failure(
        self,
        subject_id: str,
        resource_type: str,
        resource_id: str,
        secret_reference: str,
        error: BaseException,
    ) -> None:
        """Retain legacy retry evidence without duplicating logical health.

        The delete intent is the crash-safe source of truth because it was
        committed with the revoke.  The historical cleanup queue remains a
        compatible retry/backoff mechanism and records the immediate error.
        ``health`` de-duplicates the two representations by resource/file.
        """
        self.enqueue(subject_id, resource_type, resource_id, secret_reference, error)

    def repair(
        self,
        subject_id: str | None,
        resource_type: str,
        secret_dir: Path,
        *,
        limit: int = 64,
    ) -> int:
        spec = _RESOURCE_SPECS.get(resource_type)
        if spec is None:
            raise ValueError(f"unknown secret resource type: {resource_type}")
        revoked_status = spec[3]
        bounded = max(1, min(limit, 256))
        now = utc_now()
        with self.database.connection() as connection:
            if subject_id is None:
                rows = connection.execute(
                    "SELECT * FROM secret_cleanup_queue WHERE resource_type = ? "
                    "AND status IN ('pending', 'failed') "
                    "AND (next_retry_at IS NULL OR next_retry_at <= ?) "
                    "ORDER BY updated_at, task_id LIMIT ?",
                    (resource_type, now, bounded),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM secret_cleanup_queue WHERE subject_id = ? "
                    "AND resource_type = ? AND status IN ('pending', 'failed') "
                    "AND (next_retry_at IS NULL OR next_retry_at <= ?) "
                    "ORDER BY updated_at, task_id LIMIT ?",
                    (subject_id, resource_type, now, bounded),
                ).fetchall()
        repaired = 0
        try:
            root = validate_private_root(secret_dir, create=True, label="secret root")
        except AtRestError:
            return 0
        for row in rows:
            task_id = str(row["task_id"])
            try:
                reference = str(row["secret_reference"])
                path = validate_private_file(root, reference)
                with self.database.connection() as connection:
                    owners = self._reference_owners(connection, reference)
                if any(owner[3] != revoked_status for owner in owners):
                    raise RuntimeError("secret reference is bound to an active resource")
                path.unlink(missing_ok=True)
            except (AtRestError, OSError, RuntimeError) as error:
                attempts = int(row["attempts"]) + 1
                retry_at = (
                    datetime.now(UTC) + timedelta(seconds=min(3_600, 5 * 2 ** min(attempts, 9)))
                ).isoformat(timespec="milliseconds")
                with self.database.transaction() as connection:
                    connection.execute(
                        "UPDATE secret_cleanup_queue SET status = 'failed', attempts = ?, "
                        "last_error = ?, next_retry_at = ?, updated_at = ? WHERE task_id = ?",
                        (attempts, type(error).__name__, retry_at, now, task_id),
                    )
                continue
            with self.database.transaction() as connection:
                connection.execute(
                    "UPDATE secret_cleanup_queue SET status = 'removed', attempts = attempts + 1, "
                    "last_error = NULL, next_retry_at = NULL, updated_at = ? WHERE task_id = ?",
                    (now, task_id),
                )
            repaired += 1
        # Reconcile both directions after retrying the historical deletion
        # queue.  A failure is represented durably and surfaced by integrity
        # checks; startup itself remains recoverable for revoked resources.
        self.reconcile(subject_id, resource_type, secret_dir)
        return repaired

    def reconcile(
        self,
        subject_id: str | None,
        resource_type: str,
        secret_dir: Path,
        *,
        limit: int = 256,
    ) -> dict[str, int]:
        """Perform bounded startup reconciliation for one secret directory.

        The database is authoritative for active/revoked resources.  Intent
        rows bridge crashes while publishing/deleting files; legacy rows with
        no intent are adopted as committed after their file is verified.
        """
        spec = _RESOURCE_SPECS.get(resource_type)
        if spec is None:
            raise ValueError(f"unknown secret resource type: {resource_type}")
        try:
            root = validate_private_root(secret_dir, create=True, label="secret root")
        except AtRestError:
            # The caller's integrity gate will report the unsafe root.  Do not
            # follow a link or attempt repair through it.
            return {"committed": 0, "removed": 0, "failed": 1, "orphans": 0}
        bounded = max(1, min(limit, 1_024))
        table, id_column, reference_column, revoked_status = spec
        try:
            with self.database.connection() as connection:
                subject_clause = "" if subject_id is None else " AND r.subject_id = ?"
                values: tuple[Any, ...] = () if subject_id is None else (subject_id,)
                reference_expression = (
                    f"r.{id_column} || '.key'"
                    if reference_column == "__derived__"
                    else f"r.{reference_column}"
                )
                rows = connection.execute(
                    f"SELECT r.* FROM {table} r WHERE 1 = 1{subject_clause} "
                    "ORDER BY CASE WHEN NOT EXISTS ("
                    "SELECT 1 FROM secret_file_intents i WHERE i.resource_type = ? "
                    f"AND i.resource_id = r.{id_column} "
                    f"AND i.secret_reference = {reference_expression} "
                    "AND i.operation = CASE WHEN r.status = ? THEN 'delete' ELSE 'create' END"
                    ") THEN 0 ELSE 1 END, r.rowid LIMIT ?",
                    (*values, resource_type, revoked_status, bounded + 1),
                ).fetchall()
                intent_clause = "" if subject_id is None else " AND subject_id = ?"
                intents = connection.execute(
                    "SELECT * FROM secret_file_intents WHERE resource_type = ?"
                    + intent_clause
                    + " ORDER BY CASE WHEN state IN "
                    "('prepared', 'file_ready', 'pending', 'failed') THEN 0 ELSE 1 END, "
                    "updated_at, intent_id LIMIT ?",
                    (resource_type, *values, bounded + 1),
                ).fetchall()
        except sqlite3.OperationalError:
            # Databases created by very old runtimes may not have the optional
            # table until their next initialize pass.
            return {"committed": 0, "removed": 0, "failed": 0, "orphans": 0}

        rows = rows[:bounded]
        intents = intents[:bounded]
        db_rows: dict[tuple[str, str], Any] = {}
        for row in rows:
            if reference_column == "__derived__":
                reference: Any = f"{row[id_column]}.key"
            else:
                reference = row[reference_column] if reference_column in row.keys() else None  # noqa: SIM118
            if not isinstance(reference, str) or not reference:
                continue
            db_rows[(str(row[id_column]), reference)] = row

        committed = removed = failed = orphans = 0
        # First make sure every durable row has a path and a committed intent.
        for (resource_id, reference), row in db_rows.items():
            status = str(row["status"])
            try:
                path = validate_private_file(root, reference)
            except AtRestError as error:
                self._record_failure(
                    subject_id or str(row["subject_id"]),
                    resource_type,
                    resource_id,
                    reference,
                    error,
                )
                failed += 1
                continue
            if status == revoked_status:
                self._ensure_delete_intent(
                    str(row["subject_id"]), resource_type, resource_id, reference
                )
                continue
            fingerprint = row["key_fingerprint"] if "key_fingerprint" in row.keys() else None  # noqa: SIM118
            existing = next(
                (
                    item
                    for item in intents
                    if item["resource_type"] == resource_type
                    and item["resource_id"] == resource_id
                    and item["secret_reference"] == reference
                    and item["operation"] == "create"
                ),
                None,
            )
            if existing is None:
                with self.database.connection() as connection:
                    existing = connection.execute(
                        "SELECT intent_id FROM secret_file_intents WHERE resource_type = ? "
                        "AND resource_id = ? AND secret_reference = ? AND operation = 'create'",
                        (resource_type, resource_id, reference),
                    ).fetchone()
                if existing is None:
                    self.prepare_create(
                        str(row["subject_id"]), resource_type, resource_id, reference, fingerprint
                    )

        # The first pass may adopt legacy rows by creating intents.  Re-read
        # the bounded journal so those intents are applied during this same
        # startup rather than leaving health degraded until another restart.
        with self.database.connection() as connection:
            intent_clause = "" if subject_id is None else " AND subject_id = ?"
            values = () if subject_id is None else (subject_id,)
            intents = connection.execute(
                "SELECT * FROM secret_file_intents WHERE resource_type = ?"
                + intent_clause
                + " ORDER BY CASE WHEN state IN "
                "('prepared', 'file_ready', 'pending', 'failed') THEN 0 ELSE 1 END, "
                "updated_at, intent_id LIMIT ?",
                (resource_type, *values, bounded + 1),
            ).fetchall()
        intents = intents[:bounded]

        # Then apply pending intents.  Create intents without a durable row are
        # crash orphans and must not survive into backups.
        for intent in intents:
            reference = str(intent["secret_reference"])
            try:
                path = validate_private_file(root, reference)
            except AtRestError as error:
                self.mark(str(intent["intent_id"]), "failed", error=error)
                failed += 1
                continue
            key = (str(intent["resource_id"]), reference)
            row = db_rows.get(key)
            if row is None:
                # Row and intent scans have independent bounds/orderings.  An
                # intent outside the bounded row prefix must be checked by
                # primary key before it can be classified as an orphan.
                with self.database.connection() as connection:
                    candidate = connection.execute(
                        f"SELECT * FROM {table} WHERE {id_column} = ? LIMIT 1",
                        (str(intent["resource_id"]),),
                    ).fetchone()
                if candidate is not None and candidate["subject_id"] != intent["subject_id"]:
                    self.mark(
                        str(intent["intent_id"]),
                        "failed",
                        error=RuntimeError("secret intent crosses a subject boundary"),
                    )
                    failed += 1
                    continue
                if candidate is not None:
                    candidate_reference = (
                        f"{candidate[id_column]}.key"
                        if reference_column == "__derived__"
                        else candidate[reference_column]
                    )
                    if candidate_reference == reference:
                        row = candidate
                        db_rows[key] = candidate
            with self.database.connection() as connection:
                owners = self._reference_owners(connection, reference)
            expected_owner = (
                resource_type,
                str(intent["resource_id"]),
                str(intent["subject_id"]),
            )
            conflicting_owners = [owner for owner in owners if owner[:3] != expected_owner]
            if conflicting_owners:
                # Never unlink a path when another durable resource (possibly
                # from another subject/domain) owns the same reference.  A
                # malformed intent is quarantined for integrity repair instead
                # of being allowed to delete a live credential.
                self.mark(
                    str(intent["intent_id"]),
                    "failed",
                    error=RuntimeError("secret reference is bound to another resource"),
                )
                failed += 1
                continue
            operation = str(intent["operation"])
            if operation == "delete":
                if row is not None and str(row["status"]) != revoked_status:
                    self.mark(
                        str(intent["intent_id"]),
                        "failed",
                        error=RuntimeError("delete intent conflicts with an active resource"),
                    )
                    failed += 1
                    continue
                if path.exists():
                    try:
                        path.unlink()
                        removed += 1
                    except OSError as error:
                        self.mark(str(intent["intent_id"]), "failed", error=error)
                        failed += 1
                        continue
                self.complete_delete(
                    str(intent["subject_id"]),
                    resource_type,
                    str(intent["resource_id"]),
                    reference,
                )
                continue
            if row is None or str(row["status"]) == revoked_status:
                if path.exists():
                    try:
                        path.unlink()
                        removed += 1
                    except OSError as error:
                        self.mark(str(intent["intent_id"]), "failed", error=error)
                        failed += 1
                        continue
                self.mark(str(intent["intent_id"]), "removed")
                continue
            if path.exists():
                if intent["state"] != "committed":
                    if intent["state"] == "failed":
                        # A create intent may be marked failed when its file is
                        # missing during startup.  If a later repair restores
                        # the file, re-enter the create workflow explicitly;
                        # ``failed -> committed`` is intentionally not a valid
                        # direct state transition.
                        self.mark(str(intent["intent_id"]), "prepared")
                    self.mark(str(intent["intent_id"]), "committed")
                    committed += 1
            else:
                if intent["state"] != "failed":
                    self.mark(str(intent["intent_id"]), "failed", error=FileNotFoundError())
                failed += 1

        # A bounded row/intent prefix cannot prove a file is orphaned.  Walk
        # the directory lazily and check each top-level name against durable
        # resource ownership instead.  Deletions remain bounded, while live
        # files before an orphan no longer starve its cleanup on every restart.
        with self.database.connection() as connection:
            protected_intent_refs = {
                str(row["secret_reference"])
                for row in connection.execute(
                    "SELECT secret_reference FROM secret_file_intents "
                    "WHERE state IN ('prepared', 'file_ready', 'committed', 'pending', 'failed')"
                ).fetchall()
            }
            for path in self._files(root):
                if path.name in protected_intent_refs:
                    continue
                owners = (
                    self._reference_owners(connection, path.name) if path.parent == root else []
                )
                if any(owner[3] != revoked_status for owner in owners):
                    continue
                try:
                    path.unlink()
                    orphans += 1
                except OSError as error:
                    # There is no trusted resource id for an orphan; leave it for
                    # the next pass and let the private-root integrity check fail.
                    del error
                    failed += 1
                if orphans >= bounded:
                    break
        return {"committed": committed, "removed": removed, "failed": failed, "orphans": orphans}

    def _ensure_delete_intent(
        self, subject_id: str, resource_type: str, resource_id: str, reference: str
    ) -> None:
        self.prepare_delete(subject_id, resource_type, resource_id, reference)

    @staticmethod
    def _reference_owners(connection: Any, reference: str) -> list[tuple[str, str, str, str]]:
        """Return every durable resource that currently claims a file reference."""
        rows = connection.execute(
            "SELECT 'transport' AS resource_type, transport_id AS resource_id, "
            "subject_id, status FROM interaction_transports WHERE secret_reference = ? "
            "UNION ALL SELECT 'search', config_id, subject_id, status "
            "FROM search_provider_configs WHERE key_reference = ? "
            "UNION ALL SELECT 'cognitive', key_id, subject_id, status "
            "FROM cognitive_resource_keys WHERE key_reference = ? "
            "UNION ALL SELECT 'embedding', config_id, subject_id, status "
            "FROM embedding_resources WHERE config_id || '.key' = ?",
            (reference, reference, reference, reference),
        ).fetchall()
        return [
            (
                str(row["resource_type"]),
                str(row["resource_id"]),
                str(row["subject_id"]),
                str(row["status"]),
            )
            for row in rows
        ]

    @staticmethod
    def _validate_intent_key(resource_type: str, resource_id: str, secret_reference: str) -> None:
        if resource_type not in _RESOURCE_SPECS:
            raise ValueError("unknown secret resource type")
        if not isinstance(resource_id, str) or not resource_id.strip():
            raise ValueError("secret resource identifier is invalid")
        if (
            not isinstance(secret_reference, str)
            or not secret_reference
            or Path(secret_reference).name != secret_reference
            or secret_reference in {".", ".."}
            or "/" in secret_reference
            or "\\" in secret_reference
        ):
            raise ValueError("secret reference is invalid")

    def _record_failure(
        self,
        subject_id: str,
        resource_type: str,
        resource_id: str,
        reference: str,
        error: BaseException,
    ) -> None:
        try:
            intent_id = self.prepare_create(subject_id, resource_type, resource_id, reference)
            self.mark(intent_id, "failed", error=error)
        except Exception:
            # Integrity will still report the missing path; avoid masking the
            # original startup failure with a journal write failure.
            pass

    @staticmethod
    def _files(root: Path) -> Iterator[Path]:
        """Yield non-symlink files without materializing the whole secret tree."""
        pending = [root]
        while pending:
            current = pending.pop()
            try:
                entries = sorted(current.iterdir(), key=lambda item: item.name)
            except OSError:
                continue
            for path in entries:
                # ``is_symlink`` misses Windows junctions and other reparse
                # points.  Never descend through an entry that the private
                # storage contract would reject.
                if _is_reparse_entry(path):
                    continue
                try:
                    if path.is_dir():
                        pending.append(path)
                    elif path.is_file():
                        yield path
                except OSError:
                    continue

    def pending(self, subject_id: str, resource_type: str | None = None) -> int:
        with self.database.connection() as connection:
            if resource_type is None:
                queue_rows = connection.execute(
                    "SELECT resource_type, resource_id, secret_reference "
                    "FROM secret_cleanup_queue WHERE subject_id = ? "
                    "AND status IN ('pending', 'failed')",
                    (subject_id,),
                ).fetchall()
                intent_rows = connection.execute(
                    "SELECT resource_type, resource_id, secret_reference "
                    "FROM secret_file_intents WHERE subject_id = ? "
                    "AND state IN ('prepared', 'file_ready', 'pending', 'failed')",
                    (subject_id,),
                ).fetchall()
            else:
                queue_rows = connection.execute(
                    "SELECT resource_type, resource_id, secret_reference "
                    "FROM secret_cleanup_queue WHERE subject_id = ? "
                    "AND resource_type = ? AND status IN ('pending', 'failed')",
                    (subject_id, resource_type),
                ).fetchall()
                intent_rows = connection.execute(
                    "SELECT resource_type, resource_id, secret_reference "
                    "FROM secret_file_intents WHERE subject_id = ? "
                    "AND resource_type = ? "
                    "AND state IN ('prepared', 'file_ready', 'pending', 'failed')",
                    (subject_id, resource_type),
                ).fetchall()
        return len(
            {
                (str(row["resource_type"]), str(row["resource_id"]), str(row["secret_reference"]))
                for row in (*queue_rows, *intent_rows)
            }
        )

    def health(self, subject_id: str, resource_type: str) -> dict[str, int | str | None]:
        """Return bounded, secret-free repair health for one resource domain."""
        with self.database.connection() as connection:
            queue_rows = connection.execute(
                "SELECT resource_type, resource_id, secret_reference, status, attempts, created_at "
                "FROM secret_cleanup_queue WHERE subject_id = ? AND resource_type = ? "
                "AND status IN ('pending', 'failed')",
                (subject_id, resource_type),
            ).fetchall()
            try:
                intent_rows = connection.execute(
                    "SELECT resource_type, resource_id, secret_reference, state, created_at "
                    "FROM secret_file_intents WHERE subject_id = ? AND resource_type = ? "
                    "AND state IN ('prepared', 'file_ready', 'pending', 'failed')",
                    (subject_id, resource_type),
                ).fetchall()
            except sqlite3.OperationalError:
                intent_rows = []
        entries: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in queue_rows:
            key = (
                str(row["resource_type"]),
                str(row["resource_id"]),
                str(row["secret_reference"]),
            )
            entries[key] = {
                "failed": row["status"] == "failed",
                "attempts": int(row["attempts"]),
                "created_at": str(row["created_at"]),
            }
        for row in intent_rows:
            key = (
                str(row["resource_type"]),
                str(row["resource_id"]),
                str(row["secret_reference"]),
            )
            entry = entries.setdefault(
                key,
                {"failed": False, "attempts": 0, "created_at": str(row["created_at"])},
            )
            entry["failed"] = bool(entry["failed"] or row["state"] == "failed")
            entry["created_at"] = min(str(entry["created_at"]), str(row["created_at"]))
        pending = len(entries)
        return {
            "status": "degraded" if pending else "ok",
            "pending": pending,
            "failed": sum(int(bool(entry["failed"])) for entry in entries.values()),
            "max_attempts": max((int(entry["attempts"]) for entry in entries.values()), default=0),
            "oldest_pending_at": min(
                (str(entry["created_at"]) for entry in entries.values()), default=None
            ),
        }
