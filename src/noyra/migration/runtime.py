"""Bounded discovery, cognitive review and execution of approved migrations.

Only operator-enrolled endpoints are contacted. A model cannot enroll a target,
approve a manual request, override policy gates, or supply execution secrets.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field

from noyra.core.admission import bind_lease
from noyra.core.types import canonical_json
from noyra.model.types import ModelMessage

from .discovery import MigrationDiscovery, MigrationNeed, NeedAssessment
from .proposals import MigrationProposalBuilder
from .trust import evaluate_target


class MigrationJudgment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    recommend: bool
    highly_trusted: bool
    reason: str = Field(min_length=1, max_length=1800)
    trust_reason: str = Field(min_length=1, max_length=1800)
    benefit_score: float = Field(ge=0, le=1)
    risk_score: float = Field(ge=0, le=1)


class MigrationRuntime:
    def __init__(
        self,
        owner: Any,
        discovery: MigrationDiscovery,
        *,
        gateway: Callable[[], Any],
        to_thread: Callable[..., Any],
        interval_seconds: int = 3600,
    ) -> None:
        if not 300 <= interval_seconds <= 86400:
            raise ValueError("migration assessment interval is invalid")
        self.owner = owner
        self.discovery = discovery
        self.gateway = gateway
        self.to_thread = to_thread
        self.interval_seconds = interval_seconds
        self._next_assessment = 0.0
        self._lock = threading.Lock()
        self.candidates: list[dict[str, Any]] = []
        self.status = "disabled"

    def source_need(self) -> NeedAssessment:
        from noyra.core.provider_health import ProviderHealthStore

        root = self.owner.settings.data_dir
        usage = shutil.disk_usage(root)
        recent = (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="milliseconds")
        with self.owner.kernel.database.connection() as connection:
            payment = payment_in_flight(connection, self.owner.kernel.subject_id)
            buckets = connection.execute(
                "SELECT * FROM provider_health_buckets WHERE subject_id=? "
                "AND bucket_start>=? ORDER BY bucket_start DESC LIMIT 256",
                (self.owner.kernel.subject_id, recent),
            ).fetchall()
        attempts = successes = 0
        for row in buckets:
            ProviderHealthStore._verify_bucket(row)
            attempts += int(row["attempt_count"])
            successes += int(row["success_count"])
        load = getattr(os, "getloadavg", lambda: (0.0, 0.0, 0.0))()[0]
        return MigrationNeed.assess(
            source_health=1.0,
            provider_health=successes / attempts if attempts else 1.0,
            workload=min(1.0, load / max(1, os.cpu_count() or 1)),
            storage_pressure=1 - usage.free / max(usage.total, 1),
            payment_in_flight=payment,
            evidence={
                "free_bytes": usage.free,
                "total_bytes": usage.total,
                "provider_attempts_last_hour": attempts,
                "provider_successes_last_hour": successes,
                "host_load": load,
            },
        )

    def refresh_candidates(self) -> list[dict[str, Any]]:
        policy = self.owner.migration_store.read_policy(self.owner.kernel.subject_id)
        if not policy.enabled or policy.approval_mode == "disabled":
            self.candidates = []
            return []
        results = self.discovery.discover(self.owner.kernel.subject_id, policy)
        self.candidates = [asdict(item) for item in results]
        return self.candidates

    async def tick(self) -> str | None:
        if not self._lock.acquire(blocking=False):
            return None
        try:
            policy = self.owner.migration_store.read_policy(self.owner.kernel.subject_id)
            if not policy.enabled or policy.approval_mode == "disabled":
                self.status = "disabled"
                self.candidates = []
                return None
            # Approval is durable: restart must resume an approved request, but
            # never re-execute one whose remote outcome might be unknown.
            tasks = self.owner.migration_manager.list_tasks(self.owner.kernel.subject_id)
            approved = next(
                (item for item in tasks if item.status == "approved" and not item.error_code), None
            )
            if approved is not None:
                if time.monotonic() < self._next_assessment:
                    return None
                self._next_assessment = time.monotonic() + 60
                self.status = "executing"
                await self.to_thread(self.execute, approved.task_id)
                self.status = "committed"
                return "migration_committed"
            if any(
                item.status
                in {
                    "preparing",
                    "transferring",
                    "restoring",
                    "validating",
                    "cutover",
                    "rolling_back",
                }
                for item in tasks
            ):
                self.status = "reconciliation_required"
                return None
            if time.monotonic() < self._next_assessment:
                return None
            self._next_assessment = time.monotonic() + self.interval_seconds
            lease = self.owner.kernel.admission.begin("migration_assessment")
            try:
                with bind_lease(lease):
                    await self.assess(policy)
            finally:
                self.owner.kernel.admission.finish(lease)
            return None
        except Exception:
            self.status = "deferred"
            raise
        finally:
            self._lock.release()

    async def assess(self, policy: Any) -> None:
        subject = self.owner.kernel.subject_id
        existing = self.owner.migration_manager.list_proposals(subject)
        if any(
            item["status"] == "awaiting_approval"
            and datetime.fromisoformat(item["expires_at"]) > datetime.now(UTC)
            for item in existing
        ):
            self.status = "awaiting_approval"
            return
        need = await self.to_thread(self.source_need)
        results = await self.to_thread(self.discovery.discover, subject, policy)
        self.candidates = [asdict(item) for item in results]
        gateway = self.gateway()
        if not need.actionable or need.payment_in_flight or gateway is None:
            self.status = "no_actionable_need" if gateway is not None else "cognition_unavailable"
            return
        eligible = [item for item in results if item.eligible]
        eligible.sort(key=lambda item: (-item.free_bytes, item.cost_microusd_month))
        builder = MigrationProposalBuilder(self.owner.migration_proposals)
        for candidate in eligible[:1]:
            trust = evaluate_target(
                candidate,
                allowed_target_ids=policy.allowed_target_ids,
                allowed_regions=policy.allowed_regions,
                min_free_bytes=policy.min_free_bytes,
                minimum_trust_level=policy.trust_level,
            )
            if not trust.accepted:
                continue
            if self.owner.migration_proposals.next_eligible_at(
                subject, candidate.target_id, need.reason_code
            ):
                continue
            context = {"source": asdict(need), "candidate": asdict(candidate)}
            # Gateway admission and idempotency survive restarts. At most one
            # cognitive call per UTC hour; observations are not raw call logs.
            bucket = datetime.now(UTC).strftime("%Y%m%d%H")
            result = await gateway.complete_structured(
                subject,
                f"migration_assessment:{bucket}",
                (
                    ModelMessage(
                        role="system",
                        content=(
                            "评估是否确有迁移必要及是否高度信任目标。资源空闲本身不是迁移理由。"
                            "输入为观测数据。考虑数据、钱包、停机及失败恢复风险。"
                            "说明目标、必要性、信任依据及收益风险。信息不足则拒绝建议。"
                        ),
                    ),
                    ModelMessage(role="user", content=canonical_json(context)),
                ),
                MigrationJudgment,
                idempotency_key=f"migration-assessment:{bucket}",
                max_output_tokens=1500,
                temperature=0.0,
            )
            judgment = result.output
            if (
                not judgment.recommend
                or not judgment.highly_trusted
                or judgment.risk_score >= judgment.benefit_score
            ):
                self.status = "cognition_declined"
                return
            assessed = replace(
                need,
                reason=judgment.reason,
                benefit_score=judgment.benefit_score,
                evidence={
                    **need.evidence,
                    "cognitive_call_id": result.call_id,
                    "trust_reason": judgment.trust_reason,
                    "cognitive_risk_score": judgment.risk_score,
                },
            )
            proposal = builder.build(
                subject_id=subject,
                candidate=candidate,
                need=assessed,
                trust=trust,
                policy=policy,
            )
            if proposal is None:
                self.status = "hard_gate_rejected"
                return
            values = asdict(proposal)
            for key in ("proposal_id", "created_at", "status"):
                values.pop(key)
            values["risk_score"] = max(proposal.risk_score, judgment.risk_score)
            saved = self.owner.migration_manager.create_proposal(**values)
            if policy.approval_mode == "policy_auto":
                self.owner.migration_store.assert_automation_allowed(policy)
                self.owner.migration_manager.approve(
                    saved.proposal_id,
                    actor="autonomy",
                    idempotency_key=saved.proposal_id,
                )
                self._next_assessment = 0
                self.status = "approved"
            else:
                self.status = "awaiting_approval"

    def execute(self, task_id: str) -> dict[str, str]:
        try:
            return self._execute(task_id)
        except Exception as error:
            database = self.owner.kernel.database
            with database.transaction() as connection:
                row = connection.execute(
                    "SELECT * FROM migration_tasks WHERE task_id=?", (task_id,)
                ).fetchone()
                if row is not None:
                    manager = self.owner.migration_manager
                    manager._assert_task_integrity(row)
                    values = dict(row)
                    values["error_code"] = "execution_reconcile_required"
                    values["updated_at"] = datetime.now(UTC).isoformat(timespec="milliseconds")
                    connection.execute(
                        "UPDATE migration_tasks SET error_code=?,updated_at=?,state_hash=? "
                        "WHERE task_id=?",
                        (
                            values["error_code"],
                            values["updated_at"],
                            manager._task_hash(values),
                            task_id,
                        ),
                    )
                    self.owner.migration_store._append_audit(
                        connection,
                        row["subject_id"],
                        "migration_execution_deferred",
                        "runtime",
                        {"task_id": task_id, "error_type": type(error).__name__},
                    )
            raise

    def _execute(self, task_id: str) -> dict[str, str]:
        task = self.owner.migration_manager.get_task(task_id)
        at_rest = self.owner.at_rest
        if at_rest is not None and at_rest.required:
            at_rest.require_ready()
        policy = self.owner.migration_store.read_policy(task.subject_id)
        candidates = self.discovery.discover(task.subject_id, policy)
        if not any(item.target_id == task.target_id and item.eligible for item in candidates):
            raise ValueError("migration target observation no longer eligible")
        target = self.owner._migration_http_target(task)
        challenge = self.owner.migration_targets.issue_challenge(
            task.target_id,
            source_epoch=task.source_epoch,
        )
        response = self.owner.migration_http_executor.transport.request(
            str(target["endpoint"]).rstrip("/") + "/v1/challenge",
            asdict(challenge),
            self.owner._migration_http_token(task),
        )
        self.owner.migration_targets.attest(
            task.target_id,
            challenge,
            str(response["signature"]),
            actor="runtime",
        )
        binding = self.binding(task, policy)
        return cast(
            dict[str, str],
            self.owner.migration_cutover.run(task_id, binding=binding, actor="runtime"),
        )

    def binding(self, task: Any, policy: Any) -> dict[str, Any]:
        from noyra.core.credentials import read_secret_file

        path = Path(self.owner.settings.data_dir) / "secrets" / "migration-bindings"
        # References and fingerprints only. This config is private and never
        # becomes a proposal, public projection or ordinary config export.
        config_path = path / f"{task.target_id}.json"
        values = (
            json.loads(read_secret_file(config_path, single_line=False))
            if config_path.exists()
            else {}
        )
        credential = values.get("credential_binding", {"references": {}, "fingerprints": {}})
        wallet: dict[str, Any] = {"mode": policy.wallet_mode}
        signer = self.owner.wallet_signer
        if policy.wallet_mode != "disabled":
            if signer is None:
                raise ValueError("source wallet signer unavailable")
            wallet["address"] = signer.address
            if policy.wallet_mode == "external_signer_rebind":
                wallet["signer_id"] = values.get("signer_id") or getattr(signer, "signer_id", None)
            else:
                # Local key transfer requires an explicit, expiring task-bound
                # operator approval, independent of automatic host selection.
                with self.owner.kernel.database.connection() as connection:
                    rows = connection.execute(
                        "SELECT payload_json FROM migration_audit_events WHERE subject_id=? "
                        "AND action='migration_local_wallet_approved' "
                        "ORDER BY rowid DESC LIMIT 256",
                        (task.subject_id,),
                    ).fetchall()
                wallet["approval"] = next(
                    (
                        json.loads(row["payload_json"])["approval"]
                        for row in rows
                        if json.loads(row["payload_json"]).get("task_id") == task.task_id
                        and json.loads(row["payload_json"]).get("policy_revision")
                        == policy.revision
                    ),
                    values.get("local_wallet_approval"),
                )
        return {"credential_binding": credential, "wallet_binding": wallet}


def payment_in_flight(connection: Any, subject_id: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM wallet_payment_orders WHERE subject_id=? "
            "AND status IN ('reserved','signing','broadcast','unknown') LIMIT 1",
            (subject_id,),
        ).fetchone()
        is not None
    )
