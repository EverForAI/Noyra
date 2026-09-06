from __future__ import annotations

import re
from typing import Any

from .database import Database, wallet_payment_policy_state_hash
from .errors import IdentityConflictError, IntegrityError, NotFoundError
from .types import SubjectIdentity, new_id, utc_now

SUBJECT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,127}$")
SUBJECT_STORAGE_KEY_PATTERN = re.compile(r"^subject_[0-9a-f]{32}$")
_WINDOWS_RESERVED_SUBJECT_IDS = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


def validate_subject_id(subject_id: str) -> str:
    if not isinstance(subject_id, str) or not SUBJECT_ID_PATTERN.fullmatch(subject_id):
        raise ValueError("subject_id must be 3-128 ASCII characters: letters, digits, '_' or '-'")
    if subject_id.casefold() in _WINDOWS_RESERVED_SUBJECT_IDS:
        raise ValueError("subject_id cannot be a reserved filesystem device name")
    return subject_id


def validate_subject_storage_key(storage_key: str) -> str:
    if not isinstance(storage_key, str) or not SUBJECT_STORAGE_KEY_PATTERN.fullmatch(storage_key):
        raise IntegrityError("subject storage key has an invalid format")
    return storage_key


class IdentityStore:
    def __init__(self, database: Database):
        self.database = database

    def ensure(
        self,
        subject_id: str,
        genesis_hash: str,
        *,
        project_name: str = "Noyra",
        origin_subject_id: str | None = None,
        branch_reason: str | None = None,
    ) -> SubjectIdentity:
        validate_subject_id(subject_id)
        if origin_subject_id is not None:
            validate_subject_id(origin_subject_id)
        if not genesis_hash:
            raise ValueError("genesis_hash is required")
        now = utc_now()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM subject_identity WHERE subject_id = ?", (subject_id,)
            ).fetchone()
            if row:
                if row["genesis_hash"] != genesis_hash or row["project_name"] != project_name:
                    raise IdentityConflictError(
                        f"identity already exists with different genesis: {subject_id}"
                    )
                if origin_subject_id is not None and row["origin_subject_id"] != origin_subject_id:
                    raise IdentityConflictError(
                        f"identity already exists with different origin: {subject_id}"
                    )
                if branch_reason is not None and row["branch_reason"] != branch_reason:
                    raise IdentityConflictError(
                        f"identity already exists with different branch reason: {subject_id}"
                    )
                self._storage_key_connection(connection, subject_id)
                return self._from_row(row)
            storage_key = self._new_storage_key(connection, subject_id)
            connection.execute(
                """INSERT INTO subject_identity(
                    subject_id, project_name, genesis_hash, personal_name,
                    identity_status, created_at, updated_at, state_version,
                    model_name, last_checkpoint, origin_subject_id, branch_reason
                ) VALUES (?, ?, ?, NULL, 'active', ?, ?, 0, NULL, NULL, ?, ?)""",
                (
                    subject_id,
                    project_name,
                    genesis_hash,
                    now,
                    now,
                    origin_subject_id,
                    branch_reason,
                ),
            )
            connection.execute(
                "INSERT INTO subject_storage_keys(subject_id, storage_key) VALUES (?, ?)",
                (subject_id, storage_key),
            )
            connection.execute(
                "INSERT OR IGNORE INTO training_policies(subject_id, updated_at) VALUES (?, ?)",
                (subject_id, now),
            )
            connection.execute(
                """INSERT OR IGNORE INTO wallet_payment_policies(
                    subject_id, mode, allowed_network_ids_json, allowed_asset_ids_json,
                    per_order_limit, daily_limit, monthly_limit, daily_order_limit,
                    monthly_order_limit, min_balance, max_observation_age_seconds,
                    automatic_max_amount, anomaly_block, emergency_paused, policy_version,
                    updated_at, state_hash
                ) VALUES (
                    ?, 'disabled', '[]', '[]', '0', '0', '0', 0, 0, '0', 0, '0', 1, 0, 1, ?,
                    ?
                )""",
                (
                    subject_id,
                    now,
                    wallet_payment_policy_state_hash(
                        subject_id=subject_id,
                        mode="disabled",
                        allowed_network_ids_json="[]",
                        allowed_asset_ids_json="[]",
                        per_order_limit="0",
                        daily_limit="0",
                        monthly_limit="0",
                        daily_order_limit=0,
                        monthly_order_limit=0,
                        min_balance="0",
                        max_observation_age_seconds=0,
                        automatic_max_amount="0",
                        anomaly_block=1,
                        emergency_paused=0,
                        policy_version=1,
                        updated_at=now,
                    ),
                ),
            )
            return self._from_row(
                connection.execute(
                    "SELECT * FROM subject_identity WHERE subject_id = ?", (subject_id,)
                ).fetchone()
            )

    def storage_key(self, subject_id: str) -> str:
        validate_subject_id(subject_id)
        with self.database.connection() as connection:
            return self._storage_key_connection(connection, subject_id)

    def legacy_storage_path_is_unambiguous(self, subject_id: str) -> bool:
        validate_subject_id(subject_id)
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM subject_identity WHERE lower(subject_id) = lower(?)",
                (subject_id,),
            ).fetchone()
        return row is not None and int(row[0]) == 1

    def load(self, subject_id: str) -> SubjectIdentity:
        validate_subject_id(subject_id)
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM subject_identity WHERE subject_id = ?", (subject_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"identity not found: {subject_id}")
        return self._from_row(row)

    def update_checkpoint(
        self,
        subject_id: str,
        *,
        state_version: int,
        checkpoint_id: str,
        model_name: str | None = None,
    ) -> SubjectIdentity:
        validate_subject_id(subject_id)
        if state_version < 0:
            raise ValueError("state_version cannot be negative")
        if not checkpoint_id.strip():
            raise ValueError("checkpoint_id is required")
        with self.database.transaction() as connection:
            self._update_checkpoint_connection(
                connection, subject_id, state_version, checkpoint_id, model_name
            )
            row = connection.execute(
                "SELECT * FROM subject_identity WHERE subject_id = ?", (subject_id,)
            ).fetchone()
        return self._from_row(row)

    def set_personal_name(self, subject_id: str, personal_name: str | None) -> SubjectIdentity:
        validate_subject_id(subject_id)
        with self.database.transaction() as connection:
            updated = connection.execute(
                "UPDATE subject_identity SET personal_name = ?, updated_at = ? "
                "WHERE subject_id = ?",
                (personal_name, utc_now(), subject_id),
            )
            if updated.rowcount != 1:
                raise NotFoundError(f"identity not found: {subject_id}")
            row = connection.execute(
                "SELECT * FROM subject_identity WHERE subject_id = ?", (subject_id,)
            ).fetchone()
        return self._from_row(row)

    def _update_checkpoint_connection(
        self,
        connection: Any,
        subject_id: str,
        state_version: int,
        checkpoint_id: str,
        model_name: str | None,
    ) -> None:
        validate_subject_id(subject_id)
        if state_version < 0:
            raise ValueError("state_version cannot be negative")
        if not checkpoint_id.strip():
            raise ValueError("checkpoint_id is required")
        current = connection.execute(
            "SELECT state_version FROM subject_identity WHERE subject_id = ?", (subject_id,)
        ).fetchone()
        if current is None:
            raise NotFoundError(f"identity not found: {subject_id}")
        if state_version <= int(current["state_version"]):
            raise IdentityConflictError(
                f"checkpoint version must increase: {state_version} <= {current['state_version']}"
            )
        snapshot = connection.execute(
            "SELECT subject_id, state_version FROM state_snapshots WHERE snapshot_id = ?",
            (checkpoint_id,),
        ).fetchone()
        if snapshot is None:
            raise NotFoundError(f"checkpoint not found: {checkpoint_id}")
        if snapshot["subject_id"] != subject_id or int(snapshot["state_version"]) != state_version:
            raise IdentityConflictError(
                "checkpoint subject or version does not match the identity update"
            )
        updated = connection.execute(
            """UPDATE subject_identity
               SET state_version = ?, last_checkpoint = ?,
                   model_name = COALESCE(?, model_name), updated_at = ?
               WHERE subject_id = ?""",
            (state_version, checkpoint_id, model_name, utc_now(), subject_id),
        )
        if updated.rowcount != 1:
            raise NotFoundError(f"identity not found: {subject_id}")

    @staticmethod
    def _new_storage_key(connection: Any, subject_id: str) -> str:
        for _attempt in range(16):
            candidate: str = str(new_id("subject"))
            if candidate.casefold() == subject_id.casefold():
                continue
            row = connection.execute(
                "SELECT 1 FROM subject_storage_keys WHERE storage_key = ?", (candidate,)
            ).fetchone()
            if row is None:
                return candidate
        raise RuntimeError("could not allocate a unique subject storage key")

    @staticmethod
    def _storage_key_connection(connection: Any, subject_id: str) -> str:
        validate_subject_id(subject_id)
        row = connection.execute(
            "SELECT storage_key FROM subject_storage_keys WHERE subject_id = ?", (subject_id,)
        ).fetchone()
        if row is None:
            raise IntegrityError(f"subject storage key is missing: {subject_id}")
        storage_key = validate_subject_storage_key(str(row["storage_key"]))
        if storage_key.casefold() == subject_id.casefold():
            raise IntegrityError("subject storage key is controlled by the logical subject id")
        return storage_key

    @staticmethod
    def _from_row(row: Any) -> SubjectIdentity:
        subject_id = row["subject_id"]
        if not isinstance(subject_id, str):
            raise IntegrityError("identity subject id is invalid")
        validate_subject_id(subject_id)
        origin_subject_id = row["origin_subject_id"]
        if origin_subject_id is not None:
            if not isinstance(origin_subject_id, str):
                raise IntegrityError("identity origin subject id is invalid")
            try:
                validate_subject_id(origin_subject_id)
            except ValueError as error:
                raise IntegrityError("identity origin subject id is invalid") from error
        state_version = int(row["state_version"])
        if state_version < 0:
            raise IntegrityError(f"identity has a negative state version: {subject_id}")
        return SubjectIdentity(
            subject_id=subject_id,
            project_name=row["project_name"],
            genesis_hash=row["genesis_hash"],
            personal_name=row["personal_name"],
            identity_status=row["identity_status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            state_version=state_version,
            model_name=row["model_name"],
            last_checkpoint=row["last_checkpoint"],
            origin_subject_id=origin_subject_id,
            branch_reason=row["branch_reason"],
        )
