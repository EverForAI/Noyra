"""Immutable increment metadata and dependency receipts. No file writer or HTTP control."""

from __future__ import annotations

from typing import Any

from .admission import OperationInvalidated
from .archive import ArchiveKeyring
from .database import Database
from .errors import IntegrityError
from .events import EventStore
from .types import canonical_json, content_hash, strict_json_loads, utc_now
from .workflow_bundle import SCOPE, require, source_identity
from .workflow_export_catalog import WorkflowExportCatalog
from .workflow_export_files import PUBLICATION
from .workflow_increment_contract import (
    CATALOG_FORMAT,
    FORMAT,
    INCREMENT,
    MAX_CHAIN_NODES,
    compatible,
    digest,
    known_source,
    publication_id,
    validate_manifest,
    validate_parent,
)

FIELDS = (
    "publication_id",
    "subject_id",
    "workflow_id",
    "parent_base_id",
    "parent_increment_id",
    "parent_ref_json",
    "source_json",
    "source_hash",
    "first_version",
    "last_version",
    "chain_depth",
    "encryption_key_id",
    "encryption_key_fingerprint",
    "key_generation",
    "status",
    "manifest_json",
    "receipt_event_id",
    "created_at",
    "updated_at",
)
REQUEST_FIELDS = (
    "subject_id",
    "request_hash",
    "publication_id",
    "workflow_id",
    "parent_id",
    "expected_version",
    "receipt_event_id",
    "created_at",
)


class WorkflowIncrementCatalog:
    def __init__(self, database: Database, subject: str, *, events: EventStore | None = None):
        self.database, self.subject = database, subject
        self.events = events if events is not None else EventStore(database)

    def _base_catalog(self) -> WorkflowExportCatalog:
        catalog = WorkflowExportCatalog(self.database, self.subject)
        catalog.events = self.events
        return catalog

    def _event(self, c: Any, event_id: str, kind: str) -> dict[str, Any]:
        row = c.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        require(row is not None, "increment receipt is missing")
        value = self.events.payload_from_row(row, connection=c)
        require(
            isinstance(value, dict)
            and row["subject_id"] == self.subject
            and row["event_type"] == kind
            and row["source"] == "workflow_increment"
            and row["privacy_level"] == "private"
            and content_hash(value) == row["payload_hash"],
            "increment receipt is invalid",
        )
        return value

    def _append(self, c: Any, event_id: str, kind: str, payload: dict[str, Any]) -> None:
        self.events._append_connection(
            c,
            self.subject,
            kind,
            "workflow_increment",
            payload,
            privacy_level="private",
            causal_parent_ids=(),
            occurred_at=None,
            event_id=event_id,
        )

    @staticmethod
    def _binding(record: dict[str, Any]) -> dict[str, Any]:
        return {
            "format": CATALOG_FORMAT,
            "artifact_format": FORMAT,
            "publication_id": record["publication_id"],
            "source_hash": record["source_hash"],
            "source_json_hash": content_hash(record["source"]),
            "parent": record["parent"],
            "first_version": record["first_version"],
            "last_version": record["last_version"],
            "chain_depth": record["chain_depth"],
            "encryption_key_id": record["encryption_key_id"],
            "encryption_key_fingerprint": record["encryption_key_fingerprint"],
            "key_generation": record["key_generation"],
        }

    def _decode(self, c: Any, row: Any) -> dict[str, Any]:
        require(row is not None and row["subject_id"] == self.subject, "increment is missing")
        raw = {key: row[key] for key in FIELDS}
        require(content_hash(raw) == row["state_hash"], "increment state hash differs")
        source, parent = (
            strict_json_loads(row["source_json"]),
            strict_json_loads(row["parent_ref_json"]),
        )
        known_source(source)
        validate_parent(parent)
        parent_id = row["parent_base_id"] or row["parent_increment_id"]
        require(
            source["subject_id"] == self.subject
            and source["workflow_id"] == row["workflow_id"]
            and source_identity(source) == row["source_hash"]
            and row["publication_id"] == publication_id(parent_id, source)
            and parent_id == parent["publication_id"]
            and (
                (parent["kind"] == "base" and row["parent_increment_id"] is None)
                or (parent["kind"] == "increment" and row["parent_base_id"] is None)
            ),
            "increment identity or scope differs",
        )
        require(
            type(row["first_version"]) is int
            and row["first_version"] == parent["end_version"] + 1
            and type(row["last_version"]) is int
            and row["last_version"] == source["records"]
            and row["last_version"] >= row["first_version"]
            and type(row["chain_depth"]) is int
            and row["chain_depth"] == parent["chain_depth"] + 1
            and row["chain_depth"] < MAX_CHAIN_NODES
            and type(row["key_generation"]) is int
            and row["key_generation"] >= 1
            and digest(row["encryption_key_fingerprint"])
            and row["status"] in {"prepared", "published_durability_unknown", "accepted"},
            "increment range or state is invalid",
        )
        ArchiveKeyring._validate_key_id(row["encryption_key_id"])
        manifest = None if row["manifest_json"] is None else strict_json_loads(row["manifest_json"])
        record = {**raw, "source": source, "parent": parent, "manifest": manifest}
        prepared = self._event(
            c, "evt_wincrement_prepared_" + row["publication_id"], "workflow_increment_prepared"
        )
        require(
            canonical_json(prepared) == canonical_json(self._binding(record)),
            "increment preparation differs",
        )
        if row["status"] == "prepared":
            require(
                manifest is None and row["receipt_event_id"] is None,
                "prepared increment has terminal data",
            )
        else:
            validate_manifest(manifest)
            if row["status"] == "accepted":
                expected = "evt_wincrement_accepted_" + row["publication_id"]
                require(
                    row["receipt_event_id"] == expected, "increment acceptance identity differs"
                )
                payload = self._event(c, expected, "workflow_increment_accepted")
                require(
                    canonical_json(payload)
                    == canonical_json(
                        {
                            **self._binding(record),
                            "manifest": manifest,
                            "scope": SCOPE,
                        }
                    ),
                    "increment acceptance differs",
                )
            else:
                require(row["receipt_event_id"] is None, "unconfirmed increment has acceptance")
        return record

    def _load(self, c: Any, identifier: str) -> dict[str, Any] | None:
        row = c.execute(
            "SELECT * FROM workflow_increment_publications WHERE subject_id=? AND publication_id=?",
            (self.subject, identifier),
        ).fetchone()
        if row is None:
            return None
        try:
            return self._decode(c, row)
        except (KeyError, TypeError, ValueError) as error:
            raise IntegrityError("workflow increment metadata is invalid") from error

    def _reference(self, c: Any, record: dict[str, Any], kind: str) -> dict[str, Any]:
        require(record["status"] == "accepted", "increment parent is not accepted")
        validate_manifest(record["manifest"])
        if kind == "base":
            payload = self._base_catalog().event(
                c, record["receipt_event_id"], "workflow_export_accepted"
            )
            binding = WorkflowExportCatalog._binding(record)
            depth = 0
        else:
            payload = self._event(c, record["receipt_event_id"], "workflow_increment_accepted")
            binding = self._binding(record)
            depth = record["chain_depth"]
        return {
            "kind": kind,
            "publication_id": record["publication_id"],
            "binding_hash": content_hash(binding),
            "source_json_hash": content_hash(record["source"]),
            "manifest_hash": content_hash(record["manifest"]),
            "acceptance_event_id": record["receipt_event_id"],
            "acceptance_payload_hash": content_hash(payload),
            "end_version": record["source"]["records"],
            "chain_depth": depth,
        }

    def get(self, c: Any, identifier: str) -> dict[str, Any] | None:
        if not isinstance(identifier, str) or INCREMENT.fullmatch(identifier) is None:
            raise ValueError("invalid increment identity")
        first = self._load(c, identifier)
        if first is None:
            return None
        current = first
        seen = {identifier}
        for _ in range(MAX_CHAIN_NODES - 1):
            parent_id = current["parent"]["publication_id"]
            require(parent_id not in seen, "increment parent cycle")
            seen.add(parent_id)
            if current["parent"]["kind"] == "base":
                parent = self._base_catalog().get(c, parent_id)
            else:
                parent = self._load(c, parent_id)
            require(parent is not None, "increment ancestor is missing")
            assert parent is not None
            known_source(parent["source"])
            actual = self._reference(c, parent, current["parent"]["kind"])
            require(
                canonical_json(current["parent"]) == canonical_json(actual),
                "increment exact parent reference differs",
            )
            compatible(parent["source"], current["source"])
            if current["parent"]["kind"] == "base":
                return first
            current = parent
        require(False, "increment chain exceeds depth budget")
        return None

    def parent(self, c: Any, identifier: str) -> tuple[dict[str, Any], dict[str, Any]]:
        if isinstance(identifier, str) and PUBLICATION.fullmatch(identifier):
            record = self._base_catalog().get(c, identifier)
            kind = "base"
        else:
            record = self.get(c, identifier)
            kind = "increment"
        require(record is not None, "increment parent is missing")
        assert record is not None
        known_source(record["source"])
        return record, self._reference(c, record, kind)

    def requested(
        self, c: Any, request_hash: str, parent_id: str, workflow: str, expected_version: int
    ) -> dict[str, Any] | None:
        if (
            not digest(request_hash)
            or not isinstance(parent_id, str)
            or not isinstance(workflow, str)
            or type(expected_version) is not int
            or expected_version < 2
        ):
            raise ValueError("invalid increment request intent")
        row = c.execute(
            "SELECT * FROM workflow_increment_requests WHERE subject_id=? AND request_hash=?",
            (self.subject, request_hash),
        ).fetchone()
        if row is None:
            return None
        raw = {key: row[key] for key in REQUEST_FIELDS}
        require(content_hash(raw) == row["state_hash"], "increment request hash differs")
        require(
            row["receipt_event_id"] == self._request_event(request_hash),
            "increment request identity differs",
        )
        payload = self._event(c, row["receipt_event_id"], "workflow_increment_requested")
        require(
            canonical_json(payload) == canonical_json({"format": CATALOG_FORMAT, **raw}),
            "increment request receipt differs",
        )
        if (parent_id, workflow, expected_version) != (
            row["parent_id"],
            row["workflow_id"],
            row["expected_version"],
        ):
            raise ValueError("increment request key already binds another intent")
        record = self.get(c, row["publication_id"])
        require(
            record is not None
            and record["workflow_id"] == workflow
            and record["last_version"] == expected_version
            and record["parent"]["publication_id"] == parent_id,
            "increment request target differs",
        )
        return record

    def _request_event(self, request_hash: str) -> str:
        return "evt_wincrement_requested_" + content_hash([self.subject, request_hash])

    def _privacy(self, c: Any) -> None:
        if (
            c.execute(
                "SELECT 1 FROM private_conversations WHERE subject_id=? "
                "AND status IN ('deleting','deleted') LIMIT 1",
                (self.subject,),
            ).fetchone()
            or c.execute(
                "SELECT 1 FROM private_erasure_jobs WHERE subject_id=? LIMIT 1",
                (self.subject,),
            ).fetchone()
        ):
            raise OperationInvalidated("increment metadata requires privacy derivative review")

    def prepare(
        self,
        c: Any,
        source: dict[str, Any],
        parent_id: str,
        request_hash: str,
        ring: ArchiveKeyring,
    ) -> dict[str, Any]:
        """Internal transaction primitive; I2 must supply a fully verified source snapshot."""
        self._privacy(c)
        known_source(source)
        if source["subject_id"] != self.subject or not digest(request_hash):
            raise ValueError("invalid increment source or request")
        ring.validate_revision(self.database, self.subject, connection=c)
        require(
            c.execute(
                "SELECT 1 FROM archive_keyring_revisions WHERE subject_id=? AND generation=?",
                (self.subject, ring.generation),
            ).fetchone()
            is not None,
            "increment keyring generation must be recorded first",
        )
        existing = self.requested(
            c, request_hash, parent_id, source["workflow_id"], source["records"]
        )
        if existing is not None:
            if source_identity(existing["source"]) != source_identity(source):
                raise ValueError("increment request is bound to a different frozen source")
            return existing
        parent, reference = self.parent(c, parent_id)
        compatible(parent["source"], source)
        validate_parent(reference)
        ring.validate_revision(self.database, self.subject, connection=c)
        identifier = publication_id(parent_id, source)
        record = self.get(c, identifier)
        now = utc_now()
        if record is None:
            raw = {
                "publication_id": identifier,
                "subject_id": self.subject,
                "workflow_id": source["workflow_id"],
                "parent_base_id": parent_id if reference["kind"] == "base" else None,
                "parent_increment_id": parent_id if reference["kind"] == "increment" else None,
                "parent_ref_json": canonical_json(reference),
                "source_json": canonical_json(source),
                "source_hash": source_identity(source),
                "first_version": reference["end_version"] + 1,
                "last_version": source["records"],
                "chain_depth": reference["chain_depth"] + 1,
                "encryption_key_id": ring.active.key_id,
                "encryption_key_fingerprint": ring.active.fingerprint,
                "key_generation": ring.generation,
                "status": "prepared",
                "manifest_json": None,
                "receipt_event_id": None,
                "created_at": now,
                "updated_at": now,
            }
            binding = {**raw, "source": source, "parent": reference}
            self._append(
                c,
                "evt_wincrement_prepared_" + identifier,
                "workflow_increment_prepared",
                self._binding(binding),
            )
            c.execute(
                "INSERT INTO workflow_increment_publications("
                + ",".join(FIELDS)
                + ",state_hash) VALUES("
                + ",".join("?" for _ in range(len(FIELDS) + 1))
                + ")",
                (*(raw[key] for key in FIELDS), content_hash(raw)),
            )
        request = {
            "subject_id": self.subject,
            "request_hash": request_hash,
            "publication_id": identifier,
            "workflow_id": source["workflow_id"],
            "parent_id": parent_id,
            "expected_version": source["records"],
            "receipt_event_id": self._request_event(request_hash),
            "created_at": now,
        }
        self._append(
            c,
            request["receipt_event_id"],
            "workflow_increment_requested",
            {"format": CATALOG_FORMAT, **request},
        )
        c.execute(
            "INSERT INTO workflow_increment_requests("
            + ",".join(REQUEST_FIELDS)
            + ",state_hash) VALUES("
            + ",".join("?" for _ in range(len(REQUEST_FIELDS) + 1))
            + ")",
            (*(request[key] for key in REQUEST_FIELDS), content_hash(request)),
        )
        result = self.get(c, identifier)
        require(result is not None, "increment preparation disappeared")
        assert result is not None
        return result

    def finish(
        self, c: Any, record: dict[str, Any], manifest: dict[str, Any], *, durable: bool
    ) -> dict[str, Any]:
        """Metadata primitive: the caller verifies artifacts/fences and commits audit atomically.

        No runtime route calls this until the separate I2 publication control exists.
        """
        self._privacy(c)
        validate_manifest(manifest)
        if type(durable) is not bool:
            raise ValueError("invalid increment durability observation")
        current = self.get(c, record["publication_id"])
        require(current is not None, "increment disappeared")
        assert current is not None
        require(
            canonical_json(self._binding(record)) == canonical_json(self._binding(current)),
            "increment frozen binding changed",
        )
        if current["status"] != "prepared":
            require(
                canonical_json(current["manifest"]) == canonical_json(manifest),
                "published increment manifest cannot be replaced",
            )
            if current["status"] == "accepted" or not durable:
                return current
        raw = {key: current[key] for key in FIELDS}
        raw.update(
            status="accepted" if durable else "published_durability_unknown",
            manifest_json=canonical_json(manifest),
            updated_at=utc_now(),
        )
        if durable:
            raw["receipt_event_id"] = "evt_wincrement_accepted_" + current["publication_id"]
            self._append(
                c,
                raw["receipt_event_id"],
                "workflow_increment_accepted",
                {
                    **self._binding(current),
                    "manifest": manifest,
                    "scope": SCOPE,
                },
            )
        changed = c.execute(
            "UPDATE workflow_increment_publications SET status=?,manifest_json=?,"
            "receipt_event_id=?,updated_at=?,state_hash=? WHERE publication_id=? "
            "AND subject_id=? AND status=? AND state_hash=?",
            (
                raw["status"],
                raw["manifest_json"],
                raw["receipt_event_id"],
                raw["updated_at"],
                content_hash(raw),
                raw["publication_id"],
                self.subject,
                current["status"],
                content_hash({key: current[key] for key in FIELDS}),
            ),
        )
        require(changed.rowcount == 1, "increment publication compare-and-swap failed")
        result = self.get(c, raw["publication_id"])
        require(result is not None, "increment publication disappeared")
        assert result is not None
        return result

    def verify_requests(self, c: Any) -> int:
        require(
            c.execute(
                "SELECT 1 FROM workflow_increment_publications p "
                "WHERE p.subject_id=? AND NOT EXISTS (SELECT 1 FROM workflow_increment_requests r "
                "WHERE r.publication_id=p.publication_id "
                "AND r.subject_id=p.subject_id) LIMIT 1",
                (self.subject,),
            ).fetchone()
            is None,
            "increment has no immutable request intent",
        )
        count = 0
        for row in c.execute(
            "SELECT * FROM workflow_increment_requests WHERE subject_id=?", (self.subject,)
        ):
            self.requested(
                c,
                row["request_hash"],
                row["parent_id"],
                row["workflow_id"],
                row["expected_version"],
            )
            count += 1
        return count

    @staticmethod
    def public(record: dict[str, Any]) -> dict[str, Any]:
        return {
            "publication_id": record["publication_id"],
            "workflow_id": record["workflow_id"],
            "parent_id": record["parent"]["publication_id"],
            "status": record["status"],
            "artifact_format": FORMAT,
            "first_version": record["first_version"],
            "last_version": record["last_version"],
            "chain_depth": record["chain_depth"],
            "increment_records": record["last_version"] - record["first_version"] + 1,
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
            raise ValueError("invalid increment directory limit")
        with self.database.read_transaction() as c:
            rows = c.execute(
                "SELECT publication_id FROM workflow_increment_publications WHERE subject_id=? "
                "ORDER BY created_at DESC,publication_id DESC LIMIT ?",
                (self.subject, limit + 1),
            ).fetchall()
            items = []
            for row in rows[:limit]:
                record = self.get(c, row["publication_id"])
                require(record is not None, "increment directory record missing")
                assert record is not None
                items.append(self.public(record))
            return {
                "format": CATALOG_FORMAT,
                "items": items,
                "truncated": len(rows) > limit,
                "record_limit": limit,
                "scope": "latest_publications_not_total",
            }
