"""Durable source-bound workflow publications and immutable acceptance receipts."""

from __future__ import annotations

from typing import Any

from .archive import ArchiveKeyring
from .database import Database
from .events import EventStore
from .types import canonical_json, content_hash, strict_json_loads, utc_now
from .workflow_bundle import SCOPE, require, source_identity, validate_source

CATALOG_FORMAT = "workflow-export-publication/v1"
FIELDS = (
    "publication_id",
    "subject_id",
    "workflow_id",
    "source_json",
    "source_hash",
    "encryption_key_id",
    "encryption_key_fingerprint",
    "key_generation",
    "status",
    "manifest_json",
    "receipt_event_id",
    "created_at",
    "updated_at",
)


def request_id(subject: str, request_hash: str) -> str:
    return "evt_wexport_request_" + content_hash([subject, request_hash])


def publication_id(source: dict[str, Any]) -> str:
    return "wexport_" + source_identity(source)


class WorkflowExportCatalog:
    def __init__(self, database: Database, subject: str):
        self.database, self.subject = database, subject
        self.events = EventStore(database)

    def event(self, connection: Any, identifier: str, kind: str) -> dict[str, Any] | None:
        row = connection.execute("SELECT * FROM events WHERE event_id=?", (identifier,)).fetchone()
        if row is None:
            return None
        value = self.events.payload_from_row(row, connection=connection)
        require(
            row["subject_id"] == self.subject
            and row["event_type"] == kind
            and row["source"] == "workflow_export"
            and row["privacy_level"] == "private"
            and content_hash(value) == row["payload_hash"],
            "publication event is invalid",
        )
        return value

    @staticmethod
    def _binding(record: dict[str, Any]) -> dict[str, Any]:
        return {
            "format": CATALOG_FORMAT,
            "publication_id": record["publication_id"],
            "source_hash": record["source_hash"],
            "source_json_hash": content_hash(record["source"]),
            "encryption_key_id": record["encryption_key_id"],
            "encryption_key_fingerprint": record["encryption_key_fingerprint"],
            "key_generation": record["key_generation"],
        }

    def decode(self, connection: Any, row: Any) -> dict[str, Any]:
        require(row is not None and row["subject_id"] == self.subject, "publication is missing")
        raw = {key: row[key] for key in FIELDS}
        require(content_hash(raw) == row["state_hash"], "publication state hash differs")
        source = strict_json_loads(row["source_json"])
        validate_source(source)
        require(
            source.get("subject_id") == self.subject
            and source.get("workflow_id") == row["workflow_id"]
            and type(source.get("records")) is int
            and source["records"] > 0
            and row["source_hash"] == source_identity(source)
            and row["publication_id"] == publication_id(source),
            "publication source binding differs",
        )
        require(
            type(row["key_generation"]) is int
            and row["key_generation"] >= 1
            and row["status"] in {"prepared", "published_durability_unknown", "accepted"},
            "publication state is invalid",
        )
        manifest = None if row["manifest_json"] is None else strict_json_loads(row["manifest_json"])
        result = {**raw, "source": source, "manifest": manifest}
        prepared = self.event(
            connection, "evt_wexport_prepared_" + row["source_hash"], "workflow_export_prepared"
        )
        require(prepared == self._binding(result), "publication preparation receipt differs")
        if row["status"] == "prepared":
            require(
                manifest is None and row["receipt_event_id"] is None,
                "prepared publication has terminal data",
            )
        else:
            require(
                isinstance(manifest, dict)
                and set(manifest) == {"name", "stored_bytes", "stored_hash", "plaintext_hash"}
                and manifest["name"] == "manifest.enc",
                "published manifest descriptor differs",
            )
            if row["status"] == "accepted":
                expected_id = "evt_wexport_accepted_" + row["source_hash"]
                require(
                    row["receipt_event_id"] == expected_id,
                    "publication acceptance identity differs",
                )
                accepted = self.event(connection, expected_id, "workflow_export_accepted")
                require(
                    accepted == {**self._binding(result), "manifest": manifest, "scope": SCOPE},
                    "publication acceptance receipt differs",
                )
            else:
                require(row["receipt_event_id"] is None, "unconfirmed publication has acceptance")
        return result

    def get(self, connection: Any, identifier: str) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT * FROM workflow_export_publications WHERE publication_id=? AND subject_id=?",
            (identifier, self.subject),
        ).fetchone()
        return None if row is None else self.decode(connection, row)

    def requested(
        self, connection: Any, request_hash: str, workflow: str, version: int
    ) -> dict[str, Any] | None:
        value = self.event(
            connection, request_id(self.subject, request_hash), "workflow_export_requested"
        )
        if value is None:
            return None
        require(
            set(value)
            == {"format", "publication_id", "workflow_id", "expected_version", "request_hash"}
            and value["format"] == CATALOG_FORMAT
            and value["request_hash"] == request_hash,
            "request receipt differs",
        )
        if value["workflow_id"] != workflow or value["expected_version"] != version:
            raise ValueError("workflow export request key was already used for another snapshot")
        result = self.get(connection, value["publication_id"])
        require(result is not None, "request references missing publication")
        return result

    def prepare(
        self, connection: Any, source: dict[str, Any], request_hash: str, ring: ArchiveKeyring
    ) -> dict[str, Any]:
        identifier = publication_id(source)
        existing = self.get(connection, identifier)
        if existing is None:
            now = utc_now()
            raw = {
                "publication_id": identifier,
                "subject_id": self.subject,
                "workflow_id": source["workflow_id"],
                "source_json": canonical_json(source),
                "source_hash": source_identity(source),
                "encryption_key_id": ring.active.key_id,
                "encryption_key_fingerprint": ring.active.fingerprint,
                "key_generation": ring.generation,
                "status": "prepared",
                "manifest_json": None,
                "receipt_event_id": None,
                "created_at": now,
                "updated_at": now,
            }
            self.events._append_connection(
                connection,
                self.subject,
                "workflow_export_prepared",
                "workflow_export",
                self._binding({**raw, "source": source}),
                privacy_level="private",
                causal_parent_ids=(),
                occurred_at=now,
                event_id="evt_wexport_prepared_" + raw["source_hash"],
            )
            connection.execute(
                "INSERT INTO workflow_export_publications(" + ",".join(FIELDS) + ",state_hash) "
                "VALUES (" + ",".join("?" for _ in range(len(FIELDS) + 1)) + ")",
                (*(raw[key] for key in FIELDS), content_hash(raw)),
            )
        self.events._append_connection(
            connection,
            self.subject,
            "workflow_export_requested",
            "workflow_export",
            {
                "format": CATALOG_FORMAT,
                "publication_id": identifier,
                "workflow_id": source["workflow_id"],
                "expected_version": source["records"],
                "request_hash": request_hash,
            },
            privacy_level="private",
            causal_parent_ids=(),
            occurred_at=None,
            event_id=request_id(self.subject, request_hash),
        )
        result = self.get(connection, identifier)
        require(result is not None, "prepared publication disappeared")
        assert result is not None
        return result

    def finish(
        self, connection: Any, record: dict[str, Any], manifest: dict[str, Any], *, durable: bool
    ) -> dict[str, Any]:
        current = self.get(connection, record["publication_id"])
        require(current is not None, "publication disappeared")
        assert current is not None
        require(current["source_hash"] == record["source_hash"], "publication source changed")
        if current["status"] == "accepted" or (
            current["status"] == "published_durability_unknown" and not durable
        ):
            return current
        raw = {key: current[key] for key in FIELDS}
        raw.update(
            status="accepted" if durable else "published_durability_unknown",
            manifest_json=canonical_json(manifest),
            updated_at=utc_now(),
        )
        if durable:
            raw["receipt_event_id"] = "evt_wexport_accepted_" + current["source_hash"]
            self.events._append_connection(
                connection,
                self.subject,
                "workflow_export_accepted",
                "workflow_export",
                {**self._binding(current), "manifest": manifest, "scope": SCOPE},
                privacy_level="private",
                causal_parent_ids=(),
                occurred_at=None,
                event_id=raw["receipt_event_id"],
            )
        changed = connection.execute(
            "UPDATE workflow_export_publications SET status=?,manifest_json=?,receipt_event_id=?,"
            "updated_at=?,state_hash=? WHERE publication_id=? "
            "AND subject_id=? AND status=? AND state_hash=?",
            (
                raw["status"],
                raw["manifest_json"],
                raw["receipt_event_id"],
                raw["updated_at"],
                content_hash(raw),
                current["publication_id"],
                self.subject,
                current["status"],
                content_hash({k: current[k] for k in FIELDS}),
            ),
        )
        require(changed.rowcount == 1, "publication compare-and-swap failed")
        result = self.get(connection, current["publication_id"])
        require(result is not None, "finished publication disappeared")
        assert result is not None
        return result

    @staticmethod
    def public(record: dict[str, Any]) -> dict[str, Any]:
        return {
            "publication_id": record["publication_id"],
            "workflow_id": record["workflow_id"],
            "status": record["status"],
            "records": record["source"]["records"],
            "state_bytes": record["source"]["state_bytes"],
            "scope": SCOPE,
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
            "artifact_observation": "not_checked_by_catalog_read",
            "source_history_preserved": True,
            "restore_or_dispatch_authorized": False,
        }

    def page(self, *, limit: int = 50) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid workflow export limit")
        with self.database.read_transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM workflow_export_publications WHERE subject_id=? "
                "ORDER BY created_at DESC,publication_id DESC LIMIT ?",
                (self.subject, limit + 1),
            ).fetchall()
            return {
                "format": CATALOG_FORMAT,
                "items": [self.public(self.decode(connection, row)) for row in rows[:limit]],
                "truncated": len(rows) > limit,
                "record_limit": limit,
                "scope": "latest_publications_not_total",
            }
