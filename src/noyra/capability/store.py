from __future__ import annotations

import ipaddress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from noyra.core.database import Database
from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.types import (
    canonical_json,
    content_hash,
    new_id,
    strict_bool,
    strict_int,
    strict_json_loads,
    utc_now,
)

from .errors import CapabilityDeniedError
from .types import CapabilityGrant, CapabilityGrantRecord


class CapabilityStore:
    """User-owned capability grants with scope and rolling rate-limit enforcement."""

    def __init__(self, database: Database):
        self.database = database

    def grant(
        self, subject_id: str, proposal: CapabilityGrant, *, actor: str
    ) -> CapabilityGrantRecord:
        if not actor.strip() or actor == "subject":
            raise PermissionError("capabilities are granted by an operator, not by the subject")
        if proposal.requires_approval:
            raise ValueError(
                "per-use approvals are not supported; revoke the capability grant instead"
            )
        if proposal.expires_at is not None:
            self._parse_time(proposal.expires_at)
        self._validate_scope(proposal)
        now = utc_now()
        grant_id = new_id("grant")
        state_hash = self._state_hash_values(subject_id, proposal, "active", now, None, None)
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO capability_grants(
                    grant_id, subject_id, capability_type, scope_json, issuer,
                    rate_limit_per_hour, side_effect, requires_approval, status,
                    state_hash, created_at, expires_at, revoked_at, revoke_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, NULL, NULL)""",
                (
                    grant_id,
                    subject_id,
                    proposal.capability_type,
                    canonical_json(proposal.scope),
                    proposal.issuer,
                    proposal.rate_limit_per_hour,
                    int(proposal.side_effect),
                    int(proposal.requires_approval),
                    state_hash,
                    now,
                    proposal.expires_at,
                ),
            )
            return self._load_connection(connection, grant_id)

    def revoke(
        self,
        grant_id: str,
        *,
        reason: str,
        actor: str,
        subject_id: str,
    ) -> CapabilityGrantRecord:
        if not actor.strip() or actor == "subject" or not reason.strip():
            raise PermissionError("only an operator can revoke a grant with a reason")
        with self.database.transaction() as connection:
            row = self._get_row(connection, grant_id, subject_id=subject_id)
            if row["status"] == "revoked":
                return self._from_row(row)
            now = utc_now()
            proposal = self._proposal_from_row(row)
            state_hash = self._state_hash_values(
                row["subject_id"], proposal, "revoked", row["created_at"], now, reason
            )
            connection.execute(
                """UPDATE capability_grants SET status = 'revoked', state_hash = ?,
                    revoked_at = ?, revoke_reason = ? WHERE grant_id = ? AND subject_id = ?""",
                (state_hash, now, reason, grant_id, subject_id),
            )
            return self._load_connection(connection, grant_id, subject_id=subject_id)

    def use(
        self,
        subject_id: str,
        capability_type: str,
        resource: str,
        *,
        side_effect: bool,
        # Deprecated compatibility input; never authorizes a grant.
        approval_id: str | None = None,
        action_id: str | None = None,
        now: str | None = None,
    ) -> CapabilityGrantRecord:
        if not isinstance(resource, str):
            raise ValueError("capability resource must be text")
        if not resource.strip():
            raise ValueError("capability resource cannot be blank")
        timestamp = now or utc_now()
        current = self._parse_time(timestamp)
        with self.database.transaction() as connection:
            selected = self._select_connection(
                connection,
                subject_id,
                capability_type,
                resource,
                side_effect=side_effect,
                approval_id=approval_id,
                current=current,
            )
            if selected is None:
                raise CapabilityDeniedError(
                    f"no active grant authorizes {capability_type} for {resource}"
                )
            if action_id is not None:
                action = connection.execute(
                    "SELECT subject_id FROM actions WHERE action_id = ?", (action_id,)
                ).fetchone()
                if action is None or action["subject_id"] != subject_id:
                    raise CapabilityDeniedError(
                        "capability action is missing or belongs to another subject"
                    )
            connection.execute(
                """INSERT INTO capability_uses(
                    use_id, grant_id, subject_id, resource, action_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (new_id("cuse"), selected["grant_id"], subject_id, resource, action_id, timestamp),
            )
            return self._from_row(selected)

    def allows(
        self,
        subject_id: str,
        capability_type: str,
        resource: str,
        *,
        side_effect: bool,
        # Deprecated compatibility input; never authorizes a grant.
        approval_id: str | None = None,
        now: str | None = None,
    ) -> bool:
        if not isinstance(resource, str):
            return False
        if not resource.strip():
            return False
        current = self._parse_time(now or utc_now())
        with self.database.connection() as connection:
            return (
                self._select_connection(
                    connection,
                    subject_id,
                    capability_type,
                    resource,
                    side_effect=side_effect,
                    approval_id=approval_id,
                    current=current,
                )
                is not None
            )

    def list(self, subject_id: str) -> list[CapabilityGrantRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM capability_grants WHERE subject_id = ? ORDER BY created_at DESC",
                (subject_id,),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    @classmethod
    def _select_connection(
        cls,
        connection: Any,
        subject_id: str,
        capability_type: str,
        resource: str,
        *,
        side_effect: bool,
        approval_id: str | None,
        current: datetime,
    ) -> Any:
        rows = connection.execute(
            "SELECT * FROM capability_grants WHERE subject_id = ? "
            "AND capability_type = ? AND status = 'active' ORDER BY created_at",
            (subject_id, capability_type),
        ).fetchall()
        for row in rows:
            # Authorization decisions must be based on the same strictly
            # validated, integrity-checked state returned by get/list/use.
            # SQLite's dynamic typing can otherwise admit values such as a
            # fractional rate limit that bool()/int() would silently coerce.
            cls._from_row(row)
            if row["expires_at"] is not None and cls._parse_time(row["expires_at"]) <= current:
                continue
            if side_effect and not bool(row["side_effect"]):
                continue
            # The legacy flag is fail-closed.  It was never backed by an
            # approval ledger, so accepting an arbitrary token would create a
            # false security boundary.  New grants reject the flag outright.
            if bool(row["requires_approval"]):
                continue
            if not cls._scope_matches(row["capability_type"], row["scope_json"], resource):
                continue
            if int(row["rate_limit_per_hour"]) > 0:
                cutoff = (current - timedelta(hours=1)).isoformat(timespec="milliseconds")
                used = connection.execute(
                    "SELECT COUNT(*) FROM capability_uses WHERE grant_id = ? AND created_at > ?",
                    (row["grant_id"], cutoff),
                ).fetchone()[0]
                if int(used) >= int(row["rate_limit_per_hour"]):
                    continue
            return row
        return None

    @staticmethod
    def _scope_matches(capability_type: str, scope_json: str, resource: str) -> bool:
        if not isinstance(resource, str):
            return False
        if not isinstance(scope_json, str):
            raise IntegrityError("capability scope JSON must be text")
        try:
            scope = strict_json_loads(scope_json)
        except (TypeError, ValueError) as error:
            raise IntegrityError("capability scope JSON is invalid") from error
        try:
            CapabilityStore._validate_scope_values(
                capability_type, scope, allow_legacy_wildcard=True
            )
        except (KeyError, TypeError, ValueError) as error:
            raise IntegrityError("capability scope is invalid") from error
        if capability_type.startswith("filesystem"):
            root = scope["root"]
            try:
                root_path = Path(root).expanduser().resolve()
                target = Path(resource).expanduser().resolve()
            except (OSError, RuntimeError, TypeError, ValueError):
                return False
            return target == root_path or root_path in target.parents
        if capability_type == "web_read":
            try:
                parsed = urlsplit(resource)
                hostname = parsed.hostname
                port = parsed.port
                username = parsed.username
                password = parsed.password
                fragment = parsed.fragment
            except (TypeError, ValueError):
                return False
            if (
                parsed.scheme.lower() != "https"
                or hostname is None
                or username is not None
                or password is not None
                or fragment
            ):
                return False
            if port not in {None, 443}:
                return False
            try:
                address = ipaddress.ip_address(hostname.split("%", 1)[0])
            except (TypeError, ValueError):
                address = None
            if address is not None and not address.is_global:
                return False
            if scope.get("public_https") is True:
                return True
            hosts = scope.get("hosts")
            if hosts == ["*"]:
                return False
            return hostname.lower() in {host.strip().lower() for host in hosts}
        exact = scope.get("exact")
        return resource in set(exact)

    @staticmethod
    def _validate_scope(proposal: CapabilityGrant) -> None:
        CapabilityStore._validate_scope_values(
            proposal.capability_type, proposal.scope, allow_legacy_wildcard=False
        )
        if (
            proposal.capability_type
            in {
                "filesystem_write",
                "publish",
                "message",
                "wallet",
            }
            and not proposal.side_effect
        ):
            raise ValueError("side-effect capability grants must enable side effects")

    @staticmethod
    def _validate_scope_values(
        capability_type: str,
        scope: Any,
        *,
        allow_legacy_wildcard: bool,
    ) -> None:
        if not isinstance(capability_type, str):
            raise TypeError("capability type must be text")
        if not isinstance(scope, dict):
            raise ValueError("capability scope must be an object")
        if capability_type.startswith("filesystem"):
            root = scope.get("root")
            if not isinstance(root, str) or not root.strip() or not Path(root).is_absolute():
                raise ValueError("filesystem capability scope requires an absolute root")
            return
        if capability_type == "web_read":
            has_public_https = "public_https" in scope
            public_https = scope.get("public_https")
            if has_public_https and not isinstance(public_https, bool):
                raise ValueError("web capability public_https scope must be boolean")
            has_hosts = "hosts" in scope
            hosts = scope.get("hosts")
            if has_hosts:
                if not isinstance(hosts, list) or not hosts:
                    raise ValueError("web capability scope requires one or more hosts")
                if any(not isinstance(host, str) or not host.strip() for host in hosts):
                    raise ValueError("web capability hosts must be non-empty strings")
                if "*" in hosts and not (
                    allow_legacy_wildcard and hosts == ["*"] and not has_public_https
                ):
                    raise ValueError(
                        "web capability host scopes cannot use wildcard hosts; "
                        "use public_https instead"
                    )
            if public_https is not True and not has_hosts:
                raise ValueError("web capability scope requires public_https or one or more hosts")
            return
        exact = scope.get("exact")
        if (
            not isinstance(exact, list)
            or not exact
            or any(not isinstance(value, str) or not value.strip() for value in exact)
        ):
            raise ValueError("capability scope requires one or more exact resources")

    @staticmethod
    def _validate_persisted_scope(row: Any) -> None:
        grant_id = row["grant_id"]
        scope_json = row["scope_json"]
        if not isinstance(scope_json, str):
            raise IntegrityError(f"capability scope is invalid: {grant_id}")
        try:
            scope = strict_json_loads(scope_json)
            CapabilityStore._validate_scope_values(
                row["capability_type"], scope, allow_legacy_wildcard=True
            )
        except (KeyError, TypeError, ValueError) as error:
            raise IntegrityError(f"capability scope is invalid: {grant_id}") from error

    @staticmethod
    def _parse_time(value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise ValueError("capability time must be ISO-8601") from error
        if parsed.tzinfo is None:
            raise ValueError("capability time must include a timezone")
        return parsed.astimezone(UTC)

    @staticmethod
    def _state_hash_values(
        subject_id: str,
        proposal: CapabilityGrant,
        status: str,
        created_at: str,
        revoked_at: str | None,
        revoke_reason: str | None,
    ) -> str:
        return content_hash(
            {
                "subject_id": subject_id,
                **proposal.model_dump(mode="json"),
                "status": status,
                "created_at": created_at,
                "revoked_at": revoked_at,
                "revoke_reason": revoke_reason,
            }
        )

    @staticmethod
    def _proposal_from_row(row: Any) -> CapabilityGrant:
        try:
            if not isinstance(row["scope_json"], str):
                raise TypeError("capability scope JSON must be text")
            return CapabilityGrant(
                capability_type=row["capability_type"],
                scope=strict_json_loads(row["scope_json"]),
                issuer=row["issuer"],
                rate_limit_per_hour=strict_int(row["rate_limit_per_hour"]),
                side_effect=strict_bool(row["side_effect"]),
                requires_approval=strict_bool(row["requires_approval"]),
                expires_at=row["expires_at"],
            )
        except IntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise IntegrityError(f"capability grant is invalid: {row['grant_id']}") from error

    @staticmethod
    def _get_row(connection: Any, grant_id: str, *, subject_id: str | None = None) -> Any:
        if subject_id is None:
            row = connection.execute(
                "SELECT * FROM capability_grants WHERE grant_id = ?", (grant_id,)
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT * FROM capability_grants WHERE grant_id = ? AND subject_id = ?",
                (grant_id, subject_id),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"capability grant not found: {grant_id}")
        return row

    @classmethod
    def _load_connection(
        cls,
        connection: Any,
        grant_id: str,
        *,
        subject_id: str | None = None,
    ) -> CapabilityGrantRecord:
        return cls._from_row(cls._get_row(connection, grant_id, subject_id=subject_id))

    @classmethod
    def _from_row(cls, row: Any) -> CapabilityGrantRecord:
        proposal = cls._proposal_from_row(row)
        cls._validate_persisted_scope(row)
        expected = cls._state_hash_values(
            row["subject_id"],
            proposal,
            row["status"],
            row["created_at"],
            row["revoked_at"],
            row["revoke_reason"],
        )
        if expected != row["state_hash"]:
            raise IntegrityError(f"capability grant state hash mismatch: {row['grant_id']}")
        return CapabilityGrantRecord(
            row["grant_id"],
            row["subject_id"],
            row["capability_type"],
            proposal.scope,
            row["issuer"],
            proposal.rate_limit_per_hour,
            proposal.side_effect,
            proposal.requires_approval,
            row["status"],
            row["created_at"],
            row["expires_at"],
            row["revoked_at"],
            row["revoke_reason"],
        )
