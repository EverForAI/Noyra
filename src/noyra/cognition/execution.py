from __future__ import annotations

import ast
import builtins
import hashlib
import importlib
import json
import os
import re
import stat
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from noyra.autonomy.workflow import DurableWorkflowStore
from noyra.core.admission import assert_current_lease, current_commit_scope
from noyra.core.database import Database
from noyra.core.errors import IntegrityError
from noyra.core.events import EventStore
from noyra.core.identity import IdentityStore, validate_subject_id
from noyra.core.subject_storage import SubjectStorageDirectory
from noyra.core.types import canonical_json, content_hash, new_id, strict_json_loads, utc_now
from noyra.interaction import InteractionStore
from noyra.model import ModelLedger, ModelMessage
from noyra.model.errors import BudgetExhaustedError, ModelCallStateError, ProviderCallError
from noyra.world import PredictionStore
from noyra.world.types import PredictionProposal

from ._integrity import durable_boundary, durable_json
from .projects import AutonomousProjectPhaseRecord, AutonomousProjectRecord
from .research import AutonomousResearch, AutonomousResearchValidationError


class ProjectExecutionError(RuntimeError):
    pass


class ProjectExecutionValidationError(ProjectExecutionError):
    pass


_PLACEHOLDER_PATTERN = re.compile(
    r"^\s*(?:todo|tbd|placeholder|not\s+implemented|coming\s+soon|lorem\s+ipsum)\b",
    re.IGNORECASE,
)
_PROTOTYPE_PLACEHOLDER_PATTERN = re.compile(
    r"\b(?:todo|tbd|placeholder|not\s+implemented|coming\s+soon|lorem\s+ipsum)\b",
    re.IGNORECASE,
)
_O_DIRECTORY = int(getattr(os, "O_DIRECTORY", 0))
_O_NOFOLLOW = int(getattr(os, "O_NOFOLLOW", 0))
_O_CLOEXEC = int(getattr(os, "O_CLOEXEC", 0))


def _artifact_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_evidence_bytes(evidence: dict[str, Any]) -> bytes:
    return canonical_json(evidence).encode("utf-8")


def _require_substantive_text(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip() or _PLACEHOLDER_PATTERN.search(value):
        raise ProjectExecutionValidationError(f"{context} is empty or placeholder content")
    return value


class _PrototypeHTMLValidator(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.start_tags = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del tag, attrs
        self.start_tags += 1


def _validate_balanced_source(source: str, context: str) -> None:
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack: list[str] = []
    quote: str | None = None
    escaped = False
    for char in source:
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {"'", '"', "`"}:
            quote = char
        elif char in pairs:
            stack.append(pairs[char])
        elif char in pairs.values() and (not stack or stack.pop() != char):
            raise ProjectExecutionValidationError(f"{context} has unbalanced delimiters")
    if quote is not None or stack:
        raise ProjectExecutionValidationError(f"{context} has unterminated syntax")


def _validate_prototype_file(path: str, payload: bytes) -> str:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ProjectExecutionValidationError(
            f"prototype file is not UTF-8 text: {path}"
        ) from error
    _require_substantive_text(text, f"prototype file {path}")
    visible_text = re.sub(r"<[^>]+>", " ", text)
    if _PROTOTYPE_PLACEHOLDER_PATTERN.search(visible_text):
        raise ProjectExecutionValidationError(f"prototype file {path} contains placeholder content")
    suffix = Path(path).suffix.casefold()
    try:
        if suffix == ".py":
            ast.parse(text, filename=path)
            return "python_ast"
        if suffix == ".json":
            strict_json_loads(text)
            return "json_parse"
        if suffix in {".js", ".css"}:
            _validate_balanced_source(text, path)
            return "balanced_source"
        if suffix == ".html":
            parser = _PrototypeHTMLValidator()
            parser.feed(text)
            parser.close()
            if parser.start_tags == 0:
                raise ProjectExecutionValidationError("prototype HTML contains no elements")
            return "html_parse"
    except (SyntaxError, TypeError, ValueError) as error:
        raise ProjectExecutionValidationError(
            f"prototype syntax validation failed: {path}"
        ) from error
    return "substantive_text"


def _run_prototype_tests(files: dict[str, bytes]) -> dict[str, Any]:
    """Run a bounded declarative test manifest without executing model code."""
    manifest_payload = files.get("prototype-tests.json")
    if manifest_payload is None:
        raise ProjectExecutionValidationError("prototype test manifest is required")
    if len(manifest_payload) > 128_000:
        raise ProjectExecutionValidationError("prototype test manifest exceeds its byte limit")
    try:
        manifest = strict_json_loads(manifest_payload)
    except (TypeError, ValueError) as error:
        raise ProjectExecutionValidationError("prototype test manifest is invalid JSON") from error
    if not isinstance(manifest, dict) or set(manifest) != {"version", "tests"}:
        raise ProjectExecutionValidationError("prototype test manifest schema is invalid")
    if manifest.get("version") != 1:
        raise ProjectExecutionValidationError("prototype test manifest version is unsupported")
    tests = manifest.get("tests")
    if not isinstance(tests, list) or not 1 <= len(tests) <= 32:
        raise ProjectExecutionValidationError("prototype test manifest must contain 1-32 tests")
    checks: list[dict[str, Any]] = []
    for item in tests:
        if not isinstance(item, dict) or set(item) not in (
            {"name", "path", "contains"},
            {"name", "path", "sha256"},
        ):
            raise ProjectExecutionValidationError("prototype test case schema is invalid")
        name = item["name"]
        path = item["path"]
        if (
            not isinstance(name, str)
            or not name.strip()
            or len(name) > 128
            or not isinstance(path, str)
            or not path.strip()
            or "\\" in path
            or "\x00" in path
            or path in {".", ".."}
            or path.startswith(("/", "\\"))
            or any(part in {"", ".", ".."} for part in PurePosixPath(path).parts)
        ):
            raise ProjectExecutionValidationError("prototype test case path is invalid")
        normalized = PurePosixPath(path).as_posix()
        if normalized == "prototype-tests.json":
            raise ProjectExecutionValidationError(
                "prototype tests cannot assert only on their own manifest"
            )
        payload = files.get(normalized)
        if payload is None:
            raise ProjectExecutionValidationError("prototype test case references a missing file")
        if "contains" in item:
            needle = item["contains"]
            if (
                not isinstance(needle, str)
                or len(needle.strip()) < 3
                or len(needle) > 4_000
                or _PLACEHOLDER_PATTERN.search(needle)
            ):
                raise ProjectExecutionValidationError("prototype text assertion is invalid")
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ProjectExecutionValidationError(
                    "prototype text assertion references non-UTF-8 content"
                ) from error
            passed = needle in text
            check = "contains"
        else:
            digest = item["sha256"]
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ProjectExecutionValidationError("prototype hash assertion is invalid")
            passed = _artifact_sha256(payload) == digest
            check = "sha256"
        if not passed:
            raise ProjectExecutionValidationError(f"prototype test failed: {name}")
        checks.append({"name": name, "path": normalized, "check": check})
    return {
        "format": "noyra-prototype-tests/v1",
        "status": "passed",
        "test_count": len(checks),
        "checks": checks,
    }


class PrototypeFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str = Field(min_length=1, max_length=256)
    content: str = Field(max_length=200_000)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        path = Path(value)
        allowed = {".html", ".css", ".js", ".py", ".md", ".json", ".txt"}
        if path.is_absolute() or ".." in path.parts or path.suffix.casefold() not in allowed:
            raise ValueError("prototype file path is outside the artifact sandbox")
        return path.as_posix()


class SoftwarePrototypeProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    summary: str = Field(min_length=1, max_length=2_000)
    files: tuple[PrototypeFile, ...] = Field(min_length=1, max_length=20)
    validation: tuple[str, ...] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_files(self) -> SoftwarePrototypeProposal:
        paths = [item.path.casefold() for item in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("prototype file paths must be unique")
        return self


@dataclass(frozen=True)
class ProjectExecutionRecord:
    execution_id: str
    subject_id: str
    project_id: str
    phase_id: str
    execution_key: str
    execution_type: str
    workflow: str
    status: str
    research_id: str | None
    action_id: str | None
    model_call_id: str | None
    artifact_path: str | None
    artifact_hash: str | None
    result_hash: str | None
    acceptance: dict[str, Any]
    error_code: str | None
    created_at: str
    updated_at: str
    completed_at: str | None


class ProjectExecutionLedger:
    """Append-only execution identity and acceptance evidence for project phases."""

    CONTRACT_VERSION = "project-execution/v2"
    VALIDATOR_VERSION = 1

    def __init__(
        self,
        database: Database,
        subject_id: str,
        *,
        workspace_root: Path | str | None = None,
    ):
        self.database = database
        self.subject_id = validate_subject_id(subject_id)
        self.workspace_root = Path(workspace_root or database.path.parent / "workspace").resolve()
        self._storage_key: str | None = None
        self._legacy_storage_path_is_unambiguous = False

    def _ensure_storage_binding(self) -> None:
        if self._storage_key is not None:
            return
        identities = IdentityStore(self.database)
        self._storage_key = identities.storage_key(self.subject_id)
        self._legacy_storage_path_is_unambiguous = identities.legacy_storage_path_is_unambiguous(
            self.subject_id
        )

    @property
    def storage_key(self) -> str:
        self._ensure_storage_binding()
        assert self._storage_key is not None
        return self._storage_key

    @property
    def legacy_storage_path_is_unambiguous(self) -> bool:
        self._ensure_storage_binding()
        return self._legacy_storage_path_is_unambiguous

    def get_or_prepare(
        self,
        project: AutonomousProjectRecord,
        phase: AutonomousProjectPhaseRecord,
        *,
        workflow: str,
    ) -> ProjectExecutionRecord:
        if project.subject_id != self.subject_id or phase.project_id != project.project_id:
            raise ProjectExecutionError("project execution input ownership is invalid")
        key = f"{project.project_id}:{phase.phase_id}:{phase.attempt_count}"
        now = utc_now()
        with self.database.transaction() as connection:
            binding = connection.execute(
                "SELECT p.subject_id AS project_subject_id, ph.subject_id AS phase_subject_id, "
                "ph.project_id AS phase_project_id, ph.output_type "
                "FROM autonomous_projects p JOIN autonomous_project_phases ph "
                "ON ph.project_id = p.project_id "
                "WHERE p.project_id = ? AND ph.phase_id = ?",
                (project.project_id, phase.phase_id),
            ).fetchone()
            if (
                binding is None
                or binding["project_subject_id"] != self.subject_id
                or binding["phase_subject_id"] != self.subject_id
                or binding["phase_project_id"] != project.project_id
                or binding["output_type"] != phase.output_type
            ):
                raise ProjectExecutionError("project execution phase ownership is invalid")
            row = connection.execute(
                "SELECT * FROM autonomous_project_executions "
                "WHERE subject_id = ? AND execution_key = ?",
                (self.subject_id, key),
            ).fetchone()
            if row is not None:
                return self._from_row(row)
            execution_id = new_id("pexec")
            evidence: dict[str, Any] = {}
            acceptance = {
                "contract_version": self.CONTRACT_VERSION,
                "phase_criteria": list(phase.acceptance_criteria),
                "evidence": evidence,
                "evidence_hash": content_hash(evidence),
            }
            connection.execute(
                """INSERT INTO autonomous_project_executions(
                    execution_id, subject_id, project_id, phase_id, execution_key,
                    execution_type, workflow, status, acceptance_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?, ?)""",
                (
                    execution_id,
                    self.subject_id,
                    project.project_id,
                    phase.phase_id,
                    key,
                    phase.output_type,
                    workflow,
                    canonical_json(acceptance),
                    now,
                    now,
                ),
            )
            self._revision(connection, execution_id, "prepared", None, "execution prepared", now)
            row = connection.execute(
                "SELECT * FROM autonomous_project_executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            assert row is not None
            return self._from_row(row)

    def transition(
        self,
        execution_id: str,
        status: str,
        *,
        evidence: dict[str, Any] | None = None,
        research_id: str | None = None,
        action_id: str | None = None,
        model_call_id: str | None = None,
        artifact_path: str | None = None,
        artifact_hash: str | None = None,
        error_code: str | None = None,
        reason: str,
    ) -> ProjectExecutionRecord:
        allowed = {"prepared", "executing", "succeeded", "failed", "unknown", "blocked"}
        if status not in allowed:
            raise ProjectExecutionError("invalid execution status")
        now = utc_now()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM autonomous_project_executions WHERE execution_id = ? "
                "AND subject_id = ?",
                (execution_id, self.subject_id),
            ).fetchone()
            if row is None:
                raise ProjectExecutionError("execution is unavailable")
            current = self._from_row(row)
            if current.status in {"succeeded", "failed", "unknown", "blocked"}:
                if current.status != status:
                    raise ProjectExecutionError("terminal execution cannot transition")
                self._verify_execution_connection(connection, row)
                return current
            if current.status == "prepared" and status != "executing":
                raise ProjectExecutionError("prepared execution must enter executing state")
            acceptance = dict(current.acceptance)
            if evidence is not None:
                if not isinstance(evidence, dict):
                    raise ProjectExecutionError("execution evidence must be an object")
                acceptance["evidence"] = evidence
                acceptance["evidence_hash"] = content_hash(evidence)
            acceptance.setdefault("contract_version", self.CONTRACT_VERSION)
            effective_research_id = research_id or current.research_id
            effective_action_id = action_id or current.action_id
            effective_model_call_id = model_call_id or current.model_call_id
            effective_artifact_path = artifact_path or current.artifact_path
            effective_artifact_hash = artifact_hash or current.artifact_hash
            effective_error_code = error_code
            completed_at = now if status in {"succeeded", "failed", "unknown", "blocked"} else None
            if status == "succeeded":
                validation = self._validate_success_connection(
                    connection,
                    current,
                    acceptance,
                    research_id=effective_research_id,
                    action_id=effective_action_id,
                    model_call_id=effective_model_call_id,
                    artifact_path=effective_artifact_path,
                    artifact_hash=effective_artifact_hash,
                    validated_at=completed_at or now,
                )
                acceptance["validator"] = validation
                effective_error_code = None
            result_hash = self._result_hash(
                current,
                status=status,
                research_id=effective_research_id,
                action_id=effective_action_id,
                model_call_id=effective_model_call_id,
                artifact_path=effective_artifact_path,
                artifact_hash=effective_artifact_hash,
                acceptance=acceptance,
                error_code=effective_error_code,
                updated_at=now,
                completed_at=completed_at,
            )
            connection.execute(
                """UPDATE autonomous_project_executions SET status = ?, research_id = ?,
                    action_id = ?, model_call_id = ?, artifact_path = ?, artifact_hash = ?,
                    result_hash = ?, acceptance_json = ?, error_code = ?, updated_at = ?,
                    completed_at = ? WHERE execution_id = ?""",
                (
                    status,
                    effective_research_id,
                    effective_action_id,
                    effective_model_call_id,
                    effective_artifact_path,
                    effective_artifact_hash,
                    result_hash,
                    canonical_json(acceptance),
                    effective_error_code,
                    now,
                    completed_at,
                    execution_id,
                ),
            )
            self._revision(connection, execution_id, status, result_hash, reason, now)
            row = connection.execute(
                "SELECT * FROM autonomous_project_executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            assert row is not None
            return self._from_row(row)

    def list(self, project_id: str | None = None) -> list[ProjectExecutionRecord]:
        with self.database.connection() as connection:
            if project_id is None:
                rows = connection.execute(
                    "SELECT * FROM autonomous_project_executions WHERE subject_id = ? "
                    "ORDER BY created_at, execution_id",
                    (self.subject_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM autonomous_project_executions WHERE subject_id = ? "
                    "AND project_id = ? ORDER BY created_at, execution_id",
                    (self.subject_id, project_id),
                ).fetchall()
        return [self._from_row(row) for row in rows]

    def get(self, execution_id: str) -> ProjectExecutionRecord:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM autonomous_project_executions WHERE execution_id = ? "
                "AND subject_id = ?",
                (execution_id, self.subject_id),
            ).fetchone()
        if row is None:
            raise ProjectExecutionError("execution is unavailable")
        return self._from_row(row)

    def verified(self, execution_id: str) -> ProjectExecutionRecord:
        with self.database.read_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM autonomous_project_executions WHERE execution_id = ? "
                "AND subject_id = ?",
                (execution_id, self.subject_id),
            ).fetchone()
            if row is None:
                raise ProjectExecutionError("execution is unavailable")
            self._verify_execution_connection(connection, row)
            return self._from_row(row)

    def verify_integrity(self) -> int:
        with self.database.read_transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM autonomous_project_executions WHERE subject_id = ? "
                "ORDER BY created_at, execution_id",
                (self.subject_id,),
            ).fetchall()
            for row in rows:
                self._verify_execution_connection(connection, row)
            completed_phases = connection.execute(
                "SELECT phase_id FROM autonomous_project_phases "
                "WHERE subject_id = ? AND status = 'completed'",
                (self.subject_id,),
            ).fetchall()
            for phase in completed_phases:
                succeeded = connection.execute(
                    "SELECT 1 FROM autonomous_project_executions WHERE subject_id = ? "
                    "AND phase_id = ? AND status = 'succeeded' LIMIT 1",
                    (self.subject_id, phase["phase_id"]),
                ).fetchone()
                if succeeded is None:
                    raise IntegrityError("completed project phase has no validated execution")
            DurableWorkflowStore._verify_integrity_connection(connection, self.subject_id)
        return len(rows)

    def _verify_execution_connection(self, connection: Any, row: Any) -> None:
        record = self._from_row(row)
        if record.subject_id != self.subject_id:
            raise IntegrityError("project execution subject ownership mismatch")
        project_phase = connection.execute(
            "SELECT p.subject_id AS project_subject_id, p.status AS project_status, "
            "ph.subject_id AS phase_subject_id, ph.project_id AS phase_project_id, "
            "ph.output_type, ph.acceptance_criteria_json, ph.status AS phase_status "
            "FROM autonomous_projects p JOIN autonomous_project_phases ph "
            "ON ph.project_id = p.project_id WHERE p.project_id = ? AND ph.phase_id = ?",
            (record.project_id, record.phase_id),
        ).fetchone()
        if (
            project_phase is None
            or project_phase["project_subject_id"] != self.subject_id
            or project_phase["phase_subject_id"] != self.subject_id
            or project_phase["phase_project_id"] != record.project_id
            or project_phase["output_type"] != record.execution_type
        ):
            raise IntegrityError("project execution ownership or output type mismatch")
        criteria = durable_json(
            project_phase["acceptance_criteria_json"],
            "project execution phase criteria",
            record.phase_id,
        )
        if not isinstance(criteria, list) or record.acceptance.get("phase_criteria") != criteria:
            raise IntegrityError("project execution acceptance criteria mismatch")
        if record.acceptance.get("contract_version") != self.CONTRACT_VERSION:
            raise IntegrityError("project execution contract version mismatch")
        evidence = record.acceptance.get("evidence")
        if not isinstance(evidence, dict) or record.acceptance.get("evidence_hash") != content_hash(
            evidence
        ):
            raise IntegrityError("project execution evidence hash mismatch")

        revisions = connection.execute(
            "SELECT * FROM autonomous_project_execution_revisions "
            "WHERE execution_id = ? ORDER BY created_at, revision_id",
            (record.execution_id,),
        ).fetchall()
        if not revisions:
            raise IntegrityError("project execution has no revision")
        previous_status: str | None = None
        for index, revision in enumerate(revisions):
            revision_id = revision["revision_id"]
            with durable_boundary("project execution revision", revision_id):
                expected = content_hash(
                    {
                        "execution_id": record.execution_id,
                        "status": revision["status"],
                        "result_hash": revision["result_hash"],
                        "reason": revision["reason"],
                        "created_at": revision["created_at"],
                    }
                )
            if revision["state_hash"] != expected:
                raise IntegrityError("project execution revision hash mismatch")
            revision_status = revision["status"]
            if index == 0:
                if revision_status != "prepared" or revision["result_hash"] is not None:
                    raise IntegrityError(
                        "project execution revision history has no prepared origin"
                    )
            elif previous_status == "prepared" and revision_status != "executing":
                raise IntegrityError("project execution skipped the executing state")
            elif previous_status in {"succeeded", "failed", "unknown", "blocked"}:
                raise IntegrityError("project execution revision follows a terminal state")
            previous_status = revision_status
        latest = revisions[-1]
        if latest["status"] != record.status or latest["result_hash"] != record.result_hash:
            raise IntegrityError("project execution current row diverges from its latest revision")

        terminal = record.status in {"succeeded", "failed", "unknown", "blocked"}
        if (record.completed_at is not None) != terminal:
            raise IntegrityError("project execution completion timestamp mismatch")
        if record.status == "prepared":
            if record.result_hash is not None or len(revisions) != 1:
                raise IntegrityError("prepared project execution contains result state")
        else:
            expected_result = self._result_hash(
                record,
                status=record.status,
                research_id=record.research_id,
                action_id=record.action_id,
                model_call_id=record.model_call_id,
                artifact_path=record.artifact_path,
                artifact_hash=record.artifact_hash,
                acceptance=record.acceptance,
                error_code=record.error_code,
                updated_at=record.updated_at,
                completed_at=record.completed_at,
            )
            if record.result_hash != expected_result:
                raise IntegrityError("project execution result hash mismatch")

        artifact_payload: bytes | None = None
        if (record.artifact_path is None) != (record.artifact_hash is None):
            raise IntegrityError("project execution artifact metadata mismatch")
        if record.artifact_path is not None and record.artifact_hash is not None:
            artifact_payload = self._read_owned_artifact(
                record, record.artifact_path, record.artifact_hash
            )
            try:
                artifact_evidence = strict_json_loads(artifact_payload)
            except (TypeError, ValueError) as error:
                raise IntegrityError("project execution artifact is not valid JSON") from error
            if artifact_evidence != evidence:
                raise IntegrityError("project execution artifact and evidence diverge")

        validator = record.acceptance.get("validator")
        if record.status == "succeeded":
            try:
                expected_validator = self._validate_success_connection(
                    connection,
                    record,
                    record.acceptance,
                    research_id=record.research_id,
                    action_id=record.action_id,
                    model_call_id=record.model_call_id,
                    artifact_path=record.artifact_path,
                    artifact_hash=record.artifact_hash,
                    validated_at=record.completed_at or record.updated_at,
                    artifact_payload=artifact_payload,
                )
            except ProjectExecutionValidationError as error:
                raise IntegrityError("successful project execution no longer validates") from error
            if validator != expected_validator:
                raise IntegrityError("project execution validator evidence mismatch")
        elif isinstance(validator, dict) and validator.get("status") == "passed":
            raise IntegrityError("non-successful project execution has a passed validator")

        if project_phase["phase_status"] == "completed" and record.status == "succeeded":
            return
        if project_phase["project_status"] == "completed" and project_phase["phase_status"] != (
            "completed"
        ):
            raise IntegrityError("completed project contains an incomplete execution phase")

    @staticmethod
    def _result_hash(
        current: ProjectExecutionRecord,
        *,
        status: str,
        research_id: str | None,
        action_id: str | None,
        model_call_id: str | None,
        artifact_path: str | None,
        artifact_hash: str | None,
        acceptance: dict[str, Any],
        error_code: str | None,
        updated_at: str,
        completed_at: str | None,
    ) -> str:
        return content_hash(
            {
                "execution_id": current.execution_id,
                "subject_id": current.subject_id,
                "project_id": current.project_id,
                "phase_id": current.phase_id,
                "execution_key": current.execution_key,
                "execution_type": current.execution_type,
                "workflow": current.workflow,
                "status": status,
                "research_id": research_id,
                "action_id": action_id,
                "model_call_id": model_call_id,
                "artifact_path": artifact_path,
                "artifact_hash": artifact_hash,
                "acceptance": acceptance,
                "error_code": error_code,
                "created_at": current.created_at,
                "updated_at": updated_at,
                "completed_at": completed_at,
            }
        )

    def _read_owned_artifact(
        self,
        execution: ProjectExecutionRecord,
        artifact_path: str,
        artifact_hash: str,
    ) -> bytes:
        if not re.fullmatch(r"[0-9a-f]{64}", artifact_hash):
            raise IntegrityError("project execution artifact digest is invalid")
        candidate = Path(artifact_path)
        if not candidate.is_absolute():
            raise IntegrityError("project execution artifact path is not absolute")
        try:
            workspace = ProjectWorkspace(
                self.workspace_root,
                self.storage_key,
                quota_bytes=1,
                legacy_subject_id=execution.subject_id,
                legacy_migration_allowed=self.legacy_storage_path_is_unambiguous,
            )
            relative = workspace.relative_path(execution.project_id, candidate)
            parts = PurePosixPath(relative).parts
            if len(parts) < 3 or parts[:2] != ("phases", execution.phase_id):
                raise ProjectExecutionError(
                    "project execution artifact escaped its phase workspace"
                )
            payload = workspace.read(execution.project_id, relative)
        except (OSError, ProjectExecutionError, ValueError) as error:
            raise IntegrityError("project execution artifact could not be read") from error
        if _artifact_sha256(payload) != artifact_hash:
            raise IntegrityError("project execution artifact byte digest mismatch")
        return payload

    def _validate_success_connection(
        self,
        connection: Any,
        execution: ProjectExecutionRecord,
        acceptance: dict[str, Any],
        *,
        research_id: str | None,
        action_id: str | None,
        model_call_id: str | None,
        artifact_path: str | None,
        artifact_hash: str | None,
        validated_at: str,
        artifact_payload: bytes | None = None,
    ) -> dict[str, Any]:
        evidence = acceptance.get("evidence")
        if not isinstance(evidence, dict) or not evidence:
            raise ProjectExecutionValidationError("successful execution requires evidence")
        evidence_hash = content_hash(evidence)
        if acceptance.get("evidence_hash") != evidence_hash:
            raise ProjectExecutionValidationError("execution evidence digest is invalid")
        if artifact_path is None or artifact_hash is None:
            raise ProjectExecutionValidationError("successful execution requires an artifact")
        if artifact_payload is None:
            artifact_payload = self._read_owned_artifact(execution, artifact_path, artifact_hash)
        try:
            artifact_evidence = strict_json_loads(artifact_payload)
        except (TypeError, ValueError) as error:
            raise ProjectExecutionValidationError("execution artifact is not valid JSON") from error
        if artifact_evidence != evidence or _canonical_evidence_bytes(evidence) != artifact_payload:
            raise ProjectExecutionValidationError(
                "execution artifact must be the canonical acceptance evidence"
            )
        phase = connection.execute(
            "SELECT * FROM autonomous_project_phases WHERE phase_id = ? AND project_id = ? "
            "AND subject_id = ?",
            (execution.phase_id, execution.project_id, execution.subject_id),
        ).fetchone()
        if phase is None or phase["output_type"] != execution.execution_type:
            raise ProjectExecutionValidationError("execution phase binding is invalid")
        criteria = durable_json(
            phase["acceptance_criteria_json"], "project execution criteria", execution.phase_id
        )
        if not isinstance(criteria, list) or not all(isinstance(item, str) for item in criteria):
            raise ProjectExecutionValidationError("execution phase criteria are invalid")
        _require_substantive_text(phase["objective"], "project phase objective")
        for item in criteria:
            _require_substantive_text(item, "project phase acceptance criterion")

        checks = ["artifact_bytes", "evidence_digest", "phase_binding"]
        if action_id is not None:
            checks.extend(self._validate_action_connection(connection, execution, action_id))
        if execution.execution_type in {"research_note", "knowledge_collection"}:
            checks.extend(
                self._validate_research_connection(
                    connection,
                    execution,
                    evidence,
                    research_id=research_id,
                    model_call_id=model_call_id,
                )
            )
        elif execution.execution_type == "software_prototype":
            checks.extend(
                self._validate_software_connection(
                    connection,
                    execution,
                    evidence,
                    model_call_id=model_call_id,
                    artifact_path=artifact_path,
                )
            )
        elif execution.execution_type == "prediction_record":
            checks.extend(
                self._validate_prediction_connection(connection, execution, evidence, phase)
            )
        elif execution.execution_type == "self_experiment":
            checks.extend(
                self._validate_self_experiment_connection(connection, execution, evidence, phase)
            )
        elif execution.execution_type == "collaboration_request":
            checks.extend(
                self._validate_collaboration_connection(connection, execution, evidence, phase)
            )
        else:
            raise ProjectExecutionValidationError("execution output validator is unavailable")
        return {
            "name": execution.execution_type,
            "version": self.VALIDATOR_VERSION,
            "status": "passed",
            "checks": checks,
            "evidence_hash": evidence_hash,
            "artifact_hash": artifact_hash,
            "validated_at": validated_at,
        }

    @staticmethod
    def _validate_action_connection(
        connection: Any, execution: ProjectExecutionRecord, action_id: str
    ) -> builtins.list[str]:
        from noyra.core.actions import ActionLedger

        row = connection.execute(
            "SELECT * FROM actions WHERE action_id = ?", (action_id,)
        ).fetchone()
        if row is None:
            raise ProjectExecutionValidationError("linked execution action is missing")
        ActionLedger._from_row(row)
        if (
            row["subject_id"] != execution.subject_id
            or row["project_id"] != execution.project_id
            or row["phase_id"] != execution.phase_id
            or row["status"] != "succeeded"
        ):
            raise ProjectExecutionValidationError(
                "linked execution action is not a succeeded owner"
            )
        return ["action_ownership", "action_status"]

    @staticmethod
    def _validated_model_call(connection: Any, call_id: str, subject_id: str) -> Any:
        row = connection.execute(
            "SELECT * FROM model_calls WHERE call_id = ?", (call_id,)
        ).fetchone()
        if row is None:
            raise ProjectExecutionValidationError("linked model call is missing")
        record = ModelLedger._call_from_row(row)
        if record.subject_id != subject_id or record.status != "succeeded":
            raise ProjectExecutionValidationError("linked model call is not a succeeded owner")
        return row

    def _validate_research_connection(
        self,
        connection: Any,
        execution: ProjectExecutionRecord,
        evidence: dict[str, Any],
        *,
        research_id: str | None,
        model_call_id: str | None,
    ) -> builtins.list[str]:
        if research_id is None or model_call_id is None:
            raise ProjectExecutionValidationError("research output lacks durable source links")
        row = connection.execute(
            "SELECT * FROM research_search_runs WHERE research_id = ?", (research_id,)
        ).fetchone()
        if row is None:
            raise ProjectExecutionValidationError("linked research run is missing")
        record = AutonomousResearch._from_row(row)
        accepted = durable_json(
            row["accepted_source_ids_json"], "project research accepted sources", research_id
        )
        rounds = durable_json(row["rounds_json"], "project research rounds", research_id)
        plan = durable_json(row["plan_json"], "project research plan", research_id)
        if (
            not isinstance(accepted, list)
            or not accepted
            or not all(isinstance(item, str) and item for item in accepted)
            or len(accepted) != len(set(accepted))
            or not isinstance(rounds, list)
            or not isinstance(plan, dict)
        ):
            raise ProjectExecutionValidationError(
                "research output evidence is structurally invalid"
            )
        if content_hash(plan) != row["plan_hash"]:
            raise IntegrityError("linked research plan hash mismatch")
        try:
            round_count = sum(int(item["result_count"]) for item in rounds)
        except (KeyError, TypeError, ValueError) as error:
            raise ProjectExecutionValidationError("research round evidence is invalid") from error
        if round_count != record.result_count:
            raise ProjectExecutionValidationError("research result count does not reconcile")
        expected_state = AutonomousResearch._state_hash(
            row["planner_call_id"],
            row["goal_id"],
            row["status"],
            row["initial_method"],
            row["final_method"],
            row["provider_config_id"],
            row["query_hash"],
            tuple(accepted),
            tuple(rounds),
            row["plan_hash"],
            row["created_at"],
        )
        if row["state_hash"] != expected_state:
            raise IntegrityError("linked research state hash mismatch")
        if (
            record.subject_id != execution.subject_id
            or record.project_id != execution.project_id
            or record.phase_id != execution.phase_id
            or record.status != "accepted"
            or record.result_count < 1
            or row["planner_call_id"] != model_call_id
        ):
            raise ProjectExecutionValidationError("research output source binding is invalid")
        model_call = self._validated_model_call(connection, model_call_id, execution.subject_id)
        if model_call["call_id"] != row["planner_call_id"]:
            raise ProjectExecutionValidationError("research planner call binding is invalid")
        placeholders = ",".join("?" for _ in accepted)
        source_rows = connection.execute(
            f"SELECT source_id FROM world_sources WHERE subject_id = ? "
            f"AND source_id IN ({placeholders})",
            (execution.subject_id, *accepted),
        ).fetchall()
        if {item["source_id"] for item in source_rows} != set(accepted):
            raise ProjectExecutionValidationError("research accepted sources are unavailable")
        expected_evidence = {
            "research_id": research_id,
            "status": "accepted",
            "result_count": record.result_count,
            "accepted_source_ids": accepted,
        }
        if evidence != expected_evidence:
            raise ProjectExecutionValidationError("research acceptance evidence was forged")
        return ["research_state", "research_sources", "research_model_call"]

    def _validate_software_connection(
        self,
        connection: Any,
        execution: ProjectExecutionRecord,
        evidence: dict[str, Any],
        *,
        model_call_id: str | None,
        artifact_path: str,
    ) -> builtins.list[str]:
        if model_call_id is None:
            raise ProjectExecutionValidationError("software output lacks its model-call source")
        if set(evidence) != {
            "summary",
            "files",
            "model_validation_claims",
            "sandbox",
            "build",
            "tests",
            "publication",
        }:
            raise ProjectExecutionValidationError("software prototype evidence schema is invalid")
        call = self._validated_model_call(connection, model_call_id, execution.subject_id)
        expected_purpose = (
            f"autonomous_project_software:{execution.project_id}:{execution.phase_id}"
        )
        if call["purpose"] != expected_purpose:
            raise ProjectExecutionValidationError("software model call purpose is invalid")
        _require_substantive_text(evidence.get("summary"), "software prototype summary")
        files = evidence.get("files")
        if not isinstance(files, list) or not 1 <= len(files) <= 20:
            raise ProjectExecutionValidationError("software prototype file manifest is invalid")
        declared = evidence.get("model_validation_claims")
        if (
            not isinstance(declared, list)
            or not declared
            or not all(isinstance(item, str) and item.strip() for item in declared)
        ):
            raise ProjectExecutionValidationError("software model claims are invalid")
        if evidence.get("sandbox") != "text-artifact-only; no host execution":
            raise ProjectExecutionValidationError("software sandbox declaration is invalid")
        if not isinstance(evidence.get("build"), dict) or not isinstance(
            evidence.get("tests"), dict
        ):
            raise ProjectExecutionValidationError(
                "software output lacks measured build and test evidence"
            )
        publication = evidence.get("publication")
        if not isinstance(publication, dict) or set(publication) != {
            "format",
            "generation_id",
            "generation_root",
            "manifest_path",
        }:
            raise ProjectExecutionValidationError("software publication metadata is invalid")
        if (
            publication["format"] != _PrototypePublication.FORMAT
            or publication["generation_id"] != execution.execution_id
            or publication["manifest_path"] != artifact_path
        ):
            raise ProjectExecutionValidationError("software publication identity is invalid")
        workspace = ProjectWorkspace(
            self.workspace_root,
            self.storage_key,
            quota_bytes=1,
            legacy_subject_id=execution.subject_id,
            legacy_migration_allowed=self.legacy_storage_path_is_unambiguous,
        )
        generation_relative = (
            f"phases/{execution.phase_id}/prototype-generations/{execution.execution_id}"
        )
        manifest_relative = (
            f"phases/{execution.phase_id}/prototype-manifests/{execution.execution_id}.json"
        )
        expected_generation_root = workspace.path(execution.project_id, generation_relative)
        expected_manifest_path = workspace.path(execution.project_id, manifest_relative)
        legacy_generation_root = workspace.legacy_path(execution.project_id, generation_relative)
        legacy_manifest_path = workspace.legacy_path(execution.project_id, manifest_relative)
        valid_generation_roots = {str(expected_generation_root)}
        valid_manifest_paths = {str(expected_manifest_path)}
        if legacy_generation_root is not None:
            valid_generation_roots.add(str(legacy_generation_root))
        if legacy_manifest_path is not None:
            valid_manifest_paths.add(str(legacy_manifest_path))
        if (
            publication["generation_root"] not in valid_generation_roots
            or publication["manifest_path"] not in valid_manifest_paths
        ):
            raise ProjectExecutionValidationError("software publication path is invalid")
        seen: set[str] = set()
        tree: dict[str, bytes] = {}
        checks = ["software_model_call", "atomic_publication", "handle_relative_paths"]
        for item in files:
            if not isinstance(item, dict) or set(item) != {"path", "bytes", "sha256"}:
                raise ProjectExecutionValidationError("software file manifest entry is invalid")
            path = item["path"]
            byte_count = item["bytes"]
            digest = item["sha256"]
            if (
                not isinstance(path, str)
                or path in seen
                or not isinstance(byte_count, int)
                or isinstance(byte_count, bool)
                or byte_count < 1
                or not isinstance(digest, str)
            ):
                raise ProjectExecutionValidationError("software file manifest values are invalid")
            try:
                relative = workspace.relative_path(execution.project_id, path)
            except ProjectExecutionError as error:
                raise ProjectExecutionValidationError(
                    "software file escaped its immutable generation"
                ) from error
            if not relative.startswith(generation_relative + "/"):
                raise ProjectExecutionValidationError(
                    "software file escaped its immutable generation"
                )
            seen.add(path)
            payload = self._read_owned_artifact(execution, path, digest)
            logical_path = relative.removeprefix(generation_relative + "/")
            tree[logical_path] = payload
            if len(payload) != byte_count:
                raise ProjectExecutionValidationError("software file byte count mismatch")
            check = _validate_prototype_file(path, payload)
            checks.append(f"file:{Path(path).name}:{check}")
        actual = {
            str(workspace.path(execution.project_id, relative))
            for relative in workspace.files(execution.project_id, generation_relative)
        }
        if actual != seen:
            raise ProjectExecutionValidationError("software prototype contains unmanifested files")
        expected_build = {"status": "passed", "checks": sorted(tree)}
        if evidence["build"] != expected_build:
            raise ProjectExecutionValidationError("software build evidence was forged")
        expected_tests = _run_prototype_tests(tree)
        tested_paths = {str(item["path"]) for item in expected_tests["checks"]}
        required_paths = set(tree) - {"prototype-tests.json"}
        if not required_paths.issubset(tested_paths):
            raise ProjectExecutionValidationError(
                "prototype test manifest must exercise every output file"
            )
        if evidence["tests"] != expected_tests:
            raise ProjectExecutionValidationError("software test evidence was forged")
        checks.extend(["prototype_build", "prototype_tests"])
        return checks

    @staticmethod
    def _validate_prediction_connection(
        connection: Any,
        execution: ProjectExecutionRecord,
        evidence: dict[str, Any],
        phase: Any,
    ) -> builtins.list[str]:
        prediction_id = evidence.get("prediction_id")
        resolution_evidence = evidence.get("resolution_evidence_observation_ids")
        if not isinstance(prediction_id, str) or not prediction_id:
            raise ProjectExecutionValidationError("prediction evidence lacks an id")
        row = connection.execute(
            "SELECT * FROM predictions WHERE prediction_id = ?", (prediction_id,)
        ).fetchone()
        if row is None:
            raise ProjectExecutionValidationError("prediction output is missing")
        prediction = PredictionStore._from_row(row)
        review = connection.execute(
            "SELECT * FROM prediction_reviews WHERE prediction_id = ? "
            "ORDER BY created_at, review_id LIMIT 1",
            (prediction_id,),
        ).fetchone()
        if review is None:
            raise ProjectExecutionValidationError("prediction creation review is missing")
        review_evidence = durable_json(
            review["evidence_observation_ids_json"], "prediction review evidence", prediction_id
        )
        if (
            not isinstance(review_evidence, list)
            or not review_evidence
            or len(review_evidence) > 64
            or not all(isinstance(item, str) and item for item in review_evidence)
            or len(review_evidence) != len(set(review_evidence))
            or review["resulting_status"] != "open"
            or review["outcome"] is not None
            or review["brier_score"] is not None
        ):
            raise ProjectExecutionValidationError("prediction evidence observations are missing")
        expected_review_hash = PredictionStore._review_hash(
            None,
            tuple(review_evidence),
            review["rationale"],
            review["resulting_status"],
            None,
        )
        if review["state_hash"] != expected_review_hash:
            raise IntegrityError("prediction creation review hash mismatch")
        resolution_review = connection.execute(
            "SELECT * FROM prediction_reviews WHERE prediction_id = ? "
            "AND resulting_status = 'resolved' ORDER BY created_at, review_id LIMIT 1",
            (prediction_id,),
        ).fetchone()
        if resolution_review is None:
            raise ProjectExecutionValidationError("prediction resolution review is missing")
        resolution_ids = durable_json(
            resolution_review["evidence_observation_ids_json"],
            "prediction resolution evidence",
            prediction_id,
        )
        if (
            not isinstance(resolution_ids, list)
            or not resolution_ids
            or len(resolution_ids) > 64
            or not all(isinstance(item, str) and item for item in resolution_ids)
            or len(resolution_ids) != len(set(resolution_ids))
            or resolution_ids != resolution_evidence
        ):
            raise ProjectExecutionValidationError("prediction resolution evidence diverges")
        expected_resolution_hash = PredictionStore._review_hash(
            bool(prediction.outcome),
            tuple(resolution_ids),
            resolution_review["rationale"],
            "resolved",
            prediction.brier_score,
        )
        if resolution_review["state_hash"] != expected_resolution_hash:
            raise IntegrityError("prediction resolution review hash mismatch")
        latest_review = connection.execute(
            "SELECT review_id FROM prediction_reviews WHERE prediction_id = ? "
            "ORDER BY created_at DESC, review_id DESC LIMIT 1",
            (prediction_id,),
        ).fetchone()
        if (
            latest_review is None
            or latest_review["review_id"] != resolution_review["review_id"]
            or resolution_review["resulting_status"] != "resolved"
            or resolution_review["outcome"] != int(bool(prediction.outcome))
            or resolution_review["brier_score"] != prediction.brier_score
            or resolution_review["created_at"] != prediction.resolved_at
        ):
            raise ProjectExecutionValidationError(
                "prediction resolution review is not authoritative"
            )
        if prediction.resolved_at is None or PredictionStore._parse_time(
            prediction.resolved_at
        ) < PredictionStore._parse_time(prediction.target_at):
            raise ProjectExecutionValidationError("prediction resolved before its target")
        placeholders = ",".join("?" for _ in review_evidence)
        observations = connection.execute(
            f"SELECT observation_id, status FROM observations WHERE subject_id = ? "
            f"AND observation_id IN ({placeholders})",
            (execution.subject_id, *review_evidence),
        ).fetchall()
        if {item["observation_id"] for item in observations} != set(review_evidence) or any(
            item["status"] != "analyzed" for item in observations
        ):
            raise ProjectExecutionValidationError("prediction observations are not analyzed owners")
        resolution_observations = connection.execute(
            f"SELECT observation_id, status FROM observations WHERE subject_id = ? "
            f"AND observation_id IN ({','.join('?' for _ in resolution_ids)})",
            (execution.subject_id, *resolution_ids),
        ).fetchall()
        if {item["observation_id"] for item in resolution_observations} != set(
            resolution_ids
        ) or any(item["status"] != "analyzed" for item in resolution_observations):
            raise ProjectExecutionValidationError(
                "prediction resolution observations are not analyzed owners"
            )
        expected_criteria = "; ".join(
            durable_json(
                phase["acceptance_criteria_json"],
                "prediction acceptance criteria",
                execution.phase_id,
            )
        )[:10_000]
        calibration_ids = evidence.get("calibration_prediction_ids")
        if (
            not isinstance(calibration_ids, list)
            or len(calibration_ids) < 3
            or not all(isinstance(item, str) and item for item in calibration_ids)
            or len(calibration_ids) != len(set(calibration_ids))
        ):
            raise ProjectExecutionValidationError("prediction calibration history is insufficient")
        placeholders = ",".join("?" for _ in calibration_ids)
        calibration_rows = connection.execute(
            f"SELECT prediction_id, outcome, resolved_at FROM predictions "
            f"WHERE subject_id = ? AND prediction_id IN ({placeholders}) "
            "AND status = 'resolved' AND outcome IS NOT NULL",
            (execution.subject_id, *calibration_ids),
        ).fetchall()
        by_calibration_id = {str(item["prediction_id"]): item for item in calibration_rows}
        if set(by_calibration_id) != set(calibration_ids) or any(
            item["resolved_at"] is None or str(item["resolved_at"]) > prediction.created_at
            for item in calibration_rows
        ):
            raise ProjectExecutionValidationError("prediction calibration sources are invalid")
        calibration_successes = sum(
            int(by_calibration_id[prediction_id]["outcome"]) for prediction_id in calibration_ids
        )
        calibrated_probability = round((calibration_successes + 1) / (len(calibration_rows) + 2), 6)
        probability_value = evidence.get("probability")
        if not isinstance(probability_value, (int, float)) or isinstance(probability_value, bool):
            raise ProjectExecutionValidationError("prediction probability evidence is invalid")
        if (
            prediction.subject_id != execution.subject_id
            or prediction.statement != phase["objective"]
            or prediction.resolution_criteria != expected_criteria
            or prediction.status != "resolved"
            or prediction.outcome is None
            or prediction.brier_score is None
            or prediction.probability != float(probability_value)
            or evidence.get("calibration_prediction_ids") != calibration_ids
            or evidence.get("calibration_successes") != calibration_successes
            or evidence.get("calibration_count") != len(calibration_rows)
            or evidence.get("probability") != calibrated_probability
            or not isinstance(resolution_evidence, list)
            or not resolution_evidence
            or evidence.get("outcome") != bool(prediction.outcome)
            or evidence.get("brier_score") != prediction.brier_score
        ):
            raise ProjectExecutionValidationError("prediction output does not match the phase")
        expected_evidence = {
            "prediction_id": prediction.prediction_id,
            "target_at": prediction.target_at,
            "probability": prediction.probability,
            "evidence_observation_ids": review_evidence,
            "calibration_prediction_ids": calibration_ids,
            "calibration_successes": calibration_successes,
            "calibration_count": len(calibration_rows),
            "outcome": bool(prediction.outcome),
            "brier_score": prediction.brier_score,
            "resolution_evidence_observation_ids": resolution_evidence,
            "resolved_at": prediction.resolved_at,
        }
        if evidence != expected_evidence:
            raise ProjectExecutionValidationError("prediction acceptance evidence was forged")
        return ["prediction_state", "prediction_review", "prediction_observations"]

    @staticmethod
    def _validate_self_experiment_connection(
        connection: Any,
        execution: ProjectExecutionRecord,
        evidence: dict[str, Any],
        phase: Any,
    ) -> builtins.list[str]:
        required = {
            "hypothesis",
            "baseline_event_id",
            "follow_up_event_id",
            "baseline",
            "follow_up",
            "delta",
        }
        if set(evidence) != required:
            raise ProjectExecutionValidationError(
                "self experiment requires baseline and independent follow-up evidence"
            )
        _require_substantive_text(evidence["hypothesis"], "self experiment hypothesis")
        if evidence["hypothesis"] != phase["objective"]:
            raise ProjectExecutionValidationError(
                "self experiment hypothesis does not match the phase"
            )
        baseline = evidence["baseline"]
        follow_up = evidence["follow_up"]
        delta = evidence["delta"]
        if not all(isinstance(item, dict) and item for item in (baseline, follow_up, delta)):
            raise ProjectExecutionValidationError("self experiment measurements are invalid")
        if set(baseline) != set(follow_up) or set(delta) != set(baseline):
            raise ProjectExecutionValidationError("self experiment metric sets do not reconcile")
        for key in baseline:
            values = baseline[key], follow_up[key], delta[key]
            if any(not isinstance(value, int) or isinstance(value, bool) for value in values):
                raise ProjectExecutionValidationError("self experiment metrics must be integers")
            if follow_up[key] - baseline[key] != delta[key]:
                raise ProjectExecutionValidationError("self experiment delta is incorrect")
        events = connection.execute(
            "SELECT event_id, subject_id, event_type, payload_json, causal_parent_ids_json, "
            "occurred_at FROM events "
            "WHERE event_id IN (?, ?)",
            (evidence["baseline_event_id"], evidence["follow_up_event_id"]),
        ).fetchall()
        if len(events) != 2 or any(item["subject_id"] != execution.subject_id for item in events):
            raise ProjectExecutionValidationError("self experiment events are missing or foreign")
        by_id = {item["event_id"]: item for item in events}
        baseline_event = by_id.get(evidence["baseline_event_id"])
        follow_up_event = by_id.get(evidence["follow_up_event_id"])
        if (
            baseline_event is None
            or follow_up_event is None
            or baseline_event["event_type"] != "project_self_experiment_baseline"
            or follow_up_event["event_type"] != "project_self_experiment_follow_up"
            or follow_up_event["occurred_at"] <= baseline_event["occurred_at"]
        ):
            raise ProjectExecutionValidationError("self experiment event lifecycle is invalid")
        try:
            baseline_payload = strict_json_loads(baseline_event["payload_json"])
            follow_up_payload = strict_json_loads(follow_up_event["payload_json"])
        except (TypeError, ValueError) as error:
            raise ProjectExecutionValidationError(
                "self experiment event payloads are invalid"
            ) from error
        if (
            not isinstance(baseline_payload, dict)
            or baseline_payload.get("hypothesis") != phase["objective"]
            or baseline_payload.get("project_id") != execution.project_id
            or baseline_payload.get("phase_id") != execution.phase_id
            or baseline_payload.get("baseline") != baseline
            or not isinstance(follow_up_payload, dict)
            or follow_up_payload.get("hypothesis") != phase["objective"]
            or follow_up_payload.get("project_id") != execution.project_id
            or follow_up_payload.get("phase_id") != execution.phase_id
            or follow_up_payload.get("follow_up") != follow_up
        ):
            raise ProjectExecutionValidationError("self experiment event measurements diverge")
        try:
            baseline_parents = strict_json_loads(baseline_event["causal_parent_ids_json"])
            follow_up_parents = strict_json_loads(follow_up_event["causal_parent_ids_json"])
        except (TypeError, ValueError) as error:
            raise ProjectExecutionValidationError(
                "self experiment event causal links are invalid"
            ) from error
        if baseline_parents != [] or follow_up_parents != [evidence["baseline_event_id"]]:
            raise ProjectExecutionValidationError("self experiment event causal links diverge")
        return ["self_experiment_baseline", "self_experiment_follow_up", "self_experiment_delta"]

    @staticmethod
    def _validate_collaboration_connection(
        connection: Any,
        execution: ProjectExecutionRecord,
        evidence: dict[str, Any],
        phase: Any,
    ) -> builtins.list[str]:
        interaction_id = evidence.get("interaction_id")
        if not isinstance(interaction_id, str) or not interaction_id:
            raise ProjectExecutionValidationError("collaboration evidence lacks an interaction")
        row = connection.execute(
            "SELECT * FROM interactions WHERE interaction_id = ?", (interaction_id,)
        ).fetchone()
        if row is None:
            raise ProjectExecutionValidationError("collaboration interaction is missing")
        interaction = InteractionStore._from_row(row)
        criteria = durable_json(
            phase["acceptance_criteria_json"],
            "collaboration acceptance criteria",
            execution.phase_id,
        )
        expected_content = f"{phase['objective']}\n\nAcceptance: {'; '.join(criteria)}"
        if (
            interaction.subject_id != execution.subject_id
            or interaction.direction != "outgoing"
            or interaction.kind != "help_request"
            or interaction.status != "sent"
            or interaction.content != expected_content
        ):
            raise ProjectExecutionValidationError(
                "collaboration request is not the expected sent intent"
            )
        if evidence != {"interaction_id": interaction.interaction_id, "status": "sent"}:
            raise ProjectExecutionValidationError("collaboration acceptance evidence was forged")
        return ["collaboration_state", "collaboration_content", "collaboration_delivery_intent"]

    @staticmethod
    def _revision(
        connection: Any,
        execution_id: str,
        status: str,
        result_hash: str | None,
        reason: str,
        now: str,
    ) -> None:
        payload = {
            "execution_id": execution_id,
            "status": status,
            "result_hash": result_hash,
            "reason": reason,
            "created_at": now,
        }
        connection.execute(
            """INSERT INTO autonomous_project_execution_revisions(
                revision_id, execution_id, status, result_hash, reason, state_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                new_id("pexec-rev"),
                execution_id,
                status,
                result_hash,
                reason,
                content_hash(payload),
                now,
            ),
        )

    @staticmethod
    def _from_row(row: Any) -> ProjectExecutionRecord:
        execution_id = row["execution_id"]
        with durable_boundary("project execution", execution_id):
            acceptance = durable_json(
                row["acceptance_json"], "project execution acceptance", execution_id
            )
            if not isinstance(acceptance, dict):
                raise IntegrityError("project execution acceptance is not an object")
            return ProjectExecutionRecord(
                execution_id=execution_id,
                subject_id=row["subject_id"],
                project_id=row["project_id"],
                phase_id=row["phase_id"],
                execution_key=row["execution_key"],
                execution_type=row["execution_type"],
                workflow=row["workflow"],
                status=row["status"],
                research_id=row["research_id"],
                action_id=row["action_id"],
                model_call_id=row["model_call_id"],
                artifact_path=row["artifact_path"],
                artifact_hash=row["artifact_hash"],
                result_hash=row["result_hash"],
                acceptance=acceptance,
                error_code=row["error_code"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                completed_at=row["completed_at"],
            )


class _WindowsWorkspaceAPI:
    """Handle-relative Windows I/O that refuses every reparse point."""

    FILE_ATTRIBUTE_DIRECTORY = 0x10
    FILE_ATTRIBUTE_NORMAL = 0x80
    FILE_ATTRIBUTE_REPARSE_POINT = 0x400
    FILE_LIST_DIRECTORY = 0x0001
    FILE_READ_DATA = 0x0001
    FILE_WRITE_DATA = 0x0002
    FILE_TRAVERSE = 0x0020
    FILE_READ_ATTRIBUTES = 0x0080
    DELETE = 0x00010000
    SYNCHRONIZE = 0x00100000
    FILE_SHARE_ALL = 0x00000007
    FILE_OPEN = 0x00000001
    FILE_CREATE = 0x00000002
    FILE_OPEN_IF = 0x00000003
    FILE_DIRECTORY_FILE = 0x00000001
    FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
    FILE_NON_DIRECTORY_FILE = 0x00000040
    FILE_OPEN_FOR_BACKUP_INTENT = 0x00004000
    FILE_OPEN_REPARSE_POINT = 0x00200000
    OBJ_CASE_INSENSITIVE = 0x00000040
    OBJ_DONT_REPARSE = 0x00001000
    FILE_ATTRIBUTE_TAG_INFO_CLASS = 9
    FILE_ID_BOTH_DIRECTORY_INFO_CLASS = 10
    FILE_RENAME_INFORMATION_CLASS = 10
    FILE_DISPOSITION_INFORMATION_CLASS = 13
    ERROR_NO_MORE_FILES = 18

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        ctypes_api: Any = ctypes
        self.ctypes = ctypes_api
        self.wintypes = wintypes
        self.kernel32 = ctypes_api.WinDLL("kernel32", use_last_error=True)
        self.ntdll = ctypes_api.WinDLL("ntdll")

        class UnicodeString(ctypes.Structure):
            _fields_ = [
                ("Length", wintypes.USHORT),
                ("MaximumLength", wintypes.USHORT),
                ("Buffer", wintypes.LPWSTR),
            ]

        class ObjectAttributes(ctypes.Structure):
            _fields_ = [
                ("Length", wintypes.ULONG),
                ("RootDirectory", wintypes.HANDLE),
                ("ObjectName", ctypes.POINTER(UnicodeString)),
                ("Attributes", wintypes.ULONG),
                ("SecurityDescriptor", ctypes.c_void_p),
                ("SecurityQualityOfService", ctypes.c_void_p),
            ]

        class IoStatusUnion(ctypes.Union):
            _fields_: ClassVar[list[tuple[str, Any]]] = [
                ("Status", ctypes.c_long),
                ("Pointer", ctypes.c_void_p),
            ]

        class IoStatusBlock(ctypes.Structure):
            _fields_ = [("value", IoStatusUnion), ("Information", ctypes.c_size_t)]

        class FileAttributeTagInfo(ctypes.Structure):
            _fields_ = [
                ("FileAttributes", wintypes.DWORD),
                ("ReparseTag", wintypes.DWORD),
            ]

        class FileIdBothDirectoryInfo(ctypes.Structure):
            _fields_ = [
                ("NextEntryOffset", wintypes.DWORD),
                ("FileIndex", wintypes.DWORD),
                ("CreationTime", ctypes.c_longlong),
                ("LastAccessTime", ctypes.c_longlong),
                ("LastWriteTime", ctypes.c_longlong),
                ("ChangeTime", ctypes.c_longlong),
                ("EndOfFile", ctypes.c_longlong),
                ("AllocationSize", ctypes.c_longlong),
                ("FileAttributes", wintypes.DWORD),
                ("FileNameLength", wintypes.DWORD),
                ("EaSize", wintypes.DWORD),
                ("ShortNameLength", ctypes.c_byte),
                ("ShortName", wintypes.WCHAR * 12),
                ("FileId", ctypes.c_longlong),
                ("FileName", wintypes.WCHAR * 1),
            ]

        class FileRenameInformation(ctypes.Structure):
            _fields_ = [
                ("ReplaceIfExists", ctypes.c_ubyte),
                ("RootDirectory", wintypes.HANDLE),
                ("FileNameLength", wintypes.DWORD),
                ("FileName", wintypes.WCHAR * 1),
            ]

        class FileDispositionInformation(ctypes.Structure):
            _fields_ = [("DeleteFile", ctypes.c_ubyte)]

        self.UnicodeString = UnicodeString
        self.ObjectAttributes = ObjectAttributes
        self.IoStatusBlock = IoStatusBlock
        self.FileAttributeTagInfo = FileAttributeTagInfo
        self.FileIdBothDirectoryInfo = FileIdBothDirectoryInfo
        self.FileRenameInformation = FileRenameInformation
        self.FileDispositionInformation = FileDispositionInformation

        self.create_file = self.kernel32.CreateFileW
        self.create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        self.create_file.restype = wintypes.HANDLE
        self.close_handle = self.kernel32.CloseHandle
        self.close_handle.argtypes = [wintypes.HANDLE]
        self.close_handle.restype = wintypes.BOOL
        self.get_information = self.kernel32.GetFileInformationByHandleEx
        self.get_information.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        self.get_information.restype = wintypes.BOOL
        self.get_size = self.kernel32.GetFileSizeEx
        self.get_size.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_longlong)]
        self.get_size.restype = wintypes.BOOL
        self.flush_buffers = self.kernel32.FlushFileBuffers
        self.flush_buffers.argtypes = [wintypes.HANDLE]
        self.flush_buffers.restype = wintypes.BOOL
        self.final_path_name = self.kernel32.GetFinalPathNameByHandleW
        self.final_path_name.argtypes = [
            wintypes.HANDLE,
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        self.final_path_name.restype = wintypes.DWORD
        self.nt_create_file = self.ntdll.NtCreateFile
        self.nt_create_file.argtypes = [
            ctypes.POINTER(wintypes.HANDLE),
            wintypes.DWORD,
            ctypes.POINTER(ObjectAttributes),
            ctypes.POINTER(IoStatusBlock),
            ctypes.c_void_p,
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.ULONG,
            ctypes.c_void_p,
            wintypes.ULONG,
        ]
        self.nt_create_file.restype = ctypes.c_long
        self.nt_set_information = self.ntdll.NtSetInformationFile
        self.nt_set_information.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(IoStatusBlock),
            ctypes.c_void_p,
            wintypes.ULONG,
            ctypes.c_int,
        ]
        self.nt_set_information.restype = ctypes.c_long
        self.ntstatus_to_error = self.ntdll.RtlNtStatusToDosError
        self.ntstatus_to_error.argtypes = [ctypes.c_long]
        self.ntstatus_to_error.restype = wintypes.ULONG

    def _raise_status(self, status: int, context: str) -> None:
        code = int(self.ntstatus_to_error(status))
        raise OSError(code, f"{context}: {self.ctypes.FormatError(code).strip()}")

    def _check_attributes(self, handle: int, *, directory: bool | None) -> int:
        info = self.FileAttributeTagInfo()
        if not self.get_information(
            handle,
            self.FILE_ATTRIBUTE_TAG_INFO_CLASS,
            self.ctypes.byref(info),
            self.ctypes.sizeof(info),
        ):
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        attributes = int(info.FileAttributes)
        if attributes & self.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ProjectExecutionError("artifact path contains a Windows reparse point")
        is_directory = bool(attributes & self.FILE_ATTRIBUTE_DIRECTORY)
        if directory is not None and is_directory != directory:
            raise ProjectExecutionError("artifact path has an unexpected file type")
        return attributes

    def open_root(self, path: Path) -> int:
        handle = self.create_file(
            str(path),
            self.FILE_LIST_DIRECTORY
            | self.FILE_TRAVERSE
            | self.FILE_READ_ATTRIBUTES
            | self.SYNCHRONIZE,
            self.FILE_SHARE_ALL,
            None,
            3,
            0x02000000 | self.FILE_OPEN_REPARSE_POINT,
            None,
        )
        if handle == self.ctypes.c_void_p(-1).value:
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        try:
            self._check_attributes(int(handle), directory=True)
        except Exception:
            self.close(int(handle))
            raise
        return int(handle)

    def open_relative(
        self,
        parent: int,
        name: str,
        *,
        directory: bool | None,
        create: bool = False,
        write: bool = False,
        delete: bool = False,
    ) -> int:
        encoded = name.encode("utf-16-le")
        buffer = self.ctypes.create_unicode_buffer(name)
        unicode_name = self.UnicodeString(
            len(encoded),
            len(encoded),
            self.ctypes.cast(buffer, self.wintypes.LPWSTR),
        )
        attributes = self.ObjectAttributes(
            self.ctypes.sizeof(self.ObjectAttributes),
            parent,
            self.ctypes.pointer(unicode_name),
            self.OBJ_CASE_INSENSITIVE | self.OBJ_DONT_REPARSE,
            None,
            None,
        )
        status_block = self.IoStatusBlock()
        handle = self.wintypes.HANDLE()
        desired_access = self.FILE_READ_ATTRIBUTES | self.SYNCHRONIZE
        if directory is not False:
            desired_access |= self.FILE_LIST_DIRECTORY | self.FILE_TRAVERSE
        if directory is False:
            desired_access |= self.FILE_WRITE_DATA if write else self.FILE_READ_DATA
        if delete:
            desired_access |= self.DELETE
        disposition = self.FILE_OPEN_IF if directory is True and create else self.FILE_OPEN
        if directory is False and create:
            disposition = self.FILE_CREATE
        options = self.FILE_SYNCHRONOUS_IO_NONALERT | self.FILE_OPEN_REPARSE_POINT
        file_attributes = self.FILE_ATTRIBUTE_NORMAL
        if directory is True:
            options |= self.FILE_DIRECTORY_FILE | self.FILE_OPEN_FOR_BACKUP_INTENT
            file_attributes = self.FILE_ATTRIBUTE_DIRECTORY
        elif directory is False:
            options |= self.FILE_NON_DIRECTORY_FILE
        status = int(
            self.nt_create_file(
                self.ctypes.byref(handle),
                desired_access,
                self.ctypes.byref(attributes),
                self.ctypes.byref(status_block),
                None,
                file_attributes,
                self.FILE_SHARE_ALL,
                disposition,
                options,
                None,
                0,
            )
        )
        if status < 0:
            self._raise_status(status, f"cannot open workspace component {name!r}")
        raw_handle = handle.value
        if raw_handle is None:
            raise OSError("Windows returned an empty workspace handle")
        try:
            self._check_attributes(int(raw_handle), directory=directory)
        except Exception:
            self.close(int(raw_handle))
            raise
        return int(raw_handle)

    def entries(self, handle: int) -> list[str]:
        names: list[str] = []
        while True:
            buffer = self.ctypes.create_string_buffer(64 * 1024)
            self.ctypes.set_last_error(0)
            ok = self.get_information(
                handle,
                self.FILE_ID_BOTH_DIRECTORY_INFO_CLASS,
                buffer,
                len(buffer),
            )
            if not ok:
                error = self.ctypes.get_last_error()
                if error == self.ERROR_NO_MORE_FILES:
                    break
                raise self.ctypes.WinError(error)
            offset = 0
            while True:
                item = self.ctypes.cast(
                    self.ctypes.addressof(buffer) + offset,
                    self.ctypes.POINTER(self.FileIdBothDirectoryInfo),
                ).contents
                raw_name = self.ctypes.string_at(
                    self.ctypes.addressof(buffer)
                    + offset
                    + self.FileIdBothDirectoryInfo.FileName.offset,
                    item.FileNameLength,
                )
                name = raw_name.decode("utf-16-le")
                if name not in {".", ".."}:
                    names.append(name)
                    if len(names) > 100_000:
                        raise ProjectExecutionError("project workspace file count is unbounded")
                if not item.NextEntryOffset:
                    break
                offset += int(item.NextEntryOffset)
        return names

    def size(self, handle: int) -> int:
        value = self.ctypes.c_longlong()
        if not self.get_size(handle, self.ctypes.byref(value)):
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        return int(value.value)

    def assert_path(self, handle: int, expected: Path) -> None:
        length = int(self.final_path_name(handle, None, 0, 0))
        if length < 1:
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        buffer = self.ctypes.create_unicode_buffer(length + 1)
        written = int(self.final_path_name(handle, buffer, len(buffer), 0))
        if written < 1 or written > length:
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        actual = buffer.value
        if actual.startswith("\\\\?\\UNC\\"):
            actual = "\\\\" + actual[8:]
        elif actual.startswith("\\\\?\\"):
            actual = actual[4:]
        if os.path.normcase(os.path.abspath(actual)) != os.path.normcase(os.path.abspath(expected)):
            raise ProjectExecutionError("artifact directory was replaced during access")

    def rename(self, handle: int, destination_parent: int, name: str, *, replace: bool) -> None:
        encoded = name.encode("utf-16-le")
        size = self.FileRenameInformation.FileName.offset + len(encoded)
        buffer = self.ctypes.create_string_buffer(size)
        info = self.ctypes.cast(buffer, self.ctypes.POINTER(self.FileRenameInformation)).contents
        info.ReplaceIfExists = int(replace)
        info.RootDirectory = destination_parent
        info.FileNameLength = len(encoded)
        self.ctypes.memmove(
            self.ctypes.addressof(buffer) + self.FileRenameInformation.FileName.offset,
            encoded,
            len(encoded),
        )
        status_block = self.IoStatusBlock()
        status = int(
            self.nt_set_information(
                handle,
                self.ctypes.byref(status_block),
                buffer,
                size,
                self.FILE_RENAME_INFORMATION_CLASS,
            )
        )
        if status < 0:
            self._raise_status(status, f"cannot publish workspace entry {name!r}")

    def delete(self, handle: int) -> None:
        info = self.FileDispositionInformation(1)
        status_block = self.IoStatusBlock()
        status = int(
            self.nt_set_information(
                handle,
                self.ctypes.byref(status_block),
                self.ctypes.byref(info),
                self.ctypes.sizeof(info),
                self.FILE_DISPOSITION_INFORMATION_CLASS,
            )
        )
        if status < 0:
            self._raise_status(status, "cannot remove workspace entry")

    def flush(self, handle: int) -> None:
        self.flush_buffers(handle)

    def close(self, handle: int) -> None:
        if handle:
            self.close_handle(handle)


_WINDOWS_WORKSPACE_API: _WindowsWorkspaceAPI | None = None


def _windows_workspace_api() -> _WindowsWorkspaceAPI:
    global _WINDOWS_WORKSPACE_API
    if _WINDOWS_WORKSPACE_API is None:
        _WINDOWS_WORKSPACE_API = _WindowsWorkspaceAPI()
    return _WINDOWS_WORKSPACE_API


class ProjectWorkspace:
    """Confines project files with handle-relative, no-follow filesystem operations."""

    _locks_guard: ClassVar[threading.Lock] = threading.Lock()
    _locks: ClassVar[dict[str, threading.RLock]] = {}

    def __init__(
        self,
        root: Path | str,
        storage_key: str,
        *,
        quota_bytes: int,
        legacy_subject_id: str | None = None,
        legacy_migration_allowed: bool = True,
    ):
        if quota_bytes < 1:
            raise ValueError("project workspace quota must be positive")
        base_root = Path(root).expanduser().resolve()
        try:
            located = SubjectStorageDirectory.locate(
                base_root,
                storage_key,
                legacy_subject_id=legacy_subject_id,
                legacy_migration_allowed=legacy_migration_allowed,
                create=True,
            )
        except (IntegrityError, ValueError) as error:
            raise ProjectExecutionError("subject workspace binding is invalid") from error
        if located is None:
            raise ProjectExecutionError("subject workspace could not be created")
        self.root = located
        self.legacy_root = (
            None
            if legacy_subject_id is None
            else Path(os.path.abspath(base_root / legacy_subject_id))
        )
        self.quota_bytes = quota_bytes

    @staticmethod
    def _project_component(project_id: str) -> str:
        if (
            not project_id.strip()
            or project_id in {".", ".."}
            or any(char in project_id for char in "/\\\x00")
        ):
            raise ProjectExecutionError("project id is invalid for a workspace")
        return project_id

    @staticmethod
    def _relative_parts(relative_path: str, *, allow_empty: bool = False) -> tuple[str, ...]:
        if "\\" in relative_path or "\x00" in relative_path:
            raise ProjectExecutionError("artifact path escapes project workspace")
        path = PurePosixPath(relative_path)
        parts = tuple(path.parts)
        if (
            path.is_absolute()
            or any(part in {"", ".", ".."} for part in parts)
            or (not parts and not allow_empty)
        ):
            raise ProjectExecutionError("artifact path escapes project workspace")
        return parts

    def _lock(self, project_id: str) -> threading.RLock:
        key = os.path.normcase(str(self.root / self._project_component(project_id)))
        with self._locks_guard:
            return self._locks.setdefault(key, threading.RLock())

    @contextmanager
    def locked(self, project_id: str) -> Iterator[None]:
        lock = self._lock(project_id)
        with lock:
            yield

    def project_root(self, project_id: str) -> Path:
        project = self._project_component(project_id)
        with self.locked(project):
            self._ensure_directory_unlocked(project, ())
        return self.root / project

    def path(self, project_id: str, relative_path: str) -> Path:
        project = self._project_component(project_id)
        parts = self._relative_parts(relative_path)
        return self.root / project / Path(*parts)

    def legacy_path(self, project_id: str, relative_path: str) -> Path | None:
        if self.legacy_root is None:
            return None
        project = self._project_component(project_id)
        parts = self._relative_parts(relative_path)
        return self.legacy_root / project / Path(*parts)

    def relative_path(self, project_id: str, absolute_path: Path | str) -> str:
        project_root = self.root / self._project_component(project_id)
        candidate = Path(os.path.abspath(absolute_path))
        try:
            relative = candidate.relative_to(project_root)
        except ValueError:
            if self.legacy_root is None:
                raise ProjectExecutionError("artifact path escapes project workspace") from None
            legacy_project_root = self.legacy_root / self._project_component(project_id)
            try:
                relative = candidate.relative_to(legacy_project_root)
            except ValueError as error:
                raise ProjectExecutionError("artifact path escapes project workspace") from error
        return PurePosixPath(*relative.parts).as_posix()

    def ensure_directory(self, project_id: str, relative_path: str) -> Path:
        project = self._project_component(project_id)
        parts = self._relative_parts(relative_path, allow_empty=True)
        with self.locked(project):
            self._ensure_directory_unlocked(project, parts)
        return self.root / project / Path(*parts)

    def write(self, project_id: str, relative_path: str, payload: bytes) -> Path:
        project = self._project_component(project_id)
        parts = self._relative_parts(relative_path)
        with self.locked(project):
            files = self._files_unlocked(project, ())
            key = PurePosixPath(*parts).as_posix()
            existing = files.get(key, 0)
            if sum(files.values()) - existing + len(payload) > self.quota_bytes:
                raise ProjectExecutionError("project workspace quota exceeded")
            self._write_unlocked(project, parts, payload)
        return self.root / project / Path(*parts)

    def read(self, project_id: str, relative_path: str) -> bytes:
        project = self._project_component(project_id)
        parts = self._relative_parts(relative_path)
        with self.locked(project):
            try:
                return self._read_unlocked(project, parts)
            except OSError as error:
                raise ProjectExecutionError("artifact is unavailable or unsafe") from error

    def files(self, project_id: str, relative_path: str = "") -> dict[str, int]:
        project = self._project_component(project_id)
        parts = self._relative_parts(relative_path, allow_empty=True)
        with self.locked(project):
            return self._files_unlocked(project, parts)

    def usage(self, project_id: str) -> int:
        return sum(self.files(project_id).values())

    def remove(self, project_id: str, relative_path: str) -> None:
        project = self._project_component(project_id)
        parts = self._relative_parts(relative_path)
        with self.locked(project):
            self._remove_unlocked(project, parts)

    def rename_tree(self, project_id: str, source: str, destination: str) -> Path:
        project = self._project_component(project_id)
        source_parts = self._relative_parts(source)
        destination_parts = self._relative_parts(destination)
        with self.locked(project):
            self._rename_tree_unlocked(project, source_parts, destination_parts)
        return self.root / project / Path(*destination_parts)

    @contextmanager
    def _posix_directory(
        self,
        project_id: str,
        parts: tuple[str, ...],
        *,
        create: bool,
    ) -> Iterator[int]:
        flags = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC
        handles: list[int] = []
        try:
            current = os.open(self.root, flags)
            handles.append(current)
            for part in (project_id, *parts):
                if create:
                    with suppress(FileExistsError):
                        os.mkdir(part, 0o700, dir_fd=current)
                current = os.open(part, flags, dir_fd=current)
                handles.append(current)
        except OSError as error:
            for handle in reversed(handles):
                os.close(handle)
            raise ProjectExecutionError("artifact directory is unavailable or unsafe") from error
        try:
            yield current
        finally:
            for handle in reversed(handles):
                os.close(handle)

    @contextmanager
    def _windows_directory(
        self,
        project_id: str,
        parts: tuple[str, ...],
        *,
        create: bool,
    ) -> Iterator[int]:
        api = _windows_workspace_api()
        handles: list[int] = []
        try:
            current = api.open_root(self.root)
            handles.append(current)
            expected = self.root
            api.assert_path(current, expected)
            for part in (project_id, *parts):
                current = api.open_relative(
                    current,
                    part,
                    directory=True,
                    create=create,
                )
                handles.append(current)
                expected /= part
                api.assert_path(current, expected)
        except (OSError, ProjectExecutionError) as error:
            for handle in reversed(handles):
                api.close(handle)
            if isinstance(error, ProjectExecutionError):
                raise
            raise ProjectExecutionError("artifact directory is unavailable or unsafe") from error
        try:
            yield current
        finally:
            for handle in reversed(handles):
                api.close(handle)

    def _directory(
        self,
        project_id: str,
        parts: tuple[str, ...],
        *,
        create: bool,
    ) -> Any:
        if os.name == "nt":
            return self._windows_directory(project_id, parts, create=create)
        return self._posix_directory(project_id, parts, create=create)

    def _ensure_directory_unlocked(self, project_id: str, parts: tuple[str, ...]) -> None:
        with self._directory(project_id, parts, create=True):
            pass

    def _write_unlocked(
        self,
        project_id: str,
        parts: tuple[str, ...],
        payload: bytes,
    ) -> None:
        parent_parts, target_name = parts[:-1], parts[-1]
        temporary_name = f".{target_name}.{new_id('write')}.tmp"
        with self._directory(project_id, parent_parts, create=True) as parent:
            if os.name == "nt":
                msvcrt: Any = importlib.import_module("msvcrt")

                api = _windows_workspace_api()
                handle = api.open_relative(
                    parent,
                    temporary_name,
                    directory=False,
                    create=True,
                    write=True,
                    delete=True,
                )
                descriptor = msvcrt.open_osfhandle(
                    handle, os.O_WRONLY | int(getattr(os, "O_BINARY", 0))
                )
                renamed = False
                try:
                    expected_parent = self.root / project_id / Path(*parent_parts)
                    api.assert_path(parent, expected_parent)
                    api.assert_path(
                        msvcrt.get_osfhandle(descriptor),
                        expected_parent / temporary_name,
                    )
                    self._write_descriptor(descriptor, payload)
                    os.fsync(descriptor)
                    api.rename(
                        msvcrt.get_osfhandle(descriptor),
                        parent,
                        target_name,
                        replace=True,
                    )
                    api.assert_path(parent, expected_parent)
                    api.assert_path(
                        msvcrt.get_osfhandle(descriptor),
                        expected_parent / target_name,
                    )
                    renamed = True
                    api.flush(parent)
                finally:
                    if not renamed:
                        with suppress(OSError):
                            api.delete(msvcrt.get_osfhandle(descriptor))
                    os.close(descriptor)
                return
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC
            descriptor = os.open(temporary_name, flags, 0o600, dir_fd=parent)
            renamed = False
            try:
                self._write_descriptor(descriptor, payload)
                os.fsync(descriptor)
                os.replace(
                    temporary_name,
                    target_name,
                    src_dir_fd=parent,
                    dst_dir_fd=parent,
                )
                renamed = True
                os.fsync(parent)
            finally:
                os.close(descriptor)
                if not renamed:
                    with suppress(FileNotFoundError):
                        os.unlink(temporary_name, dir_fd=parent)

    @staticmethod
    def _write_descriptor(descriptor: int, payload: bytes) -> None:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("project artifact write made no progress")
            view = view[written:]

    def _read_unlocked(self, project_id: str, parts: tuple[str, ...]) -> bytes:
        with self._directory(project_id, parts[:-1], create=False) as parent:
            if os.name == "nt":
                msvcrt: Any = importlib.import_module("msvcrt")

                api = _windows_workspace_api()
                handle = api.open_relative(parent, parts[-1], directory=False)
                descriptor = msvcrt.open_osfhandle(
                    handle, os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
                )
                expected = self.root / project_id / Path(*parts)
                api.assert_path(parent, expected.parent)
                api.assert_path(msvcrt.get_osfhandle(descriptor), expected)
            else:
                flags = os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC
                descriptor = os.open(parts[-1], flags, dir_fd=parent)
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    os.close(descriptor)
                    raise ProjectExecutionError("artifact is not a regular file")
            try:
                chunks: list[bytes] = []
                while chunk := os.read(descriptor, 1024 * 1024):
                    chunks.append(chunk)
                if os.name == "nt":
                    api.assert_path(msvcrt.get_osfhandle(descriptor), expected)
                return b"".join(chunks)
            finally:
                os.close(descriptor)

    def _files_unlocked(
        self,
        project_id: str,
        parts: tuple[str, ...],
    ) -> dict[str, int]:
        prefix = PurePosixPath(*parts).as_posix() if parts else ""
        result: dict[str, int] = {}
        try:
            with self._directory(project_id, parts, create=False) as directory:
                if os.name == "nt":
                    self._walk_windows(
                        directory,
                        prefix,
                        result,
                        self.root / project_id / Path(*parts),
                    )
                else:
                    self._walk_posix(directory, prefix, result)
        except ProjectExecutionError:
            if parts:
                raise
            self._ensure_directory_unlocked(project_id, ())
        return result

    def _walk_posix(self, directory: int, prefix: str, result: dict[str, int]) -> None:
        flags = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC
        for name in os.listdir(directory):
            item = os.stat(name, dir_fd=directory, follow_symlinks=False)
            relative = f"{prefix}/{name}" if prefix else name
            if stat.S_ISLNK(item.st_mode):
                raise ProjectExecutionError("artifact path contains a symbolic link")
            if stat.S_ISDIR(item.st_mode):
                child = os.open(name, flags, dir_fd=directory)
                try:
                    self._walk_posix(child, relative, result)
                finally:
                    os.close(child)
            elif stat.S_ISREG(item.st_mode):
                result[relative] = int(item.st_size)
            else:
                raise ProjectExecutionError("project workspace contains a special file")
            if len(result) > 100_000:
                raise ProjectExecutionError("project workspace file count is unbounded")

    def _walk_windows(
        self,
        directory: int,
        prefix: str,
        result: dict[str, int],
        expected_directory: Path,
    ) -> None:
        api = _windows_workspace_api()
        api.assert_path(directory, expected_directory)
        for name in api.entries(directory):
            handle = api.open_relative(directory, name, directory=None)
            try:
                expected = expected_directory / name
                api.assert_path(handle, expected)
                attributes = api._check_attributes(handle, directory=None)
                relative = f"{prefix}/{name}" if prefix else name
                if attributes & api.FILE_ATTRIBUTE_DIRECTORY:
                    self._walk_windows(handle, relative, result, expected)
                else:
                    result[relative] = api.size(handle)
                    api.assert_path(handle, expected)
            finally:
                api.close(handle)
            if len(result) > 100_000:
                raise ProjectExecutionError("project workspace file count is unbounded")
        api.assert_path(directory, expected_directory)

    def _remove_unlocked(self, project_id: str, parts: tuple[str, ...]) -> None:
        try:
            with self._directory(project_id, parts[:-1], create=False) as parent:
                if os.name == "nt":
                    api = _windows_workspace_api()
                    try:
                        handle = api.open_relative(
                            parent,
                            parts[-1],
                            directory=None,
                            delete=True,
                        )
                    except OSError as error:
                        code = getattr(error, "winerror", None) or error.errno
                        if code in {2, 3}:
                            return
                        raise
                    try:
                        attributes = api._check_attributes(handle, directory=None)
                        if attributes & api.FILE_ATTRIBUTE_DIRECTORY:
                            self._remove_windows_children(handle)
                        api.delete(handle)
                    finally:
                        api.close(handle)
                    api.flush(parent)
                    return
                try:
                    item = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    return
                if stat.S_ISDIR(item.st_mode):
                    flags = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW
                    child = os.open(parts[-1], flags, dir_fd=parent)
                    try:
                        self._remove_posix_children(child)
                    finally:
                        os.close(child)
                    os.rmdir(parts[-1], dir_fd=parent)
                else:
                    os.unlink(parts[-1], dir_fd=parent)
                os.fsync(parent)
        except ProjectExecutionError:
            return

    def _remove_posix_children(self, directory: int) -> None:
        flags = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW
        for name in os.listdir(directory):
            item = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISDIR(item.st_mode):
                child = os.open(name, flags, dir_fd=directory)
                try:
                    self._remove_posix_children(child)
                finally:
                    os.close(child)
                os.rmdir(name, dir_fd=directory)
            else:
                os.unlink(name, dir_fd=directory)

    def _remove_windows_children(self, directory: int) -> None:
        api = _windows_workspace_api()
        for name in api.entries(directory):
            child = api.open_relative(directory, name, directory=None, delete=True)
            try:
                attributes = api._check_attributes(child, directory=None)
                if attributes & api.FILE_ATTRIBUTE_DIRECTORY:
                    self._remove_windows_children(child)
                api.delete(child)
            finally:
                api.close(child)

    def _rename_tree_unlocked(
        self,
        project_id: str,
        source: tuple[str, ...],
        destination: tuple[str, ...],
    ) -> None:
        self._ensure_directory_unlocked(project_id, destination[:-1])
        with (
            self._directory(project_id, source[:-1], create=False) as source_parent,
            self._directory(project_id, destination[:-1], create=False) as destination_parent,
        ):
            if os.name == "nt":
                api = _windows_workspace_api()
                source_handle = api.open_relative(
                    source_parent,
                    source[-1],
                    directory=True,
                    delete=True,
                )
                try:
                    api.rename(
                        source_handle,
                        destination_parent,
                        destination[-1],
                        replace=False,
                    )
                    api.assert_path(
                        source_handle,
                        self.root / project_id / Path(*destination),
                    )
                finally:
                    api.close(source_handle)
                api.flush(source_parent)
                api.flush(destination_parent)
                return
            os.replace(
                source[-1],
                destination[-1],
                src_dir_fd=source_parent,
                dst_dir_fd=destination_parent,
            )
            os.fsync(source_parent)
            if source_parent != destination_parent:
                os.fsync(destination_parent)


class _PrototypePublication:
    FORMAT = "noyra-prototype-publication/v1"

    def __init__(
        self,
        workspace: ProjectWorkspace,
        project_id: str,
        phase_id: str,
        execution_id: str,
    ):
        self.workspace = workspace
        self.project_id = project_id
        self.phase_id = phase_id
        self.execution_id = execution_id
        self.staging_root = f".noyra-staging/{execution_id}"
        self.generation_root = f"phases/{phase_id}/prototype-generations/{execution_id}"
        self.manifest_path = f"phases/{phase_id}/prototype-manifests/{execution_id}.json"
        self._expected: dict[str, tuple[int, str]] = {}
        self._lock_context: Any = None
        self._committed = False

    def __enter__(self) -> _PrototypePublication:
        self._lock_context = self.workspace.locked(self.project_id)
        self._lock_context.__enter__()
        self.rollback()
        self.workspace.ensure_directory(self.project_id, self.staging_root)
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        try:
            if not self._committed:
                self.rollback()
        finally:
            self._lock_context.__exit__(exc_type, exc, traceback)

    def write_file(self, relative_path: str, payload: bytes) -> None:
        parts = self.workspace._relative_parts(relative_path)
        normalized = PurePosixPath(*parts).as_posix()
        if normalized in self._expected:
            raise ProjectExecutionError("prototype file paths must be unique")
        self.workspace.write(
            self.project_id,
            f"{self.staging_root}/{normalized}",
            payload,
        )
        self._expected[normalized] = (len(payload), _artifact_sha256(payload))

    def publish(
        self,
        *,
        summary: str,
        validation_claims: tuple[str, ...],
        validator: Callable[[str, bytes], str],
        require_tests: bool = False,
    ) -> tuple[dict[str, Any], Path, str]:
        self._validate_tree(self.staging_root, validator)
        self.workspace.rename_tree(
            self.project_id,
            self.staging_root,
            self.generation_root,
        )
        self._validate_tree(self.generation_root, validator)
        test_result: dict[str, Any] | None = None
        if require_tests:
            test_result = _run_prototype_tests(self._tree_bytes(self.generation_root))
        generation_path = self.workspace.path(self.project_id, self.generation_root)
        files = [
            {
                "path": str(generation_path / Path(*PurePosixPath(path).parts)),
                "bytes": byte_count,
                "sha256": digest,
            }
            for path, (byte_count, digest) in sorted(self._expected.items())
        ]
        manifest_path = self.workspace.path(self.project_id, self.manifest_path)
        evidence = {
            "summary": summary,
            "files": files,
            "model_validation_claims": list(validation_claims),
            "sandbox": "text-artifact-only; no host execution",
            "build": {
                "status": "passed",
                "checks": sorted(self._expected),
            },
            "publication": {
                "format": self.FORMAT,
                "generation_id": self.execution_id,
                "generation_root": str(generation_path),
                "manifest_path": str(manifest_path),
            },
        }
        if test_result is not None:
            evidence["tests"] = test_result
        payload = _canonical_evidence_bytes(evidence)
        self.workspace.write(self.project_id, self.manifest_path, payload)
        return evidence, manifest_path, _artifact_sha256(payload)

    def _tree_bytes(self, root: str) -> dict[str, bytes]:
        prefix = root + "/"
        files = self.workspace.files(self.project_id, root)
        result: dict[str, bytes] = {}
        for path in files:
            if not path.startswith(prefix):
                raise ProjectExecutionValidationError("prototype tree contains an invalid path")
            relative = path.removeprefix(prefix)
            result[relative] = self.workspace.read(self.project_id, path)
        return result

    def _validate_tree(
        self,
        root: str,
        validator: Callable[[str, bytes], str],
    ) -> None:
        actual = self.workspace.files(self.project_id, root)
        expected = {f"{root}/{path}": value[0] for path, value in self._expected.items()}
        if actual != expected:
            raise ProjectExecutionValidationError(
                "software prototype staging tree does not match its manifest"
            )
        for path, (byte_count, digest) in self._expected.items():
            payload = self.workspace.read(self.project_id, f"{root}/{path}")
            if len(payload) != byte_count or _artifact_sha256(payload) != digest:
                raise ProjectExecutionValidationError(
                    "software prototype staging bytes changed during validation"
                )
            validator(path, payload)

    def commit(self) -> None:
        self._committed = True

    def rollback(self) -> None:
        for relative_path in (self.manifest_path, self.generation_root, self.staging_root):
            self.workspace.remove(self.project_id, relative_path)


class ProjectPhaseExecutor:
    """Dispatch one project phase to existing, authorized workflows."""

    def __init__(
        self,
        database: Database,
        subject_id: str,
        research: AutonomousResearch,
        *,
        workspace_root: Path | str | None = None,
    ):
        self.database = database
        self.subject_id = validate_subject_id(subject_id)
        self.research = research
        self.workspace_root = Path(workspace_root or database.path.parent / "workspace").resolve()
        self._storage_key: str | None = None
        self._legacy_storage_path_is_unambiguous = False
        self.ledger = ProjectExecutionLedger(
            database, subject_id, workspace_root=self.workspace_root
        )
        self.events = EventStore(database)
        self.interactions = InteractionStore(database)
        self.predictions = PredictionStore(database)

    def _ensure_storage_binding(self) -> None:
        if self._storage_key is not None:
            return
        identities = IdentityStore(self.database)
        self._storage_key = identities.storage_key(self.subject_id)
        self._legacy_storage_path_is_unambiguous = identities.legacy_storage_path_is_unambiguous(
            self.subject_id
        )

    @property
    def storage_key(self) -> str:
        self._ensure_storage_binding()
        assert self._storage_key is not None
        return self._storage_key

    @property
    def legacy_storage_path_is_unambiguous(self) -> bool:
        self._ensure_storage_binding()
        return self._legacy_storage_path_is_unambiguous

    async def run_phase(
        self, project: AutonomousProjectRecord, phase: AutonomousProjectPhaseRecord
    ) -> ProjectExecutionRecord:
        if phase.output_type in {"research_note", "knowledge_collection"}:
            return await self.run_research_phase(project, phase)
        if phase.output_type == "software_prototype":
            return await self._run_software_prototype(project, phase)
        if phase.output_type == "prediction_record":
            return self._run_prediction_record(project, phase)
        if phase.output_type == "self_experiment":
            return self._run_self_experiment(project, phase)
        if phase.output_type == "collaboration_request":
            return self._run_collaboration_request(project, phase)
        raise ProjectExecutionError(f"unsupported project phase output: {phase.output_type}")

    async def run_research_phase(
        self, project: AutonomousProjectRecord, phase: AutonomousProjectPhaseRecord
    ) -> ProjectExecutionRecord:
        if phase.output_type not in {"research_note", "knowledge_collection"}:
            raise ProjectExecutionError("phase output is not research-executable")
        if project.status not in {"planned", "active"} or phase.status != "active":
            raise ProjectExecutionError("project phase is not active")
        execution = self.ledger.get_or_prepare(project, phase, workflow="autonomous_research")
        if execution.status in {"succeeded", "failed", "unknown", "blocked"}:
            return execution
        self.ledger.transition(
            execution.execution_id, "executing", reason="research workflow started"
        )
        try:
            self.research.bind_project_phase(project.project_id, phase.phase_id)
            result = await self.research.run_due()
            assert_current_lease()
        except (AutonomousResearchValidationError, PermissionError, ValueError) as error:
            self.research.clear_project_binding()
            return self.ledger.transition(
                execution.execution_id,
                "blocked",
                error_code=type(error).__name__,
                reason="research workflow was not authorized or valid",
            )
        except Exception as error:
            return self.ledger.transition(
                execution.execution_id,
                "unknown",
                error_code=type(error).__name__,
                reason="research workflow ended before its outcome was known",
            )
        finally:
            self.research.clear_project_binding()
        record = self._latest_bound_research(project.project_id, phase.phase_id)
        if record is None:
            return self.ledger.transition(
                execution.execution_id,
                "failed",
                error_code=result or "research_not_committed",
                reason="research workflow produced no durable project record",
            )
        success = (
            record["status"] == "accepted"
            and int(record["result_count"]) > 0
            and bool(record["accepted_source_ids_json"])
        )
        evidence = {
            "research_id": record["research_id"],
            "status": record["status"],
            "result_count": int(record["result_count"]),
            "accepted_source_ids": json.loads(record["accepted_source_ids_json"]),
        }
        artifact_path: str | None = None
        artifact_hash: str | None = None
        if success:
            try:
                payload = canonical_json(evidence).encode()
                workspace = ProjectWorkspace(
                    self.workspace_root,
                    self.storage_key,
                    quota_bytes=max(1, project.budget.max_storage_bytes),
                    legacy_subject_id=self.subject_id,
                    legacy_migration_allowed=self.legacy_storage_path_is_unambiguous,
                )
                with current_commit_scope():
                    assert_current_lease()
                    artifact = workspace.write(
                        project.project_id,
                        f"phases/{phase.phase_id}/research-result.json",
                        payload,
                    )
                    artifact_path = str(artifact)
                    artifact_hash = _artifact_sha256(payload)
            except (OSError, ProjectExecutionError) as error:
                return self.ledger.transition(
                    execution.execution_id,
                    "blocked",
                    evidence=evidence,
                    research_id=record["research_id"],
                    error_code=type(error).__name__,
                    reason="research artifact could not be confined to project workspace",
                )
        if success:
            return self._validated_success(
                execution,
                evidence=evidence,
                research_id=record["research_id"],
                model_call_id=record["planner_call_id"],
                artifact_path=artifact_path,
                artifact_hash=artifact_hash,
                reason="durable research acceptance evaluated",
            )
        return self.ledger.transition(
            execution.execution_id,
            "failed",
            evidence=evidence,
            research_id=record["research_id"],
            model_call_id=record["planner_call_id"],
            reason="durable research acceptance evaluated",
            error_code="research_acceptance_not_met",
        )

    def _latest_bound_research(self, project_id: str, phase_id: str) -> Any | None:
        with self.database.connection() as connection:
            return connection.execute(
                "SELECT * FROM research_search_runs WHERE subject_id = ? AND project_id = ? "
                "AND phase_id = ? ORDER BY created_at DESC, research_id DESC LIMIT 1",
                (self.subject_id, project_id, phase_id),
            ).fetchone()

    def _prepare_adapter(
        self,
        project: AutonomousProjectRecord,
        phase: AutonomousProjectPhaseRecord,
        workflow: str,
    ) -> ProjectExecutionRecord:
        if project.status not in {"planned", "active"} or phase.status != "active":
            raise ProjectExecutionError("project phase is not active")
        execution = self.ledger.get_or_prepare(project, phase, workflow=workflow)
        if execution.status == "prepared":
            execution = self.ledger.transition(
                execution.execution_id,
                "executing",
                reason=f"{workflow} adapter started",
            )
        return execution

    async def _run_software_prototype(
        self, project: AutonomousProjectRecord, phase: AutonomousProjectPhaseRecord
    ) -> ProjectExecutionRecord:
        execution = self._prepare_adapter(project, phase, "artifact_sandbox")
        if execution.status != "executing":
            return execution
        workspace = ProjectWorkspace(
            self.workspace_root,
            self.storage_key,
            quota_bytes=max(1, project.budget.max_storage_bytes),
            legacy_subject_id=self.subject_id,
            legacy_migration_allowed=self.legacy_storage_path_is_unambiguous,
        )
        publication = _PrototypePublication(
            workspace,
            project.project_id,
            phase.phase_id,
            execution.execution_id,
        )
        try:
            with workspace.locked(project.project_id):
                current = self.ledger.get(execution.execution_id)
                if current.status != "executing":
                    return current
                publication.rollback()
        except (OSError, ProjectExecutionError) as error:
            return self.ledger.transition(
                execution.execution_id,
                "blocked",
                error_code=type(error).__name__,
                reason="stale prototype publication could not be recovered safely",
            )
        try:
            result = await self.research.gateway.complete_structured(
                self.subject_id,
                f"autonomous_project_software:{project.project_id}:{phase.phase_id}",
                (
                    ModelMessage(
                        role="system",
                        content=(
                            "Create a small static prototype as bounded text files. Do not include "
                            "shell commands, binaries, credentials, network listeners, installers, "
                            "or paths outside the project. Return only the requested JSON schema."
                        ),
                    ),
                    ModelMessage(
                        role="user",
                        content=canonical_json(
                            {
                                "project": project.title,
                                "purpose": project.purpose,
                                "phase": phase.title,
                                "objective": phase.objective,
                                "acceptance_criteria": list(phase.acceptance_criteria),
                                "maximum_files": 20,
                                "maximum_storage_bytes": project.budget.max_storage_bytes,
                            }
                        ),
                    ),
                ),
                SoftwarePrototypeProposal,
                idempotency_key=f"project-software:{project.project_id}:{phase.phase_id}",
                max_output_tokens=8_000,
                temperature=0.2,
            )
            assert_current_lease()
        except (BudgetExhaustedError, ModelCallStateError, ProviderCallError) as error:
            return self.ledger.transition(
                execution.execution_id,
                "unknown" if isinstance(error, ModelCallStateError) else "blocked",
                error_code=type(error).__name__,
                reason="software proposal did not produce a known sandbox input",
            )
        staged: list[dict[str, str | int]] = []
        try:
            with workspace.locked(project.project_id):
                current = self.ledger.get(execution.execution_id)
                if current.status != "executing":
                    return current
                with current_commit_scope():
                    assert_current_lease()
                    with publication:
                        for item in result.output.files:
                            payload = item.content.encode("utf-8")
                            publication.write_file(item.path, payload)
                            staged.append(
                                {
                                    "path": item.path,
                                    "bytes": len(payload),
                                    "sha256": _artifact_sha256(payload),
                                }
                            )
                        evidence, manifest, manifest_hash = publication.publish(
                            summary=result.output.summary,
                            validation_claims=result.output.validation,
                            validator=_validate_prototype_file,
                            require_tests=True,
                        )
                        completed = self._validated_success(
                            execution,
                            model_call_id=result.call_id,
                            evidence=evidence,
                            artifact_path=str(manifest),
                            artifact_hash=manifest_hash,
                            reason="prototype staging tree passed validation and was published",
                            cleanup_failed_artifact=publication.rollback,
                        )
                        if completed.status == "succeeded":
                            publication.commit()
                        return completed
        except (OSError, ProjectExecutionError) as error:
            current = self.ledger.get(execution.execution_id)
            if current.status != "executing":
                return current
            return self.ledger.transition(
                execution.execution_id,
                "blocked",
                model_call_id=result.call_id,
                evidence={"files_staged": staged},
                error_code=(
                    "output_validation_failed"
                    if isinstance(error, ProjectExecutionValidationError)
                    else type(error).__name__
                ),
                reason="prototype artifact sandbox rejected the output",
            )

    def _run_prediction_record(
        self, project: AutonomousProjectRecord, phase: AutonomousProjectPhaseRecord
    ) -> ProjectExecutionRecord:
        execution = self._prepare_adapter(project, phase, "prediction_record")
        if execution.status != "executing":
            return execution
        current_evidence = execution.acceptance.get("evidence")
        if isinstance(current_evidence, dict) and isinstance(
            current_evidence.get("prediction_id"), str
        ):
            prediction_id = str(current_evidence["prediction_id"])
            with self.database.connection() as connection:
                prediction_row = connection.execute(
                    "SELECT * FROM predictions WHERE prediction_id = ? AND subject_id = ?",
                    (prediction_id, self.subject_id),
                ).fetchone()
            if prediction_row is None:
                return self.ledger.transition(
                    execution.execution_id,
                    "blocked",
                    evidence=current_evidence,
                    error_code="prediction_missing",
                    reason="durable prediction disappeared before resolution",
                )
            prediction = PredictionStore._from_row(prediction_row)
            if prediction.status == "open":
                return execution
            if prediction.status != "resolved":
                return self.ledger.transition(
                    execution.execution_id,
                    "blocked",
                    evidence=current_evidence,
                    error_code="prediction_not_resolved",
                    reason="prediction was cancelled before measurable resolution",
                )
            with self.database.connection() as connection:
                reviews = connection.execute(
                    "SELECT * FROM prediction_reviews WHERE prediction_id = ? "
                    "ORDER BY created_at, review_id",
                    (prediction_id,),
                ).fetchall()
            if len(reviews) < 2:
                return self.ledger.transition(
                    execution.execution_id,
                    "executing",
                    evidence=current_evidence,
                    error_code="prediction_resolution_evidence_pending",
                    reason="prediction status is resolved but its review evidence is incomplete",
                )
            resolution = reviews[-1]
            try:
                resolution_evidence = strict_json_loads(resolution["evidence_observation_ids_json"])
            except (TypeError, ValueError):
                return self.ledger.transition(
                    execution.execution_id,
                    "blocked",
                    evidence=current_evidence,
                    error_code="prediction_resolution_evidence_invalid",
                    reason="prediction resolution evidence is malformed",
                )
            if not isinstance(resolution_evidence, list):
                return self.ledger.transition(
                    execution.execution_id,
                    "blocked",
                    evidence=current_evidence,
                    error_code="prediction_resolution_evidence_invalid",
                    reason="prediction resolution evidence is malformed",
                )
            evidence = {
                **current_evidence,
                "outcome": bool(prediction.outcome),
                "brier_score": prediction.brier_score,
                "resolution_evidence_observation_ids": resolution_evidence,
                "resolved_at": prediction.resolved_at,
            }
            artifact, artifact_hash = self._write_evidence(
                project, phase, "prediction.json", evidence
            )
            return self._validated_success(
                execution,
                evidence=evidence,
                artifact_path=str(artifact),
                artifact_hash=artifact_hash,
                reason="prediction reached a durable measured resolution",
            )
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT observation_id FROM observations WHERE subject_id = ? "
                "AND status = 'analyzed' ORDER BY fetched_at DESC LIMIT 8",
                (self.subject_id,),
            ).fetchall()
        evidence_ids = tuple(str(row["observation_id"]) for row in rows)
        if not evidence_ids:
            return self.ledger.transition(
                execution.execution_id,
                "blocked",
                error_code="prediction_evidence_unavailable",
                reason="prediction phase requires analyzed observations",
            )
        with self.database.connection() as connection:
            calibration_rows = connection.execute(
                "SELECT prediction_id, outcome FROM predictions WHERE subject_id = ? "
                "AND status = 'resolved' AND outcome IS NOT NULL "
                "ORDER BY resolved_at, prediction_id",
                (self.subject_id,),
            ).fetchall()
        if len(calibration_rows) < 3:
            pending = {
                "status": "pending_calibration",
                "required_resolved_predictions": 3,
                "resolved_prediction_count": len(calibration_rows),
                "evidence_observation_ids": list(evidence_ids),
            }
            return self.ledger.transition(
                execution.execution_id,
                "executing",
                evidence=pending,
                error_code="prediction_calibration_unavailable",
                reason="prediction remains open until calibrated historical outcomes exist",
            )
        successes = sum(int(row["outcome"]) for row in calibration_rows)
        probability = round((successes + 1) / (len(calibration_rows) + 2), 6)
        target = (datetime.now(UTC) + timedelta(days=30)).isoformat(timespec="milliseconds")
        prediction = self.predictions.create(
            self.subject_id,
            PredictionProposal(
                statement=phase.objective,
                probability=probability,
                target_at=target,
                resolution_criteria="; ".join(phase.acceptance_criteria)[:10_000],
            ),
            evidence_observation_ids=evidence_ids,
            rationale=(
                f"Laplace calibration from {len(calibration_rows)} durable resolved predictions"
            ),
            idempotency_key=f"project-prediction:{project.project_id}:{phase.phase_id}",
        )
        evidence = {
            "prediction_id": prediction.prediction_id,
            "target_at": prediction.target_at,
            "probability": prediction.probability,
            "evidence_observation_ids": list(evidence_ids),
            "calibration_prediction_ids": [str(row["prediction_id"]) for row in calibration_rows],
            "calibration_successes": successes,
            "calibration_count": len(calibration_rows),
        }
        artifact, artifact_hash = self._write_evidence(project, phase, "prediction.json", evidence)
        return self.ledger.transition(
            execution.execution_id,
            "executing",
            evidence=evidence,
            artifact_path=str(artifact),
            artifact_hash=artifact_hash,
            error_code="prediction_resolution_pending",
            reason="prediction was committed; measurable target resolution is pending",
        )

    def _run_self_experiment(
        self, project: AutonomousProjectRecord, phase: AutonomousProjectPhaseRecord
    ) -> ProjectExecutionRecord:
        execution = self._prepare_adapter(project, phase, "self_experiment")
        if execution.status != "executing":
            return execution
        prior_evidence = execution.acceptance.get("evidence")
        if not isinstance(prior_evidence, dict) or "baseline_event_id" not in prior_evidence:
            counts = self._self_experiment_measurements()
            event = self.events.append(
                self.subject_id,
                "project_self_experiment_baseline",
                "project_executor",
                {
                    "project_id": project.project_id,
                    "phase_id": phase.phase_id,
                    "hypothesis": phase.objective,
                    "baseline": counts,
                    "acceptance_criteria": list(phase.acceptance_criteria),
                    "measurement_protocol": "non-experiment events and active memories",
                },
            )
            pending = {
                "hypothesis": phase.objective,
                "baseline_event_id": event.event_id,
                "baseline": counts,
                "status": "awaiting_follow_up",
            }
            return self.ledger.transition(
                execution.execution_id,
                "executing",
                evidence=pending,
                error_code="self_experiment_follow_up_pending",
                reason="baseline recorded; an independent follow-up measurement is required",
            )
        baseline_event_id = prior_evidence.get("baseline_event_id")
        if not isinstance(baseline_event_id, str) or not baseline_event_id.strip():
            return self.ledger.transition(
                execution.execution_id,
                "blocked",
                evidence=prior_evidence,
                error_code="self_experiment_baseline_invalid",
                reason="self experiment baseline evidence is malformed",
            )
        with self.database.connection() as connection:
            baseline_row = connection.execute(
                "SELECT occurred_at FROM events WHERE event_id = ? AND subject_id = ? "
                "AND event_type = 'project_self_experiment_baseline'",
                (baseline_event_id, self.subject_id),
            ).fetchone()
        if baseline_row is None:
            return self.ledger.transition(
                execution.execution_id,
                "blocked",
                evidence=prior_evidence,
                error_code="self_experiment_baseline_missing",
                reason="self experiment baseline event is unavailable",
            )
        baseline = prior_evidence.get("baseline")
        if not isinstance(baseline, dict) or not baseline:
            return self.ledger.transition(
                execution.execution_id,
                "blocked",
                evidence=prior_evidence,
                error_code="self_experiment_baseline_invalid",
                reason="self experiment baseline measurements are unavailable",
            )
        follow_up = self._self_experiment_measurements()
        follow_up_at = utc_now()
        baseline_at = str(baseline_row["occurred_at"])
        if follow_up_at <= baseline_at:
            follow_up_at = (
                datetime.fromisoformat(baseline_at) + timedelta(milliseconds=1)
            ).isoformat(timespec="milliseconds")
        event = self.events.append(
            self.subject_id,
            "project_self_experiment_follow_up",
            "project_executor",
            {
                "project_id": project.project_id,
                "phase_id": phase.phase_id,
                "hypothesis": phase.objective,
                "follow_up": follow_up,
                "acceptance_criteria": list(phase.acceptance_criteria),
            },
            causal_parent_ids=(baseline_event_id,),
            occurred_at=follow_up_at,
        )
        delta = {
            key: int(follow_up[key]) - int(baseline[key]) for key in baseline if key in follow_up
        }
        evidence = {
            "hypothesis": phase.objective,
            "baseline_event_id": baseline_event_id,
            "follow_up_event_id": event.event_id,
            "baseline": baseline,
            "follow_up": follow_up,
            "delta": delta,
        }
        artifact, artifact_hash = self._write_evidence(
            project, phase, "self-experiment.json", evidence
        )
        return self._validated_success(
            execution,
            evidence=evidence,
            artifact_path=str(artifact),
            artifact_hash=artifact_hash,
            reason="self experiment baseline and follow-up measurements reconciled",
        )

    def _self_experiment_measurements(self) -> dict[str, int]:
        with self.database.connection() as connection:
            return {
                "events": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM events WHERE subject_id = ? "
                        "AND event_type NOT LIKE 'project_self_experiment_%'",
                        (self.subject_id,),
                    ).fetchone()[0]
                ),
                "active_memories": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM memories WHERE subject_id = ? AND status = 'active'",
                        (self.subject_id,),
                    ).fetchone()[0]
                ),
            }

    def _run_collaboration_request(
        self, project: AutonomousProjectRecord, phase: AutonomousProjectPhaseRecord
    ) -> ProjectExecutionRecord:
        execution = self._prepare_adapter(project, phase, "collaboration_request")
        if execution.status != "executing":
            return execution
        message = self.interactions.send(
            self.subject_id,
            "web",
            "web-user",
            f"{phase.objective}\n\nAcceptance: {'; '.join(phase.acceptance_criteria)}",
            kind="help_request",
            idempotency_key=f"project-help:{project.project_id}:{phase.phase_id}",
        )
        evidence = {"interaction_id": message.interaction_id, "status": message.status}
        artifact, artifact_hash = self._write_evidence(
            project, phase, "collaboration-request.json", evidence
        )
        return self._validated_success(
            execution,
            evidence=evidence,
            artifact_path=str(artifact),
            artifact_hash=artifact_hash,
            reason="collaboration request was placed in the human communication mailbox",
        )

    def _validated_success(
        self,
        execution: ProjectExecutionRecord,
        *,
        evidence: dict[str, Any],
        artifact_path: str | None,
        artifact_hash: str | None,
        reason: str,
        research_id: str | None = None,
        action_id: str | None = None,
        model_call_id: str | None = None,
        cleanup_failed_artifact: Callable[[], None] | None = None,
    ) -> ProjectExecutionRecord:
        try:
            return self.ledger.transition(
                execution.execution_id,
                "succeeded",
                evidence=evidence,
                research_id=research_id,
                action_id=action_id,
                model_call_id=model_call_id,
                artifact_path=artifact_path,
                artifact_hash=artifact_hash,
                reason=reason,
            )
        except ProjectExecutionValidationError:
            if cleanup_failed_artifact is not None:
                cleanup_failed_artifact()
            return self.ledger.transition(
                execution.execution_id,
                "blocked",
                evidence=evidence,
                research_id=research_id,
                action_id=action_id,
                model_call_id=model_call_id,
                artifact_path=None if cleanup_failed_artifact is not None else artifact_path,
                artifact_hash=None if cleanup_failed_artifact is not None else artifact_hash,
                error_code="output_validation_failed",
                reason="project output failed executable acceptance validation",
            )

    def _write_evidence(
        self,
        project: AutonomousProjectRecord,
        phase: AutonomousProjectPhaseRecord,
        filename: str,
        evidence: dict[str, Any],
    ) -> tuple[Path, str]:
        workspace = ProjectWorkspace(
            self.workspace_root,
            self.storage_key,
            quota_bytes=max(1, project.budget.max_storage_bytes),
            legacy_subject_id=self.subject_id,
            legacy_migration_allowed=self.legacy_storage_path_is_unambiguous,
        )
        payload = _canonical_evidence_bytes(evidence)
        with current_commit_scope():
            assert_current_lease()
            artifact = workspace.write(
                project.project_id,
                f"phases/{phase.phase_id}/{filename}",
                payload,
            )
        return artifact, _artifact_sha256(payload)
