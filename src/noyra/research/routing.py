from __future__ import annotations

from noyra.core.database import Database
from noyra.core.errors import IntegrityError
from noyra.core.types import content_hash, utc_now

SEARCH_ROUTING_MODES = frozenset({"model_first", "api_first", "auto"})


def _state_hash(subject_id: str, mode: str, updated_at: str) -> str:
    return content_hash({"subject_id": subject_id, "mode": mode, "updated_at": updated_at})


def get_search_routing_mode(database: Database, subject_id: str) -> tuple[str, str | None]:
    with database.connection() as connection:
        row = connection.execute(
            "SELECT mode, state_hash, updated_at FROM search_routing_settings WHERE subject_id = ?",
            (subject_id,),
        ).fetchone()
    if row is None:
        return "auto", None
    mode = row["mode"]
    updated_at = row["updated_at"]
    if (
        not isinstance(mode, str)
        or mode not in SEARCH_ROUTING_MODES
        or not isinstance(updated_at, str)
        or not updated_at
        or row["state_hash"] != _state_hash(subject_id, mode, updated_at)
    ):
        raise IntegrityError("search routing setting is invalid")
    return mode, updated_at


def set_search_routing_mode(
    database: Database, subject_id: str, mode: str, *, updated_at: str | None = None
) -> str:
    if not isinstance(mode, str) or mode not in SEARCH_ROUTING_MODES:
        raise ValueError("search routing mode is invalid")
    timestamp = utc_now() if updated_at is None else updated_at
    state_hash = _state_hash(subject_id, mode, timestamp)
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO search_routing_settings(subject_id, mode, state_hash, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(subject_id) DO UPDATE SET mode=excluded.mode, "
            "state_hash=excluded.state_hash, updated_at=excluded.updated_at",
            (subject_id, mode, state_hash, timestamp),
        )
    return timestamp


def _provider_state_hash(config_id: str, subject_id: str, enabled: bool, updated_at: str) -> str:
    return content_hash(
        {
            "config_id": config_id,
            "subject_id": subject_id,
            "enabled": enabled,
            "updated_at": updated_at,
        }
    )


def list_search_provider_controls(database: Database, subject_id: str) -> dict[str, bool]:
    with database.connection() as connection:
        rows = connection.execute(
            "SELECT c.*, p.subject_id AS provider_subject_id, p.status AS provider_status "
            "FROM search_provider_controls c LEFT JOIN search_provider_configs p "
            "ON p.config_id = c.config_id WHERE p.subject_id = ?",
            (subject_id,),
        ).fetchall()
    controls: dict[str, bool] = {}
    for row in rows:
        config_id = row["config_id"]
        enabled_value = row["enabled"]
        updated_at = row["updated_at"]
        if (
            not isinstance(config_id, str)
            or not config_id
            or row["provider_subject_id"] != subject_id
            or row["provider_status"] not in {"active", "revoked"}
            or type(enabled_value) is not int
            or enabled_value not in {0, 1}
            or not isinstance(updated_at, str)
            or not updated_at
            or row["state_hash"]
            != _provider_state_hash(config_id, subject_id, bool(enabled_value), updated_at)
        ):
            raise IntegrityError("search provider control is invalid")
        controls[config_id] = bool(enabled_value)
    return controls


def set_search_provider_enabled(
    database: Database,
    config_id: str,
    subject_id: str,
    enabled: bool,
    *,
    updated_at: str | None = None,
) -> str:
    if not isinstance(config_id, str) or not config_id or type(enabled) is not bool:
        raise ValueError("search provider control is invalid")
    timestamp = utc_now() if updated_at is None else updated_at
    state_hash = _provider_state_hash(config_id, subject_id, enabled, timestamp)
    with database.transaction() as connection:
        provider = connection.execute(
            "SELECT status FROM search_provider_configs WHERE config_id = ? AND subject_id = ?",
            (config_id, subject_id),
        ).fetchone()
        if provider is None or provider["status"] != "active":
            raise ValueError("search provider is unavailable")
        connection.execute(
            "INSERT INTO search_provider_controls(config_id, enabled, state_hash, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(config_id) DO UPDATE SET "
            "enabled=excluded.enabled, state_hash=excluded.state_hash, "
            "updated_at=excluded.updated_at",
            (config_id, int(enabled), state_hash, timestamp),
        )
    return timestamp


__all__ = [
    "SEARCH_ROUTING_MODES",
    "get_search_routing_mode",
    "list_search_provider_controls",
    "set_search_provider_enabled",
    "set_search_routing_mode",
]
