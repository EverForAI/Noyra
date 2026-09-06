from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, cast

from pydantic import TypeAdapter, ValidationError

from noyra.core.database import Database
from noyra.core.errors import IntegrityError
from noyra.core.types import canonical_json, content_hash, new_id, utc_now

from ._integrity import (
    durable_boundary,
    durable_float,
    durable_int,
    durable_json,
    durable_string_list,
)
from .settings import CognitionSettings


@dataclass(frozen=True)
class MutableSettingRule:
    adapter: TypeAdapter[Any]
    minimum: float
    maximum: float
    max_relative_change: float
    risk_weight: float


@dataclass(frozen=True)
class SelfModificationRecord:
    proposal_id: str
    subject_id: str
    setting_key: str
    old_value: int | float
    proposed_value: int | float
    reason: str
    evidence_ids: tuple[str, ...]
    risk_score: float
    status: str
    validation: dict[str, Any]
    simulation: dict[str, Any]
    created_at: str
    updated_at: str
    applied_at: str | None
    observation_deadline: str | None


class SelfModificationError(ValueError):
    pass


class ControlledSelfModification:
    """Bounded, evidence-linked adaptation with observation and rollback."""

    STRATEGY_TO_SETTING: ClassVar[dict[str, str]] = {
        "think": "max_thought_no_change_streak",
        "research": "research_interval_seconds",
    }

    RULES: ClassVar[dict[str, MutableSettingRule]] = {
        "retrieval_semantic_weight": MutableSettingRule(TypeAdapter(float), 0.05, 0.6, 0.25, 0.4),
        "retrieval_lexical_weight": MutableSettingRule(TypeAdapter(float), 0.05, 0.6, 0.25, 0.4),
        "research_interval_seconds": MutableSettingRule(TypeAdapter(float), 300, 604_800, 0.5, 0.5),
        "thought_interval_seconds": MutableSettingRule(TypeAdapter(float), 300, 604_800, 0.5, 0.5),
        "memory_consolidation_interval_seconds": MutableSettingRule(
            TypeAdapter(float), 3_600, 2_592_000, 0.5, 0.45
        ),
        "max_search_rounds_per_run": MutableSettingRule(TypeAdapter(int), 1, 4, 0.5, 0.7),
        "max_thought_no_change_streak": MutableSettingRule(TypeAdapter(int), 1, 12, 0.5, 0.5),
        "max_project_no_progress_reviews": MutableSettingRule(TypeAdapter(int), 1, 12, 0.5, 0.6),
    }

    def __init__(
        self,
        database: Database,
        subject_id: str,
        settings: CognitionSettings,
        *,
        clock: Callable[[], str] = utc_now,
        observation_seconds: float = 86_400,
    ):
        self.database = database
        self.subject_id = subject_id
        self.settings = settings
        self.clock = clock
        self.observation_seconds = observation_seconds
        self._validate_strategy_mapping()

    @classmethod
    def _validate_strategy_mapping(cls) -> None:
        mapped = tuple(cls.STRATEGY_TO_SETTING.values())
        if len(mapped) != len(set(mapped)) or any(key not in cls.RULES for key in mapped):
            raise RuntimeError("self-modification strategy mapping is invalid")

    def effective(self, setting_key: str) -> int | float:
        if setting_key not in self.RULES:
            raise SelfModificationError("setting is not self-modifiable")
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT value_json FROM self_modification_settings "
                "WHERE subject_id = ? AND setting_key = ?",
                (self.subject_id, setting_key),
            ).fetchone()
        if row is None:
            return self._baseline(setting_key)
        return self._validate_value(setting_key, json.loads(row["value_json"]))

    def effective_settings(self) -> CognitionSettings:
        updates = {key: self.effective(key) for key in self.RULES if hasattr(self.settings, key)}
        return self.settings.model_copy(update=updates)

    def run_due(self) -> str | None:
        pending = self._pending()
        if pending is not None:
            if pending.status == "simulated":
                self.apply(pending.proposal_id)
                return "self_modification_applied"
            if pending.status == "applied":
                counts = self._outcomes_since(
                    pending.applied_at or pending.created_at, pending.setting_key
                )
                total = sum(counts.values())
                due = self._time(self.clock()) >= self._time(
                    pending.observation_deadline or self.clock()
                )
                if total >= 3 or due:
                    result = self.observe(pending.proposal_id, **counts)
                    return f"self_modification_{result.status}"
            return None
        candidate = self._stagnant_strategy_candidate()
        if candidate is None:
            return None
        setting_key, proposed_value, reason, evidence_ids = candidate
        result = self.propose(
            setting_key,
            proposed_value,
            reason=reason,
            evidence_ids=evidence_ids,
        )
        return f"self_modification_{result.status}"

    def propose(
        self,
        setting_key: str,
        proposed_value: int | float,
        *,
        reason: str,
        evidence_ids: tuple[str, ...],
    ) -> SelfModificationRecord:
        if setting_key not in self.RULES:
            raise SelfModificationError("setting is not self-modifiable")
        if not reason.strip() or not evidence_ids or len(set(evidence_ids)) != len(evidence_ids):
            raise SelfModificationError("reason and distinct evidence are required")
        old_value = self.effective(setting_key)
        value = self._validate_value(setting_key, proposed_value)
        rule = self.RULES[setting_key]
        relative = abs(float(value) - float(old_value)) / max(abs(float(old_value)), 1.0)
        risk = min(1.0, relative / max(rule.max_relative_change, 0.01) * rule.risk_weight)
        validation = {
            "allowlisted": True,
            "in_range": True,
            "relative_change": round(relative, 8),
            "max_relative_change": rule.max_relative_change,
        }
        status = (
            "validated"
            if relative <= rule.max_relative_change and value != old_value
            else "rejected"
        )
        simulation = self._simulate(setting_key, old_value, value)
        if status == "validated" and not simulation["passed"]:
            status = "rejected"
        elif status == "validated":
            status = "simulated"
        now = self.clock()
        proposal_id = new_id("selfmod")
        state_hash = self._proposal_hash(
            setting_key,
            old_value,
            value,
            reason,
            evidence_ids,
            risk,
            status,
            validation,
            simulation,
        )
        with self.database.transaction() as connection:
            evidence_rows = [
                connection.execute(
                    "SELECT event_type FROM events WHERE subject_id = ? AND event_id = ?",
                    (self.subject_id, evidence_id),
                ).fetchone()
                for evidence_id in evidence_ids
            ]
            if any(row is None for row in evidence_rows):
                raise SelfModificationError("self-modification evidence is unavailable")
            event_types = {str(row["event_type"]) for row in evidence_rows if row is not None}
            if "metacognitive_outcome" not in event_types or event_types & {
                "human_message",
                "interaction_received",
            }:
                raise SelfModificationError("self-modification lacks autonomous evidence")
            if connection.execute(
                "SELECT 1 FROM self_modification_proposals WHERE subject_id = ? "
                "AND setting_key = ? AND status IN ('proposed','validated','simulated','applied')",
                (self.subject_id, setting_key),
            ).fetchone():
                raise SelfModificationError("setting already has a pending proposal")
            connection.execute(
                """INSERT INTO self_modification_proposals(
                    proposal_id, subject_id, setting_key, old_value_json, proposed_value_json,
                    reason, evidence_ids_json, risk_score, status, validation_json,
                    simulation_json, state_hash, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    proposal_id,
                    self.subject_id,
                    setting_key,
                    canonical_json(old_value),
                    canonical_json(value),
                    reason.strip(),
                    canonical_json(list(evidence_ids)),
                    risk,
                    status,
                    canonical_json(validation),
                    canonical_json(simulation),
                    state_hash,
                    now,
                    now,
                ),
            )
        return self.get(proposal_id)

    def apply(self, proposal_id: str) -> SelfModificationRecord:
        proposal = self.get(proposal_id)
        if proposal.status != "simulated" or proposal.risk_score > 0.7:
            raise SelfModificationError("proposal is not safe to apply")
        now = self.clock()
        deadline = (self._time(now) + timedelta(seconds=self.observation_seconds)).isoformat(
            timespec="milliseconds"
        )
        with self.database.transaction() as connection:
            current = connection.execute(
                "SELECT value_json, revision FROM self_modification_settings "
                "WHERE subject_id = ? AND setting_key = ?",
                (self.subject_id, proposal.setting_key),
            ).fetchone()
            effective = (
                self._baseline(proposal.setting_key)
                if current is None
                else json.loads(current["value_json"])
            )
            if effective != proposal.old_value:
                raise SelfModificationError("effective setting changed since proposal")
            revision = 1 if current is None else int(current["revision"]) + 1
            setting_hash = content_hash(
                {
                    "setting_key": proposal.setting_key,
                    "value": proposal.proposed_value,
                    "revision": revision,
                }
            )
            connection.execute(
                """INSERT INTO self_modification_settings(
                    subject_id, setting_key, value_json, proposal_id, revision,
                    state_hash, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(subject_id, setting_key) DO UPDATE SET
                    value_json=excluded.value_json, proposal_id=excluded.proposal_id,
                    revision=excluded.revision, state_hash=excluded.state_hash,
                    updated_at=excluded.updated_at""",
                (
                    self.subject_id,
                    proposal.setting_key,
                    canonical_json(proposal.proposed_value),
                    proposal_id,
                    revision,
                    setting_hash,
                    now,
                ),
            )
            self._revision(
                connection,
                proposal,
                "apply",
                proposal.old_value,
                proposal.proposed_value,
                "validated local simulation",
                now,
            )
            self._set_status(connection, proposal, "applied", now, now, deadline)
        return self.get(proposal_id)

    def observe(
        self,
        proposal_id: str,
        *,
        productive: int,
        failed: int,
        stagnant: int,
    ) -> SelfModificationRecord:
        proposal = self.get(proposal_id)
        if proposal.status != "applied":
            raise SelfModificationError("proposal is not in its observation window")
        if min(productive, failed, stagnant) < 0 or productive + failed + stagnant < 1:
            raise SelfModificationError("observation counts are invalid")
        now = self.clock()
        deadline_elapsed = self._time(now) >= self._time(proposal.observation_deadline or now)
        harmful = failed > productive or failed >= 2 or (deadline_elapsed and productive == 0)
        if not harmful and not deadline_elapsed:
            return proposal
        action = "rollback" if harmful else "accept"
        new_status = "rolled_back" if harmful else "accepted"
        new_value = proposal.old_value if harmful else proposal.proposed_value
        with self.database.transaction() as connection:
            current = connection.execute(
                "SELECT revision FROM self_modification_settings "
                "WHERE subject_id = ? AND setting_key = ?",
                (self.subject_id, proposal.setting_key),
            ).fetchone()
            if current is None:
                raise IntegrityError("applied self-modification setting is missing")
            revision = int(current["revision"]) + 1
            connection.execute(
                "UPDATE self_modification_settings SET value_json = ?, revision = ?, "
                "state_hash = ?, updated_at = ? "
                "WHERE subject_id = ? AND setting_key = ?",
                (
                    canonical_json(new_value),
                    revision,
                    content_hash(
                        {
                            "setting_key": proposal.setting_key,
                            "value": new_value,
                            "revision": revision,
                        }
                    ),
                    now,
                    self.subject_id,
                    proposal.setting_key,
                ),
            )
            self._revision(
                connection,
                proposal,
                action,
                proposal.proposed_value,
                new_value,
                f"productive={productive};failed={failed};stagnant={stagnant}",
                now,
            )
            self._set_status(
                connection,
                proposal,
                new_status,
                now,
                proposal.applied_at,
                proposal.observation_deadline,
            )
        return self.get(proposal_id)

    def get(self, proposal_id: str) -> SelfModificationRecord:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM self_modification_proposals "
                "WHERE subject_id = ? AND proposal_id = ?",
                (self.subject_id, proposal_id),
            ).fetchone()
        if row is None:
            raise SelfModificationError("self-modification proposal is unavailable")
        with durable_boundary("self-modification proposal", proposal_id):
            record = self._record(row)
            expected = self._proposal_hash(
                record.setting_key,
                record.old_value,
                record.proposed_value,
                record.reason,
                record.evidence_ids,
                record.risk_score,
                record.status,
                record.validation,
                record.simulation,
            )
            if expected != row["state_hash"]:
                raise IntegrityError("self-modification proposal hash mismatch")
            return record

    def verify_integrity(self) -> dict[str, int]:
        with self.database.read_transaction() as connection:
            proposals = connection.execute(
                "SELECT proposal_id FROM self_modification_proposals WHERE subject_id = ?",
                (self.subject_id,),
            ).fetchall()
            settings = connection.execute(
                "SELECT * FROM self_modification_settings WHERE subject_id = ?", (self.subject_id,)
            ).fetchall()
            revisions = connection.execute(
                "SELECT * FROM self_modification_revisions WHERE subject_id = ?", (self.subject_id,)
            ).fetchall()
            for row in proposals:
                self.get(row["proposal_id"])
            for row in settings:
                setting_key = row["setting_key"]
                with durable_boundary("self-modification setting", setting_key):
                    value = self._validate_value(
                        setting_key,
                        durable_json(
                            row["value_json"], "self-modification setting value", setting_key
                        ),
                    )
                    expected = content_hash(
                        {
                            "setting_key": setting_key,
                            "value": value,
                            "revision": durable_int(
                                row["revision"], "self-modification setting", setting_key
                            ),
                        }
                    )
                if expected != row["state_hash"]:
                    raise IntegrityError("self-modification setting hash mismatch")
            for row in revisions:
                revision_id = row["revision_id"]
                with durable_boundary("self-modification revision", revision_id):
                    setting_key = row["setting_key"]
                    old_value = self._validate_value(
                        setting_key,
                        durable_json(
                            row["old_value_json"],
                            "self-modification revision old value",
                            revision_id,
                        ),
                    )
                    new_value = self._validate_value(
                        setting_key,
                        durable_json(
                            row["new_value_json"],
                            "self-modification revision new value",
                            revision_id,
                        ),
                    )
                    expected = content_hash(
                        {
                            "proposal_id": row["proposal_id"],
                            "setting_key": setting_key,
                            "old_value": old_value,
                            "new_value": new_value,
                            "action": row["action"],
                            "reason": row["reason"],
                            "created_at": row["created_at"],
                        }
                    )
                if expected != row["state_hash"]:
                    raise IntegrityError("self-modification revision hash mismatch")
        return {
            "self_modification_proposals": len(proposals),
            "self_modification_revisions": len(revisions),
        }

    def _pending(self) -> SelfModificationRecord | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT proposal_id FROM self_modification_proposals WHERE subject_id = ? "
                "AND status IN ('simulated','applied') ORDER BY updated_at LIMIT 1",
                (self.subject_id,),
            ).fetchone()
        return None if row is None else self.get(str(row["proposal_id"]))

    def _outcomes_since(self, started_at: str, setting_key: str) -> dict[str, int]:
        strategy = {
            "thought_interval_seconds": "think",
            "max_thought_no_change_streak": "think",
            "research_interval_seconds": "research",
        }.get(setting_key)
        with self.database.connection() as connection:
            if strategy is None:
                rows = connection.execute(
                    "SELECT outcome, COUNT(*) AS total FROM metacognitive_outcomes "
                    "WHERE subject_id = ? AND created_at >= ? GROUP BY outcome",
                    (self.subject_id, started_at),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT outcome, COUNT(*) AS total FROM metacognitive_outcomes "
                    "WHERE subject_id = ? AND strategy = ? AND created_at >= ? GROUP BY outcome",
                    (self.subject_id, strategy, started_at),
                ).fetchall()
        counts = {"productive": 0, "failed": 0, "stagnant": 0}
        for row in rows:
            if row["outcome"] in counts:
                counts[str(row["outcome"])] = int(row["total"])
        return counts

    def _stagnant_strategy_candidate(
        self,
    ) -> tuple[str, int | float, str, tuple[str, ...]] | None:
        with self.database.connection() as connection:
            profile = connection.execute(
                "SELECT strategy, attempts, stagnant, failed FROM cognitive_strategy_profiles "
                "WHERE subject_id = ? AND attempts >= 4 "
                "AND (stagnant + failed) * 1.0 / attempts >= 0.6 "
                "ORDER BY (stagnant + failed) * 1.0 / attempts DESC LIMIT 1",
                (self.subject_id,),
            ).fetchone()
            events = connection.execute(
                "SELECT event_id FROM events WHERE subject_id = ? "
                "AND event_type = 'metacognitive_outcome' "
                "ORDER BY occurred_at DESC, event_id DESC LIMIT 4",
                (self.subject_id,),
            ).fetchall()
        if profile is None or not events:
            return None
        strategy = str(profile["strategy"])
        setting_key = self.STRATEGY_TO_SETTING.get(strategy)
        if setting_key is None:
            return None
        old = self.effective(setting_key)
        if isinstance(old, int):
            proposed: int | float = max(1, round(old * 0.8))
        else:
            proposed = float(old) * 1.2
        if proposed == old:
            return None
        return (
            setting_key,
            proposed,
            f"adapt after repeated {strategy} stagnation",
            tuple(str(row["event_id"]) for row in events),
        )

    def _baseline(self, setting_key: str) -> int | float:
        if setting_key == "retrieval_semantic_weight":
            return 0.25
        if setting_key == "retrieval_lexical_weight":
            return 0.35
        return self._validate_value(setting_key, getattr(self.settings, setting_key))

    @classmethod
    def _validate_value(cls, setting_key: str, value: Any) -> int | float:
        rule = cls.RULES[setting_key]
        try:
            parsed = rule.adapter.validate_python(value, strict=True)
        except ValidationError as error:
            raise SelfModificationError("self-modification value has the wrong type") from error
        if not rule.minimum <= float(parsed) <= rule.maximum:
            raise SelfModificationError("self-modification value exceeds its hard envelope")
        return cast(int | float, parsed)

    def _simulate(
        self, setting_key: str, old_value: int | float, value: int | float
    ) -> dict[str, Any]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT strategy, outcome, token_cost, created_at FROM ("
                "SELECT strategy, outcome, token_cost, created_at, outcome_id "
                "FROM metacognitive_outcomes WHERE subject_id = ? "
                "ORDER BY created_at DESC, outcome_id DESC LIMIT 64"
                ") ORDER BY created_at, outcome_id",
                (self.subject_id,),
            ).fetchall()
        if rows:
            history = [
                {
                    "strategy": str(row["strategy"]),
                    "outcome": str(row["outcome"]),
                    "token_cost": int(row["token_cost"]),
                    "created_at": str(row["created_at"]),
                }
                for row in rows
            ]
            history_source = "subject_history"
        else:
            base = datetime(2026, 1, 1, tzinfo=UTC)
            fixture = (
                (0, "think", "stagnant", 120),
                (10, "think", "stagnant", 110),
                (25, "think", "failed", 100),
                (35, "research", "productive", 240),
                (50, "action", "productive", 80),
                (55, "think", "stagnant", 90),
                (80, "think", "failed", 100),
                (105, "sleep", "productive", 0),
                (130, "research", "failed", 220),
            )
            history = [
                {
                    "strategy": strategy,
                    "outcome": outcome,
                    "token_cost": token_cost,
                    "created_at": (base + timedelta(minutes=minute)).isoformat(
                        timespec="milliseconds"
                    ),
                }
                for minute, strategy, outcome, token_cost in fixture
            ]
            history_source = "deterministic_safety_fixture"

        effective_settings = {key: self.effective(key) for key in self.RULES}
        baseline_settings = {**effective_settings, setting_key: old_value}
        candidate_settings = {**effective_settings, setting_key: value}
        baseline = self._replay_simulation(history, baseline_settings)
        candidate = self._replay_simulation(history, candidate_settings)
        workflow_keys = sorted(set(baseline["workflow_counts"]) | set(candidate["workflow_counts"]))
        workflow_diff = {
            key: int(candidate["workflow_counts"].get(key, 0))
            - int(baseline["workflow_counts"].get(key, 0))
            for key in workflow_keys
            if candidate["workflow_counts"].get(key, 0) != baseline["workflow_counts"].get(key, 0)
        }
        diff = {
            "workflow_counts": workflow_diff,
            "budget_units": int(candidate["budget_units"]) - int(baseline["budget_units"]),
            "stagnation_triggers": int(candidate["stagnation_triggers"])
            - int(baseline["stagnation_triggers"]),
            "sleep_triggers": int(candidate["sleep_triggers"]) - int(baseline["sleep_triggers"]),
        }
        budget_ceiling = max(1, int(baseline["budget_units"])) * 1.5
        sleep_ceiling = int(baseline["sleep_triggers"]) + max(1, len(history) // 4)
        passed = (
            value != old_value
            and int(candidate["budget_units"]) <= budget_ceiling
            and int(candidate["sleep_triggers"]) <= sleep_ceiling
        )
        return {
            "version": "self-modification-replay/v1",
            "passed": passed,
            "setting_key": setting_key,
            "old_value": old_value,
            "candidate_value": value,
            "history_source": history_source,
            "history_count": len(history),
            "history_hash": content_hash(history),
            "baseline": baseline,
            "candidate": candidate,
            "diff": diff,
            "safety_envelope": {
                "budget_ceiling": budget_ceiling,
                "sleep_trigger_ceiling": sleep_ceiling,
            },
            "checks": [
                "fixed_history_replay",
                "workflow_selection",
                "budget_effect",
                "stagnation_effect",
                "sleep_effect",
            ],
        }

    def _replay_simulation(
        self,
        history: list[dict[str, Any]],
        settings: dict[str, int | float],
    ) -> dict[str, Any]:
        thought_threshold = int(settings["max_thought_no_change_streak"])
        project_threshold = int(settings["max_project_no_progress_reviews"])
        search_rounds = int(settings["max_search_rounds_per_run"])
        workflow_counts: dict[str, int] = {}
        last_selected: dict[str, datetime] = {}
        thought_streak = 0
        project_streak = 0
        sleep_triggers = 0
        stagnation_triggers = 0
        budget_units = 0
        first_time: datetime | None = None
        last_time: datetime | None = None
        for item in history:
            occurred_at = self._time(str(item["created_at"]))
            first_time = occurred_at if first_time is None else first_time
            last_time = occurred_at
            strategy = str(item["strategy"])
            selected = strategy
            interval_setting = {
                "think": "thought_interval_seconds",
                "research": "research_interval_seconds",
            }.get(strategy)
            if interval_setting is not None:
                interval = float(settings[interval_setting])
                previous = last_selected.get(strategy)
                if previous is not None and (occurred_at - previous).total_seconds() < interval:
                    selected = "wait"
                else:
                    last_selected[strategy] = occurred_at
            workflow_counts[selected] = workflow_counts.get(selected, 0) + 1
            if selected == "wait":
                continue
            budget_units += max(0, int(item["token_cost"]))
            if selected == "research":
                budget_units += search_rounds
            if selected in {"think", "research"}:
                semantic_weight = float(settings["retrieval_semantic_weight"])
                budget_units += round(semantic_weight * 100)

            outcome = str(item["outcome"])
            if selected == "think":
                thought_streak = thought_streak + 1 if outcome != "productive" else 0
                if thought_streak >= thought_threshold:
                    sleep_triggers += 1
                    workflow_counts["sleep"] = workflow_counts.get("sleep", 0) + 1
                    thought_streak = 0
            project_streak = project_streak + 1 if outcome in {"stagnant", "failed"} else 0
            if project_streak >= project_threshold:
                stagnation_triggers += 1
                project_streak = 0

        maintenance_runs = 0
        if first_time is not None and last_time is not None:
            consolidation_interval = float(settings["memory_consolidation_interval_seconds"])
            elapsed = max(0.0, (last_time - first_time).total_seconds())
            maintenance_runs = int(elapsed // consolidation_interval)
            budget_units += maintenance_runs * 10
        return {
            "workflow_counts": dict(sorted(workflow_counts.items())),
            "budget_units": budget_units,
            "stagnation_triggers": stagnation_triggers,
            "sleep_triggers": sleep_triggers,
            "maintenance_runs": maintenance_runs,
        }

    @staticmethod
    def _proposal_hash(
        key: str,
        old: int | float,
        new: int | float,
        reason: str,
        evidence: tuple[str, ...],
        risk: float,
        status: str,
        validation: dict[str, Any],
        simulation: dict[str, Any],
    ) -> str:
        return content_hash(
            {
                "setting_key": key,
                "old_value": old,
                "proposed_value": new,
                "reason": reason,
                "evidence_ids": list(evidence),
                "risk_score": risk,
                "status": status,
                "validation": validation,
                "simulation": simulation,
            }
        )

    @classmethod
    def _record(cls, row: Any) -> SelfModificationRecord:
        proposal_id = row["proposal_id"]
        with durable_boundary("self-modification proposal", proposal_id):
            setting_key = row["setting_key"]
            old_value = cls._validate_value(
                setting_key,
                durable_json(row["old_value_json"], "self-modification old value", proposal_id),
            )
            proposed_value = cls._validate_value(
                setting_key,
                durable_json(
                    row["proposed_value_json"], "self-modification proposed value", proposal_id
                ),
            )
            evidence = durable_string_list(
                row["evidence_ids_json"], "self-modification evidence", proposal_id
            )
            validation = durable_json(
                row["validation_json"], "self-modification validation", proposal_id
            )
            simulation = durable_json(
                row["simulation_json"], "self-modification simulation", proposal_id
            )
            if not isinstance(validation, dict) or not isinstance(simulation, dict):
                raise IntegrityError("self-modification validation state is invalid")
            return SelfModificationRecord(
                proposal_id,
                row["subject_id"],
                setting_key,
                old_value,
                proposed_value,
                row["reason"],
                evidence,
                durable_float(row["risk_score"], "self-modification proposal", proposal_id),
                row["status"],
                validation,
                simulation,
                row["created_at"],
                row["updated_at"],
                row["applied_at"],
                row["observation_deadline"],
            )

    @staticmethod
    def _time(value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise SelfModificationError("self-modification clock requires a timezone")
        return parsed.astimezone(UTC)

    def _set_status(
        self,
        connection: Any,
        proposal: SelfModificationRecord,
        status: str,
        updated: str,
        applied: str | None,
        deadline: str | None,
    ) -> None:
        state_hash = self._proposal_hash(
            proposal.setting_key,
            proposal.old_value,
            proposal.proposed_value,
            proposal.reason,
            proposal.evidence_ids,
            proposal.risk_score,
            status,
            proposal.validation,
            proposal.simulation,
        )
        connection.execute(
            "UPDATE self_modification_proposals SET status = ?, state_hash = ?, updated_at = ?, "
            "applied_at = ?, observation_deadline = ? WHERE proposal_id = ?",
            (status, state_hash, updated, applied, deadline, proposal.proposal_id),
        )

    def _revision(
        self,
        connection: Any,
        proposal: SelfModificationRecord,
        action: str,
        old_value: int | float,
        new_value: int | float,
        reason: str,
        created_at: str,
    ) -> None:
        payload = {
            "proposal_id": proposal.proposal_id,
            "setting_key": proposal.setting_key,
            "old_value": old_value,
            "new_value": new_value,
            "action": action,
            "reason": reason,
            "created_at": created_at,
        }
        connection.execute(
            """INSERT INTO self_modification_revisions(
                revision_id, subject_id, proposal_id, setting_key, old_value_json,
                new_value_json, action, reason, state_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                new_id("selfmodrev"),
                self.subject_id,
                proposal.proposal_id,
                proposal.setting_key,
                canonical_json(old_value),
                canonical_json(new_value),
                action,
                reason,
                content_hash(payload),
                created_at,
            ),
        )
