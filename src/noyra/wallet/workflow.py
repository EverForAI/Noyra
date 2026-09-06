from __future__ import annotations

# The workflow deliberately keeps SQL at the transaction boundary.
# ruff: noqa: E501, B905
from collections.abc import Callable
from typing import Any

from noyra.core.database import Database
from noyra.core.errors import IntegrityError, InvalidTransitionError, NotFoundError
from noyra.core.identity import validate_subject_id
from noyra.core.types import canonical_json, content_hash, new_id, strict_json_loads, utc_now
from noyra.interaction.inbound import InboundAccepted
from noyra.interaction.posts import PublicPostStore
from noyra.interaction.types import PublicPostInput

from .economy import WalletEconomyStore
from .economy_types import BountyInput, SubmissionInput
from .execution import (
    WalletChainReorganizationError,
    WalletExecutionError,
    WalletPaymentExecutionEngine,
)
from .workflow_types import (
    RewardAdvanceResult,
    RewardEvidenceDecisionInput,
    RewardInboundSubmissionPayload,
    RewardIncidentRecord,
    RewardIncidentResolutionInput,
    RewardPublicSubmissionInput,
    RewardSubmissionLinkRecord,
    RewardWorkflowInput,
    RewardWorkflowRecord,
)


class WalletRewardWorkflow:
    """Transactional supervisor joining projects, channels and wallet execution."""

    def __init__(
        self,
        database: Database,
        *,
        economy: WalletEconomyStore | None = None,
        posts: PublicPostStore | None = None,
        execution: WalletPaymentExecutionEngine | None = None,
        clock: Callable[[], str] = utc_now,
    ) -> None:
        self.database = database
        self.economy = economy or WalletEconomyStore(database)
        self.posts = posts or PublicPostStore(database)
        self.execution = execution
        self.clock = clock

    def create(
        self,
        subject_id: str,
        proposal: RewardWorkflowInput,
        *,
        actor: str,
    ) -> RewardWorkflowRecord:
        validate_subject_id(subject_id)
        actor = self.economy._operator(actor)
        if not isinstance(proposal, RewardWorkflowInput):
            raise TypeError("reward workflow input is invalid")
        now = self.clock()
        with self.database.transaction() as c:
            existing = c.execute(
                "SELECT * FROM wallet_reward_workflows WHERE subject_id=? AND idempotency_key=?",
                (subject_id, proposal.idempotency_key),
            ).fetchone()
            if existing is not None:
                if not self._workflow_input_matches(c, existing, proposal):
                    raise ValueError("reward workflow idempotency key identifies different input")
                return self._workflow_from_row(existing)
            request = c.execute(
                "SELECT request_id,project_id,phase_id,status FROM autonomous_project_assistance_requests "
                "WHERE request_id=? AND subject_id=?",
                (proposal.assistance_request_id, subject_id),
            ).fetchone()
            if request is None or request["status"] != "open":
                raise ValueError("assistance request is not open")
            project = c.execute(
                "SELECT project_id,goal_id,subject_id FROM autonomous_projects WHERE project_id=?",
                (request["project_id"],),
            ).fetchone()
            phase = c.execute(
                "SELECT phase_id,project_id,subject_id FROM autonomous_project_phases WHERE phase_id=?",
                (request["phase_id"],),
            ).fetchone()
            if (
                project is None
                or phase is None
                or project["subject_id"] != subject_id
                or phase["subject_id"] != subject_id
                or phase["project_id"] != project["project_id"]
            ):
                raise IntegrityError("assistance request project reference is invalid")
            project_id = str(project["project_id"])
            phase_id = str(phase["phase_id"])
            goal_id = str(project["goal_id"])
            title = f"{self._request_title(c, proposal.assistance_request_id)} reward"
            summary = self._request_summary(c, proposal.assistance_request_id)
            return self._create_workflow_connection(
                c,
                subject_id,
                proposal,
                actor=actor,
                now=now,
                project_id=project_id,
                phase_id=phase_id,
                goal_id=goal_id,
                title=title,
                summary=summary,
            )

    def _create_workflow_connection(
        self,
        c: Any,
        subject_id: str,
        proposal: RewardWorkflowInput,
        *,
        actor: str,
        now: str,
        project_id: str,
        phase_id: str,
        goal_id: str,
        title: str,
        summary: str,
    ) -> RewardWorkflowRecord:
        bounty = self.economy.create_bounty(
            subject_id,
            BountyInput(
                title=title,
                description=summary,
                acceptance_criteria=proposal.acceptance_criteria,
                network_id=proposal.network_id,
                asset_id=proposal.asset_id,
                reward_amount=proposal.reward_amount,
                opens_at=proposal.opens_at,
                expires_at=proposal.expires_at,
                max_submissions=proposal.max_submissions,
                reward_slots=proposal.reward_slots,
                project_id=project_id,
                goal_id=goal_id,
                idempotency_key=f"workflow-bounty:{proposal.idempotency_key}",
            ),
            actor=actor,
            _connection=c,
        )
        post = self.posts.create(
            subject_id,
            PublicPostInput(
                kind="help_request",
                title=bounty.title,
                content=bounty.description,
                author_label="Noyra",
            ),
            idempotency_key=f"workflow-post:{proposal.idempotency_key}",
            author_provenance="subject",
            _connection=c,
        )
        workflow_id = new_id("reward")
        existing = c.execute(
            "SELECT * FROM wallet_reward_workflows WHERE subject_id=? AND idempotency_key=?",
            (subject_id, proposal.idempotency_key),
        ).fetchone()
        if existing is not None:
            if not self._workflow_input_matches(c, existing, proposal):
                raise ValueError("reward workflow idempotency key identifies different input")
            return self._workflow_from_row(existing)
        audit_id = self.economy._audit(
            c,
            subject_id,
            "wallet_reward_workflow_created",
            actor,
            {
                "workflow_id": workflow_id,
                "assistance_request_id": proposal.assistance_request_id,
                "bounty_id": bounty.bounty_id,
                "post_id": post.post_id,
            },
        )
        payload = self._workflow_hash_payload(
            workflow_id,
            subject_id,
            proposal.assistance_request_id,
            project_id,
            phase_id,
            goal_id,
            post.post_id,
            bounty.bounty_id,
            proposal.idempotency_key,
            "awaiting_publication",
            None,
        )
        c.execute(
            "INSERT INTO wallet_reward_workflows(workflow_id,subject_id,assistance_request_id,"
            "project_id,phase_id,goal_id,post_id,bounty_id,idempotency_key,status,manual_reason,"
            "state_hash,created_audit_id,last_audit_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'awaiting_publication',NULL,?,?,?,?,?)",
            (
                workflow_id,
                subject_id,
                proposal.assistance_request_id,
                project_id,
                phase_id,
                goal_id,
                post.post_id,
                bounty.bounty_id,
                proposal.idempotency_key,
                content_hash(payload),
                audit_id,
                audit_id,
                now,
                now,
            ),
        )
        return self._workflow_from_row(
            c.execute(
                "SELECT * FROM wallet_reward_workflows WHERE workflow_id=?", (workflow_id,)
            ).fetchone()
        )

    def publish(self, workflow_id: str, subject_id: str, *, actor: str) -> RewardWorkflowRecord:
        actor = self.economy._operator(actor)
        with self.database.transaction() as c:
            row = self._workflow_row(c, workflow_id, subject_id)
            if row["status"] not in {"awaiting_publication", "open"}:
                raise InvalidTransitionError("reward workflow is not publishable")
            post = c.execute(
                "SELECT * FROM public_posts WHERE post_id=? AND subject_id=?",
                (row["post_id"], subject_id),
            ).fetchone()
            if post is None:
                raise IntegrityError("reward workflow post is missing")
            if post["status"] == "pending_review":
                raise ValueError("reward workflow post requires moderation")
            if post["status"] != "published":
                raise InvalidTransitionError("reward workflow post is not published")
            bounty = c.execute(
                "SELECT * FROM wallet_bounties WHERE bounty_id=? AND subject_id=?",
                (row["bounty_id"], subject_id),
            ).fetchone()
            if bounty is None:
                raise IntegrityError("reward workflow bounty is missing")
            if bounty["status"] == "draft":
                self.economy._set_bounty_status(
                    c,
                    bounty,
                    "published",
                    "reward workflow publication approved",
                    actor,
                )
            elif bounty["status"] != "published":
                raise InvalidTransitionError("reward workflow bounty is not publishable")
            return self._transition_workflow(c, row, "open", actor, "public help request published")

    def submit_public(
        self,
        workflow_id: str,
        subject_id: str,
        proposal: RewardPublicSubmissionInput,
        *,
        client_ip: str | None = None,
    ) -> RewardAdvanceResult:
        validate_subject_id(subject_id)
        with self.database.transaction() as c:
            workflow = self._workflow_row(c, workflow_id, subject_id)
            if workflow["status"] != "open":
                raise ValueError("reward workflow is not open")
            bounty = c.execute(
                "SELECT * FROM wallet_bounties WHERE bounty_id=?", (workflow["bounty_id"],)
            ).fetchone()
            if bounty is None or bounty["network_id"] != proposal.network_id:
                mismatch = True
                bounty_id = str(workflow["bounty_id"])
            else:
                mismatch = False
                bounty_id = str(workflow["bounty_id"])
            if mismatch:
                return self._incident_result(
                    c,
                    workflow,
                    None,
                    "source_mismatch",
                    "submission network does not match bounty",
                    f"network:{proposal.idempotency_key}",
                )
            submission = self.economy.submit_to_bounty(
                bounty_id,
                subject_id,
                SubmissionInput(
                    counterparty=proposal.counterparty,
                    content=proposal.content,
                    evidence=proposal.evidence,
                    recipient_address=proposal.recipient_address,
                    idempotency_key=proposal.idempotency_key,
                    consent_version=proposal.consent_version,
                ),
                client_ip=client_ip,
                _connection=c,
            )
            link = self._link_submission(
                c,
                workflow,
                submission.submission_id,
                source_type="public_form",
                source_content_hash=content_hash(proposal.content),
                claimed_network_id=proposal.network_id,
                actor="visitor",
            )
            return RewardAdvanceResult(
                self._workflow_from_row(workflow), link, None, None, None, None
            )

    def submit_inbound(
        self,
        accepted: InboundAccepted,
        *,
        external_counterparty: str,
    ) -> RewardAdvanceResult:
        with self.database.transaction() as c:
            event = c.execute(
                "SELECT * FROM interaction_inbound_events WHERE event_id=? AND interaction_id=? AND status='processed'",
                (accepted.event_id, accepted.interaction_id),
            ).fetchone()
            if event is None:
                raise IntegrityError("inbound reward event is not processed")
            interaction = c.execute(
                "SELECT * FROM interactions WHERE interaction_id=?", (accepted.interaction_id,)
            ).fetchone()
            if interaction is None:
                raise IntegrityError("inbound reward interaction is missing")
            if str(event["content_hash"]) != str(interaction["content_hash"]):
                raise IntegrityError("inbound reward content hash mismatch")
            try:
                payload = RewardInboundSubmissionPayload.model_validate(
                    strict_json_loads(interaction["content"])
                )
            except Exception as error:
                raise ValueError("inbound content is not a reward submission envelope") from error
            workflow = self._workflow_row(c, payload.workflow_id, event["subject_id"])
            if workflow["status"] != "open":
                raise ValueError("reward workflow is not open")
            bounty = c.execute(
                "SELECT * FROM wallet_bounties WHERE bounty_id=?", (workflow["bounty_id"],)
            ).fetchone()
            if bounty is None or bounty["network_id"] != payload.network_id:
                return self._incident_result(
                    c,
                    workflow,
                    None,
                    "source_mismatch",
                    "inbound network does not match bounty",
                    f"inbound-network:{accepted.event_id}",
                )
            subject_id = str(event["subject_id"])
            bounty_id = str(workflow["bounty_id"])
            source_content_hash = str(event["content_hash"])
            interaction_content = str(interaction["content"])
            submission = self.economy.submit_to_bounty(
                bounty_id,
                subject_id,
                SubmissionInput(
                    counterparty=external_counterparty,
                    content=interaction_content,
                    evidence=payload.evidence,
                    recipient_address=payload.recipient_address,
                    idempotency_key=payload.idempotency_key,
                    consent_version=payload.consent_version,
                ),
                _connection=c,
            )
            link = self._link_submission(
                c,
                workflow,
                submission.submission_id,
                source_type="inbound_event",
                inbound_event_id=accepted.event_id,
                interaction_id=accepted.interaction_id,
                source_content_hash=source_content_hash,
                claimed_network_id=payload.network_id,
                actor="verified-channel",
            )
            return RewardAdvanceResult(
                self._workflow_from_row(workflow), link, None, None, None, None
            )

    def decide(
        self,
        submission_id: str,
        subject_id: str,
        proposal: RewardEvidenceDecisionInput,
        *,
        actor: str,
    ) -> RewardAdvanceResult:
        actor = self.economy._operator(actor)
        with self.database.transaction() as c:
            link_row = c.execute(
                "SELECT * FROM wallet_reward_submission_links WHERE submission_id=? AND subject_id=?",
                (submission_id, subject_id),
            ).fetchone()
            if link_row is None:
                raise NotFoundError("reward submission link not found")
            workflow = self._workflow_row(c, link_row["workflow_id"], subject_id)
            criteria = strict_json_loads(
                c.execute(
                    "SELECT acceptance_criteria_json FROM wallet_bounties WHERE bounty_id=?",
                    (workflow["bounty_id"],),
                ).fetchone()[0]
            )
            if len(proposal.criteria_results) != len(criteria):
                return self._incident_result(
                    c,
                    workflow,
                    submission_id,
                    "evidence_mismatch",
                    "criteria result count does not match bounty",
                    f"criteria:{proposal.idempotency_key}",
                )
            verified = proposal.accepted and all(proposal.criteria_results)
            target = "accepted" if verified else "rejected"
            existing = c.execute(
                "SELECT * FROM wallet_reward_submission_links WHERE submission_id=?",
                (submission_id,),
            ).fetchone()
            if existing["verification_status"] != "pending_verification":
                if existing["verification_status"] == target:
                    stored = strict_json_loads(existing["criteria_results_json"])
                    audit = c.execute(
                        "SELECT payload_json FROM audit_records WHERE audit_id=?",
                        (existing["decision_audit_id"],),
                    ).fetchone()
                    prior_key = (
                        None
                        if audit is None
                        else strict_json_loads(audit["payload_json"]).get("idempotency_key")
                    )
                    if (
                        stored != list(proposal.criteria_results)
                        or existing["verification_reason"] != proposal.reason
                        or (prior_key is not None and prior_key != proposal.idempotency_key)
                    ):
                        raise ValueError("reward evidence decision idempotency conflict")
                    return self._result_from_link(c, workflow, existing)
                raise InvalidTransitionError("reward evidence is already decided")
            now = self.clock()
            audit_id = self.economy._audit(
                c,
                subject_id,
                f"wallet_reward_submission_{target}",
                actor,
                {
                    "submission_id": submission_id,
                    "reason": proposal.reason,
                    "idempotency_key": proposal.idempotency_key,
                },
            )
            # Keep the provenance link and the bounty submission in the same
            # caller-owned transaction; the link trigger requires matching
            # terminal submission state.
            self.economy._decide_submission_connection(
                c, submission_id, subject_id, accepted=verified, reason=proposal.reason, actor=actor
            )
            state_hash = self._link_hash(
                existing, target, proposal.criteria_results, proposal.reason, actor
            )
            c.execute(
                "UPDATE wallet_reward_submission_links SET verification_status=?,criteria_results_json=?,verification_reason=?,verified_by=?,decision_audit_id=?,state_hash=?,updated_at=? WHERE submission_id=?",
                (
                    target,
                    canonical_json(proposal.criteria_results),
                    proposal.reason,
                    actor,
                    audit_id,
                    state_hash,
                    now,
                    submission_id,
                ),
            )
            link = c.execute(
                "SELECT * FROM wallet_reward_submission_links WHERE submission_id=?",
                (submission_id,),
            ).fetchone()
            order = c.execute(
                "SELECT * FROM wallet_payment_orders WHERE submission_id=?", (submission_id,)
            ).fetchone()
            return RewardAdvanceResult(
                self._workflow_from_row(workflow),
                self._link_from_row(link),
                None if order is None else order["status"],
                None,
                None,
                None,
            )

    def execute_ready(
        self, subject_id: str, *, actor: str, limit: int = 20
    ) -> list[RewardAdvanceResult]:
        validate_subject_id(subject_id)
        actor = self.economy._operator(actor)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid reward execution limit")
        if self.execution is None:
            raise WalletExecutionError("wallet execution is unavailable")
        with self.database.connection() as c:
            rows = c.execute(
                "SELECT l.workflow_id,l.submission_id FROM wallet_reward_submission_links l "
                "JOIN wallet_reward_workflows w ON w.workflow_id=l.workflow_id "
                "JOIN wallet_payment_orders o ON o.submission_id=l.submission_id "
                "WHERE l.subject_id=? AND l.verification_status='accepted' AND w.status='open' "
                "AND o.status IN ('reserved','broadcast','unknown','confirmed') "
                "ORDER BY l.updated_at,l.submission_id LIMIT ?",
                (subject_id, limit),
            ).fetchall()
        results: list[RewardAdvanceResult] = []
        for row in rows:
            # A failed payment or provenance fault transitions the workflow to
            # manual intervention. Do not continue executing a stale snapshot
            # of rows from that workflow in this batch.
            with self.database.connection() as c:
                current_status = c.execute(
                    "SELECT status FROM wallet_reward_workflows "
                    "WHERE workflow_id=? AND subject_id=?",
                    (row["workflow_id"], subject_id),
                ).fetchone()
            if current_status is None:
                raise IntegrityError("wallet reward workflow reference is missing")
            if current_status["status"] != "open":
                continue
            target = self.economy.order_for_submission(row["submission_id"], subject_id)
            if target is None or target.status not in {
                "reserved",
                "broadcast",
                "unknown",
                "confirmed",
            }:
                continue
            try:
                if target.status == "reserved":
                    execution = self.execution.execute_order(
                        target.order_id, subject_id, actor=actor
                    )
                elif target.status == "unknown":
                    # An unknown broadcast may already exist on-chain.  A
                    # scheduler tick may reconcile a receipt, but it must
                    # never create a second signed transaction implicitly.
                    # Re-sending requires the explicit operator retry API,
                    # which rechecks the current pause/policy fence.
                    current = self.execution._order_execution(target.order_id, subject_id)
                    if current is None:
                        raise IntegrityError("wallet reward execution is missing")
                    execution = self.execution.poll_receipt(
                        current.execution_id, subject_id, actor=actor
                    )
                else:
                    current = self.execution._order_execution(target.order_id, subject_id)
                    if current is None:
                        raise IntegrityError("wallet reward execution is missing")
                    execution = (
                        self.execution.poll_receipt(current.execution_id, subject_id, actor=actor)
                        if target.status == "broadcast"
                        else self.execution.verify_confirmed_receipt(
                            current.execution_id, subject_id
                        )
                    )
                results.append(
                    self._result_for_submission(
                        row["workflow_id"],
                        row["submission_id"],
                        subject_id,
                        execution.execution_id,
                        execution.status,
                        complete=target.status == "confirmed",
                    )
                )
                if execution.status in {"failed", "unknown"}:
                    kind = self._execution_incident_kind(execution.error_code, execution.status)
                    with self.database.transaction() as c:
                        workflow = self._workflow_row(c, row["workflow_id"], subject_id)
                        results[-1] = self._incident_result(
                            c,
                            workflow,
                            row["submission_id"],
                            kind,
                            execution.error_code or "wallet execution did not settle",
                            f"execution:{execution.execution_id}:{execution.status}",
                            execution_id=execution.execution_id,
                        )
            except WalletChainReorganizationError as error:
                with self.database.transaction() as c:
                    workflow = self._workflow_row(c, row["workflow_id"], subject_id)
                    execution = c.execute(
                        "SELECT execution_id FROM wallet_payment_executions "
                        "WHERE order_id=? AND subject_id=?",
                        (target.order_id, subject_id),
                    ).fetchone()
                    results.append(
                        self._incident_result(
                            c,
                            workflow,
                            row["submission_id"],
                            "chain_reorganization",
                            str(error),
                            f"chain-reorganization:{target.order_id}",
                            execution_id=None if execution is None else execution["execution_id"],
                        )
                    )
            except Exception as error:
                with self.database.transaction() as c:
                    workflow = self._workflow_row(c, row["workflow_id"], subject_id)
                    results.append(
                        self._incident_result(
                            c,
                            workflow,
                            row["submission_id"],
                            "recovery_mismatch",
                            str(error)[:2000],
                            f"execution:{row['submission_id']}",
                        )
                    )
        return results

    @staticmethod
    def _execution_incident_kind(error_code: str | None, status: str) -> str:
        if error_code == "signer_rejected":
            return "signer_rejection"
        if error_code in {
            "broadcast_unknown",
            "signer_transport_unknown",
            "signer_response_invalid",
        }:
            return "broadcast_unknown"
        if error_code in {"receipt_lookup_unknown", "receipt_invalid", "chain_receipt_failed"}:
            return "receipt_chain_unknown"
        return "recovery_mismatch" if status == "failed" else "payment_unknown"

    def list_workflows(self, subject_id: str, *, limit: int = 100) -> list[RewardWorkflowRecord]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("invalid reward workflow limit")
        with self.database.connection() as c:
            rows = c.execute(
                "SELECT * FROM wallet_reward_workflows WHERE subject_id=? ORDER BY created_at DESC,workflow_id DESC LIMIT ?",
                (subject_id, limit),
            ).fetchall()
        return [self._workflow_from_row(row) for row in rows]

    def incidents(self, subject_id: str, *, limit: int = 100) -> list[RewardIncidentRecord]:
        validate_subject_id(subject_id)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("invalid reward incident limit")
        with self.database.connection() as c:
            rows = c.execute(
                "SELECT * FROM wallet_reward_incidents WHERE subject_id=? ORDER BY created_at DESC,incident_id DESC LIMIT ?",
                (subject_id, limit),
            ).fetchall()
        return [self._incident_from_row(row) for row in rows]

    def resolve_incident(
        self,
        incident_id: str,
        subject_id: str,
        proposal: RewardIncidentResolutionInput,
        *,
        actor: str,
    ) -> RewardIncidentRecord:
        actor = self.economy._operator(actor)
        with self.database.transaction() as c:
            row = c.execute(
                "SELECT * FROM wallet_reward_incidents WHERE incident_id=? AND subject_id=?",
                (incident_id, subject_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("wallet reward incident not found")
            if row["status"] == "resolved":
                audit = c.execute(
                    "SELECT payload_json FROM audit_records WHERE audit_id=?",
                    (row["resolved_audit_id"],),
                ).fetchone()
                prior_key = (
                    None
                    if audit is None
                    else strict_json_loads(audit["payload_json"]).get("idempotency_key")
                )
                if row["resolution"] != proposal.resolution or (
                    prior_key is not None and prior_key != proposal.idempotency_key
                ):
                    raise ValueError("incident resolution idempotency conflict")
                return self._incident_from_row(row)
            audit_id = self.economy._audit(
                c,
                subject_id,
                "wallet_reward_incident_resolved",
                actor,
                {
                    "incident_id": incident_id,
                    "resolution": proposal.resolution,
                    "idempotency_key": proposal.idempotency_key,
                },
            )
            now = self.clock()
            payload = {
                "incident_id": incident_id,
                "subject_id": subject_id,
                "workflow_id": row["workflow_id"],
                "submission_id": row["submission_id"],
                "execution_id": row["execution_id"],
                "kind": row["kind"],
                "idempotency_key": row["idempotency_key"],
                "status": "resolved",
                "reason": row["reason"],
                "resolution": proposal.resolution,
            }
            c.execute(
                "UPDATE wallet_reward_incidents SET status='resolved',resolution=?,resolved_audit_id=?,state_hash=?,updated_at=? WHERE incident_id=?",
                (proposal.resolution, audit_id, content_hash(payload), now, incident_id),
            )
            return self._incident_from_row(
                c.execute(
                    "SELECT * FROM wallet_reward_incidents WHERE incident_id=?", (incident_id,)
                ).fetchone()
            )

    def resume(
        self, workflow_id: str, subject_id: str, *, actor: str, reason: str
    ) -> RewardWorkflowRecord:
        actor = self.economy._operator(actor)
        reason = reason.strip()
        if not reason or len(reason) > 2_000:
            raise ValueError("resume reason is invalid")
        with self.database.transaction() as c:
            row = self._workflow_row(c, workflow_id, subject_id)
            if row["status"] != "manual_intervention":
                raise InvalidTransitionError("reward workflow is not awaiting intervention")
            open_incident = c.execute(
                "SELECT 1 FROM wallet_reward_incidents WHERE workflow_id=? AND subject_id=? AND status='open'",
                (workflow_id, subject_id),
            ).fetchone()
            if open_incident is not None:
                raise InvalidTransitionError("open reward incident must be resolved first")
            return self._transition_workflow(c, row, "open", actor, reason)

    def verify_integrity(self, subject_id: str) -> dict[str, int]:
        validate_subject_id(subject_id)
        with self.database.read_transaction() as c:
            for row in c.execute(
                "SELECT * FROM wallet_reward_workflows WHERE subject_id=?", (subject_id,)
            ):
                if row["state_hash"] != content_hash(self._workflow_hash_payload_from_row(row)):
                    raise IntegrityError(f"reward workflow hash mismatch: {row['workflow_id']}")
                references = c.execute(
                    "SELECT r.project_id,r.phase_id,r.status,p.subject_id AS project_subject, "
                    "p.goal_id AS project_goal_id,ph.subject_id AS phase_subject,ph.project_id AS phase_project "
                    "FROM autonomous_project_assistance_requests r "
                    "JOIN autonomous_projects p ON p.project_id=r.project_id "
                    "JOIN autonomous_project_phases ph ON ph.phase_id=r.phase_id "
                    "WHERE r.request_id=?",
                    (row["assistance_request_id"],),
                ).fetchone()
                expected_request_status = {
                    "closed": "resolved",
                    "cancelled": "withdrawn",
                }.get(row["status"], "open")
                if references is None or references["status"] != expected_request_status:
                    raise IntegrityError(f"reward workflow request mismatch: {row['workflow_id']}")
                if (
                    references["project_id"] != row["project_id"]
                    or references["phase_id"] != row["phase_id"]
                    or references["project_goal_id"] != row["goal_id"]
                    or references["project_subject"] != subject_id
                    or references["phase_subject"] != subject_id
                    or references["phase_project"] != row["project_id"]
                ):
                    raise IntegrityError(
                        f"reward workflow reference mismatch: {row['workflow_id']}"
                    )
            for row in c.execute(
                "SELECT * FROM wallet_reward_submission_links WHERE subject_id=?", (subject_id,)
            ):
                expected = self._link_hash(
                    row,
                    row["verification_status"],
                    None
                    if row["criteria_results_json"] is None
                    else strict_json_loads(row["criteria_results_json"]),
                    row["verification_reason"],
                    row["verified_by"],
                )
                if row["state_hash"] != expected:
                    raise IntegrityError(
                        f"reward submission link hash mismatch: {row['submission_id']}"
                    )
            for row in c.execute(
                "SELECT * FROM wallet_reward_incidents WHERE subject_id=?", (subject_id,)
            ):
                if row["state_hash"] != self._incident_hash(row):
                    raise IntegrityError(f"reward incident hash mismatch: {row['incident_id']}")
                if row["status"] == "resolved" and row["resolved_audit_id"] is None:
                    raise IntegrityError(
                        f"resolved reward incident missing audit: {row['incident_id']}"
                    )
            mismatch = c.execute(
                "SELECT 1 FROM wallet_reward_submission_links l "
                "JOIN wallet_reward_workflows w ON w.workflow_id=l.workflow_id "
                "JOIN wallet_bounty_submissions s ON s.submission_id=l.submission_id "
                "WHERE l.subject_id=? AND (w.subject_id<>l.subject_id OR s.subject_id<>l.subject_id OR s.bounty_id<>w.bounty_id)",
                (subject_id,),
            ).fetchone()
            if mismatch is not None:
                raise IntegrityError("reward provenance subject mismatch")
            incident_mismatch = c.execute(
                "SELECT 1 FROM wallet_reward_incidents i "
                "JOIN wallet_reward_workflows w ON w.workflow_id=i.workflow_id "
                "LEFT JOIN wallet_bounty_submissions s ON s.submission_id=i.submission_id "
                "LEFT JOIN wallet_payment_executions e ON e.execution_id=i.execution_id "
                "WHERE i.subject_id=? AND (w.subject_id<>i.subject_id "
                "OR (s.submission_id IS NOT NULL AND s.subject_id<>i.subject_id) "
                "OR (e.execution_id IS NOT NULL AND e.subject_id<>i.subject_id) "
                "OR (s.submission_id IS NOT NULL AND s.bounty_id<>w.bounty_id))",
                (subject_id,),
            ).fetchone()
            if incident_mismatch is not None:
                raise IntegrityError("reward incident provenance mismatch")
            return {
                "wallet_reward_workflows": c.execute(
                    "SELECT count(*) FROM wallet_reward_workflows WHERE subject_id=?", (subject_id,)
                ).fetchone()[0],
                "wallet_reward_submission_links": c.execute(
                    "SELECT count(*) FROM wallet_reward_submission_links WHERE subject_id=?",
                    (subject_id,),
                ).fetchone()[0],
                "wallet_reward_incidents": c.execute(
                    "SELECT count(*) FROM wallet_reward_incidents WHERE subject_id=?", (subject_id,)
                ).fetchone()[0],
            }

    def _workflow_input_matches(self, c: Any, row: Any, proposal: RewardWorkflowInput) -> bool:
        if row["assistance_request_id"] != proposal.assistance_request_id:
            return False
        bounty = c.execute(
            "SELECT acceptance_criteria_json,network_id,asset_id,reward_amount,opens_at,expires_at,"
            "max_submissions,reward_slots FROM wallet_bounties WHERE bounty_id=?",
            (row["bounty_id"],),
        ).fetchone()
        return bounty is not None and (
            row["idempotency_key"] == proposal.idempotency_key
            and strict_json_loads(bounty["acceptance_criteria_json"])
            == proposal.acceptance_criteria
            and bounty["network_id"] == proposal.network_id
            and bounty["asset_id"] == proposal.asset_id
            and bounty["reward_amount"] == proposal.reward_amount
            and bounty["opens_at"] == proposal.opens_at
            and bounty["expires_at"] == proposal.expires_at
            and int(bounty["max_submissions"]) == proposal.max_submissions
            and int(bounty["reward_slots"]) == proposal.reward_slots
        )

    @staticmethod
    def _request_title(c: Any, request_id: str) -> str:
        return str(
            c.execute(
                "SELECT title FROM autonomous_project_assistance_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()[0]
        )

    @staticmethod
    def _request_summary(c: Any, request_id: str) -> str:
        return str(
            c.execute(
                "SELECT public_summary FROM autonomous_project_assistance_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()[0]
        )

    @staticmethod
    def _workflow_row(c: Any, workflow_id: str, subject_id: str) -> Any:
        row = c.execute(
            "SELECT * FROM wallet_reward_workflows WHERE workflow_id=? AND subject_id=?",
            (workflow_id, subject_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("wallet reward workflow not found")
        return row

    def _transition_workflow(
        self, c: Any, row: Any, target: str, actor: str, reason: str
    ) -> RewardWorkflowRecord:
        if row["status"] == target:
            return self._workflow_from_row(row)
        allowed = {
            "awaiting_publication": {"open", "cancelled", "manual_intervention"},
            "open": {"closed", "cancelled", "manual_intervention"},
            "manual_intervention": {"open", "closed", "cancelled"},
        }
        if target not in allowed.get(row["status"], set()):
            raise InvalidTransitionError("reward workflow transition is invalid")
        now = self.clock()
        audit_id = self.economy._audit(
            c,
            row["subject_id"],
            f"wallet_reward_workflow_{target}",
            actor,
            {"workflow_id": row["workflow_id"], "reason": reason},
        )
        manual_reason = reason if target == "manual_intervention" else None
        state_hash = content_hash(
            self._workflow_hash_payload_from_row(row, status=target, manual_reason=manual_reason)
        )
        c.execute(
            "UPDATE wallet_reward_workflows SET status=?,manual_reason=?,state_hash=?,last_audit_id=?,updated_at=? WHERE workflow_id=?",
            (target, manual_reason, state_hash, audit_id, now, row["workflow_id"]),
        )
        return self._workflow_from_row(
            c.execute(
                "SELECT * FROM wallet_reward_workflows WHERE workflow_id=?", (row["workflow_id"],)
            ).fetchone()
        )

    def _link_submission(
        self,
        c: Any,
        workflow: Any,
        submission_id: str,
        *,
        source_type: str,
        source_content_hash: str,
        claimed_network_id: str,
        actor: str,
        inbound_event_id: str | None = None,
        interaction_id: str | None = None,
    ) -> RewardSubmissionLinkRecord:
        existing = c.execute(
            "SELECT * FROM wallet_reward_submission_links WHERE submission_id=?", (submission_id,)
        ).fetchone()
        if existing is not None:
            if (
                existing["workflow_id"] != workflow["workflow_id"]
                or existing["subject_id"] != workflow["subject_id"]
                or existing["source_type"] != source_type
                or existing["inbound_event_id"] != inbound_event_id
                or existing["interaction_id"] != interaction_id
                or existing["claimed_network_id"] != claimed_network_id
                or existing["source_content_hash"] != source_content_hash
            ):
                raise IntegrityError("reward submission provenance conflict")
            return self._link_from_row(existing)
        now = self.clock()
        audit_id = self.economy._audit(
            c,
            workflow["subject_id"],
            "wallet_reward_submission_linked",
            actor,
            {
                "submission_id": submission_id,
                "workflow_id": workflow["workflow_id"],
                "source_type": source_type,
            },
        )
        payload = {
            "submission_id": submission_id,
            "workflow_id": workflow["workflow_id"],
            "subject_id": workflow["subject_id"],
            "source_type": source_type,
            "inbound_event_id": inbound_event_id,
            "interaction_id": interaction_id,
            "claimed_network_id": claimed_network_id,
            "source_content_hash": source_content_hash,
            "verification_status": "pending_verification",
            "criteria_results_json": None,
            "verification_reason": None,
            "verified_by": None,
        }
        c.execute(
            "INSERT INTO wallet_reward_submission_links(submission_id,workflow_id,subject_id,source_type,inbound_event_id,interaction_id,claimed_network_id,source_content_hash,verification_status,criteria_results_json,verification_reason,verified_by,created_audit_id,decision_audit_id,state_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?, 'pending_verification',NULL,NULL,NULL,?,NULL,?,?,?)",
            (
                submission_id,
                workflow["workflow_id"],
                workflow["subject_id"],
                source_type,
                inbound_event_id,
                interaction_id,
                claimed_network_id,
                source_content_hash,
                audit_id,
                content_hash(payload),
                now,
                now,
            ),
        )
        return self._link_from_row(
            c.execute(
                "SELECT * FROM wallet_reward_submission_links WHERE submission_id=?",
                (submission_id,),
            ).fetchone()
        )

    def _incident_result(
        self,
        c: Any,
        workflow: Any,
        submission_id: str | None,
        kind: str,
        reason: str,
        key: str,
        *,
        execution_id: str | None = None,
    ) -> RewardAdvanceResult:
        incident = self._open_incident(
            c,
            workflow,
            submission_id,
            kind,
            reason,
            key,
            execution_id=execution_id,
        )
        workflow = self._workflow_row(c, workflow["workflow_id"], workflow["subject_id"])
        link = (
            c.execute(
                "SELECT * FROM wallet_reward_submission_links WHERE submission_id=?",
                (submission_id,),
            ).fetchone()
            if submission_id
            else None
        )
        order = (
            c.execute(
                "SELECT * FROM wallet_payment_orders WHERE submission_id=? AND subject_id=?",
                (submission_id, workflow["subject_id"]),
            ).fetchone()
            if submission_id
            else None
        )
        execution = (
            c.execute(
                "SELECT * FROM wallet_payment_executions WHERE execution_id=? AND subject_id=?",
                (execution_id, workflow["subject_id"]),
            ).fetchone()
            if execution_id
            else None
        )
        return RewardAdvanceResult(
            self._workflow_from_row(workflow),
            self._link_from_row(link) if link else None,
            None if order is None else str(order["status"]),
            None if execution is None else str(execution["execution_id"]),
            None if execution is None else str(execution["status"]),
            incident,
        )

    def _open_incident(
        self,
        c: Any,
        workflow: Any,
        submission_id: str | None,
        kind: str,
        reason: str,
        key: str,
        *,
        execution_id: str | None = None,
    ) -> RewardIncidentRecord:
        key = "incident:" + content_hash(
            {
                "workflow_id": workflow["workflow_id"],
                "workflow_revision": workflow["last_audit_id"],
                "cause": key,
            }
        )
        existing = c.execute(
            "SELECT * FROM wallet_reward_incidents WHERE subject_id=? AND idempotency_key=?",
            (workflow["subject_id"], key),
        ).fetchone()
        if existing is not None:
            if (
                existing["workflow_id"] != workflow["workflow_id"]
                or existing["subject_id"] != workflow["subject_id"]
                or existing["submission_id"] != submission_id
                or existing["execution_id"] != execution_id
                or existing["kind"] != kind
                or existing["reason"] != reason
            ):
                raise ValueError("reward incident idempotency conflict")
            return self._incident_from_row(existing)
        now = self.clock()
        audit_id = self.economy._audit(
            c,
            workflow["subject_id"],
            "wallet_reward_incident_opened",
            "system",
            {"workflow_id": workflow["workflow_id"], "kind": kind, "reason": reason},
        )
        incident_id = new_id("incident")
        payload = {
            "incident_id": incident_id,
            "subject_id": workflow["subject_id"],
            "workflow_id": workflow["workflow_id"],
            "submission_id": submission_id,
            "execution_id": execution_id,
            "kind": kind,
            "idempotency_key": key,
            "status": "open",
            "reason": reason,
            "resolution": None,
        }
        c.execute(
            "INSERT INTO wallet_reward_incidents(incident_id,subject_id,workflow_id,submission_id,execution_id,kind,idempotency_key,status,reason,resolution,opened_audit_id,resolved_audit_id,state_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?, 'open',?,NULL,?,NULL,?,?,?)",
            (
                incident_id,
                workflow["subject_id"],
                workflow["workflow_id"],
                submission_id,
                execution_id,
                kind,
                key,
                reason,
                audit_id,
                content_hash(payload),
                now,
                now,
            ),
        )
        self._transition_workflow(c, workflow, "manual_intervention", "system", reason)
        return self._incident_from_row(
            c.execute(
                "SELECT * FROM wallet_reward_incidents WHERE incident_id=?", (incident_id,)
            ).fetchone()
        )

    def _result_for_submission(
        self,
        workflow_id: str,
        submission_id: str,
        subject_id: str,
        execution_id: str,
        status: str,
        *,
        complete: bool,
    ) -> RewardAdvanceResult:
        with self.database.transaction() as c:
            workflow = self._workflow_row(c, workflow_id, subject_id)
            if status == "confirmed" and complete:
                workflow = self._complete_if_settled(c, workflow, actor="system")
            link = c.execute(
                "SELECT * FROM wallet_reward_submission_links WHERE submission_id=?",
                (submission_id,),
            ).fetchone()
            order = c.execute(
                "SELECT status FROM wallet_payment_orders WHERE submission_id=?", (submission_id,)
            ).fetchone()
            return RewardAdvanceResult(
                self._workflow_from_row(workflow),
                self._link_from_row(link),
                None if order is None else order[0],
                execution_id,
                status,
                None,
            )

    def _complete_if_settled(self, c: Any, workflow: Any, *, actor: str) -> Any:
        bounty = c.execute(
            "SELECT * FROM wallet_bounties WHERE bounty_id=? AND subject_id=?",
            (workflow["bounty_id"], workflow["subject_id"]),
        ).fetchone()
        if bounty is None:
            raise IntegrityError("reward workflow bounty is missing")
        settled = c.execute(
            "SELECT count(*) FROM wallet_reward_submission_links l "
            "JOIN wallet_payment_orders o ON o.submission_id=l.submission_id "
            "WHERE l.workflow_id=? AND l.subject_id=? "
            "AND l.verification_status='accepted' AND o.status='confirmed'",
            (workflow["workflow_id"], workflow["subject_id"]),
        ).fetchone()[0]
        if int(settled) < int(bounty["reward_slots"]):
            return workflow
        if workflow["status"] == "open":
            self._transition_workflow(c, workflow, "closed", actor, "all reward slots settled")
            workflow = self._workflow_row(c, workflow["workflow_id"], workflow["subject_id"])
        elif workflow["status"] != "closed":
            raise InvalidTransitionError("settled reward workflow cannot be completed")
        if bounty["status"] == "published":
            self.economy._set_bounty_status(c, bounty, "closed", "all reward slots settled", actor)
        elif bounty["status"] != "closed":
            raise IntegrityError("settled reward workflow bounty status is invalid")
        request = c.execute(
            "SELECT * FROM autonomous_project_assistance_requests "
            "WHERE request_id=? AND subject_id=?",
            (workflow["assistance_request_id"], workflow["subject_id"]),
        ).fetchone()
        if request is None:
            raise IntegrityError("reward workflow assistance request is missing")
        if request["status"] == "open":
            now = self.clock()
            request_payload = {
                "subject_id": request["subject_id"],
                "project_id": request["project_id"],
                "phase_id": request["phase_id"],
                "request_kind": request["request_kind"],
                "title": request["title"],
                "description": request["description"],
                "public_summary": request["public_summary"],
                "status": "resolved",
                "created_at": request["created_at"],
                "updated_at": now,
            }
            c.execute(
                "UPDATE autonomous_project_assistance_requests "
                "SET status='resolved',state_hash=?,updated_at=? WHERE request_id=?",
                (content_hash(request_payload), now, request["request_id"]),
            )
        elif request["status"] != "resolved":
            raise IntegrityError("settled reward workflow assistance request is invalid")
        return workflow

    def _result_from_link(self, c: Any, workflow: Any, link: Any) -> RewardAdvanceResult:
        order = c.execute(
            "SELECT * FROM wallet_payment_orders WHERE submission_id=?", (link["submission_id"],)
        ).fetchone()
        return RewardAdvanceResult(
            self._workflow_from_row(workflow),
            self._link_from_row(link),
            None if order is None else order["status"],
            None,
            None,
            None,
        )

    @staticmethod
    def _workflow_hash_payload(*values: Any) -> dict[str, Any]:
        names = (
            "workflow_id",
            "subject_id",
            "assistance_request_id",
            "project_id",
            "phase_id",
            "goal_id",
            "post_id",
            "bounty_id",
            "idempotency_key",
            "status",
            "manual_reason",
        )
        return dict(zip(names, values))

    @classmethod
    def _workflow_hash_payload_from_row(
        cls, row: Any, *, status: str | None = None, manual_reason: str | None = None
    ) -> dict[str, Any]:
        return cls._workflow_hash_payload(
            row["workflow_id"],
            row["subject_id"],
            row["assistance_request_id"],
            row["project_id"],
            row["phase_id"],
            row["goal_id"],
            row["post_id"],
            row["bounty_id"],
            row["idempotency_key"],
            row["status"] if status is None else status,
            row["manual_reason"] if status is None else manual_reason,
        )

    @staticmethod
    def _link_hash(
        row: Any, status: str, criteria: Any, reason: str | None, verified_by: str | None
    ) -> str:
        return content_hash(
            {
                "submission_id": row["submission_id"],
                "workflow_id": row["workflow_id"],
                "subject_id": row["subject_id"],
                "source_type": row["source_type"],
                "inbound_event_id": row["inbound_event_id"],
                "interaction_id": row["interaction_id"],
                "claimed_network_id": row["claimed_network_id"],
                "source_content_hash": row["source_content_hash"],
                "verification_status": status,
                "criteria_results_json": None if criteria is None else canonical_json(criteria),
                "verification_reason": reason,
                "verified_by": verified_by,
            }
        )

    @classmethod
    def _workflow_from_row(cls, row: Any) -> RewardWorkflowRecord:
        return RewardWorkflowRecord(
            row["workflow_id"],
            row["subject_id"],
            row["assistance_request_id"],
            row["project_id"],
            row["phase_id"],
            row["goal_id"],
            row["post_id"],
            row["bounty_id"],
            row["idempotency_key"],
            row["status"],
            row["manual_reason"],
            row["created_at"],
            row["updated_at"],
        )

    @staticmethod
    def _link_from_row(row: Any) -> RewardSubmissionLinkRecord:
        return RewardSubmissionLinkRecord(
            row["submission_id"],
            row["workflow_id"],
            row["subject_id"],
            row["source_type"],
            row["inbound_event_id"],
            row["interaction_id"],
            row["claimed_network_id"],
            row["source_content_hash"],
            row["verification_status"],
            None
            if row["criteria_results_json"] is None
            else tuple(strict_json_loads(row["criteria_results_json"])),
            row["verification_reason"],
            row["verified_by"],
            row["created_at"],
            row["updated_at"],
        )

    @staticmethod
    def _incident_from_row(row: Any) -> RewardIncidentRecord:
        return RewardIncidentRecord(
            row["incident_id"],
            row["subject_id"],
            row["workflow_id"],
            row["submission_id"],
            row["execution_id"],
            row["kind"],
            row["idempotency_key"],
            row["status"],
            row["reason"],
            row["resolution"],
            row["created_at"],
            row["updated_at"],
        )

    @staticmethod
    def _incident_hash(row: Any) -> str:
        return content_hash(
            {
                "incident_id": row["incident_id"],
                "subject_id": row["subject_id"],
                "workflow_id": row["workflow_id"],
                "submission_id": row["submission_id"],
                "execution_id": row["execution_id"],
                "kind": row["kind"],
                "idempotency_key": row["idempotency_key"],
                "status": row["status"],
                "reason": row["reason"],
                "resolution": row["resolution"],
            }
        )
