from __future__ import annotations

from noyra.core.database import Database
from noyra.core.errors import IntegrityError
from noyra.core.types import strict_json_loads

from .store import CapabilityStore


class CapabilityIntegrity:
    """Verify grants and their append-only use ledger."""

    def __init__(self, database: Database):
        self.database = database

    def verify(self, subject_id: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        with self.database.read_transaction() as connection:
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise IntegrityError("capability state contains broken foreign keys")
            grants = connection.execute(
                "SELECT * FROM capability_grants WHERE subject_id = ?", (subject_id,)
            ).fetchall()
            grant_ids = {row["grant_id"] for row in grants}
            for row in grants:
                CapabilityStore._from_row(row)
                revoked = row["status"] == "revoked"
                if revoked != (row["revoked_at"] is not None and row["revoke_reason"] is not None):
                    raise IntegrityError(f"capability revocation mismatch: {row['grant_id']}")
                try:
                    CapabilityStore._scope_matches(
                        row["capability_type"], row["scope_json"], self._sample_resource(row)
                    )
                except IntegrityError:
                    raise
                except (KeyError, TypeError, ValueError) as error:
                    raise IntegrityError(
                        f"capability scope is invalid: {row['grant_id']}"
                    ) from error
            counts["capability_grants"] = len(grants)
            uses = connection.execute(
                "SELECT * FROM capability_uses WHERE subject_id = ?", (subject_id,)
            ).fetchall()
            for row in uses:
                if row["grant_id"] not in grant_ids:
                    raise IntegrityError(
                        f"capability use crosses subject boundary: {row['use_id']}"
                    )
                if row["action_id"] is not None:
                    action = connection.execute(
                        "SELECT subject_id FROM actions WHERE action_id = ?", (row["action_id"],)
                    ).fetchone()
                    if action is None or action["subject_id"] != subject_id:
                        raise IntegrityError(f"capability action mismatch: {row['use_id']}")
            counts["capability_uses"] = len(uses)
        return counts

    @staticmethod
    def _sample_resource(row: object) -> str:
        mapping = row  # sqlite Row supports mapping access while keeping mypy generic here.
        capability_type = mapping["capability_type"]  # type: ignore[index]
        scope_json = mapping["scope_json"]  # type: ignore[index]
        try:
            scope = strict_json_loads(scope_json)
        except (TypeError, ValueError) as error:
            raise IntegrityError("capability scope is invalid") from error
        if not isinstance(scope, dict):
            raise IntegrityError("capability scope is invalid")
        if capability_type.startswith("filesystem"):
            return str(scope.get("root", ""))
        if capability_type == "web_read":
            if scope.get("public_https") is True:
                return "https://integrity-sample.invalid/"
            hosts = scope.get("hosts", [])
            return f"https://{hosts[0]}/" if hosts else "https://invalid.invalid/"
        exact = scope.get("exact", [])
        return str(exact[0]) if exact else "__invalid__"
