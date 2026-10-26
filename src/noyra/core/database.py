# ruff: noqa: E501
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from .admission import current_commit_scope, is_accounting_scope
from .errors import RuntimeOwnershipError, SchemaContractError
from .locking import ProcessLock
from .private_erasure_schema import (
    PRIVATE_ROUTE_PUSH_COLUMNS,
)
from .private_sources import assert_private_inputs_available
from .relationship_identity import backfill_legacy_bindings
from .resource_policy import (
    LIMIT_DIMENSIONS,
    read_resource_policy,
    write_resource_policy,
)
from .schema.base import CURRENT_SCHEMA_VERSION as CURRENT_SCHEMA_VERSION
from .schema.base import SCHEMA_SQL as SCHEMA_SQL
from .schema.contracts import _PERSISTENT_FEATURE_MIN_SCHEMA as _PERSISTENT_FEATURE_MIN_SCHEMA
from .schema.contracts import _PERSISTENT_FEATURE_OBJECTS as _PERSISTENT_FEATURE_OBJECTS
from .schema.contracts import _SCHEMA_DDL_FINGERPRINTS as _SCHEMA_DDL_FINGERPRINTS
from .schema.contracts import _SCHEMA_STRUCTURE_FINGERPRINTS as _SCHEMA_STRUCTURE_FINGERPRINTS
from .schema.contracts import _canonical_schema_sql as _canonical_schema_sql
from .schema.evidence import action_state_hash as action_state_hash
from .schema.evidence import behavior_log_state_hash as behavior_log_state_hash
from .schema.evidence import goal_origin_evidence_hash as goal_origin_evidence_hash
from .schema.evidence import (
    legacy_wallet_payment_policy_state_hash_v65 as legacy_wallet_payment_policy_state_hash_v65,
)
from .schema.evidence import public_post_identity_hash as public_post_identity_hash
from .schema.evidence import wallet_payment_policy_state_hash as wallet_payment_policy_state_hash
from .schema.evidence import wallet_submission_state_hash as wallet_submission_state_hash
from .schema.migrations import MIGRATIONS as MIGRATIONS
from .types import canonical_json, content_hash, new_id, strict_int, utc_now
from .usage_schema import ATTEMPT_USAGE_COLUMNS
from .wallet_schema import (
    WalletLegacyApproval,
    legacy_entry_hash,
    legacy_journal_hash,
    wallet_entry_hash,
    wallet_journal_hash,
    wallet_upgrade_fingerprint,
)

# Compatibility exports preserve historical imports. Execution and ownership
# remain on this facade; schema modules never open database connections.


class Database:
    """Small SQLite boundary with WAL and explicit transactions."""

    _SNAPSHOT_STALE_AFTER_SECONDS = 60 * 60

    def __init__(
        self,
        path: Path | str,
        *,
        initialize: bool = True,
        read_only: bool = False,
        strict_schema_contract: bool = False,
    ):
        self.path = Path(path).resolve()
        self.read_only = read_only
        self.strict_schema_contract = strict_schema_contract
        if initialize:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.initialize()
        elif not self.path.is_file():
            raise FileNotFoundError(self.path)
        elif strict_schema_contract:
            self.assert_schema_contract()

    def _connect(self) -> sqlite3.Connection:
        if self.read_only:
            connection = sqlite3.connect(
                f"{self.path.as_uri()}?mode=ro&immutable=1",
                timeout=30,
                check_same_thread=False,
                uri=True,
            )
        else:
            connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        # REPLACE conflict resolution performs an implicit DELETE.  Recursive
        # triggers must be enabled so append-only evidence guards also cover
        # that implicit path instead of allowing a tampered intent row to be
        # replaced without firing its DELETE trigger.
        connection.execute("PRAGMA recursive_triggers = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        if self.read_only:
            connection.execute("PRAGMA query_only = ON")
        return connection

    def promote_to_writable(self) -> None:
        """Switch a preflight read-only facade to the owned database boundary."""
        self.read_only = False

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with current_commit_scope():
                connection.execute("BEGIN IMMEDIATE")
                if not is_accounting_scope():
                    assert_private_inputs_available(connection)
                yield connection
                connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _execute_sql_script(connection: sqlite3.Connection, script: str) -> None:
        """Execute a SQL script without breaking the caller's transaction.

        ``Connection.executescript`` commits any transaction that is already
        open before running the script.  Database initialization and feature
        repair both rely on all DDL and backfills being atomic, so split the
        script at complete SQLite statements and execute each one through the
        normal connection API instead.
        """
        statement: list[str] = []
        for character in script:
            statement.append(character)
            if character != ";":
                continue
            candidate = "".join(statement)
            if not sqlite3.complete_statement(candidate):
                continue
            sql = candidate.strip()
            if sql:
                connection.execute(sql)
            statement.clear()

        remainder = "".join(statement).strip()
        if not remainder or all(
            not line.strip() or line.lstrip().startswith("--") for line in remainder.splitlines()
        ):
            return
        if not sqlite3.complete_statement(remainder):
            raise sqlite3.OperationalError("incomplete SQL script")
        connection.execute(remainder)

    @contextmanager
    def read_transaction(self) -> Iterator[sqlite3.Connection]:
        """Hold one WAL snapshot across a multi-query integrity check."""
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def read_snapshot(
        self, *, checkpoint: Callable[[], None] | None = None
    ) -> Iterator[sqlite3.Connection]:
        """Yield an isolated SQLite backup for long-running read-only work.

        A normal ``read_transaction`` keeps a WAL snapshot open until the
        caller finishes serialising its result.  That is appropriate for a
        short integrity check, but a multi-gigabyte export can otherwise hold
        the WAL checkpoint hostage for the entire duration of compression.
        The backup API holds the live connection only while copying the
        database, then all subsequent reads happen against a private,
        same-volume temporary image.  Callers must not write through the
        returned connection; any audit/publication writes stay on the live
        :class:`Database` instance.
        """
        self._cleanup_stale_snapshots()
        snapshot_path = self.path.with_name(f".{self.path.name}.{new_id('snapshot')}.sqlite")
        snapshot_lock_path = snapshot_path.with_name(snapshot_path.name + ".lock")
        snapshot_lock = ProcessLock(snapshot_lock_path)
        lock_held = False
        source: sqlite3.Connection | None = None
        snapshot: sqlite3.Connection | None = None
        try:
            if checkpoint is not None:
                checkpoint()
            snapshot_lock.acquire()
            lock_held = True
            source = self._connect()
            snapshot = sqlite3.connect(snapshot_path, timeout=30, check_same_thread=False)
            snapshot.row_factory = sqlite3.Row
            snapshot.execute("PRAGMA foreign_keys = ON")
            snapshot.execute("PRAGMA busy_timeout = 30000")
            if checkpoint is None:
                source.backup(snapshot)
            else:
                source.backup(
                    snapshot,
                    pages=64,
                    progress=lambda _status, _remaining, _total: checkpoint(),
                )
                checkpoint()
            snapshot.commit()
            source.close()
            source = None
            snapshot.execute("PRAGMA query_only = ON")
            yield snapshot
        finally:
            if snapshot is not None:
                snapshot.close()
            if source is not None:
                source.close()
            if lock_held:
                snapshot_lock.release()
            cleanup_paths = [
                snapshot_path,
                snapshot_path.with_name(snapshot_path.name + "-wal"),
                snapshot_path.with_name(snapshot_path.name + "-shm"),
            ]
            if lock_held:
                cleanup_paths.append(snapshot_lock_path)
            for cleanup_path in cleanup_paths:
                with suppress(OSError):
                    cleanup_path.unlink(missing_ok=True)

    def _cleanup_stale_snapshots(self) -> int:
        """Remove abandoned, unlocked backup images left by a crashed export.

        Snapshot images are derived data, but they can be as large as the live
        database.  Only files older than the grace period are considered, and
        an exclusive probe protects an in-progress export from being removed.
        A failed probe is retained for a later maintenance pass.
        """
        pattern = f".{self.path.name}.snapshot_*.sqlite"
        cutoff = time.time() - self._SNAPSHOT_STALE_AFTER_SECONDS
        removed = 0
        for candidate in self.path.parent.glob(pattern):
            try:
                if not candidate.is_file() or candidate.stat().st_mtime > cutoff:
                    continue
            except OSError:
                continue
            if not self._snapshot_is_idle(candidate):
                continue
            try:
                candidate.unlink()
                for suffix in ("-wal", "-shm"):
                    candidate.with_name(candidate.name + suffix).unlink(missing_ok=True)
                removed += 1
            except OSError:
                continue
        return removed

    @staticmethod
    def _snapshot_is_idle(path: Path) -> bool:
        """Return whether an old snapshot can be acquired exclusively."""
        lock_path = path.with_name(path.name + ".lock")
        lock = ProcessLock(lock_path)
        try:
            lock.acquire()
        except (RuntimeOwnershipError, OSError):
            return False
        try:
            connection: sqlite3.Connection | None = None
            try:
                connection = sqlite3.connect(path, timeout=0.05)
                connection.execute("PRAGMA busy_timeout = 50")
                connection.execute("BEGIN EXCLUSIVE")
                connection.rollback()
                return True
            except sqlite3.OperationalError as error:
                return not any(token in str(error).casefold() for token in ("locked", "busy"))
            except sqlite3.DatabaseError:
                # A malformed orphan is still derived data and has no live reader
                # that can be harmed by cleanup.
                return True
            finally:
                if connection is not None:
                    connection.close()
        finally:
            lock.release()
            with suppress(OSError):
                lock_path.unlink(missing_ok=True)

    def initialize(self, *, wallet_legacy_approval: WalletLegacyApproval | None = None) -> None:
        version = self._preflight_schema_version()
        # Schema 60 is also a legitimate starting point for the chronology
        # hardening migration.  It contains the complete policy/ledger hash
        # backfill, but payment order/execution timestamps are still on the
        # legacy hash contract and therefore require the same fingerprint-
        # bound approval before they can be upgraded.
        if wallet_legacy_approval is not None and version not in {59, 60}:
            raise RuntimeError("explicit wallet upgrade approval requires schema 59 or 60")
        backup_path = (
            None
            if version is None or version >= CURRENT_SCHEMA_VERSION
            else self._create_migration_backup(version)
        )
        try:
            with self.connection() as connection:
                if version is not None and version < CURRENT_SCHEMA_VERSION:
                    connection.execute("PRAGMA journal_mode = DELETE")
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    self._execute_sql_script(connection, SCHEMA_SQL)
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
            self._migrate(wallet_legacy_approval=wallet_legacy_approval)
            self._ensure_optional_features()
            with self.transaction() as connection:
                self._upgrade_wallet_payment_policy_automation(connection)
            self._ensure_training_policies()
            self._validate_schema_contract(strict=self.strict_schema_contract)
            with self.connection() as connection:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = FULL")
                connection.commit()
        except Exception:
            if backup_path is not None:
                try:
                    self._restore_migration_backup(backup_path)
                except Exception as restore_error:
                    raise RuntimeError(
                        "database migration failed and pre-migration restore failed"
                    ) from restore_error
            raise
        # A pre-migration image is rollback state, not retained history.
        # Cleanup happens only after the complete initialization path has
        # succeeded and, critically, outside the migration rollback handler.
        # If cleanup itself fails after deleting one image, the upgraded
        # database remains authoritative instead of attempting an impossible
        # restore from a backup that may no longer exist.
        self._cleanup_migration_backups()

    def _cleanup_migration_backups(self) -> int:
        """Best-effort removal of rollback images after successful startup."""
        removed = 0
        pattern = f"{self.path.name}.pre-migration-v*.bak"
        for candidate in self.path.parent.glob(pattern):
            try:
                metadata = candidate.lstat()
                if not stat.S_ISREG(metadata.st_mode):
                    continue
                candidate.unlink(missing_ok=True)
            except OSError:
                # Keep an operator-visible rollback image for manual cleanup;
                # migration has already completed and must remain authoritative.
                continue
            removed += 1
        return removed

    @staticmethod
    def schema_ddl_fingerprint(connection: sqlite3.Connection) -> str:
        """Fingerprint the complete persistent DDL inventory."""
        return Database._schema_fingerprint(connection, include_triggers=True)

    @staticmethod
    def schema_structure_fingerprint(connection: sqlite3.Connection) -> str:
        """Fingerprint tables, indexes, and views without trigger definitions."""
        return Database._schema_fingerprint(connection, include_triggers=False)

    @staticmethod
    def _schema_fingerprint(connection: sqlite3.Connection, *, include_triggers: bool) -> str:
        shadow_tables = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_list").fetchall()
            if str(row["type"]) == "shadow"
        }
        definitions = [
            {
                "type": str(row["type"]),
                "name": str(row["name"]),
                "sql": _canonical_schema_sql(str(row["sql"])),
            }
            for row in connection.execute(
                """SELECT type, name, sql FROM sqlite_master
                   WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'
                   ORDER BY type, name"""
            ).fetchall()
            if str(row["name"]) not in shadow_tables
            and (include_triggers or str(row["type"]) != "trigger")
        ]
        return content_hash(definitions)

    @staticmethod
    def _persistent_feature_ddl_fingerprint(
        connection: sqlite3.Connection, object_names: tuple[str, ...]
    ) -> str:
        definitions: list[dict[str, str]] = []
        for object_name in object_names:
            row = connection.execute(
                "SELECT type, name, sql FROM sqlite_master WHERE name=?", (object_name,)
            ).fetchone()
            if row is None or not row["sql"]:
                raise RuntimeError(
                    f"persistent schema feature is missing database object {object_name}"
                )
            definitions.append(
                {"type": str(row["type"]), "name": str(row["name"]), "sql": str(row["sql"])}
            )
        return content_hash(definitions)

    def require_persistent_feature(self, connection: sqlite3.Connection, feature_id: str) -> None:
        """Fail closed when a module's persistent schema feature has drifted."""
        object_names = _PERSISTENT_FEATURE_OBJECTS.get(feature_id)
        if object_names is None:
            raise RuntimeError(f"unknown persistent schema feature {feature_id}")
        fingerprint = self._persistent_feature_ddl_fingerprint(connection, object_names)
        marker = connection.execute(
            "SELECT feature_version, ddl_fingerprint FROM persistent_features WHERE feature_id=?",
            (feature_id,),
        ).fetchone()
        if (
            marker is None
            or int(marker["feature_version"]) != 1
            or str(marker["ddl_fingerprint"]) != fingerprint
        ):
            raise RuntimeError(f"persistent schema feature {feature_id} contract mismatch")

    def assert_schema_contract(self) -> None:
        """Check the persisted contract without changing the database.

        The installer uses this read-only form after a failed pointer switch so
        an older release can prove that its database is still usable before it
        is started again.
        """
        with self.connection() as connection:
            self._validate_schema_contract_connection(connection, strict=True, persist_marker=False)

    def _validate_schema_contract(self, *, strict: bool = False) -> None:
        """Verify the complete versioned DDL inventory before opening service."""
        with self.transaction() as connection:
            self._validate_schema_contract_connection(
                connection, strict=strict, persist_marker=True
            )

    def _validate_schema_contract_connection(
        self,
        connection: sqlite3.Connection,
        *,
        strict: bool,
        persist_marker: bool,
    ) -> None:
        version_row = connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        if version_row is None:
            raise SchemaContractError("schema contract mismatch: schema version is missing")
        try:
            version = int(version_row[0])
        except (TypeError, ValueError) as error:
            raise SchemaContractError(
                "schema contract mismatch: schema version is invalid"
            ) from error
        expected = _SCHEMA_DDL_FINGERPRINTS.get(version)
        latest_contract_version = max(_SCHEMA_DDL_FINGERPRINTS)
        if expected is None and version < latest_contract_version:
            # Historical handoff images are checked after they reach a reviewed
            # contract.  Their migration steps still validate any version that
            # has a published fingerprint.
            return
        if expected is None or len(expected) != 64 or set(expected) - set("0123456789abcdef"):
            raise SchemaContractError(
                f"schema contract mismatch: contract unavailable for schema {version}"
            )
        structure_expected = _SCHEMA_STRUCTURE_FINGERPRINTS.get(version)
        if (
            structure_expected is None
            or len(structure_expected) != 64
            or set(structure_expected) - set("0123456789abcdef")
        ):
            raise SchemaContractError(
                f"schema contract mismatch: structural contract unavailable for schema {version}"
            )
        structure_actual = self.schema_structure_fingerprint(connection)
        if structure_actual != structure_expected:
            raise SchemaContractError(
                f"schema contract mismatch: structural mismatch for schema {version}: "
                f"expected {structure_expected}, found {structure_actual}"
            )
        actual = self.schema_ddl_fingerprint(connection)
        if strict and actual != expected:
            raise SchemaContractError(
                f"schema contract mismatch: DDL mismatch for schema {version}: "
                f"expected {expected}, found {actual}"
            )
        previous = connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_ddl_contract'"
        ).fetchone()
        if previous is not None:
            try:
                previous_version_text, previous_fingerprint = str(previous[0]).split(":", 1)
                previous_version = int(previous_version_text)
            except (ValueError, TypeError) as error:
                raise SchemaContractError("schema contract mismatch: marker is invalid") from error
            if previous_version > version:
                raise SchemaContractError("schema contract mismatch: marker is newer than runtime")
            if previous_version == version and previous_fingerprint != expected:
                raise SchemaContractError(
                    "schema contract marker mismatch (schema_contract_mismatch): "
                    "marker does not match installed DDL"
                )

        if strict:
            self._validate_persistent_feature_markers(connection, version)
        if persist_marker:
            connection.execute(
                "INSERT INTO schema_meta(key, value) VALUES ('schema_ddl_contract', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (f"{version}:{expected}",),
            )

    def _validate_persistent_feature_markers(
        self, connection: sqlite3.Connection, schema_version: int
    ) -> None:
        """Check every installed feature marker against its actual objects."""
        if (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='persistent_features'"
            ).fetchone()
            is None
        ):
            if schema_version < min(_PERSISTENT_FEATURE_MIN_SCHEMA.values(), default=0):
                return
            raise SchemaContractError(
                "schema contract mismatch: persistent feature registry is missing"
            )
        rows = connection.execute(
            "SELECT feature_id, feature_version, ddl_fingerprint FROM persistent_features "
            "ORDER BY feature_id"
        ).fetchall()
        for row in rows:
            feature_id = str(row["feature_id"])
            object_names = _PERSISTENT_FEATURE_OBJECTS.get(feature_id)
            if object_names is None:
                raise SchemaContractError(
                    f"schema contract mismatch: unknown persistent feature {feature_id}"
                )
            minimum = _PERSISTENT_FEATURE_MIN_SCHEMA.get(feature_id, 0)
            if schema_version < minimum:
                raise SchemaContractError(
                    f"schema contract mismatch: feature {feature_id} is ahead of schema"
                )
            if int(row["feature_version"]) != 1:
                raise SchemaContractError(
                    f"schema contract mismatch: persistent feature {feature_id} version is invalid"
                )
            actual = self._persistent_feature_ddl_fingerprint(connection, object_names)
            if str(row["ddl_fingerprint"]) != actual:
                raise SchemaContractError(
                    f"schema contract mismatch: persistent feature {feature_id} marker differs"
                )

    def _validate_schema_stage_contract(
        self, connection: sqlite3.Connection, schema_version: int
    ) -> None:
        """Validate a reviewed migration stage before publishing its marker."""
        # Historical fixtures intentionally rewind a current database marker to
        # replay an older migration.  The v80 boundary is the first reviewed
        # handoff that must be checked on a real legacy image; later stages are
        # validated by the final current-schema contract below.
        if (
            not self.strict_schema_contract
            or schema_version != 80
            or getattr(self, "_migration_origin_version", None) != 71
        ):
            return
        expected = _SCHEMA_DDL_FINGERPRINTS.get(schema_version)
        structure_expected = _SCHEMA_STRUCTURE_FINGERPRINTS.get(schema_version)
        if expected is None or structure_expected is None:
            return
        actual_structure = self.schema_structure_fingerprint(connection)
        actual_ddl = self.schema_ddl_fingerprint(connection)
        if actual_structure != structure_expected or actual_ddl != expected:
            raise SchemaContractError(
                f"schema contract mismatch: migration stage {schema_version} does not match "
                f"its reviewed DDL and structure fingerprints; found {actual_ddl}"
            )
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='persistent_features'"
        ).fetchone():
            self._validate_persistent_feature_markers(connection, schema_version)

    @staticmethod
    def _ensure_archive_transfer_claim_columns(connection: sqlite3.Connection) -> None:
        """Add v43 queue fencing columns idempotently for repaired markers."""
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(archive_transfer_queue)")
        }
        for name in ("claim_token", "lease_owner", "lease_expires_at"):
            if name not in columns:
                connection.execute(f"ALTER TABLE archive_transfer_queue ADD COLUMN {name} TEXT")

    @staticmethod
    def _ensure_transport_endpoint_columns(connection: sqlite3.Connection) -> None:
        """Add endpoint integrity metadata without assuming a pristine v49 row."""

        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(interaction_transports)")
        }
        if "endpoint_contract" not in columns:
            connection.execute(
                "ALTER TABLE interaction_transports ADD COLUMN endpoint_contract TEXT "
                "NOT NULL DEFAULT 'legacy_origin' "
                "CHECK (endpoint_contract IN ('legacy_origin', 'digest_v1'))"
            )
        if "endpoint_digest" not in columns:
            connection.execute("ALTER TABLE interaction_transports ADD COLUMN endpoint_digest TEXT")

    @staticmethod
    def _ensure_inbound_reply_columns(connection: sqlite3.Connection) -> None:
        """Add provider-specific reply routing metadata idempotently."""

        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(interaction_inbound_events)")
        }
        if "external_thread_id" not in columns:
            connection.execute(
                "ALTER TABLE interaction_inbound_events ADD COLUMN external_thread_id TEXT"
            )
        if "reply_selector" not in columns:
            connection.execute(
                "ALTER TABLE interaction_inbound_events ADD COLUMN reply_selector TEXT "
                "CHECK (reply_selector IS NULL OR reply_selector IN "
                "('qq:user', 'qq:group', 'qq:channel', 'qq:dm', 'feishu:chat_id'))"
            )
        if "reply_context_version" not in columns:
            connection.execute(
                "ALTER TABLE interaction_inbound_events ADD COLUMN reply_context_version "
                "INTEGER NOT NULL DEFAULT 0 CHECK (reply_context_version IN (0, 1))"
            )

    @staticmethod
    def _ensure_public_post_evidence_columns(connection: sqlite3.Connection) -> None:
        """Add v52 evidence fields even when an operator replays an old marker."""

        moderation_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(public_post_moderation_events)")
        }
        if "revision" not in moderation_columns:
            connection.execute(
                "ALTER TABLE public_post_moderation_events ADD COLUMN revision INTEGER "
                "CHECK (revision IS NULL OR revision >= 1)"
            )
        if "previous_event_id" not in moderation_columns:
            connection.execute(
                "ALTER TABLE public_post_moderation_events ADD COLUMN previous_event_id TEXT"
            )
        if "idempotency_key" not in moderation_columns:
            connection.execute(
                "ALTER TABLE public_post_moderation_events ADD COLUMN idempotency_key TEXT"
            )

        post_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(public_posts)")
        }
        if "identity_hash" not in post_columns:
            connection.execute("ALTER TABLE public_posts ADD COLUMN identity_hash TEXT")
        if "author_provenance" not in post_columns:
            connection.execute(
                "ALTER TABLE public_posts ADD COLUMN author_provenance TEXT NOT NULL "
                "DEFAULT 'visitor' CHECK (author_provenance IN "
                "('visitor', 'subject', 'operator', 'verified_channel'))"
            )

        control_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(public_post_controls)")
        }
        if "storage_cap_bytes" not in control_columns:
            connection.execute(
                "ALTER TABLE public_post_controls ADD COLUMN storage_cap_bytes INTEGER "
                "NOT NULL DEFAULT 250000000 CHECK "
                "(storage_cap_bytes >= 1000000 AND storage_cap_bytes <= 2000000000)"
            )
        if "captcha_issue_limit_per_hour" not in control_columns:
            connection.execute(
                "ALTER TABLE public_post_controls ADD COLUMN captcha_issue_limit_per_hour "
                "INTEGER NOT NULL DEFAULT 30 CHECK "
                "(captcha_issue_limit_per_hour >= 1 AND captcha_issue_limit_per_hour <= 100000)"
            )
        if "captcha_global_rate_per_minute" not in control_columns:
            connection.execute(
                "ALTER TABLE public_post_controls ADD COLUMN captcha_global_rate_per_minute "
                "INTEGER NOT NULL DEFAULT 300 CHECK "
                "(captcha_global_rate_per_minute >= 1 AND captcha_global_rate_per_minute <= 100000)"
            )

    def _preflight_schema_version(self) -> int | None:
        """Read an existing schema marker before any current-schema DDL runs."""
        if not self.path.is_file() or self.path.stat().st_size == 0:
            return None
        try:
            connection = sqlite3.connect(self.path)
            try:
                row = connection.execute(
                    "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                ).fetchone()
            finally:
                connection.close()
        except sqlite3.DatabaseError as error:
            raise RuntimeError("database schema preflight failed") from error
        if row is None:
            raise RuntimeError("database schema version is missing")
        try:
            version = int(row[0])
        except (TypeError, ValueError) as error:
            raise RuntimeError("database schema version is invalid") from error
        if version < 1:
            raise RuntimeError("database schema version is invalid")
        if version > CURRENT_SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema {version} is newer than runtime {CURRENT_SCHEMA_VERSION}"
            )
        return version

    def _create_migration_backup(self, version: int) -> Path:
        """Create and verify one durable rollback image before migration DDL."""
        backup_path = self.path.with_name(f"{self.path.name}.pre-migration-v{version}.bak")
        temporary_path = backup_path.with_name(f".{backup_path.name}.{new_id('tmp')}")
        source = sqlite3.connect(self.path)
        destination: sqlite3.Connection | None = None
        try:
            destination = sqlite3.connect(temporary_path)
            source.backup(destination)
            destination.commit()
            marker = destination.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            quick_check = str(destination.execute("PRAGMA quick_check").fetchone()[0])
            if marker is None or int(marker[0]) != version or quick_check != "ok":
                raise RuntimeError("pre-migration backup verification failed")
            destination.close()
            destination = None
            os.replace(temporary_path, backup_path)
            return backup_path
        except Exception:
            if destination is not None:
                destination.close()
                destination = None
            if temporary_path.exists():
                temporary_path.unlink(missing_ok=True)
            raise
        finally:
            if destination is not None:
                destination.close()
            source.close()

    def _restore_migration_backup(self, backup_path: Path) -> None:
        """Restore a verified pre-migration image without leaving WAL sidecars."""
        if not backup_path.is_file():
            raise FileNotFoundError(backup_path)
        temporary_path = self.path.with_name(f".{self.path.name}.{new_id('restore')}")
        try:
            shutil.copy2(backup_path, temporary_path)
            # The sqlite context manager commits/rolls back but does not close
            # the connection. Explicitly close it before replacing the target;
            # Windows otherwise keeps the temporary database handle locked.
            connection = sqlite3.connect(temporary_path)
            try:
                quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
                if quick_check != "ok":
                    raise RuntimeError("migration restore verification failed")
            finally:
                connection.close()
            for suffix in ("-wal", "-shm"):
                temporary_path.with_name(temporary_path.name + suffix).unlink(missing_ok=True)
            for suffix in ("-wal", "-shm"):
                self.path.with_name(self.path.name + suffix).unlink(missing_ok=True)
            os.replace(temporary_path, self.path)
        finally:
            temporary_path.unlink(missing_ok=True)

    def _ensure_optional_features(self) -> None:
        """Install additive features without invalidating older schema markers."""
        with self.transaction() as connection:
            self._execute_sql_script(
                connection,
                """
CREATE TABLE IF NOT EXISTS secret_cleanup_queue (
    task_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    secret_reference TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'failed', 'removed')),
    attempts INTEGER NOT NULL CHECK (attempts >= 0),
    last_error TEXT,
    next_retry_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(resource_type, resource_id, secret_reference)
);
CREATE TABLE IF NOT EXISTS persistent_features (
    feature_id TEXT PRIMARY KEY,
    feature_version INTEGER NOT NULL CHECK (feature_version >= 1),
    ddl_fingerprint TEXT NOT NULL CHECK (
        length(ddl_fingerprint) = 64
        AND ddl_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    installed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_secret_cleanup_subject_status
    ON secret_cleanup_queue(subject_id, status, updated_at);
CREATE TABLE IF NOT EXISTS secret_file_intents (
    intent_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    secret_reference TEXT NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('create', 'delete')),
    fingerprint TEXT,
    state TEXT NOT NULL CHECK (
        state IN ('prepared', 'file_ready', 'committed', 'pending', 'failed', 'removed')
    ),
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(resource_type, resource_id, secret_reference, operation)
);
CREATE INDEX IF NOT EXISTS idx_secret_file_intents_subject_state
    ON secret_file_intents(subject_id, resource_type, state, updated_at);
DROP TRIGGER IF EXISTS validate_secret_file_intent_reference_binding;
CREATE TRIGGER validate_secret_file_intent_reference_binding
BEFORE INSERT ON secret_file_intents
WHEN EXISTS (
    SELECT 1 FROM interaction_transports t
    WHERE t.secret_reference = NEW.secret_reference
      AND NOT (NEW.resource_type = 'transport'
               AND t.transport_id = NEW.resource_id AND t.subject_id = NEW.subject_id)
) OR EXISTS (
    SELECT 1 FROM search_provider_configs s
    WHERE s.key_reference = NEW.secret_reference
      AND NOT (NEW.resource_type = 'search'
               AND s.config_id = NEW.resource_id AND s.subject_id = NEW.subject_id)
) OR EXISTS (
    SELECT 1 FROM cognitive_resource_keys c
    WHERE c.key_reference = NEW.secret_reference
      AND NOT (NEW.resource_type = 'cognitive'
               AND c.key_id = NEW.resource_id AND c.subject_id = NEW.subject_id)
) OR EXISTS (
    SELECT 1 FROM embedding_resources e
    WHERE e.config_id || '.key' = NEW.secret_reference
      AND NOT (NEW.resource_type = 'embedding'
               AND e.config_id = NEW.resource_id AND e.subject_id = NEW.subject_id)
)
BEGIN
    SELECT RAISE(ABORT, 'secret file reference is already bound to another resource');
END;
DROP TRIGGER IF EXISTS validate_secret_file_intent_intent_binding;
CREATE TRIGGER validate_secret_file_intent_intent_binding
BEFORE INSERT ON secret_file_intents
WHEN EXISTS (
    SELECT 1 FROM secret_file_intents i
    WHERE i.secret_reference = NEW.secret_reference
      AND NOT (
          i.subject_id = NEW.subject_id
          AND i.resource_type = NEW.resource_type
          AND i.resource_id = NEW.resource_id
      )
)
BEGIN
    SELECT RAISE(ABORT, 'secret file reference is already bound to another intent');
END;
DROP TRIGGER IF EXISTS validate_secret_file_intent_reference_binding_update;
CREATE TRIGGER validate_secret_file_intent_reference_binding_update
BEFORE UPDATE OF subject_id, resource_type, resource_id, secret_reference ON secret_file_intents
WHEN EXISTS (
    SELECT 1 FROM interaction_transports t
    WHERE t.secret_reference = NEW.secret_reference
      AND NOT (NEW.resource_type = 'transport'
               AND t.transport_id = NEW.resource_id AND t.subject_id = NEW.subject_id)
) OR EXISTS (
    SELECT 1 FROM search_provider_configs s
    WHERE s.key_reference = NEW.secret_reference
      AND NOT (NEW.resource_type = 'search'
               AND s.config_id = NEW.resource_id AND s.subject_id = NEW.subject_id)
) OR EXISTS (
    SELECT 1 FROM cognitive_resource_keys c
    WHERE c.key_reference = NEW.secret_reference
      AND NOT (NEW.resource_type = 'cognitive'
               AND c.key_id = NEW.resource_id AND c.subject_id = NEW.subject_id)
) OR EXISTS (
    SELECT 1 FROM embedding_resources e
    WHERE e.config_id || '.key' = NEW.secret_reference
      AND NOT (NEW.resource_type = 'embedding'
               AND e.config_id = NEW.resource_id AND e.subject_id = NEW.subject_id)
)
BEGIN
    SELECT RAISE(ABORT, 'secret file reference is already bound to another resource');
END;
DROP TRIGGER IF EXISTS validate_secret_file_intent_identity_immutable;
CREATE TRIGGER validate_secret_file_intent_identity_immutable
BEFORE UPDATE OF intent_id, subject_id, resource_type, resource_id,
                 secret_reference, operation, created_at ON secret_file_intents
WHEN NEW.intent_id IS NOT OLD.intent_id
  OR NEW.subject_id IS NOT OLD.subject_id
  OR NEW.resource_type IS NOT OLD.resource_type
  OR NEW.resource_id IS NOT OLD.resource_id
  OR NEW.secret_reference IS NOT OLD.secret_reference
  OR NEW.operation IS NOT OLD.operation
  OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'secret file intent identity is immutable');
END;
DROP TRIGGER IF EXISTS prevent_secret_file_intent_delete;
CREATE TRIGGER prevent_secret_file_intent_delete
BEFORE DELETE ON secret_file_intents
BEGIN
    SELECT RAISE(ABORT, 'secret file intents are append-only');
END;
DROP TRIGGER IF EXISTS validate_secret_file_intent_transition;
CREATE TRIGGER IF NOT EXISTS validate_secret_file_intent_transition
BEFORE UPDATE OF state ON secret_file_intents
WHEN NOT (
    NEW.state = OLD.state
    OR (OLD.state = 'prepared' AND NEW.state IN ('file_ready', 'committed', 'failed', 'removed'))
    OR (OLD.state = 'file_ready' AND NEW.state IN ('committed', 'failed', 'removed'))
    OR (OLD.state = 'committed' AND NEW.state IN ('failed', 'removed'))
    OR (OLD.state = 'pending' AND NEW.state IN ('failed', 'removed'))
    OR (OLD.state = 'failed' AND NEW.state IN ('pending', 'prepared', 'removed'))
)
BEGIN
    SELECT RAISE(ABORT, 'secret file intent state transition is invalid');
END;
DROP TRIGGER IF EXISTS validate_secret_file_intent_operation_state;
CREATE TRIGGER IF NOT EXISTS validate_secret_file_intent_operation_state
BEFORE UPDATE OF state ON secret_file_intents
WHEN (NEW.operation = 'create' AND NEW.state = 'pending')
  OR (NEW.operation = 'delete' AND NEW.state IN ('prepared', 'file_ready', 'committed'))
BEGIN
    SELECT RAISE(ABORT, 'secret file intent operation state is invalid');
END;
""",
            )
            schema_version = int(
                connection.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'"
                ).fetchone()[0]
            )
            now = utc_now()
            for feature_id, object_names in _PERSISTENT_FEATURE_OBJECTS.items():
                definitions: list[dict[str, str]] = []
                for object_name in object_names:
                    row = connection.execute(
                        "SELECT type, name, sql FROM sqlite_master WHERE name=?",
                        (object_name,),
                    ).fetchone()
                    if row is None or not row["sql"]:
                        if schema_version < _PERSISTENT_FEATURE_MIN_SCHEMA.get(feature_id, 0):
                            # A migration test (or an intentionally pinned
                            # older runtime) may stop before an additive
                            # feature's introduction.  Do not register or
                            # require a feature whose schema cannot contain it.
                            definitions = []
                            break
                        raise RuntimeError(
                            f"optional feature {feature_id} is missing database object {object_name}"
                        )
                    definitions.append(
                        {"type": str(row["type"]), "name": str(row["name"]), "sql": str(row["sql"])}
                    )
                if not definitions:
                    continue
                fingerprint = content_hash(definitions)
                existing = connection.execute(
                    "SELECT feature_version, ddl_fingerprint, installed_at "
                    "FROM persistent_features WHERE feature_id=?",
                    (feature_id,),
                ).fetchone()
                if existing is not None and (
                    int(existing["feature_version"]) != 1
                    or str(existing["ddl_fingerprint"]) != fingerprint
                ):
                    raise SchemaContractError(
                        "schema contract mismatch: "
                        f"optional feature {feature_id} registry fingerprint does not match installed DDL"
                    )
                connection.execute(
                    """INSERT INTO persistent_features(
                        feature_id, feature_version, ddl_fingerprint, installed_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(feature_id) DO UPDATE SET
                        updated_at=excluded.updated_at""",
                    (feature_id, 1, fingerprint, now, now),
                )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(model_calls)").fetchall()
            }
            if "resource_group_id" not in columns:
                connection.execute("ALTER TABLE model_calls ADD COLUMN resource_group_id TEXT")
            if "request_json" not in columns:
                connection.execute("ALTER TABLE model_calls ADD COLUMN request_json TEXT")
            if "capture_policy_version" not in columns:
                connection.execute(
                    "ALTER TABLE model_calls ADD COLUMN capture_policy_version INTEGER"
                )
            event_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(events)").fetchall()
            }
            if "payload_archive_key" not in event_columns:
                connection.execute("ALTER TABLE events ADD COLUMN payload_archive_key TEXT")
            if "payload_archived_at" not in event_columns:
                connection.execute("ALTER TABLE events ADD COLUMN payload_archived_at TEXT")
            segment_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(event_payload_segments)"
                ).fetchall()
            }
            if "archive_format" not in segment_columns:
                connection.execute(
                    "ALTER TABLE event_payload_segments ADD COLUMN archive_format TEXT"
                )
            if "encryption_key_id" not in segment_columns:
                connection.execute(
                    "ALTER TABLE event_payload_segments ADD COLUMN encryption_key_id TEXT"
                )
            if "encryption_key_fingerprint" not in segment_columns:
                connection.execute(
                    "ALTER TABLE event_payload_segments ADD COLUMN encryption_key_fingerprint TEXT"
                )
            observation_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(observations)").fetchall()
            }
            if "content_archive_key" not in observation_columns:
                connection.execute("ALTER TABLE observations ADD COLUMN content_archive_key TEXT")
            if "content_archived_at" not in observation_columns:
                connection.execute("ALTER TABLE observations ADD COLUMN content_archived_at TEXT")
            self._execute_sql_script(
                connection,
                """
CREATE TABLE IF NOT EXISTS observation_content_segments (
    segment_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    object_key TEXT NOT NULL,
    first_fetched_at TEXT NOT NULL,
    last_fetched_at TEXT NOT NULL,
    observation_count INTEGER NOT NULL CHECK (observation_count > 0),
    compressed_hash TEXT NOT NULL,
    archive_format TEXT,
    encryption_key_id TEXT,
    encryption_key_fingerprint TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(subject_id, object_key)
);
CREATE INDEX IF NOT EXISTS idx_observation_content_segments_subject_key
    ON observation_content_segments(subject_id, encryption_key_id, created_at);
""",
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_model_calls_subject_group_status "
                "ON model_calls(subject_id, resource_group_id, status)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_model_calls_subject_capture_policy "
                "ON model_calls(subject_id, capture_policy_version, created_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_model_calls_subject_export_time "
                "ON model_calls(subject_id, created_at, call_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_event_payload_segments_subject_key "
                "ON event_payload_segments(subject_id, encryption_key_id, created_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_subject_archive_time "
                "ON events(subject_id, payload_archive_key, occurred_at, event_id)"
            )
            connection.execute(
                "INSERT INTO memory_fts(memory_id, subject_id, content) "
                "SELECT m.memory_id, m.subject_id, m.content FROM memories m "
                "WHERE m.status = 'active' AND NOT EXISTS ("
                "SELECT 1 FROM memory_fts f WHERE f.memory_id = m.memory_id)"
            )
            delivery_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(interaction_deliveries)"
                ).fetchall()
            }
            if "next_retry_at" not in delivery_columns:
                connection.execute(
                    "ALTER TABLE interaction_deliveries ADD COLUMN next_retry_at TEXT"
                )
            self._execute_sql_script(
                connection,
                """
DROP TRIGGER IF EXISTS prevent_event_immutable_update;
CREATE TRIGGER prevent_event_immutable_update
BEFORE UPDATE ON events
WHEN NEW.event_id != OLD.event_id
  OR NEW.subject_id != OLD.subject_id
  OR NEW.event_type != OLD.event_type
  OR NEW.source != OLD.source
  OR NEW.occurred_at != OLD.occurred_at
  OR NEW.observed_at != OLD.observed_at
  OR NEW.payload_hash != OLD.payload_hash
  OR NEW.privacy_level != OLD.privacy_level
  OR NEW.causal_parent_ids_json != OLD.causal_parent_ids_json
  OR (
      NEW.payload_json != OLD.payload_json
      AND NOT (
          OLD.payload_archive_key IS NULL
          AND NEW.payload_archive_key IS NOT NULL
          AND NEW.payload_json = '{}'
          AND NEW.payload_archived_at IS NOT NULL
      )
  )
  OR (
      OLD.payload_archive_key IS NOT NULL
      AND NOT (NEW.payload_archive_key IS OLD.payload_archive_key)
  )
  OR (
      OLD.payload_archived_at IS NOT NULL
      AND NOT (NEW.payload_archived_at IS OLD.payload_archived_at)
  )
BEGIN
    SELECT RAISE(ABORT, 'event evidence is append-only');
END;
""",
            )

            schema_version = int(
                connection.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'"
                ).fetchone()[0]
            )
            required_tables = {
                "interaction_transports",
                "interaction_deliveries",
                "interaction_bindings",
                "interaction_inbound_events",
                "interaction_threads",
                "event_chain_roots",
                "memory_fts",
                "action_revisions",
                "archive_keyring_revisions",
                "archive_object_replicas",
                "archive_object_replica_revisions",
                "behavior_log_revisions",
                "observation_content_segments",
                "secret_cleanup_queue",
                "secret_file_intents",
                "persistent_features",
                "storage_usage_samples",
                "common_knowledge_versions",
                "common_knowledge_sync_events",
                "common_knowledge_peers",
                "common_knowledge_remote_events",
                "common_knowledge_sync_runs",
                "common_knowledge_evaluation_events",
                "wallet_networks",
                "wallet_network_revisions",
                "wallet_assets",
                "wallet_asset_revisions",
                "wallet_addresses",
                "wallet_address_revisions",
                "wallet_balance_snapshots",
                "wallet_balance_acquisition_runs",
                "wallet_balance_acquisition_attempts",
                "wallet_payment_executions",
                "wallet_payment_execution_attempts",
                "wallet_reward_workflows",
                "wallet_reward_submission_links",
                "wallet_reward_incidents",
            }
            if schema_version >= 71:
                required_tables.add("wallet_payment_reconciliation_events")
            if schema_version >= 76:
                required_tables.update({"admin_login_rate_events", "admin_sessions"})
            installed_tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')"
                ).fetchall()
            }
            missing = sorted(required_tables - installed_tables)
            if missing:
                raise RuntimeError(
                    "database schema is incomplete; missing required features: "
                    + ", ".join(missing)
                )
            # Bind payment authorization to a durable timestamp.  This is an
            # additive repair for schema-60 databases; fresh schema-55
            # installs already include the column.
            order_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(wallet_payment_orders)")
            }
            if "authorized_at" not in order_columns:
                connection.execute(
                    "ALTER TABLE wallet_payment_orders ADD COLUMN authorized_at TEXT"
                )
            # This additive index keeps the unfiltered wallet history
            # projection on the subject/time keyset without rewriting the
            # already-published schema-53 migration.
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_wallet_balance_snapshots_subject_history "
                "ON wallet_balance_snapshots(subject_id, observed_at DESC, snapshot_id DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_wallet_ledger_entries_subject "
                "ON wallet_ledger_entries(subject_id, journal_id, entry_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_wallet_ledger_entries_journal_entry "
                "ON wallet_ledger_entries(journal_id, entry_id)"
            )
            self._ensure_wallet_acquisition_triggers(connection)
            self._ensure_wallet_execution_triggers(connection)
            if schema_version >= 59:
                self._ensure_wallet_submission_consent_contract(connection)
            self._ensure_behavior_log_revision_triggers(connection)
            self._assert_behavior_log_revisions_complete(connection)
            self._ensure_audit_record_triggers(connection)
            self._ensure_action_revisions(connection)
            self._ensure_subject_scoped_triggers(connection)
            self._ensure_wallet_reward_triggers(connection)
            self._ensure_inbound_triggers(connection)
            self._ensure_public_post_triggers(connection)
            # Retention diagnostics need a durable baseline of the tables
            # produced by this schema version.  A table added at runtime (or
            # by an incomplete migration) is then visible as unclassified
            # instead of silently inheriting a delete policy.
            inventory = sorted(
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')"
                ).fetchall()
                if str(row[0]) != "sqlite_sequence"
            )
            inventory_json = canonical_json(inventory)
            baseline_version = connection.execute(
                "SELECT value FROM schema_meta WHERE key='retention_contract_schema_version'"
            ).fetchone()
            baseline_inventory = connection.execute(
                "SELECT value FROM schema_meta WHERE key='retention_contract_inventory'"
            ).fetchone()
            if baseline_version is None or str(baseline_version[0]) != str(schema_version):
                connection.execute(
                    "INSERT INTO schema_meta(key, value) VALUES ('retention_contract_schema_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(schema_version),),
                )
                connection.execute(
                    "INSERT INTO schema_meta(key, value) VALUES ('retention_contract_inventory', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (inventory_json,),
                )
            elif baseline_inventory is None:
                connection.execute(
                    "INSERT INTO schema_meta(key, value) VALUES ('retention_contract_inventory', ?) ",
                    (inventory_json,),
                )

    @staticmethod
    def _ensure_subject_scoped_triggers(connection: sqlite3.Connection) -> None:
        links = (
            ("goal_governance_runs", "model_call_id", "model_calls", "call_id", False),
            ("goal_governance_runs", "focus_goal_id", "goals", "goal_id", True),
            ("action_deliberation_runs", "model_call_id", "model_calls", "call_id", False),
            ("action_deliberation_runs", "goal_id", "goals", "goal_id", True),
            ("action_deliberation_runs", "source_id", "world_sources", "source_id", True),
            ("action_deliberation_runs", "action_id", "actions", "action_id", True),
            ("action_deliberation_runs", "observation_id", "observations", "observation_id", True),
            ("research_search_runs", "planner_call_id", "model_calls", "call_id", False),
            ("research_search_runs", "goal_id", "goals", "goal_id", True),
            (
                "research_search_runs",
                "provider_config_id",
                "search_provider_configs",
                "config_id",
                True,
            ),
            ("research_search_runs", "project_id", "autonomous_projects", "project_id", True),
            (
                "research_search_runs",
                "phase_id",
                "autonomous_project_phases",
                "phase_id",
                True,
            ),
            ("epistemic_review_runs", "model_call_id", "model_calls", "call_id", False),
            (
                "epistemic_review_runs",
                "trigger_observation_id",
                "observations",
                "observation_id",
                True,
            ),
            ("memory_accesses", "memory_id", "memories", "memory_id", False),
            (
                "relationship_social_runs",
                "relationship_id",
                "relationships",
                "relationship_id",
                False,
            ),
            ("relationship_social_runs", "model_call_id", "model_calls", "call_id", False),
            ("relationship_social_runs", "interaction_id", "interactions", "interaction_id", True),
            ("self_models", "model_call_id", "model_calls", "call_id", False),
            ("thought_episodes", "agenda_id", "thought_agenda_items", "agenda_id", False),
            ("thought_episodes", "model_call_id", "model_calls", "call_id", False),
            ("thought_episodes", "created_goal_id", "goals", "goal_id", True),
            (
                "metacognitive_outcomes",
                "decision_id",
                "metacognitive_decisions",
                "decision_id",
                False,
            ),
            ("outcome_evaluations", "goal_id", "goals", "goal_id", False),
            ("outcome_evaluations", "evidence_event_id", "events", "event_id", False),
            ("outcome_evaluations", "observation_id", "observations", "observation_id", True),
            ("interaction_deliveries", "interaction_id", "interactions", "interaction_id", False),
            (
                "interaction_deliveries",
                "transport_id",
                "interaction_transports",
                "transport_id",
                False,
            ),
            ("autonomous_projects", "goal_id", "goals", "goal_id", False),
            ("autonomous_projects", "formation_call_id", "model_calls", "call_id", False),
            ("autonomous_projects", "source_mission_id", "mission_candidates", "mission_id", True),
            ("autonomous_project_phases", "project_id", "autonomous_projects", "project_id", False),
            (
                "autonomous_project_reviews",
                "project_id",
                "autonomous_projects",
                "project_id",
                False,
            ),
            ("autonomous_project_reviews", "model_call_id", "model_calls", "call_id", False),
            (
                "autonomous_project_reviews",
                "phase_id",
                "autonomous_project_phases",
                "phase_id",
                True,
            ),
            (
                "autonomous_project_resource_uses",
                "project_id",
                "autonomous_projects",
                "project_id",
                False,
            ),
            (
                "autonomous_project_assistance_requests",
                "project_id",
                "autonomous_projects",
                "project_id",
                False,
            ),
            (
                "autonomous_project_assistance_requests",
                "phase_id",
                "autonomous_project_phases",
                "phase_id",
                True,
            ),
            (
                "autonomous_project_sleep_reflections",
                "project_id",
                "autonomous_projects",
                "project_id",
                False,
            ),
            ("autonomous_project_sleep_reflections", "sleep_id", "sleep_runs", "sleep_id", False),
            (
                "autonomous_project_execution_clock_events",
                "project_id",
                "autonomous_projects",
                "project_id",
                False,
            ),
            (
                "autonomous_project_executions",
                "project_id",
                "autonomous_projects",
                "project_id",
                False,
            ),
            (
                "autonomous_project_executions",
                "phase_id",
                "autonomous_project_phases",
                "phase_id",
                False,
            ),
            (
                "self_modification_settings",
                "proposal_id",
                "self_modification_proposals",
                "proposal_id",
                False,
            ),
            (
                "self_modification_revisions",
                "proposal_id",
                "self_modification_proposals",
                "proposal_id",
                False,
            ),
            ("memory_embeddings", "memory_id", "memories", "memory_id", False),
            ("entity_relations", "source_entity_id", "entities", "entity_id", False),
            ("entity_relations", "target_entity_id", "entities", "entity_id", False),
            ("wallet_assets", "network_id", "wallet_networks", "network_id", False),
            ("wallet_addresses", "network_id", "wallet_networks", "network_id", False),
            (
                "wallet_network_revisions",
                "network_id",
                "wallet_networks",
                "network_id",
                False,
            ),
            ("wallet_asset_revisions", "asset_id", "wallet_assets", "asset_id", False),
            (
                "wallet_address_revisions",
                "address_id",
                "wallet_addresses",
                "address_id",
                False,
            ),
            (
                "wallet_balance_snapshots",
                "network_id",
                "wallet_networks",
                "network_id",
                False,
            ),
            ("wallet_balance_snapshots", "asset_id", "wallet_assets", "asset_id", False),
            (
                "wallet_balance_snapshots",
                "address_id",
                "wallet_addresses",
                "address_id",
                False,
            ),
            (
                "wallet_balance_acquisition_runs",
                "network_id",
                "wallet_networks",
                "network_id",
                False,
            ),
            (
                "wallet_balance_acquisition_runs",
                "asset_id",
                "wallet_assets",
                "asset_id",
                False,
            ),
            (
                "wallet_balance_acquisition_runs",
                "address_id",
                "wallet_addresses",
                "address_id",
                False,
            ),
            (
                "wallet_balance_acquisition_runs",
                "snapshot_id",
                "wallet_balance_snapshots",
                "snapshot_id",
                True,
            ),
            (
                "wallet_balance_acquisition_runs",
                "created_audit_id",
                "audit_records",
                "audit_id",
                False,
            ),
            (
                "wallet_balance_acquisition_attempts",
                "run_id",
                "wallet_balance_acquisition_runs",
                "run_id",
                False,
            ),
            (
                "wallet_balance_acquisition_attempts",
                "network_id",
                "wallet_networks",
                "network_id",
                False,
            ),
            (
                "wallet_balance_acquisition_attempts",
                "asset_id",
                "wallet_assets",
                "asset_id",
                False,
            ),
            (
                "wallet_balance_acquisition_attempts",
                "address_id",
                "wallet_addresses",
                "address_id",
                False,
            ),
            (
                "wallet_balance_acquisition_attempts",
                "snapshot_id",
                "wallet_balance_snapshots",
                "snapshot_id",
                True,
            ),
            (
                "wallet_reward_workflows",
                "assistance_request_id",
                "autonomous_project_assistance_requests",
                "request_id",
                False,
            ),
            ("wallet_reward_workflows", "project_id", "autonomous_projects", "project_id", False),
            ("wallet_reward_workflows", "phase_id", "autonomous_project_phases", "phase_id", False),
            ("wallet_reward_workflows", "goal_id", "goals", "goal_id", False),
            ("wallet_reward_workflows", "post_id", "public_posts", "post_id", False),
            ("wallet_reward_workflows", "bounty_id", "wallet_bounties", "bounty_id", False),
            ("wallet_reward_workflows", "created_audit_id", "audit_records", "audit_id", False),
            ("wallet_reward_workflows", "last_audit_id", "audit_records", "audit_id", False),
            (
                "wallet_reward_submission_links",
                "submission_id",
                "wallet_bounty_submissions",
                "submission_id",
                False,
            ),
            (
                "wallet_reward_submission_links",
                "workflow_id",
                "wallet_reward_workflows",
                "workflow_id",
                False,
            ),
            (
                "wallet_reward_submission_links",
                "inbound_event_id",
                "interaction_inbound_events",
                "event_id",
                True,
            ),
            (
                "wallet_reward_submission_links",
                "interaction_id",
                "interactions",
                "interaction_id",
                True,
            ),
            (
                "wallet_reward_submission_links",
                "claimed_network_id",
                "wallet_networks",
                "network_id",
                False,
            ),
            (
                "wallet_reward_submission_links",
                "created_audit_id",
                "audit_records",
                "audit_id",
                False,
            ),
            (
                "wallet_reward_submission_links",
                "decision_audit_id",
                "audit_records",
                "audit_id",
                True,
            ),
            (
                "wallet_reward_incidents",
                "workflow_id",
                "wallet_reward_workflows",
                "workflow_id",
                False,
            ),
            (
                "wallet_reward_incidents",
                "submission_id",
                "wallet_bounty_submissions",
                "submission_id",
                True,
            ),
            (
                "wallet_reward_incidents",
                "execution_id",
                "wallet_payment_executions",
                "execution_id",
                True,
            ),
            ("wallet_reward_incidents", "opened_audit_id", "audit_records", "audit_id", False),
            ("wallet_reward_incidents", "resolved_audit_id", "audit_records", "audit_id", True),
        )
        for table, column, parent, parent_column, nullable in links:
            condition = (f"NEW.{column} IS NOT NULL AND " if nullable else "") + (
                f"NOT EXISTS (SELECT 1 FROM {parent} p WHERE p.{parent_column} = NEW.{column} "
                "AND p.subject_id = NEW.subject_id)"
            )

            base = f"validate_{table}_{column}_subject"
            Database._execute_sql_script(
                connection,
                f"""
CREATE TRIGGER IF NOT EXISTS {base}_insert
BEFORE INSERT ON {table}
WHEN {condition}
BEGIN
    SELECT RAISE(ABORT, 'subject-scoped reference mismatch');
END;
CREATE TRIGGER IF NOT EXISTS {base}_update
BEFORE UPDATE OF subject_id, {column} ON {table}
WHEN {condition}
BEGIN
    SELECT RAISE(ABORT, 'subject-scoped reference mismatch');
END;
""",
            )

    @staticmethod
    def _ensure_wallet_reward_triggers(connection: sqlite3.Connection) -> None:
        """Keep reward workflow provenance append-only and state-machine bounded."""
        Database._execute_sql_script(
            connection,
            """
CREATE TRIGGER IF NOT EXISTS prevent_wallet_reward_workflow_identity_update
BEFORE UPDATE OF workflow_id,subject_id,assistance_request_id,project_id,phase_id,goal_id,post_id,bounty_id,idempotency_key,created_audit_id,created_at
ON wallet_reward_workflows
BEGIN SELECT RAISE(ABORT,'wallet reward workflow identity is immutable'); END;
CREATE TRIGGER IF NOT EXISTS validate_wallet_reward_workflow_transition
BEFORE UPDATE ON wallet_reward_workflows
WHEN NOT (
    (NEW.status=OLD.status AND NEW.manual_reason IS OLD.manual_reason AND NEW.state_hash=OLD.state_hash AND NEW.updated_at=OLD.updated_at AND NEW.last_audit_id=OLD.last_audit_id)
    OR (OLD.status='awaiting_publication' AND NEW.status IN ('open','cancelled','manual_intervention'))
    OR (OLD.status='open' AND NEW.status IN ('closed','cancelled','manual_intervention'))
    OR (OLD.status='manual_intervention' AND NEW.status IN ('open','closed','cancelled'))
)
BEGIN SELECT RAISE(ABORT,'wallet reward workflow transition is invalid'); END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_reward_submission_link_identity_update
BEFORE UPDATE OF submission_id,workflow_id,subject_id,source_type,inbound_event_id,interaction_id,claimed_network_id,source_content_hash,created_audit_id,created_at
ON wallet_reward_submission_links
BEGIN SELECT RAISE(ABORT,'wallet reward submission link identity is immutable'); END;
CREATE TRIGGER IF NOT EXISTS validate_wallet_reward_submission_link_transition
BEFORE UPDATE ON wallet_reward_submission_links
WHEN NOT (
    (NEW.verification_status=OLD.verification_status AND NEW.criteria_results_json IS OLD.criteria_results_json AND NEW.verification_reason IS OLD.verification_reason AND NEW.verified_by IS OLD.verified_by AND NEW.state_hash=OLD.state_hash AND NEW.updated_at=OLD.updated_at AND NEW.decision_audit_id IS OLD.decision_audit_id)
    OR (OLD.verification_status='pending_verification' AND NEW.verification_status IN ('accepted','rejected','manual_intervention'))
)
BEGIN SELECT RAISE(ABORT,'wallet reward submission link transition is invalid'); END;
CREATE TRIGGER IF NOT EXISTS validate_wallet_reward_submission_provenance
BEFORE INSERT ON wallet_reward_submission_links
WHEN NOT EXISTS (
    SELECT 1 FROM wallet_bounty_submissions s JOIN wallet_reward_workflows w ON w.workflow_id=NEW.workflow_id
    WHERE s.submission_id=NEW.submission_id AND s.subject_id=NEW.subject_id AND s.bounty_id=w.bounty_id
)
BEGIN SELECT RAISE(ABORT,'wallet reward submission provenance mismatch'); END;
CREATE TRIGGER IF NOT EXISTS validate_wallet_reward_submission_status
BEFORE UPDATE OF verification_status ON wallet_reward_submission_links
WHEN (NEW.verification_status='accepted' AND NOT EXISTS (SELECT 1 FROM wallet_bounty_submissions s WHERE s.submission_id=NEW.submission_id AND s.status='accepted'))
   OR (NEW.verification_status='rejected' AND NOT EXISTS (SELECT 1 FROM wallet_bounty_submissions s WHERE s.submission_id=NEW.submission_id AND s.status='rejected'))
BEGIN SELECT RAISE(ABORT,'wallet reward submission status mismatch'); END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_reward_incident_identity_update
BEFORE UPDATE OF incident_id,subject_id,workflow_id,submission_id,execution_id,kind,idempotency_key,opened_audit_id,created_at
ON wallet_reward_incidents
BEGIN SELECT RAISE(ABORT,'wallet reward incident identity is immutable'); END;
CREATE TRIGGER IF NOT EXISTS validate_wallet_reward_incident_transition
BEFORE UPDATE ON wallet_reward_incidents
WHEN NOT (
    (NEW.status=OLD.status AND NEW.reason=OLD.reason AND NEW.resolution IS OLD.resolution AND NEW.state_hash=OLD.state_hash AND NEW.updated_at=OLD.updated_at AND NEW.resolved_audit_id IS OLD.resolved_audit_id)
    OR (OLD.status='open' AND NEW.status='resolved' AND NEW.resolution IS NOT NULL AND NEW.resolved_audit_id IS NOT NULL AND NEW.state_hash<>OLD.state_hash)
)
BEGIN SELECT RAISE(ABORT,'wallet reward incident transition is invalid'); END;
CREATE TRIGGER IF NOT EXISTS validate_wallet_reward_incident_provenance
BEFORE INSERT ON wallet_reward_incidents
WHEN NOT EXISTS (SELECT 1 FROM wallet_reward_workflows w WHERE w.workflow_id=NEW.workflow_id AND w.subject_id=NEW.subject_id)
   OR (NEW.submission_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM wallet_bounty_submissions s WHERE s.submission_id=NEW.submission_id AND s.subject_id=NEW.subject_id))
   OR (NEW.execution_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM wallet_payment_executions e WHERE e.execution_id=NEW.execution_id AND e.subject_id=NEW.subject_id))
BEGIN SELECT RAISE(ABORT,'wallet reward incident provenance mismatch'); END;
""",
        )

    @staticmethod
    def _ensure_inbound_triggers(connection: sqlite3.Connection) -> None:
        Database._execute_sql_script(
            connection,
            """
DROP TRIGGER IF EXISTS validate_interaction_binding_transport_insert;
DROP TRIGGER IF EXISTS validate_interaction_binding_transport_update;
DROP TRIGGER IF EXISTS validate_interaction_inbound_transport_insert;
DROP TRIGGER IF EXISTS validate_interaction_inbound_interaction_insert;
DROP TRIGGER IF EXISTS validate_interaction_thread_transport_insert;
DROP TRIGGER IF EXISTS prevent_interaction_binding_identity_update;
DROP TRIGGER IF EXISTS prevent_interaction_binding_delete;
DROP TRIGGER IF EXISTS prevent_interaction_inbound_identity_update;
DROP TRIGGER IF EXISTS prevent_interaction_inbound_delete;
DROP TRIGGER IF EXISTS validate_interaction_inbound_status_insert;
DROP TRIGGER IF EXISTS validate_interaction_inbound_status_update;
DROP TRIGGER IF EXISTS prevent_interaction_thread_identity_update;
DROP TRIGGER IF EXISTS prevent_interaction_thread_delete;

CREATE TRIGGER IF NOT EXISTS validate_interaction_binding_transport_insert
BEFORE INSERT ON interaction_bindings
WHEN NOT EXISTS (
    SELECT 1 FROM interaction_transports t
    WHERE t.transport_id = NEW.transport_id
      AND t.subject_id = NEW.subject_id
      AND t.channel = NEW.channel
)
BEGIN
    SELECT RAISE(ABORT, 'inbound binding transport subject mismatch');
END;
CREATE TRIGGER IF NOT EXISTS validate_interaction_binding_transport_update
BEFORE UPDATE OF subject_id, transport_id, channel ON interaction_bindings
WHEN NOT EXISTS (
    SELECT 1 FROM interaction_transports t
    WHERE t.transport_id = NEW.transport_id
      AND t.subject_id = NEW.subject_id
      AND t.channel = NEW.channel
)
BEGIN
    SELECT RAISE(ABORT, 'inbound binding transport subject mismatch');
END;
CREATE TRIGGER IF NOT EXISTS validate_interaction_inbound_transport_insert
BEFORE INSERT ON interaction_inbound_events
WHEN NOT EXISTS (
    SELECT 1 FROM interaction_transports t
    WHERE t.transport_id = NEW.transport_id
      AND t.subject_id = NEW.subject_id
      AND t.channel = NEW.channel
)
BEGIN
    SELECT RAISE(ABORT, 'inbound event transport subject mismatch');
END;
CREATE TRIGGER IF NOT EXISTS validate_interaction_inbound_interaction_insert
BEFORE INSERT ON interaction_inbound_events
WHEN NEW.interaction_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM interactions i
    WHERE i.interaction_id = NEW.interaction_id AND i.subject_id = NEW.subject_id
)
BEGIN
    SELECT RAISE(ABORT, 'inbound event interaction subject mismatch');
END;
CREATE TRIGGER IF NOT EXISTS validate_interaction_thread_transport_insert
BEFORE INSERT ON interaction_threads
WHEN NOT EXISTS (
    SELECT 1 FROM interaction_transports t
    WHERE t.transport_id = NEW.transport_id
      AND t.subject_id = NEW.subject_id
      AND t.channel = NEW.channel
)
BEGIN
    SELECT RAISE(ABORT, 'interaction thread transport subject mismatch');
END;
CREATE TRIGGER prevent_interaction_binding_identity_update
BEFORE UPDATE OF binding_id, subject_id, transport_id, channel,
                 external_account_id, external_sender_id, role, label, created_at
ON interaction_bindings
WHEN NEW.binding_id IS NOT OLD.binding_id
  OR NEW.subject_id IS NOT OLD.subject_id
  OR NEW.transport_id IS NOT OLD.transport_id
  OR NEW.channel IS NOT OLD.channel
  OR NEW.external_account_id IS NOT OLD.external_account_id
  OR NEW.external_sender_id IS NOT OLD.external_sender_id
  OR NEW.role IS NOT OLD.role
  OR NEW.label IS NOT OLD.label
  OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'inbound binding identity is immutable');
END;
CREATE TRIGGER prevent_interaction_binding_delete
BEFORE DELETE ON interaction_bindings
BEGIN
    SELECT RAISE(ABORT, 'inbound bindings cannot be deleted');
END;
CREATE TRIGGER prevent_interaction_inbound_identity_update
BEFORE UPDATE OF event_id, subject_id, transport_id, channel, provider_event_id,
                 external_account_id, external_sender_id, conversation_id,
                 content_hash, scheduling_priority, received_at,
                 external_thread_id, reply_selector, reply_context_version
ON interaction_inbound_events
WHEN NEW.event_id IS NOT OLD.event_id
  OR NEW.subject_id IS NOT OLD.subject_id
  OR NEW.transport_id IS NOT OLD.transport_id
  OR NEW.channel IS NOT OLD.channel
  OR NEW.provider_event_id IS NOT OLD.provider_event_id
  OR NEW.external_account_id IS NOT OLD.external_account_id
  OR NEW.external_sender_id IS NOT OLD.external_sender_id
  OR NEW.conversation_id IS NOT OLD.conversation_id
  OR NEW.content_hash IS NOT OLD.content_hash
  OR NEW.scheduling_priority IS NOT OLD.scheduling_priority
  OR NEW.received_at IS NOT OLD.received_at
  OR NEW.external_thread_id IS NOT OLD.external_thread_id
  OR NEW.reply_selector IS NOT OLD.reply_selector
  OR NEW.reply_context_version IS NOT OLD.reply_context_version
BEGIN
    SELECT RAISE(ABORT, 'inbound event identity is immutable');
END;
CREATE TRIGGER prevent_interaction_inbound_delete
BEFORE DELETE ON interaction_inbound_events
BEGIN
    SELECT RAISE(ABORT, 'inbound events cannot be deleted');
END;
CREATE TRIGGER validate_interaction_inbound_status_insert
BEFORE INSERT ON interaction_inbound_events
WHEN NEW.status != 'received' OR NEW.processed_at IS NOT NULL OR NEW.interaction_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'inbound event must start in received state');
END;
CREATE TRIGGER validate_interaction_inbound_status_update
BEFORE UPDATE OF status, processed_at, interaction_id ON interaction_inbound_events
WHEN NOT (
    (NEW.status = OLD.status AND NEW.processed_at IS OLD.processed_at
        AND NEW.interaction_id IS OLD.interaction_id)
    OR (OLD.status = 'received' AND NEW.status = 'processed'
        AND NEW.processed_at IS NOT NULL AND NEW.interaction_id IS NOT NULL
        AND EXISTS (SELECT 1 FROM interactions i
                    WHERE i.interaction_id = NEW.interaction_id
                      AND i.subject_id = NEW.subject_id
                      AND i.direction = 'incoming'))
    OR (OLD.status = 'received' AND NEW.status = 'rejected'
        AND NEW.processed_at IS NOT NULL AND NEW.interaction_id IS NULL)
    OR (OLD.status = 'rejected' AND NEW.status = 'received'
        AND NEW.processed_at IS NULL AND NEW.interaction_id IS NULL)
)
BEGIN
    SELECT RAISE(ABORT, 'inbound event status transition is invalid');
END;
CREATE TRIGGER prevent_interaction_thread_identity_update
BEFORE UPDATE OF thread_id, subject_id, transport_id, channel,
                 external_account_id, conversation_id, external_thread_id, created_at
ON interaction_threads
WHEN NEW.thread_id IS NOT OLD.thread_id
  OR NEW.subject_id IS NOT OLD.subject_id
  OR NEW.transport_id IS NOT OLD.transport_id
  OR NEW.channel IS NOT OLD.channel
  OR NEW.external_account_id IS NOT OLD.external_account_id
  OR NEW.conversation_id IS NOT OLD.conversation_id
  OR NEW.external_thread_id IS NOT OLD.external_thread_id
  OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'interaction thread identity is immutable');
END;
CREATE TRIGGER prevent_interaction_thread_delete
BEFORE DELETE ON interaction_threads
BEGIN
    SELECT RAISE(ABORT, 'interaction threads cannot be deleted');
END;
""",
        )

    @staticmethod
    def _ensure_public_post_triggers(connection: sqlite3.Connection) -> None:
        """Keep public-post identity and moderation evidence append-only."""
        Database._execute_sql_script(
            connection,
            """
DROP TRIGGER IF EXISTS prevent_public_post_identity_update;
CREATE TRIGGER prevent_public_post_identity_update
BEFORE UPDATE OF post_id, subject_id, kind, title, content, content_hash,
                 author_label, author_provenance, identity_hash,
                 idempotency_key, created_at ON public_posts
BEGIN
    SELECT RAISE(ABORT, 'public post identity is immutable');
END;
DROP TRIGGER IF EXISTS validate_public_post_identity_insert;
CREATE TRIGGER validate_public_post_identity_insert
BEFORE INSERT ON public_posts
WHEN NEW.identity_hash IS NULL OR NEW.identity_hash = ''
BEGIN
    SELECT RAISE(ABORT, 'public post identity hash is required');
END;
DROP TRIGGER IF EXISTS prevent_public_post_moderation_update;
CREATE TRIGGER prevent_public_post_moderation_update
BEFORE UPDATE ON public_post_moderation_events
BEGIN
    SELECT RAISE(ABORT, 'public post moderation history is append-only');
END;
DROP TRIGGER IF EXISTS validate_public_post_moderation_transition;
CREATE TRIGGER validate_public_post_moderation_transition
BEFORE INSERT ON public_post_moderation_events
WHEN NEW.revision IS NULL
  OR NEW.revision < 1
  OR NEW.idempotency_key IS NULL
  OR NEW.idempotency_key = ''
  OR NOT EXISTS (
    SELECT 1 FROM public_posts p
    WHERE p.post_id = NEW.post_id
      AND p.subject_id = NEW.subject_id
      AND p.status = NEW.to_status
       AND (
           (
               NEW.revision = 1
               AND NEW.previous_event_id IS NULL
               AND NEW.from_status = 'pending_review'
               AND NEW.to_status IN ('published', 'rejected')
               AND NOT EXISTS (
                   SELECT 1 FROM public_post_moderation_events h
                   WHERE h.subject_id = NEW.subject_id AND h.post_id = NEW.post_id
               )
           )
           OR (
               NEW.revision > 1
               AND NEW.previous_event_id IS NOT NULL
               AND NEW.from_status IN ('published', 'rejected')
               AND NEW.to_status = 'archived'
               AND EXISTS (
                   SELECT 1 FROM public_post_moderation_events h
                    WHERE h.subject_id = NEW.subject_id AND h.post_id = NEW.post_id
                     AND h.event_id = NEW.previous_event_id
                     AND h.to_status = NEW.from_status
                     AND h.revision = NEW.revision - 1
                     AND h.revision = (
                         SELECT MAX(latest.revision)
                         FROM public_post_moderation_events latest
                         WHERE latest.subject_id = NEW.subject_id
                           AND latest.post_id = NEW.post_id
                     )
               )
           )
       )
)
BEGIN
    SELECT RAISE(ABORT, 'public post moderation transition mismatch');
END;
""",
        )

    @staticmethod
    def _ensure_wallet_acquisition_triggers(connection: sqlite3.Connection) -> None:
        """Repair v54 acquisition guards from the canonical migration script.

        Early v54 builds shipped an incorrect trigger definition.  Replaying
        the migration's idempotent DDL keeps repair behavior byte-for-byte
        aligned with fresh installs and prevents the repair copy from drifting.
        """
        Database._execute_sql_script(connection, MIGRATIONS[54])

    @staticmethod
    def _ensure_wallet_execution_triggers(connection: sqlite3.Connection) -> None:
        """Repair wallet transition guards without replaying stale contracts."""
        # v56 remains the historical table/identity bootstrap.  Its transition
        # guards are intentionally superseded below: replaying them alone would
        # remove receipt-evidence checks from schema 62 and reconciliation from
        # schema 71 on every startup.
        Database._execute_sql_script(connection, MIGRATIONS[56])
        execution_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(wallet_payment_executions)")
        }
        has_receipt_evidence = {
            "receipt_block_hash",
            "receipt_confirmations",
            "receipt_effect_hash",
        }.issubset(execution_columns)
        has_reconciliation_events = (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='wallet_payment_reconciliation_events'"
            ).fetchone()
            is not None
        )
        receipt_guard = (
            "".join(
                f"        AND NEW.{column} IS OLD.{column}\n"
                for column in (
                    "receipt_block_hash",
                    "receipt_confirmations",
                    "receipt_effect_hash",
                )
            )
            if has_receipt_evidence
            else ""
        )
        reconciliation_transition = (
            "    OR OLD.status='confirmed' AND NEW.status='unknown'\n"
            if has_reconciliation_events
            else ""
        )
        order_reconciliation_transition = (
            "    OR OLD.status = 'confirmed' AND NEW.status='unknown'\n"
            if has_reconciliation_events
            else ""
        )
        Database._execute_sql_script(
            connection,
            f"""
DROP TRIGGER IF EXISTS validate_wallet_execution_transition;
CREATE TRIGGER validate_wallet_execution_transition
BEFORE UPDATE ON wallet_payment_executions
WHEN NOT (
    NEW.status IS OLD.status
        AND NEW.tx_hash IS OLD.tx_hash
        AND NEW.error_code IS OLD.error_code
        AND NEW.receipt_status IS OLD.receipt_status
        AND NEW.receipt_block_number IS OLD.receipt_block_number
{receipt_guard}        AND NEW.attempt_count IS OLD.attempt_count
        AND NEW.state_hash IS OLD.state_hash
        AND NEW.last_audit_id IS OLD.last_audit_id
        AND NEW.updated_at IS OLD.updated_at
    OR OLD.status='signing' AND NEW.status IN ('broadcast','unknown','failed')
    OR OLD.status='broadcast' AND NEW.status IN ('unknown','confirmed','failed')
    OR OLD.status='unknown' AND NEW.status IN ('signing','broadcast','confirmed','failed')
{reconciliation_transition})
BEGIN SELECT RAISE(ABORT,'wallet payment execution transition is invalid'); END;
DROP TRIGGER IF EXISTS validate_wallet_execution_attempt_transition;
CREATE TRIGGER validate_wallet_execution_attempt_transition
BEFORE UPDATE ON wallet_payment_execution_attempts
WHEN NOT (
    NEW.status IS OLD.status
        AND NEW.tx_hash IS OLD.tx_hash
        AND NEW.error_code IS OLD.error_code
        AND NEW.completed_at IS OLD.completed_at
        AND NEW.state_hash IS OLD.state_hash
    OR OLD.status='signing' AND NEW.status IN ('broadcast','unknown','failed')
    OR OLD.status='broadcast' AND NEW.status IN ('unknown','confirmed','failed')
    OR OLD.status='unknown' AND NEW.status IN ('broadcast','confirmed','failed')
{reconciliation_transition})
BEGIN SELECT RAISE(ABORT,'wallet payment execution attempt transition is invalid'); END;
DROP TRIGGER IF EXISTS validate_wallet_order_transition;
CREATE TRIGGER validate_wallet_order_transition
BEFORE UPDATE ON wallet_payment_orders
WHEN NOT (
    NEW.status = OLD.status AND NEW.state_hash = OLD.state_hash AND NEW.updated_at = OLD.updated_at
    OR OLD.status = 'pending_policy' AND NEW.status IN ('awaiting_confirmation','reserved','rejected','cancelled','expired')
    OR OLD.status = 'awaiting_confirmation' AND NEW.status IN ('reserved','rejected','cancelled','expired')
    OR OLD.status = 'reserved' AND NEW.status IN ('cancelled','expired','signing')
    OR OLD.status = 'signing' AND NEW.status IN ('broadcast','unknown','failed')
    OR OLD.status = 'broadcast' AND NEW.status IN ('unknown','confirmed','failed')
    OR OLD.status = 'unknown' AND NEW.status IN ('signing','broadcast','confirmed','failed','refunded')
{order_reconciliation_transition}    OR OLD.status = 'failed' AND NEW.status = 'refunded'
)
BEGIN SELECT RAISE(ABORT,'wallet payment order transition is invalid'); END;
""",
        )

    @staticmethod
    def _ensure_wallet_submission_consent_contract(connection: sqlite3.Connection) -> None:
        """Verify the v59 column and reinstall its canonical immutable guards."""
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(wallet_bounty_submissions)")
        }
        if "consent_version" not in columns:
            raise RuntimeError("wallet submission consent column is missing")
        Database._execute_sql_script(connection, MIGRATIONS[59])

    @staticmethod
    def _upgrade_wallet_submission_consent(connection: sqlite3.Connection) -> None:
        """Add consent provenance and rehash every pre-v59 submission atomically."""
        connection.execute("DROP TRIGGER IF EXISTS prevent_wallet_submission_identity_update")
        connection.execute("DROP TRIGGER IF EXISTS validate_wallet_submission_transition")
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(wallet_bounty_submissions)")
        }
        had_consent_column = "consent_version" in columns
        rows = connection.execute(
            "SELECT * FROM wallet_bounty_submissions ORDER BY created_at,submission_id"
        ).fetchall()

        def legacy_hash(row: sqlite3.Row) -> str:
            return content_hash(
                {
                    "submission_id": str(row["submission_id"]),
                    "bounty_id": str(row["bounty_id"]),
                    "subject_id": str(row["subject_id"]),
                    "counterparty": str(row["counterparty"]),
                    "content": str(row["content"]),
                    "evidence_json": str(row["evidence_json"]),
                    "recipient_address": str(row["recipient_address"]),
                    "idempotency_key": str(row["idempotency_key"]),
                    "status": str(row["status"]),
                    "decision_reason": row["decision_reason"],
                }
            )

        # A migration must not turn an already-tampered legacy row into a
        # trusted row merely by changing the hash format.
        if not had_consent_column:
            for row in rows:
                if row["state_hash"] != legacy_hash(row):
                    raise RuntimeError(
                        f"wallet submission legacy state hash mismatch: {row['submission_id']}"
                    )
        if "consent_version" not in columns:
            connection.execute(
                "ALTER TABLE wallet_bounty_submissions ADD COLUMN consent_version "
                "INTEGER NOT NULL DEFAULT 1 CHECK(consent_version BETWEEN 1 AND 1000)"
            )
        rows = connection.execute(
            "SELECT * FROM wallet_bounty_submissions ORDER BY created_at,submission_id"
        ).fetchall()
        for row in rows:
            expected = wallet_submission_state_hash(
                submission_id=str(row["submission_id"]),
                bounty_id=str(row["bounty_id"]),
                subject_id=str(row["subject_id"]),
                counterparty=str(row["counterparty"]),
                content=str(row["content"]),
                evidence_json=str(row["evidence_json"]),
                recipient_address=str(row["recipient_address"]),
                idempotency_key=str(row["idempotency_key"]),
                consent_version=int(row["consent_version"]),
                status=str(row["status"]),
                decision_reason=row["decision_reason"],
            )
            if row["state_hash"] != expected:
                if had_consent_column and (
                    int(row["consent_version"]) != 1 or row["state_hash"] != legacy_hash(row)
                ):
                    raise RuntimeError(
                        f"wallet submission state hash mismatch: {row['submission_id']}"
                    )
                connection.execute(
                    "UPDATE wallet_bounty_submissions SET state_hash=? WHERE submission_id=?",
                    (expected, row["submission_id"]),
                )
        Database._execute_sql_script(connection, MIGRATIONS[59])

    @staticmethod
    def _upgrade_wallet_payment_policy_hashes(
        connection: sqlite3.Connection, *, approved: bool = False
    ) -> None:
        """Upgrade creation-anchored defaults or explicitly authorized legacy state."""
        rows = connection.execute(
            "SELECT p.*, s.created_at AS identity_created_at "
            "FROM wallet_payment_policies p JOIN subject_identity s USING(subject_id) "
            "ORDER BY p.subject_id"
        )
        for row in rows:
            expected = wallet_payment_policy_state_hash(
                subject_id=str(row["subject_id"]),
                mode=str(row["mode"]),
                allowed_network_ids_json=str(row["allowed_network_ids_json"]),
                allowed_asset_ids_json=str(row["allowed_asset_ids_json"]),
                per_order_limit=str(row["per_order_limit"]),
                daily_limit=str(row["daily_limit"]),
                monthly_limit=str(row["monthly_limit"]),
                daily_order_limit=strict_int(row["daily_order_limit"]),
                monthly_order_limit=strict_int(row["monthly_order_limit"]),
                min_balance=str(row["min_balance"]),
                max_observation_age_seconds=strict_int(row["max_observation_age_seconds"]),
                automatic_max_amount=str(row["automatic_max_amount"]),
                anomaly_block=int(row["anomaly_block"]),
                emergency_paused=int(row["emergency_paused"]),
                automation_enabled=(
                    # sqlite3.Row exposes column names through keys().
                    int(row["automation_enabled"])
                    if "automation_enabled" in row.keys()  # noqa: SIM118
                    else 0
                ),
                policy_version=strict_int(row["policy_version"]),
                updated_at=str(row["updated_at"]),
            )
            if row["state_hash"] == expected:
                continue
            is_default_bootstrap = (
                row["state_hash"] == "bootstrap"
                and str(row["mode"]) == "disabled"
                and str(row["allowed_network_ids_json"]) == "[]"
                and str(row["allowed_asset_ids_json"]) == "[]"
                and str(row["per_order_limit"]) == "0"
                and str(row["daily_limit"]) == "0"
                and str(row["monthly_limit"]) == "0"
                and int(row["daily_order_limit"]) == 0
                and int(row["monthly_order_limit"]) == 0
                and str(row["min_balance"]) == "0"
                and int(row["max_observation_age_seconds"]) == 0
                and str(row["automatic_max_amount"]) == "0"
                and int(row["anomaly_block"]) == 1
                and int(row["emergency_paused"]) == 0
                and int(row["policy_version"]) == 1
                and row["updated_at"] == row["identity_created_at"]
            )
            if row["state_hash"] == "bootstrap":
                if not is_default_bootstrap and not approved:
                    raise RuntimeError(
                        f"wallet payment policy bootstrap state mismatch: {row['subject_id']}"
                    )
            elif not approved:
                raise RuntimeError(
                    "wallet payment policy legacy hash cannot be upgraded safely: "
                    f"{row['subject_id']}"
                )
            elif row["state_hash"] != content_hash(
                {
                    "subject_id": row["subject_id"],
                    "version": row["policy_version"],
                    "mode": row["mode"],
                    "updated_at": row["updated_at"],
                }
            ):
                raise RuntimeError("wallet payment policy legacy hash mismatch")
            connection.execute(
                "UPDATE wallet_payment_policies SET state_hash=? WHERE subject_id=?",
                (expected, row["subject_id"]),
            )

    @staticmethod
    def _upgrade_wallet_payment_policy_recipient_allowlist(
        connection: sqlite3.Connection,
    ) -> None:
        """Add optional recipient policy fields and migrate their hash safely."""
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(wallet_payment_policies)")
        }
        if "recipient_allowlist_enabled" not in columns:
            connection.execute(
                "ALTER TABLE wallet_payment_policies ADD COLUMN "
                "recipient_allowlist_enabled INTEGER NOT NULL DEFAULT 0 "
                "CHECK(recipient_allowlist_enabled IN (0,1))"
            )
        if "allowed_recipient_addresses_json" not in columns:
            connection.execute(
                "ALTER TABLE wallet_payment_policies ADD COLUMN "
                "allowed_recipient_addresses_json TEXT NOT NULL DEFAULT '[]'"
            )
        rows = connection.execute(
            "SELECT p.*, s.created_at AS identity_created_at "
            "FROM wallet_payment_policies p JOIN subject_identity s USING(subject_id) "
            "ORDER BY p.subject_id"
        ).fetchall()
        for row in rows:
            expected = wallet_payment_policy_state_hash(
                subject_id=str(row["subject_id"]),
                mode=str(row["mode"]),
                allowed_network_ids_json=str(row["allowed_network_ids_json"]),
                allowed_asset_ids_json=str(row["allowed_asset_ids_json"]),
                per_order_limit=str(row["per_order_limit"]),
                daily_limit=str(row["daily_limit"]),
                monthly_limit=str(row["monthly_limit"]),
                daily_order_limit=strict_int(row["daily_order_limit"]),
                monthly_order_limit=strict_int(row["monthly_order_limit"]),
                min_balance=str(row["min_balance"]),
                max_observation_age_seconds=strict_int(row["max_observation_age_seconds"]),
                automatic_max_amount=str(row["automatic_max_amount"]),
                anomaly_block=int(row["anomaly_block"]),
                emergency_paused=int(row["emergency_paused"]),
                recipient_allowlist_enabled=int(row["recipient_allowlist_enabled"]),
                allowed_recipient_addresses_json=str(row["allowed_recipient_addresses_json"]),
                policy_version=strict_int(row["policy_version"]),
                updated_at=str(row["updated_at"]),
            )
            if str(row["state_hash"]) == expected:
                continue
            legacy = legacy_wallet_payment_policy_state_hash_v65(
                subject_id=str(row["subject_id"]),
                mode=str(row["mode"]),
                allowed_network_ids_json=str(row["allowed_network_ids_json"]),
                allowed_asset_ids_json=str(row["allowed_asset_ids_json"]),
                per_order_limit=str(row["per_order_limit"]),
                daily_limit=str(row["daily_limit"]),
                monthly_limit=str(row["monthly_limit"]),
                daily_order_limit=strict_int(row["daily_order_limit"]),
                monthly_order_limit=strict_int(row["monthly_order_limit"]),
                min_balance=str(row["min_balance"]),
                max_observation_age_seconds=strict_int(row["max_observation_age_seconds"]),
                automatic_max_amount=str(row["automatic_max_amount"]),
                anomaly_block=int(row["anomaly_block"]),
                emergency_paused=int(row["emergency_paused"]),
                policy_version=strict_int(row["policy_version"]),
                updated_at=str(row["updated_at"]),
            )
            is_default_bootstrap = (
                str(row["state_hash"]) == "bootstrap"
                and str(row["mode"]) == "disabled"
                and str(row["allowed_network_ids_json"]) == "[]"
                and str(row["allowed_asset_ids_json"]) == "[]"
                and str(row["per_order_limit"]) == "0"
                and str(row["daily_limit"]) == "0"
                and str(row["monthly_limit"]) == "0"
                and int(row["daily_order_limit"]) == 0
                and int(row["monthly_order_limit"]) == 0
                and str(row["min_balance"]) == "0"
                and int(row["max_observation_age_seconds"]) == 0
                and str(row["automatic_max_amount"]) == "0"
                and int(row["anomaly_block"]) == 1
                and int(row["emergency_paused"]) == 0
                and int(row["recipient_allowlist_enabled"]) == 0
                and str(row["allowed_recipient_addresses_json"]) == "[]"
                and int(row["policy_version"]) == 1
                and str(row["updated_at"]) == str(row["identity_created_at"])
            )
            if str(row["state_hash"]) != legacy and not is_default_bootstrap:
                raise RuntimeError(
                    "wallet payment policy hash mismatch before recipient allow-list migration: "
                    f"{row['subject_id']}"
                )
            connection.execute(
                "UPDATE wallet_payment_policies SET state_hash=? WHERE subject_id=?",
                (expected, row["subject_id"]),
            )

    @staticmethod
    def _upgrade_wallet_payment_policy_automation(connection: sqlite3.Connection) -> None:
        """Add the closed-by-default automation switch and bind it to policy hashes."""
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(wallet_payment_policies)")
        }
        if "automation_enabled" not in columns:
            connection.execute(
                "ALTER TABLE wallet_payment_policies ADD COLUMN automation_enabled "
                "INTEGER NOT NULL DEFAULT 0 CHECK(automation_enabled IN (0,1))"
            )
        for row in connection.execute(
            "SELECT * FROM wallet_payment_policies ORDER BY subject_id"
        ).fetchall():
            expected = wallet_payment_policy_state_hash(
                subject_id=str(row["subject_id"]),
                mode=str(row["mode"]),
                allowed_network_ids_json=str(row["allowed_network_ids_json"]),
                allowed_asset_ids_json=str(row["allowed_asset_ids_json"]),
                per_order_limit=str(row["per_order_limit"]),
                daily_limit=str(row["daily_limit"]),
                monthly_limit=str(row["monthly_limit"]),
                daily_order_limit=int(row["daily_order_limit"]),
                monthly_order_limit=int(row["monthly_order_limit"]),
                min_balance=str(row["min_balance"]),
                max_observation_age_seconds=int(row["max_observation_age_seconds"]),
                automatic_max_amount=str(row["automatic_max_amount"]),
                anomaly_block=int(row["anomaly_block"]),
                emergency_paused=int(row["emergency_paused"]),
                recipient_allowlist_enabled=int(row["recipient_allowlist_enabled"]),
                allowed_recipient_addresses_json=str(row["allowed_recipient_addresses_json"]),
                automation_enabled=int(row["automation_enabled"]),
                policy_version=int(row["policy_version"]),
                updated_at=str(row["updated_at"]),
            )
            connection.execute(
                "UPDATE wallet_payment_policies SET state_hash=? WHERE subject_id=?",
                (expected, row["subject_id"]),
            )

    @staticmethod
    def _upgrade_wallet_ledger_hashes(connection: sqlite3.Connection, *, approved: bool) -> None:
        for table, primary_key, trigger, hasher, legacy_hasher in (
            (
                "wallet_ledger_journals",
                "journal_id",
                "prevent_wallet_journal_update",
                wallet_journal_hash,
                legacy_journal_hash,
            ),
            (
                "wallet_ledger_entries",
                "entry_id",
                "prevent_wallet_entry_update",
                wallet_entry_hash,
                legacy_entry_hash,
            ),
        ):
            connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            for row in connection.execute(f"SELECT * FROM {table} ORDER BY {primary_key}"):
                expected = hasher(row)
                if row["state_hash"] == expected:
                    continue
                if row["state_hash"] != legacy_hasher(row):
                    raise RuntimeError(f"wallet ledger legacy hash mismatch: {row[primary_key]}")
                if not approved:
                    raise RuntimeError("wallet ledger legacy timestamps require explicit approval")
                connection.execute(
                    f"UPDATE {table} SET state_hash=? WHERE {primary_key}=?",
                    (expected, row[primary_key]),
                )
            connection.execute(
                f"CREATE TRIGGER {trigger} BEFORE UPDATE ON {table} "
                "BEGIN SELECT RAISE(ABORT,'wallet ledger is append-only'); END"
            )

    @staticmethod
    def _upgrade_wallet_temporal_hashes(connection: sqlite3.Connection, *, approved: bool) -> None:
        """Bind payment chronology to hashes without trusting damaged rows.

        Schema 60/early-61 order and execution hashes did not cover their
        chronology fields.  A matching legacy hash proves only the fields it
        originally covered; it cannot prove that a timestamp was not edited
        offline.  Consequently those rows require the same explicit,
        fingerprint-bound operator approval as the other legacy wallet
        records.  Never silently re-sign a damaged history during migration.
        """
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(wallet_payment_orders)")
        }
        if "authorized_at" not in columns:
            connection.execute("ALTER TABLE wallet_payment_orders ADD COLUMN authorized_at TEXT")
        connection.execute("DROP TRIGGER IF EXISTS prevent_wallet_order_identity_update")
        connection.execute("DROP TRIGGER IF EXISTS validate_wallet_order_transition")
        connection.execute("DROP TRIGGER IF EXISTS validate_wallet_execution_transition")

        def old_order(row: sqlite3.Row) -> str:
            return content_hash(
                {
                    "order_id": row["order_id"],
                    "subject_id": row["subject_id"],
                    "bounty_id": row["bounty_id"],
                    "submission_id": row["submission_id"],
                    "network_id": row["network_id"],
                    "asset_id": row["asset_id"],
                    "recipient_address": row["recipient_address"],
                    "amount": row["amount"],
                    "payment_mode": row["payment_mode"],
                    "policy_version": int(row["policy_version"]),
                    "idempotency_key": row["idempotency_key"],
                    "status": row["status"],
                }
            )

        for row in connection.execute(
            "SELECT * FROM wallet_payment_orders ORDER BY order_id"
        ).fetchall():
            authorized_at = row["authorized_at"]
            if authorized_at is None and row["status"] in {
                "reserved",
                "signing",
                "broadcast",
                "unknown",
                "confirmed",
                "failed",
                "refunded",
            }:
                authorized_at = row["updated_at"]
            temporal = content_hash(
                {
                    "order_id": row["order_id"],
                    "subject_id": row["subject_id"],
                    "bounty_id": row["bounty_id"],
                    "submission_id": row["submission_id"],
                    "network_id": row["network_id"],
                    "asset_id": row["asset_id"],
                    "recipient_address": row["recipient_address"],
                    "amount": row["amount"],
                    "payment_mode": row["payment_mode"],
                    "policy_version": int(row["policy_version"]),
                    "idempotency_key": row["idempotency_key"],
                    "status": row["status"],
                    "created_at": row["created_at"],
                    "authorized_at": authorized_at,
                    "updated_at": row["updated_at"],
                }
            )
            if row["state_hash"] != temporal and row["state_hash"] != old_order(row):
                raise RuntimeError(f"wallet payment order legacy hash mismatch: {row['order_id']}")
            if row["state_hash"] == temporal:
                continue
            if not approved:
                raise RuntimeError(
                    "wallet payment order legacy timestamps require explicit approval"
                )
            connection.execute(
                "UPDATE wallet_payment_orders SET authorized_at=?,state_hash=? WHERE order_id=?",
                (authorized_at, temporal, row["order_id"]),
            )

        def old_execution(row: sqlite3.Row) -> str:
            keys = (
                "execution_id",
                "subject_id",
                "order_id",
                "network_id",
                "asset_id",
                "source_address",
                "recipient_address",
                "asset_type",
                "contract_address",
                "amount",
                "chain_id",
                "nonce",
                "gas_limit",
                "max_fee_per_gas",
                "request_id",
                "request_hash",
                "signer_id",
                "status",
                "tx_hash",
                "error_code",
                "receipt_status",
                "receipt_block_number",
                "attempt_count",
            )
            return content_hash({key: row[key] for key in keys})

        execution_rows = connection.execute(
            "SELECT * FROM wallet_payment_executions ORDER BY execution_id"
        ).fetchall()
        for row in execution_rows:
            temporal = content_hash(
                {
                    **{
                        key: row[key]
                        for key in (
                            "execution_id",
                            "subject_id",
                            "order_id",
                            "network_id",
                            "asset_id",
                            "source_address",
                            "recipient_address",
                            "asset_type",
                            "contract_address",
                            "amount",
                            "chain_id",
                            "nonce",
                            "gas_limit",
                            "max_fee_per_gas",
                            "request_id",
                            "request_hash",
                            "signer_id",
                            "status",
                            "tx_hash",
                            "error_code",
                            "receipt_status",
                            "receipt_block_number",
                            "attempt_count",
                        )
                    },
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
            )
            if row["state_hash"] == temporal:
                continue
            if row["state_hash"] != old_execution(row):
                raise RuntimeError(
                    f"wallet payment execution legacy hash mismatch: {row['execution_id']}"
                )
            if not approved:
                raise RuntimeError(
                    "wallet payment execution legacy timestamps require explicit approval"
                )
            connection.execute(
                "UPDATE wallet_payment_executions SET state_hash=? WHERE execution_id=?",
                (temporal, row["execution_id"]),
            )
        # Restore the canonical execution transition guards after the
        # timestamp backfill temporarily disabled them.
        Database._execute_sql_script(connection, MIGRATIONS[56])

    @staticmethod
    def _upgrade_wallet_receipt_evidence(connection: sqlite3.Connection) -> None:
        """Add durable block/effect evidence without trusting old hashes."""
        existing_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(wallet_payment_executions)")
        }
        evidence_preexisted = {
            name
            for name in ("receipt_block_hash", "receipt_confirmations", "receipt_effect_hash")
            if name in existing_columns
        }
        for name, declaration in (
            ("receipt_block_hash", "TEXT"),
            ("receipt_confirmations", "INTEGER"),
            ("receipt_effect_hash", "TEXT"),
        ):
            if name not in existing_columns:
                connection.execute(
                    f"ALTER TABLE wallet_payment_executions ADD COLUMN {name} {declaration}"
                )

        base_keys = (
            "execution_id",
            "subject_id",
            "order_id",
            "network_id",
            "asset_id",
            "source_address",
            "recipient_address",
            "asset_type",
            "contract_address",
            "amount",
            "chain_id",
            "nonce",
            "gas_limit",
            "max_fee_per_gas",
            "request_id",
            "request_hash",
            "signer_id",
            "status",
            "tx_hash",
            "error_code",
            "receipt_status",
            "receipt_block_number",
            "attempt_count",
            "created_at",
            "updated_at",
        )
        for row in connection.execute(
            "SELECT * FROM wallet_payment_executions ORDER BY execution_id"
        ).fetchall():
            old_hash = content_hash({key: row[key] for key in base_keys})
            new_values = {key: row[key] for key in base_keys[:-2]}
            new_values.update(
                {
                    "receipt_block_hash": row["receipt_block_hash"],
                    "receipt_confirmations": row["receipt_confirmations"],
                    "receipt_effect_hash": row["receipt_effect_hash"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
            )
            new_hash = content_hash(new_values)
            if row["state_hash"] == new_hash:
                continue
            # Schema 61 did not define these columns.  If a hand-modified or
            # vendor-specific database already had them, an old state hash
            # cannot authenticate their values; refuse to launder them into a
            # trusted history during migration.
            if evidence_preexisted and any(
                row[name] is not None
                for name in ("receipt_block_hash", "receipt_confirmations", "receipt_effect_hash")
            ):
                raise RuntimeError(
                    f"wallet payment execution receipt evidence is unauthenticated: {row['execution_id']}"
                )
            if row["state_hash"] != old_hash:
                raise RuntimeError(
                    f"wallet payment execution receipt evidence hash mismatch: {row['execution_id']}"
                )
            connection.execute(
                "UPDATE wallet_payment_executions SET receipt_confirmations=?,state_hash=? "
                "WHERE execution_id=?",
                (new_values["receipt_confirmations"], new_hash, row["execution_id"]),
            )
        Database._execute_sql_script(connection, MIGRATIONS[62])

    @staticmethod
    def _ensure_public_post_moderation_history(connection: sqlite3.Connection) -> None:
        """Backfill an auditable baseline for posts created before history existed."""
        rows = connection.execute(
            """SELECT p.* FROM public_posts p
               WHERE p.status IN ('published', 'rejected', 'archived')
                 AND NOT EXISTS (
                     SELECT 1 FROM public_post_moderation_events h
                     WHERE h.subject_id = p.subject_id AND h.post_id = p.post_id
                 )
               ORDER BY p.created_at, p.post_id"""
        ).fetchall()
        for row in rows:
            post_id = str(row["post_id"])
            subject_id = str(row["subject_id"])
            actor = "migration"
            reason = "legacy moderation baseline"
            baseline_created_at = str(row["created_at"])
            initial_status = (
                ("published" if row["published_at"] is not None else "rejected")
                if row["status"] == "archived"
                else str(row["status"])
            )
            baseline = {
                "event_id": f"post-moderation-legacy-{post_id}-0-baseline",
                "subject_id": subject_id,
                "post_id": post_id,
                "from_status": "pending_review",
                "to_status": initial_status,
                "actor": actor,
                "reason": reason,
                "created_at": baseline_created_at,
            }
            connection.execute(
                """INSERT INTO public_post_moderation_events(
                    event_id, subject_id, post_id, from_status, to_status,
                    actor, reason, state_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    baseline["event_id"],
                    subject_id,
                    post_id,
                    baseline["from_status"],
                    baseline["to_status"],
                    actor,
                    reason,
                    content_hash(baseline),
                    baseline_created_at,
                ),
            )
            if row["status"] != "archived":
                continue
            archive_created_at = str(row["updated_at"])
            # Keep the persisted timestamp shape valid even when an older
            # database recorded equal or reversed created/updated values.  The
            # event-id suffix gives the deterministic order for equal values.
            if archive_created_at < baseline_created_at:
                archive_created_at = baseline_created_at
            archive = {
                "event_id": f"post-moderation-legacy-{post_id}-1-archive",
                "subject_id": subject_id,
                "post_id": post_id,
                "from_status": initial_status,
                "to_status": "archived",
                "actor": actor,
                "reason": reason,
                "created_at": archive_created_at,
            }
            connection.execute(
                """INSERT INTO public_post_moderation_events(
                    event_id, subject_id, post_id, from_status, to_status,
                    actor, reason, state_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    archive["event_id"],
                    subject_id,
                    post_id,
                    archive["from_status"],
                    archive["to_status"],
                    actor,
                    reason,
                    content_hash(archive),
                    archive_created_at,
                ),
            )

    @staticmethod
    def _upgrade_public_post_evidence(connection: sqlite3.Connection) -> None:
        """Upgrade legacy moderation rows into a deterministic revision chain."""
        posts = connection.execute(
            "SELECT * FROM public_posts ORDER BY subject_id, created_at, post_id"
        ).fetchall()
        for post in posts:
            if content_hash(post["content"]) != post["content_hash"]:
                raise RuntimeError(f"public post content integrity mismatch: {post['post_id']}")
            connection.execute(
                "UPDATE public_posts SET identity_hash = ? WHERE post_id = ?",
                (public_post_identity_hash(post), post["post_id"]),
            )
            events = connection.execute(
                "SELECT * FROM public_post_moderation_events "
                "WHERE subject_id = ? AND post_id = ? ORDER BY created_at, event_id",
                (post["subject_id"], post["post_id"]),
            ).fetchall()
            for event in events:
                legacy_state = {
                    key: event[key]
                    for key in (
                        "event_id",
                        "subject_id",
                        "post_id",
                        "from_status",
                        "to_status",
                        "actor",
                        "reason",
                        "created_at",
                    )
                }
                if event["state_hash"] != content_hash(legacy_state):
                    raise RuntimeError(
                        f"public post moderation integrity mismatch: {event['event_id']}"
                    )
            remaining = list(events)
            chain: list[sqlite3.Row] = []
            expected_status = "pending_review"
            allowed = {
                "pending_review": {"published", "rejected"},
                "published": {"archived"},
                "rejected": {"archived"},
            }
            while remaining:
                candidates = [
                    event
                    for event in remaining
                    if event["from_status"] == expected_status
                    and event["to_status"] in allowed.get(expected_status, set())
                ]
                if len(candidates) != 1:
                    raise RuntimeError(
                        f"public post moderation migration is ambiguous: {post['post_id']}"
                    )
                event = candidates[0]
                chain.append(event)
                remaining.remove(event)
                expected_status = str(event["to_status"])
            if expected_status != post["status"] and not (
                not chain and post["status"] in {"draft", "pending_review"}
            ):
                raise RuntimeError(
                    f"public post moderation migration is incomplete: {post['post_id']}"
                )
            previous_event_id: str | None = None
            for revision, event in enumerate(chain, start=1):
                idempotency_key = str(event["event_id"])
                state = {
                    "event_id": event["event_id"],
                    "subject_id": event["subject_id"],
                    "post_id": event["post_id"],
                    "from_status": event["from_status"],
                    "to_status": event["to_status"],
                    "actor": event["actor"],
                    "reason": event["reason"],
                    "revision": revision,
                    "previous_event_id": previous_event_id,
                    "idempotency_key": idempotency_key,
                    "created_at": event["created_at"],
                }
                connection.execute(
                    "UPDATE public_post_moderation_events SET revision = ?, "
                    "previous_event_id = ?, idempotency_key = ?, state_hash = ? "
                    "WHERE event_id = ?",
                    (
                        revision,
                        previous_event_id,
                        idempotency_key,
                        content_hash(state),
                        event["event_id"],
                    ),
                )
                previous_event_id = str(event["event_id"])

        controls = connection.execute("SELECT * FROM public_post_controls").fetchall()
        for row in controls:
            legacy_state = {
                key: row[key]
                for key in (
                    "subject_id",
                    "rate_limit_per_hour",
                    "queue_cap",
                    "captcha_ttl_seconds",
                    "captcha_max_attempts",
                    "captcha_mode",
                    "updated_at",
                    "updated_by",
                )
            }
            if row["state_hash"] != content_hash(legacy_state):
                raise RuntimeError(f"public post controls integrity mismatch: {row['subject_id']}")
            state = {
                key: row[key]
                for key in (
                    "subject_id",
                    "rate_limit_per_hour",
                    "queue_cap",
                    "captcha_ttl_seconds",
                    "captcha_max_attempts",
                    "captcha_mode",
                    "storage_cap_bytes",
                    "captcha_issue_limit_per_hour",
                    "captcha_global_rate_per_minute",
                    "updated_at",
                    "updated_by",
                )
            }
            connection.execute(
                "UPDATE public_post_controls SET state_hash = ? WHERE subject_id = ?",
                (content_hash(state), row["subject_id"]),
            )

        Database._execute_sql_script(
            connection,
            """
CREATE UNIQUE INDEX IF NOT EXISTS uq_public_post_moderation_revision
    ON public_post_moderation_events(subject_id, post_id, revision);
CREATE UNIQUE INDEX IF NOT EXISTS uq_public_post_moderation_idempotency
    ON public_post_moderation_events(subject_id, idempotency_key);
CREATE TRIGGER prevent_public_post_moderation_update
BEFORE UPDATE ON public_post_moderation_events
BEGIN
    SELECT RAISE(ABORT, 'public post moderation history is append-only');
END;
""",
        )

    @staticmethod
    def _ensure_audit_record_triggers(connection: sqlite3.Connection) -> None:
        Database._execute_sql_script(
            connection,
            """
CREATE TRIGGER IF NOT EXISTS prevent_audit_record_update
BEFORE UPDATE ON audit_records BEGIN
    SELECT RAISE(ABORT, 'audit records are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_audit_record_delete
BEFORE DELETE ON audit_records BEGIN
    SELECT RAISE(ABORT, 'audit records cannot be deleted');
END;
""",
        )

    @staticmethod
    def _ensure_behavior_log_revision_triggers(connection: sqlite3.Connection) -> None:
        Database._execute_sql_script(
            connection,
            """
DROP TRIGGER IF EXISTS validate_behavior_log_revision_insert;
CREATE TRIGGER validate_behavior_log_revision_insert
BEFORE INSERT ON behavior_log_revisions
WHEN NOT EXISTS (
    SELECT 1
    FROM behavior_logs l
    JOIN actions a ON a.action_id = l.action_id
    WHERE l.log_id = NEW.log_id
      AND l.action_id = NEW.action_id
      AND l.subject_id = NEW.subject_id
      AND a.subject_id = NEW.subject_id
      AND a.status = NEW.result_status
      AND NEW.revision_number = COALESCE(
          (SELECT MAX(r.revision_number) + 1
           FROM behavior_log_revisions r WHERE r.log_id = NEW.log_id), 1
      )
      AND (
          NEW.revision_number = 1
          OR (
              NEW.revision_number = 2
              AND NEW.result_status <> 'unknown'
              AND EXISTS (
                  SELECT 1
                  FROM behavior_log_revisions previous
                  WHERE previous.log_id = NEW.log_id
                    AND previous.revision_number = 1
                    AND previous.result_status = 'unknown'
                    AND previous.action_type IS NEW.action_type
                    AND previous.public_goal_reference IS NEW.public_goal_reference
                    AND previous.tool IS NEW.tool
                    AND previous.public_target IS NEW.public_target
                    AND previous.resource_summary IS NEW.resource_summary
                    AND previous.redaction_reason IS NEW.redaction_reason
              )
          )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'behavior log revision does not match reconciliation contract');
END;
DROP TRIGGER IF EXISTS prevent_behavior_log_revision_update;
CREATE TRIGGER prevent_behavior_log_revision_update
BEFORE UPDATE ON behavior_log_revisions BEGIN
    SELECT RAISE(ABORT, 'behavior log revisions are append-only');
END;
DROP TRIGGER IF EXISTS prevent_behavior_log_revision_delete;
CREATE TRIGGER prevent_behavior_log_revision_delete
BEFORE DELETE ON behavior_log_revisions BEGIN
    SELECT RAISE(ABORT, 'behavior log revisions cannot be deleted');
END;
DROP TRIGGER IF EXISTS prevent_behavior_log_update;
CREATE TRIGGER prevent_behavior_log_update
BEFORE UPDATE ON behavior_logs BEGIN
    SELECT RAISE(ABORT, 'behavior logs are append-only');
END;
DROP TRIGGER IF EXISTS prevent_behavior_log_delete;
CREATE TRIGGER prevent_behavior_log_delete
BEFORE DELETE ON behavior_logs BEGIN
    SELECT RAISE(ABORT, 'behavior logs cannot be deleted');
END;
""",
        )

    @staticmethod
    def _backfill_behavior_log_revisions(connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            "SELECT l.* FROM behavior_logs l "
            "LEFT JOIN behavior_log_revisions r ON r.log_id = l.log_id "
            "WHERE r.log_id IS NULL ORDER BY l.occurred_at, l.log_id"
        ).fetchall()
        for row in rows:
            revision = {
                **dict(row),
                "revision_number": 1,
                "reason": "legacy baseline",
            }
            connection.execute(
                """INSERT INTO behavior_log_revisions(
                    revision_id, log_id, action_id, subject_id, revision_number,
                    occurred_at, action_type, public_goal_reference, tool, public_target,
                    result_status, side_effect_summary, resource_summary, public_explanation,
                    redaction_reason, state_hash, reason, created_at
                ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    new_id("brev"),
                    row["log_id"],
                    row["action_id"],
                    row["subject_id"],
                    row["occurred_at"],
                    row["action_type"],
                    row["public_goal_reference"],
                    row["tool"],
                    row["public_target"],
                    row["result_status"],
                    row["side_effect_summary"],
                    row["resource_summary"],
                    row["public_explanation"],
                    row["redaction_reason"],
                    behavior_log_state_hash(revision),
                    revision["reason"],
                    row["occurred_at"],
                ),
            )

    @staticmethod
    def _assert_behavior_log_revisions_complete(connection: sqlite3.Connection) -> None:
        missing = connection.execute(
            "SELECT l.log_id FROM behavior_logs l "
            "LEFT JOIN behavior_log_revisions r ON r.log_id = l.log_id "
            "WHERE r.log_id IS NULL LIMIT 1"
        ).fetchone()
        if missing is not None:
            raise RuntimeError(
                "behavior log revision history is incomplete: " + str(missing["log_id"])
            )

    @staticmethod
    def _ensure_action_revisions(connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            "SELECT a.* FROM actions a "
            "LEFT JOIN action_revisions r ON r.action_id = a.action_id "
            "WHERE r.action_id IS NULL ORDER BY a.prepared_at, a.action_id"
        ).fetchall()
        for row in rows:
            connection.execute(
                """INSERT INTO action_revisions(
                    revision_id, action_id, subject_id, revision_number, status,
                    state_hash, reason, created_at
                ) VALUES (?, ?, ?, 1, ?, ?, ?, ?)""",
                (
                    new_id("arev"),
                    row["action_id"],
                    row["subject_id"],
                    row["status"],
                    action_state_hash(row),
                    "legacy baseline",
                    utc_now(),
                ),
            )

    def _ensure_training_policies(self) -> None:
        with self.connection() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO training_policies(subject_id, updated_at)
                   SELECT subject_id, updated_at FROM subject_identity"""
            )
            connection.commit()

    @staticmethod
    def _upgrade_provider_health_metrics(connection: sqlite3.Connection) -> None:
        """Add provider health metric columns without making replay unsafe."""
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='provider_health_buckets'"
        ).fetchone()
        if table is None:
            raise RuntimeError("provider health buckets table is missing")
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(provider_health_buckets)")
        }
        if "error_counts_json" not in columns:
            connection.execute(
                "ALTER TABLE provider_health_buckets "
                "ADD COLUMN error_counts_json TEXT NOT NULL DEFAULT '{}'"
            )
        if "latency_samples_json" not in columns:
            connection.execute(
                "ALTER TABLE provider_health_buckets "
                "ADD COLUMN latency_samples_json TEXT NOT NULL DEFAULT '[]'"
            )
        if "unknown_count" not in columns:
            connection.execute(
                "ALTER TABLE provider_health_buckets "
                "ADD COLUMN unknown_count INTEGER NOT NULL DEFAULT 0"
            )

    @staticmethod
    def _upgrade_wallet_payment_policy_layout(connection: sqlite3.Connection) -> None:
        """Reconcile the exact v63 ALTER layout without changing policy values."""
        definitions: tuple[str, ...] = (
            "subject_id TEXT PRIMARY KEY REFERENCES subject_identity(subject_id)",
            "mode TEXT NOT NULL CHECK(mode IN ('disabled','conditional_confirmation','automatic'))",
            "allowed_network_ids_json TEXT NOT NULL",
            "allowed_asset_ids_json TEXT NOT NULL",
            "per_order_limit TEXT NOT NULL",
            "daily_limit TEXT NOT NULL",
            "monthly_limit TEXT NOT NULL",
            "daily_order_limit INTEGER NOT NULL CHECK(daily_order_limit >= 0)",
            "monthly_order_limit INTEGER NOT NULL CHECK(monthly_order_limit >= 0)",
            "min_balance TEXT NOT NULL",
            "max_observation_age_seconds INTEGER NOT NULL CHECK(max_observation_age_seconds >= 0)",
            "automatic_max_amount TEXT NOT NULL",
            "anomaly_block INTEGER NOT NULL CHECK(anomaly_block IN (0,1))",
            "emergency_paused INTEGER NOT NULL CHECK(emergency_paused IN (0,1))",
            "recipient_allowlist_enabled INTEGER NOT NULL DEFAULT 0 CHECK(recipient_allowlist_enabled IN (0,1))",
            "allowed_recipient_addresses_json TEXT NOT NULL DEFAULT '[]'",
            "policy_version INTEGER NOT NULL CHECK(policy_version > 0)",
            "updated_at TEXT NOT NULL",
            "state_hash TEXT NOT NULL",
        )
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(wallet_payment_policies)")
        }
        if "automation_enabled" in columns:
            definitions += (
                "automation_enabled INTEGER NOT NULL DEFAULT 0 CHECK(automation_enabled IN (0,1))",
            )
        expected = "CREATE TABLE wallet_payment_policies (" + ",\n".join(definitions) + ")"
        legacy_order = definitions[:14] + definitions[16:19] + definitions[14:16] + definitions[19:]
        legacy = "CREATE TABLE wallet_payment_policies (" + ",\n".join(legacy_order) + ")"
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='wallet_payment_policies'"
        ).fetchone()
        actual = _canonical_schema_sql(str(row["sql"])) if row else None
        if actual not in {_canonical_schema_sql(expected), _canonical_schema_sql(legacy)}:
            raise RuntimeError("wallet payment policy layout contract mismatch")
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE tbl_name='wallet_payment_policies' "
            "AND sql IS NOT NULL AND type IN ('trigger','index')"
        ).fetchone():
            raise RuntimeError("wallet payment policy layout has unexpected dependent objects")
        if actual == _canonical_schema_sql(expected):
            return
        for table in connection.execute("PRAGMA table_list"):
            name = str(table["name"]).replace('"', '""')
            if any(
                reference["table"] == "wallet_payment_policies"
                for reference in connection.execute(f'PRAGMA foreign_key_list("{name}")')
            ):
                raise RuntimeError("wallet payment policy layout has unexpected foreign keys")
        connection.execute(
            "CREATE TEMP TABLE noyra_v80_payment_policy_layout AS SELECT * FROM wallet_payment_policies"
        )
        connection.execute("DROP TABLE wallet_payment_policies")
        connection.execute(expected)
        names = ",".join(definition.split()[0] for definition in definitions)
        connection.execute(
            f"INSERT INTO wallet_payment_policies ({names}) "
            f"SELECT {names} FROM temp.noyra_v80_payment_policy_layout"
        )
        connection.execute("DROP TABLE temp.noyra_v80_payment_policy_layout")

    @staticmethod
    def _upgrade_provider_health_bucket_hashes(connection: sqlite3.Connection) -> None:
        """Authenticate the pre-unknown-outcome format before changing its hash."""
        from .errors import IntegrityError
        from .provider_health import ProviderHealthStore

        for row in connection.execute("SELECT * FROM provider_health_buckets"):
            try:
                payload = {
                    "subject_id": row["subject_id"],
                    "provider_kind": row["provider_kind"],
                    "provider_id": row["provider_id"],
                    "bucket_start": row["bucket_start"],
                    "attempt_count": int(row["attempt_count"]),
                    "success_count": int(row["success_count"]),
                    "failure_count": int(row["failure_count"]),
                    "latency_total_ms": int(row["latency_total_ms"]),
                    "last_success_at": row["last_success_at"],
                    "last_failure_at": row["last_failure_at"],
                    "error_counts": json.loads(row["error_counts_json"] or "{}") or {},
                    "latency_samples": list(json.loads(row["latency_samples_json"] or "[]") or []),
                }
            except (TypeError, ValueError) as error:
                raise IntegrityError("provider health detail counters are invalid") from error
            if int(row["unknown_count"]) == 0 and row["state_hash"] == content_hash(payload):
                payload["unknown_count"] = 0
                migrated = dict(row)
                migrated["state_hash"] = content_hash(payload)
                ProviderHealthStore._verify_bucket(migrated)
                connection.execute(
                    """UPDATE provider_health_buckets SET state_hash=?
                       WHERE subject_id=? AND provider_kind=? AND provider_id=? AND bucket_start=?""",
                    (
                        migrated["state_hash"],
                        row["subject_id"],
                        row["provider_kind"],
                        row["provider_id"],
                        row["bucket_start"],
                    ),
                )
            else:
                ProviderHealthStore._verify_bucket(row)

    @staticmethod
    def _ensure_migration_target_attestation_columns(connection: sqlite3.Connection) -> None:
        """Add target attestation columns without making migration replay unsafe."""
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='migration_targets'"
        ).fetchone()
        if table is None:
            raise RuntimeError("migration targets table is missing")
        columns = {
            str(row["name"]) for row in connection.execute("PRAGMA table_info(migration_targets)")
        }
        if "attested_at" not in columns:
            connection.execute("ALTER TABLE migration_targets ADD COLUMN attested_at TEXT")
        if "attestation_epoch" not in columns:
            connection.execute("ALTER TABLE migration_targets ADD COLUMN attestation_epoch TEXT")

    @staticmethod
    def _ensure_migration_target_recipient_columns(connection: sqlite3.Connection) -> None:
        """Install the recipient-encryption identity without fabricating keys."""
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='migration_targets'"
        ).fetchone()
        if table is None:
            raise RuntimeError("migration targets table is missing")
        columns = {
            str(row["name"]) for row in connection.execute("PRAGMA table_info(migration_targets)")
        }
        if "recipient_public_key" not in columns:
            connection.execute("ALTER TABLE migration_targets ADD COLUMN recipient_public_key TEXT")
        if "recipient_key_fingerprint" not in columns:
            connection.execute(
                "ALTER TABLE migration_targets ADD COLUMN recipient_key_fingerprint TEXT"
            )

    @staticmethod
    def _ensure_migration_task_epoch_column(connection: sqlite3.Connection) -> None:
        """Add the target epoch fence without making migration replay unsafe."""
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='migration_tasks'"
        ).fetchone()
        if table is None:
            raise RuntimeError("migration tasks table is missing")
        columns = {
            str(row["name"]) for row in connection.execute("PRAGMA table_info(migration_tasks)")
        }
        if "target_epoch_id" not in columns:
            connection.execute(
                "ALTER TABLE migration_tasks ADD COLUMN target_epoch_id "
                "TEXT REFERENCES migration_epochs(epoch_id)"
            )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_migration_tasks_target_epoch "
            "ON migration_tasks(target_epoch_id) WHERE target_epoch_id IS NOT NULL"
        )

    @staticmethod
    def _ensure_migration_proposal_approval_mode(connection: sqlite3.Connection) -> None:
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(migration_proposals)")
        }
        if "approval_mode" not in columns:
            connection.execute(
                "ALTER TABLE migration_proposals ADD COLUMN approval_mode TEXT NOT NULL "
                "DEFAULT 'manual' CHECK (approval_mode IN ("
                "'disabled','manual','self_attested','dual_review','policy_auto','emergency_recovery'))"
            )

    @staticmethod
    def _ensure_embedding_config_hash_column(connection: sqlite3.Connection) -> None:
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(embedding_resources)")
        }
        if "config_hash" not in columns:
            connection.execute("ALTER TABLE embedding_resources ADD COLUMN config_hash TEXT")

    @staticmethod
    def _ensure_embedding_observed_dimensions_column(connection: sqlite3.Connection) -> None:
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(embedding_resources)")
        }
        if "observed_dimensions" not in columns:
            connection.execute(
                "ALTER TABLE embedding_resources ADD COLUMN observed_dimensions INTEGER"
            )

    @staticmethod
    def _embedding_config_hash(row: sqlite3.Row) -> str:
        return content_hash(
            {
                "base_url": str(row["base_url"]),
                "model": str(row["model"]),
                "dimensions": None if row["dimensions"] is None else int(row["dimensions"]),
                "timeout_seconds": float(row["timeout_seconds"]),
                "daily_call_limit": int(row["daily_call_limit"]),
                "daily_token_limit": int(row["daily_token_limit"]),
                "daily_cost_limit_microusd": int(row["daily_cost_limit_microusd"]),
                "input_cost_microusd_per_million": int(row["input_cost_microusd_per_million"]),
                "circuit_failure_threshold": int(row["circuit_failure_threshold"]),
                "circuit_cooldown_seconds": float(row["circuit_cooldown_seconds"]),
            }
        )

    @classmethod
    def _backfill_embedding_config_hashes(cls, connection: sqlite3.Connection) -> None:
        rows = connection.execute("SELECT * FROM embedding_resources").fetchall()
        for row in rows:
            connection.execute(
                "UPDATE embedding_resources SET config_hash=? WHERE config_id=?",
                (cls._embedding_config_hash(row), row["config_id"]),
            )

    def _set_schema_version(self, connection: sqlite3.Connection, version: int) -> None:
        """Advance the marker only after the complete migration stage is valid."""
        connection.execute(
            "UPDATE schema_meta SET value=? WHERE key='schema_version'",
            (str(version),),
        )
        self._validate_schema_stage_contract(connection, version)

    def _migrate(self, *, wallet_legacy_approval: WalletLegacyApproval | None = None) -> None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                raise RuntimeError("database schema version is missing")
            version = int(row["value"])
            self._migration_origin_version = version
            # A few early development databases only contained schema_meta and
            # a historical marker. Re-run additive migrations from the first
            # version when their anchor tables are absent instead of letting a
            # later optional trigger reference missing tables.
            memories_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'memories'"
            ).fetchone()
            if memories_exists is None:
                version = 1
            if version > CURRENT_SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema {version} is newer than runtime {CURRENT_SCHEMA_VERSION}"
                )
            if (
                wallet_legacy_approval is not None
                and version == 60
                and wallet_upgrade_fingerprint(connection)
                != wallet_legacy_approval.expected_fingerprint
            ):
                raise RuntimeError("wallet upgrade approval fingerprint mismatch")
            # Migration tests may open a current database with an older marker.
            # Remove reward provenance triggers that reference the v46 inbound
            # table before that table is rebuilt; repair recreates them later.
            if version < 46:
                for trigger in (
                    "validate_wallet_reward_submission_links_inbound_event_id_subject_insert",
                    "validate_wallet_reward_submission_links_inbound_event_id_subject_update",
                ):
                    connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            for target_version in range(version + 1, CURRENT_SCHEMA_VERSION + 1):
                migration = MIGRATIONS[target_version]
                if target_version == 85 and any(
                    row[1] == "author_verified"
                    for row in connection.execute(
                        "PRAGMA table_info(private_conversation_messages)"
                    )
                ):
                    connection.execute("BEGIN IMMEDIATE")
                    self._set_schema_version(connection, 85)
                    connection.commit()
                    continue
                if target_version == 84:
                    from noyra.interaction.conversations import migrate_web_history

                    try:
                        connection.execute("BEGIN IMMEDIATE")
                        self._execute_sql_script(connection, migration)
                        migrate_web_history(connection)
                        self._set_schema_version(connection, 84)
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
                    continue
                if target_version == 21:
                    self._ensure_project_execution_columns(connection)
                if target_version == 34:
                    self._ensure_embedding_resource_budget_columns(connection)
                if target_version == 43:
                    self._ensure_archive_transfer_claim_columns(connection)
                if target_version == 59:
                    try:
                        connection.execute("BEGIN IMMEDIATE")
                        self._upgrade_wallet_submission_consent(connection)
                        self._set_schema_version(connection, target_version)
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
                    continue
                if target_version == 60:
                    try:
                        connection.execute("BEGIN IMMEDIATE")
                        if (
                            wallet_legacy_approval is not None
                            and wallet_upgrade_fingerprint(connection)
                            != wallet_legacy_approval.expected_fingerprint
                        ):
                            raise RuntimeError("wallet upgrade approval fingerprint mismatch")
                        self._upgrade_wallet_payment_policy_hashes(
                            connection, approved=wallet_legacy_approval is not None
                        )
                        self._upgrade_wallet_ledger_hashes(
                            connection, approved=wallet_legacy_approval is not None
                        )
                        if wallet_legacy_approval is not None:
                            from noyra.wallet.economy import WalletEconomyStore

                            economy = WalletEconomyStore(self)
                            for subject in connection.execute(
                                "SELECT subject_id FROM subject_identity"
                            ):
                                subject_id = str(subject["subject_id"])
                                economy.verify_integrity(subject_id, _connection=connection)
                                connection.execute(
                                    "INSERT INTO audit_records(audit_id,subject_id,action,actor,payload_json,occurred_at) "
                                    "VALUES(?,?,?,?,?,?)",
                                    (
                                        new_id("audit"),
                                        subject_id,
                                        "wallet_legacy_state_authorized",
                                        wallet_legacy_approval.actor,
                                        canonical_json(
                                            {
                                                "expected_fingerprint": wallet_legacy_approval.expected_fingerprint,
                                                "reason": wallet_legacy_approval.reason,
                                                "target_schema": 60,
                                                "historical_authenticity_proven": False,
                                            }
                                        ),
                                        utc_now(),
                                    ),
                                )
                        self._execute_sql_script(connection, migration)
                        self._set_schema_version(connection, target_version)
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
                    continue
                if target_version == 61:
                    try:
                        connection.execute("BEGIN IMMEDIATE")
                        self._upgrade_wallet_temporal_hashes(
                            connection, approved=wallet_legacy_approval is not None
                        )
                        self._execute_sql_script(connection, migration)
                        self._set_schema_version(connection, target_version)
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
                    continue
                if target_version == 62:
                    try:
                        connection.execute("BEGIN IMMEDIATE")
                        self._upgrade_wallet_receipt_evidence(connection)
                        self._set_schema_version(connection, target_version)
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
                    continue
                if target_version == 63:
                    foreign_keys_enabled = bool(
                        connection.execute("PRAGMA foreign_keys").fetchone()[0]
                    )
                    try:
                        # The address table is referenced by historical child
                        # tables. SQLite only permits this rebuild with FK
                        # enforcement disabled outside the transaction.
                        connection.execute("PRAGMA foreign_keys = OFF")
                        connection.execute("BEGIN IMMEDIATE")
                        self._execute_sql_script(connection, migration)
                        self._ensure_subject_scoped_triggers(connection)
                        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
                        if violations:
                            raise RuntimeError(
                                "wallet address migration introduced foreign-key violations"
                            )
                        self._set_schema_version(connection, target_version)
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
                    finally:
                        connection.execute(
                            f"PRAGMA foreign_keys = {'ON' if foreign_keys_enabled else 'OFF'}"
                        )
                    continue
                if target_version == 66:
                    try:
                        connection.execute("BEGIN IMMEDIATE")
                        self._upgrade_wallet_payment_policy_recipient_allowlist(connection)
                        self._execute_sql_script(connection, migration)
                        self._set_schema_version(connection, target_version)
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
                    continue
                if target_version == 69:
                    try:
                        connection.execute("BEGIN IMMEDIATE")
                        self._upgrade_provider_health_metrics(connection)
                        self._set_schema_version(connection, target_version)
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
                    continue
                if target_version == 74:
                    try:
                        connection.execute("BEGIN IMMEDIATE")
                        self._ensure_migration_target_attestation_columns(connection)
                        self._execute_sql_script(connection, migration)
                        self._set_schema_version(connection, target_version)
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
                    continue
                if target_version == 75:
                    try:
                        connection.execute("BEGIN IMMEDIATE")
                        self._ensure_migration_task_epoch_column(connection)
                        self._execute_sql_script(connection, migration)
                        self._set_schema_version(connection, target_version)
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
                    continue
                if target_version == 79:
                    try:
                        connection.execute("BEGIN IMMEDIATE")
                        self._ensure_migration_target_recipient_columns(connection)
                        self._set_schema_version(connection, target_version)
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
                    continue
                if target_version in {81, 82}:
                    foreign_keys_enabled = bool(
                        connection.execute("PRAGMA foreign_keys").fetchone()[0]
                    )
                    try:
                        connection.execute("PRAGMA foreign_keys = OFF")
                        connection.execute("BEGIN IMMEDIATE")
                        self._execute_sql_script(connection, migration)
                        self._ensure_subject_scoped_triggers(connection)
                        if connection.execute("PRAGMA foreign_key_check").fetchall():
                            raise RuntimeError(
                                "resource policy migration introduced foreign-key violations"
                            )
                        for group in (
                            connection.execute(
                                "SELECT group_id, subject_id FROM cognitive_resource_groups"
                            ).fetchall()
                            if target_version == 81
                            else []
                        ):
                            if connection.execute(
                                "SELECT 1 FROM cognitive_resource_limit_policies WHERE group_id=?",
                                (group["group_id"],),
                            ).fetchone():
                                read_resource_policy(
                                    connection, group["group_id"], group["subject_id"]
                                )
                                continue
                            write_resource_policy(
                                connection,
                                group["group_id"],
                                group["subject_id"],
                                dict.fromkeys(LIMIT_DIMENSIONS, True),
                                actor="schema-migration",
                                reason="preserve existing limits",
                                now=utc_now(),
                            )
                        self._set_schema_version(connection, target_version)
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
                    finally:
                        connection.execute(
                            f"PRAGMA foreign_keys = {'ON' if foreign_keys_enabled else 'OFF'}"
                        )
                    continue
                if target_version == 80:
                    try:
                        connection.execute("BEGIN IMMEDIATE")
                        # Existing v71 installations never replay schema 69.
                        self._upgrade_provider_health_metrics(connection)
                        self._upgrade_provider_health_bucket_hashes(connection)
                        self._upgrade_wallet_payment_policy_layout(connection)
                        self._execute_sql_script(connection, migration)
                        self._set_schema_version(connection, target_version)
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
                    continue
                if target_version in {33, 48, 50, 51, 52}:
                    try:
                        connection.execute("BEGIN IMMEDIATE")
                        self._execute_sql_script(connection, migration)
                        if target_version == 33:
                            self._backfill_behavior_log_revisions(connection)
                        elif target_version == 48:
                            # This is the only legitimate point at which
                            # historical public-post moderation evidence can
                            # be synthesized.  Reopening a current database
                            # must never launder missing evidence.
                            self._ensure_public_post_moderation_history(connection)
                        elif target_version == 50:
                            self._ensure_transport_endpoint_columns(connection)
                        elif target_version == 51:
                            self._ensure_inbound_reply_columns(connection)
                        else:
                            self._ensure_public_post_evidence_columns(connection)
                            self._upgrade_public_post_evidence(connection)
                        self._set_schema_version(connection, target_version)
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
                    continue
                if target_version in (90, 91):
                    try:
                        connection.execute("BEGIN IMMEDIATE")
                        existing = {
                            row[1]
                            for row in connection.execute(
                                "PRAGMA table_info("
                                + (
                                    "private_channel_routes"
                                    if target_version == 90
                                    else "model_attempts"
                                )
                                + ")"
                            )
                        }
                        columns = (
                            PRIVATE_ROUTE_PUSH_COLUMNS
                            if target_version == 90
                            else ATTEMPT_USAGE_COLUMNS
                        )
                        table = (
                            "private_channel_routes" if target_version == 90 else "model_attempts"
                        )
                        for name, declaration in columns.items():
                            if name not in existing:
                                connection.execute(
                                    f"ALTER TABLE {table} ADD COLUMN {name} {declaration}"
                                )
                        self._execute_sql_script(connection, migration)
                        self._set_schema_version(connection, target_version)
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
                    continue
                if target_version == 96:
                    try:
                        connection.execute("BEGIN IMMEDIATE")
                        self._ensure_migration_proposal_approval_mode(connection)
                        self._execute_sql_script(connection, migration)
                        self._set_schema_version(connection, target_version)
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
                    continue
                if target_version == 97:
                    try:
                        connection.execute("BEGIN IMMEDIATE")
                        # A fresh database may not have reached the additive
                        # model-call columns yet.  Install those columns
                        # before any DDL that causes SQLite to reparse the
                        # historical erasure triggers referring to them.
                        model_columns = {
                            str(row["name"])
                            for row in connection.execute(
                                "PRAGMA table_info(model_calls)"
                            ).fetchall()
                        }
                        if "resource_group_id" not in model_columns:
                            connection.execute(
                                "ALTER TABLE model_calls ADD COLUMN resource_group_id TEXT"
                            )
                        if "request_json" not in model_columns:
                            connection.execute(
                                "ALTER TABLE model_calls ADD COLUMN request_json TEXT"
                            )
                        if "capture_policy_version" not in model_columns:
                            connection.execute(
                                "ALTER TABLE model_calls ADD COLUMN capture_policy_version INTEGER"
                            )
                        self._execute_sql_script(connection, migration)
                        self._set_schema_version(connection, target_version)
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
                    continue
                if target_version == 102:
                    try:
                        connection.execute("BEGIN IMMEDIATE")
                        self._execute_sql_script(connection, migration)
                        self._ensure_embedding_config_hash_column(connection)
                        self._backfill_embedding_config_hashes(connection)
                        self._set_schema_version(connection, target_version)
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
                    continue
                if target_version == 107:
                    try:
                        connection.execute("BEGIN IMMEDIATE")
                        self._execute_sql_script(connection, migration)
                        backfill_legacy_bindings(connection)
                        self._set_schema_version(connection, target_version)
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
                    continue
                if target_version == 106:
                    try:
                        connection.execute("BEGIN IMMEDIATE")
                        self._ensure_goal_origin_evidence(connection)
                        self._execute_sql_script(connection, migration)
                        self._set_schema_version(connection, target_version)
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
                    continue
                if target_version == 105:
                    try:
                        connection.execute("BEGIN IMMEDIATE")
                        self._ensure_participant_message_time(connection)
                        self._execute_sql_script(connection, migration)
                        self._set_schema_version(connection, target_version)
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
                    continue
                if target_version == 103:
                    try:
                        connection.execute("BEGIN IMMEDIATE")
                        self._ensure_embedding_observed_dimensions_column(connection)
                        self._set_schema_version(connection, target_version)
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
                    continue
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    self._execute_sql_script(connection, migration)
                    self._set_schema_version(connection, target_version)
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise

    @staticmethod
    def _ensure_goal_origin_evidence(connection: sqlite3.Connection) -> None:
        names = {row[1] for row in connection.execute("PRAGMA table_info(goals)")}
        evidence_columns = {"origin_evidence", "origin_evidence_hash"}
        if evidence_columns <= names:
            return
        if evidence_columns & names:
            raise SchemaContractError("goal origin evidence columns are incomplete")
        connection.execute(
            "ALTER TABLE goals ADD COLUMN origin_evidence TEXT NOT NULL DEFAULT 'created' "
            "CHECK (origin_evidence IN ('created','legacy_baseline'))"
        )
        connection.execute(
            "ALTER TABLE goals ADD COLUMN origin_evidence_hash TEXT NOT NULL DEFAULT ''"
        )
        # Old state/revision hashes did not cover origin. These witnesses pin
        # only the value observed at migration, never its original ancestry.
        for row in connection.execute("SELECT goal_id,subject_id,origin,created_at FROM goals"):
            if row["origin"] not in {
                "self",
                "environment",
                "human_proposal",
                "maintenance",
                "mixed",
            }:
                raise SchemaContractError("legacy goal origin is invalid")
            connection.execute(
                "UPDATE goals SET origin_evidence='legacy_baseline',origin_evidence_hash=? "
                "WHERE goal_id=?",
                (
                    goal_origin_evidence_hash(
                        row["goal_id"],
                        row["subject_id"],
                        row["origin"],
                        row["created_at"],
                        "legacy_baseline",
                    ),
                    row["goal_id"],
                ),
            )

    @staticmethod
    def _ensure_participant_message_time(connection: sqlite3.Connection) -> None:
        if any(
            row[1] == "interaction_created_at"
            for row in connection.execute("PRAGMA table_info(participant_message_links)")
        ):
            return
        # Add an indexed copy of the immutable interaction timestamp. Preserve
        # the attribution guard exactly after this one-time atomic backfill.
        guard = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='prevent_participant_message_links_update'"
        ).fetchone()
        if guard is None:
            raise SchemaContractError("participant attribution guard is missing")
        connection.execute("DROP TRIGGER prevent_participant_message_links_update")
        connection.execute(
            "ALTER TABLE participant_message_links ADD COLUMN interaction_created_at "
            "TEXT NOT NULL DEFAULT ''"
        )
        connection.execute(
            "UPDATE participant_message_links SET interaction_created_at=(SELECT i.created_at "
            "FROM interactions i WHERE i.interaction_id=participant_message_links.interaction_id "
            "AND i.subject_id=participant_message_links.subject_id)"
        )
        connection.execute(guard[0])

    @staticmethod
    def _ensure_project_execution_columns(connection: sqlite3.Connection) -> None:
        columns_by_table = {
            "research_search_runs": ("project_id", "phase_id"),
            "action_deliberation_runs": ("project_id", "phase_id"),
            "actions": ("project_id", "phase_id"),
        }
        references = {
            ("research_search_runs", "project_id"): "REFERENCES autonomous_projects(project_id)",
            ("research_search_runs", "phase_id"): "REFERENCES autonomous_project_phases(phase_id)",
            (
                "action_deliberation_runs",
                "project_id",
            ): "REFERENCES autonomous_projects(project_id)",
            (
                "action_deliberation_runs",
                "phase_id",
            ): "REFERENCES autonomous_project_phases(phase_id)",
            ("actions", "project_id"): "REFERENCES autonomous_projects(project_id)",
            ("actions", "phase_id"): "REFERENCES autonomous_project_phases(phase_id)",
        }
        for table, columns in columns_by_table.items():
            existing = {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}
            for column in columns:
                if column in existing:
                    continue
                reference = references[(table, column)]
                connection.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" TEXT {reference}')

    @staticmethod
    def _ensure_embedding_resource_budget_columns(connection: sqlite3.Connection) -> None:
        existing = {
            str(row[1]) for row in connection.execute('PRAGMA table_info("embedding_resources")')
        }
        columns = {
            "daily_call_limit": ("INTEGER NOT NULL DEFAULT 1000 CHECK (daily_call_limit >= 0)"),
            "daily_token_limit": (
                "INTEGER NOT NULL DEFAULT 5000000 CHECK (daily_token_limit >= 0)"
            ),
            "daily_cost_limit_microusd": (
                "INTEGER NOT NULL DEFAULT 1000000 CHECK (daily_cost_limit_microusd >= 0)"
            ),
            "input_cost_microusd_per_million": (
                "INTEGER NOT NULL DEFAULT 20000 CHECK (input_cost_microusd_per_million >= 0)"
            ),
            "circuit_failure_threshold": (
                "INTEGER NOT NULL DEFAULT 3 CHECK (circuit_failure_threshold BETWEEN 1 AND 20)"
            ),
            "circuit_cooldown_seconds": (
                "REAL NOT NULL DEFAULT 60 "
                "CHECK (circuit_cooldown_seconds >= 1 AND circuit_cooldown_seconds <= 3600)"
            ),
        }
        for column, declaration in columns.items():
            if column not in existing:
                connection.execute(
                    f'ALTER TABLE "embedding_resources" ADD COLUMN "{column}" {declaration}'
                )
