from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from noyra.core import Database, IdentityStore
from noyra.core.errors import InvalidTransitionError, NotFoundError
from noyra.core.types import content_hash, utc_now
from noyra.interaction import InboundEnvelope, InboundStore, TransportInput, TransportStore
from noyra.wallet import (
    MockSigner,
    PaymentPolicyInput,
    RewardEvidenceDecisionInput,
    RewardIncidentResolutionInput,
    RewardPublicSubmissionInput,
    RewardWorkflowInput,
    WalletAddressInput,
    WalletAssetInput,
    WalletEconomyStore,
    WalletNetworkInput,
    WalletPaymentExecutionEngine,
    WalletRewardWorkflow,
    WalletSignerError,
    WalletStore,
)

SOURCE_ADDRESS = "0xA111111111111111111111111111111111111111"
RECIPIENT_ADDRESS = "0xB111111111111111111111111111111111111111"


@dataclass
class RewardFixture:
    database: Database
    subject_id: str
    request_id: str
    network_id: str
    asset_id: str
    source_address: str
    economy: WalletEconomyStore
    signer: MockSigner
    workflow: WalletRewardWorkflow
    proposal: RewardWorkflowInput


def _assistance_hash(
    subject_id: str,
    project_id: str,
    phase_id: str,
    request_id: str,
    now: str,
) -> str:
    del request_id
    return content_hash(
        {
            "subject_id": subject_id,
            "project_id": project_id,
            "phase_id": phase_id,
            "request_kind": "human_help",
            "title": "Need a human proof",
            "description": "Produce a verifiable result.",
            "public_summary": "Submit proof for this autonomous project.",
            "status": "open",
            "created_at": now,
            "updated_at": now,
        }
    )


def _seed_project(database: Database, subject_id: str) -> str:
    now = utc_now()
    goal_id = "goal_reward"
    call_id = "call_reward"
    project_id = "project_reward"
    phase_id = "phase_reward"
    request_id = "request_reward"
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO goals(goal_id,subject_id,title,description,origin,status,priority,"
            "commitment,progress,emotional_pressure,state_hash,current_revision,created_at,"
            "updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                goal_id,
                subject_id,
                "Reward goal",
                "Reward goal",
                "self",
                "active",
                1,
                1,
                0,
                0,
                "g" * 64,
                1,
                now,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO model_calls(call_id,subject_id,provider,model,purpose,request_hash,"
            "idempotency_key,status,response_json,response_hash,usage_estimated,error_code,"
            "created_at,completed_at) VALUES(?,?,?,?,?,?,?,'prepared',NULL,NULL,0,NULL,?,NULL)",
            (call_id, subject_id, "fixture", "fixture", "formation", "c" * 64, call_id, now),
        )
        connection.execute(
            """INSERT INTO autonomous_projects(
            project_id,subject_id,goal_id,formation_call_id,project_key,project_type,title,purpose,
            deliverable,acceptance_criteria_json,size_class,estimated_duration_hours,status,progress,
            current_phase_id,max_cycles,max_model_calls,max_searches,max_external_actions,
            max_storage_bytes,source_event_ids_json,source_value_ids_json,source_mission_id,state_hash,
            current_revision,created_at,updated_at,completed_at
            ) VALUES(?,?,?,?,?,'collaboration',?,?,?,'[]','small',1.0,'blocked',0,NULL,1,1,0,1,0,
            '[]','[]',NULL,?,1,?,?,NULL)""",
            (
                project_id,
                subject_id,
                goal_id,
                call_id,
                "reward-project",
                "Reward project",
                "Obtain human help",
                "Verified proof",
                "p" * 64,
                now,
                now,
            ),
        )
        connection.execute(
            """INSERT INTO autonomous_project_phases(
            phase_id,project_id,subject_id,phase_key,position,title,objective,output_type,
            acceptance_criteria_json,dependency_keys_json,status,attempt_count,no_progress_count,
            state_hash,current_revision,created_at,updated_at,completed_at
            ) VALUES(?,?,?,'proof',1,'Human proof','Collect proof','collaboration_request',
            '[]','[]','blocked',0,0,?,1,?,?,NULL)""",
            (phase_id, project_id, subject_id, "h" * 64, now, now),
        )
        connection.execute(
            """INSERT INTO autonomous_project_assistance_requests(
            request_id,subject_id,project_id,phase_id,request_kind,title,description,public_summary,
            status,state_hash,created_at,updated_at
            ) VALUES(?,?,?,?,'human_help','Need a human proof','Produce a verifiable result.',
            'Submit proof for this autonomous project.','open',?,?,?)""",
            (
                request_id,
                subject_id,
                project_id,
                phase_id,
                _assistance_hash(subject_id, project_id, phase_id, request_id, now),
                now,
                now,
            ),
        )
    return request_id


def _fixture(
    tmp_path: Path,
    *,
    lose_first_response: bool = False,
    max_submissions: int = 2,
    reward_slots: int = 1,
) -> RewardFixture:
    database = Database(tmp_path / "noyra.sqlite3")
    subject_id = "Noyra-reward-workflow"
    IdentityStore(database).ensure(subject_id, content_hash({"subject": subject_id}))
    request_id = _seed_project(database, subject_id)
    wallets = WalletStore(database)
    network = wallets.register_network(
        subject_id,
        WalletNetworkInput(
            label="Ethereum",
            chain_id=1,
            native_symbol="ETH",
            rpc_url="https://rpc.example",
        ),
        actor="operator",
    )
    asset = wallets.register_asset(
        subject_id,
        WalletAssetInput(
            network_id=network.network_id,
            asset_type="native",
            name="Ether",
            symbol="ETH",
            decimals=18,
        ),
        actor="operator",
    )
    source = wallets.register_address(
        subject_id,
        WalletAddressInput(
            network_id=network.network_id,
            label="Reward spending",
            address=SOURCE_ADDRESS,
            purpose="spending",
        ),
        actor="operator",
    )
    economy = WalletEconomyStore(database)
    economy.update_policy(
        subject_id,
        PaymentPolicyInput(
            mode="automatic",
            allowed_network_ids=[network.network_id],
            allowed_asset_ids=[asset.asset_id],
            per_order_limit="100",
            automatic_max_amount="100",
        ),
        expected_version=1,
        actor="operator",
    )
    signer = MockSigner(
        chain_id=network.chain_id,
        source_address=source.address,
        lose_first_response=lose_first_response,
    )
    engine = WalletPaymentExecutionEngine(database, signer, economy=economy)
    workflow = WalletRewardWorkflow(database, economy=economy, execution=engine)
    current = datetime.now(UTC)
    proposal = RewardWorkflowInput(
        assistance_request_id=request_id,
        acceptance_criteria=["proof is reproducible", "recipient consent is explicit"],
        network_id=network.network_id,
        asset_id=asset.asset_id,
        reward_amount="10",
        opens_at=(current - timedelta(minutes=1)).isoformat(),
        expires_at=(current + timedelta(days=1)).isoformat(),
        max_submissions=max_submissions,
        reward_slots=reward_slots,
        idempotency_key="reward-workflow",
    )
    return RewardFixture(
        database,
        subject_id,
        request_id,
        network.network_id,
        asset.asset_id,
        source.address,
        economy,
        signer,
        workflow,
        proposal,
    )


def _open(fixture: RewardFixture) -> Any:
    created = fixture.workflow.create(fixture.subject_id, fixture.proposal, actor="operator")
    fixture.workflow.posts.moderate(
        created.post_id,
        subject_id=fixture.subject_id,
        status="published",
        actor="operator",
        reason="approved autonomous help request",
        expected_status="pending_review",
        idempotency_key="publish-reward-post",
    )
    return fixture.workflow.publish(created.workflow_id, fixture.subject_id, actor="operator")


def _submission(key: str = "submission-1", *, network_id: str) -> RewardPublicSubmissionInput:
    return RewardPublicSubmissionInput(
        counterparty="human@example.test",
        content="The requested proof and reproduction notes.",
        evidence=["https://example.test/proof", "sha256:abc"],
        recipient_address=RECIPIENT_ADDRESS,
        network_id=network_id,
        idempotency_key=key,
        consent_version=1,
    )


def _accept(fixture: RewardFixture, submission_id: str, *, key: str = "decision-1") -> Any:
    return fixture.workflow.decide(
        submission_id,
        fixture.subject_id,
        RewardEvidenceDecisionInput(
            accepted=True,
            criteria_results=[True, True],
            reason="all evidence independently verified",
            idempotency_key=key,
        ),
        actor="operator",
    )


def test_reward_workflow_happy_path_closes_only_after_receipt_recheck(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    opened = _open(fixture)
    assert opened.status == "open"
    submitted = fixture.workflow.submit_public(
        opened.workflow_id,
        fixture.subject_id,
        _submission(network_id=fixture.network_id),
    )
    assert submitted.submission is not None
    accepted = _accept(fixture, submitted.submission.submission_id)
    assert accepted.order_status == "reserved"
    assert len(fixture.economy.list_orders(fixture.subject_id)) == 1

    broadcast = fixture.workflow.execute_ready(fixture.subject_id, actor="operator")
    assert len(broadcast) == 1 and broadcast[0].execution_status == "broadcast"
    assert broadcast[0].execution_id is not None
    engine = fixture.workflow.execution
    assert engine is not None
    execution = engine.get_execution(broadcast[0].execution_id, fixture.subject_id)
    assert execution.tx_hash is not None
    fixture.signer.set_receipt(execution.tx_hash, chain_id=1, status=1, block_number=7)

    confirmed = fixture.workflow.execute_ready(fixture.subject_id, actor="operator")
    assert confirmed[0].execution_status == "confirmed"
    assert confirmed[0].workflow.status == "open"
    completed = fixture.workflow.execute_ready(fixture.subject_id, actor="operator")
    assert completed[0].workflow.status == "closed"
    with fixture.database.connection() as connection:
        request_status = connection.execute(
            "SELECT status FROM autonomous_project_assistance_requests WHERE request_id=?",
            (fixture.request_id,),
        ).fetchone()[0]
        bounty_status = connection.execute(
            "SELECT status FROM wallet_bounties WHERE bounty_id=?",
            (opened.bounty_id,),
        ).fetchone()[0]
    assert request_status == "resolved"
    assert bounty_status == "closed"
    assert fixture.workflow.verify_integrity(fixture.subject_id) == {
        "wallet_reward_workflows": 1,
        "wallet_reward_submission_links": 1,
        "wallet_reward_incidents": 0,
    }

    reopened_database = Database(fixture.database.path)
    restarted = WalletRewardWorkflow(reopened_database)
    assert restarted.list_workflows(fixture.subject_id)[0].status == "closed"
    assert restarted.verify_integrity(fixture.subject_id)["wallet_reward_workflows"] == 1


def test_workflow_and_submission_idempotency_are_exact_and_capacity_safe(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path, max_submissions=1)
    first = fixture.workflow.create(fixture.subject_id, fixture.proposal, actor="operator")
    with fixture.database.connection() as connection:
        bounty_status = connection.execute(
            "SELECT status FROM wallet_bounties WHERE bounty_id=?", (first.bounty_id,)
        ).fetchone()[0]
    assert bounty_status == "draft"
    replay = fixture.workflow.create(fixture.subject_id, fixture.proposal, actor="operator")
    assert replay.workflow_id == first.workflow_id
    with pytest.raises(ValueError, match="idempotency"):
        fixture.workflow.create(
            fixture.subject_id,
            fixture.proposal.model_copy(update={"reward_amount": "11"}),
            actor="operator",
        )
    fixture.workflow.posts.moderate(
        first.post_id,
        subject_id=fixture.subject_id,
        status="published",
        actor="operator",
        reason="approved",
        expected_status="pending_review",
    )
    fixture.workflow.publish(first.workflow_id, fixture.subject_id, actor="operator")
    with fixture.database.connection() as connection:
        bounty_status = connection.execute(
            "SELECT status FROM wallet_bounties WHERE bounty_id=?", (first.bounty_id,)
        ).fetchone()[0]
    assert bounty_status == "published"
    proposal = _submission(network_id=fixture.network_id)
    submitted = fixture.workflow.submit_public(first.workflow_id, fixture.subject_id, proposal)
    repeated = fixture.workflow.submit_public(first.workflow_id, fixture.subject_id, proposal)
    assert repeated.submission == submitted.submission
    with pytest.raises(ValueError, match="idempotency"):
        fixture.workflow.submit_public(
            first.workflow_id,
            fixture.subject_id,
            proposal.model_copy(update={"content": "different proof"}),
        )


def test_create_and_submission_link_are_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path)

    def fail_post(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise RuntimeError("post creation failed")

    monkeypatch.setattr(fixture.workflow.posts, "create", fail_post)
    with pytest.raises(RuntimeError, match="post creation failed"):
        fixture.workflow.create(fixture.subject_id, fixture.proposal, actor="operator")
    with fixture.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM wallet_bounties").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM public_posts").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM wallet_reward_workflows").fetchone()[0] == 0

    monkeypatch.undo()
    opened = _open(fixture)

    def fail_link(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise RuntimeError("link creation failed")

    monkeypatch.setattr(fixture.workflow, "_link_submission", fail_link)
    with pytest.raises(RuntimeError, match="link creation failed"):
        fixture.workflow.submit_public(
            opened.workflow_id,
            fixture.subject_id,
            _submission(network_id=fixture.network_id),
        )
    with fixture.database.connection() as connection:
        assert (
            connection.execute("SELECT count(*) FROM wallet_bounty_submissions").fetchone()[0] == 0
        )
        assert (
            connection.execute("SELECT count(*) FROM wallet_reward_submission_links").fetchone()[0]
            == 0
        )


def test_rejection_creates_no_order_and_decision_replay_is_exact(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    opened = _open(fixture)
    submitted = fixture.workflow.submit_public(
        opened.workflow_id,
        fixture.subject_id,
        _submission(network_id=fixture.network_id),
    )
    assert submitted.submission is not None
    decision = RewardEvidenceDecisionInput(
        accepted=False,
        criteria_results=[True, False],
        reason="the reproduction criterion failed",
        idempotency_key="reject-decision",
    )
    rejected = fixture.workflow.decide(
        submitted.submission.submission_id,
        fixture.subject_id,
        decision,
        actor="operator",
    )
    assert rejected.submission is not None
    assert rejected.submission.verification_status == "rejected"
    assert rejected.order_status is None
    replay = fixture.workflow.decide(
        submitted.submission.submission_id,
        fixture.subject_id,
        decision,
        actor="operator",
    )
    assert replay.submission == rejected.submission
    assert fixture.economy.list_orders(fixture.subject_id) == []
    with pytest.raises(ValueError, match="idempotency conflict"):
        fixture.workflow.decide(
            submitted.submission.submission_id,
            fixture.subject_id,
            decision.model_copy(update={"reason": "different reason"}),
            actor="operator",
        )


def test_source_incident_is_durable_exact_and_requires_resolution_before_resume(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    opened = _open(fixture)
    with fixture.database.connection() as connection:
        open_workflow_row = fixture.workflow._workflow_row(
            connection, opened.workflow_id, fixture.subject_id
        )
    mismatch = fixture.workflow.submit_public(
        opened.workflow_id,
        fixture.subject_id,
        _submission(network_id="different-network"),
    )
    assert mismatch.workflow.status == "manual_intervention"
    assert mismatch.incident is not None and mismatch.incident.kind == "source_mismatch"
    with pytest.raises(InvalidTransitionError, match="not publishable"):
        fixture.workflow.publish(opened.workflow_id, fixture.subject_id, actor="operator")
    with (
        fixture.database.transaction() as connection,
        pytest.raises(ValueError, match="incident idempotency conflict"),
    ):
        fixture.workflow._open_incident(
            connection,
            open_workflow_row,
            None,
            "evidence_mismatch",
            "different reason",
            "network:submission-1",
        )
    with pytest.raises(InvalidTransitionError, match="incident must be resolved"):
        fixture.workflow.resume(
            opened.workflow_id,
            fixture.subject_id,
            actor="operator",
            reason="resume too early",
        )
    fixture.workflow.resolve_incident(
        mismatch.incident.incident_id,
        fixture.subject_id,
        RewardIncidentResolutionInput(
            resolution="sender corrected the network selection",
            idempotency_key="resolve-source-mismatch",
        ),
        actor="operator",
    )
    resumed = fixture.workflow.resume(
        opened.workflow_id,
        fixture.subject_id,
        actor="operator",
        reason="source mismatch resolved",
    )
    assert resumed.status == "open"


def test_reward_execution_batch_stops_after_workflow_enters_manual_intervention(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path, max_submissions=2, reward_slots=2)

    class RejectFirstSigner(MockSigner):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.calls = 0

        def sign_and_broadcast(self, transfer: Any, *, request_id: str) -> Any:
            self.calls += 1
            if self.calls == 1:
                raise WalletSignerError("temporary signer rejection")
            return super().sign_and_broadcast(transfer, request_id=request_id)

    signer = RejectFirstSigner(chain_id=1, source_address=fixture.source_address)
    economy = fixture.economy
    engine = WalletPaymentExecutionEngine(fixture.database, signer, economy=economy)
    workflow = WalletRewardWorkflow(fixture.database, economy=economy, execution=engine)
    fixture.workflow = workflow
    opened = _open(fixture)
    first = workflow.submit_public(
        opened.workflow_id,
        fixture.subject_id,
        _submission("submission-first", network_id=fixture.network_id),
    )
    second = workflow.submit_public(
        opened.workflow_id,
        fixture.subject_id,
        _submission("submission-second", network_id=fixture.network_id),
    )
    assert first.submission is not None and second.submission is not None
    _accept(fixture, first.submission.submission_id, key="decision-first")
    _accept(fixture, second.submission.submission_id, key="decision-second")

    results = workflow.execute_ready(fixture.subject_id, actor="operator", limit=2)

    assert len(results) == 1
    assert results[0].workflow.status == "manual_intervention"
    assert signer.calls == 1
    orders = economy.list_orders(fixture.subject_id)
    assert len(orders) == 2
    assert sum(order.status == "reserved" for order in orders) == 1


def test_reward_workflow_bounds_inputs_and_rejects_blank_decision_text(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    with pytest.raises(ValueError):
        RewardEvidenceDecisionInput(
            accepted=True,
            criteria_results=[True],
            reason=" ",
            idempotency_key="blank-reason",
        )
    with pytest.raises(ValueError):
        RewardIncidentResolutionInput(
            resolution=" ",
            idempotency_key="blank-resolution",
        )
    with pytest.raises(ValueError, match="limit"):
        fixture.workflow.execute_ready(fixture.subject_id, actor="operator", limit=0)
    with pytest.raises(ValueError, match="limit"):
        fixture.workflow.incidents(fixture.subject_id, limit=0)


def test_schema_59_migration_rejects_tampered_legacy_submission(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    opened = _open(fixture)
    submitted = fixture.workflow.submit_public(
        opened.workflow_id,
        fixture.subject_id,
        _submission(network_id=fixture.network_id),
    )
    assert submitted.submission is not None
    with fixture.database.transaction() as connection:
        connection.execute("DROP TRIGGER prevent_wallet_submission_identity_update")
        connection.execute("DROP TRIGGER validate_wallet_submission_transition")
        connection.execute(
            "UPDATE wallet_bounty_submissions SET state_hash=? WHERE submission_id=?",
            ("0" * 64, submitted.submission.submission_id),
        )
        connection.execute("UPDATE schema_meta SET value='58' WHERE key='schema_version'")
    with pytest.raises(RuntimeError, match="state hash mismatch"):
        Database(fixture.database.path)


def test_unknown_broadcast_recovers_after_restart_and_settles_once(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, lose_first_response=True)
    opened = _open(fixture)
    submitted = fixture.workflow.submit_public(
        opened.workflow_id,
        fixture.subject_id,
        _submission(network_id=fixture.network_id),
    )
    assert submitted.submission is not None
    _accept(fixture, submitted.submission.submission_id)
    unknown = fixture.workflow.execute_ready(fixture.subject_id, actor="operator")
    assert unknown[0].execution_status == "unknown"
    assert unknown[0].incident is not None
    assert unknown[0].incident.kind == "broadcast_unknown"

    database = Database(fixture.database.path)
    economy = WalletEconomyStore(database)
    engine = WalletPaymentExecutionEngine(database, fixture.signer, economy=economy)
    restarted = WalletRewardWorkflow(database, economy=economy, execution=engine)
    restarted.resolve_incident(
        unknown[0].incident.incident_id,
        fixture.subject_id,
        RewardIncidentResolutionInput(
            resolution="signer request identity verified",
            idempotency_key="resolve-unknown",
        ),
        actor="operator",
    )
    restarted.resume(
        opened.workflow_id,
        fixture.subject_id,
        actor="operator",
        reason="retry is authorized",
    )
    # Recovery polling is intentionally read-only for an unknown broadcast;
    # an operator must explicitly authorize a resend with the fixed nonce.
    order = restarted.economy.order_for_submission(
        submitted.submission.submission_id, fixture.subject_id
    )
    assert order is not None
    restarted_engine = restarted.execution
    assert restarted_engine is not None
    execution = restarted_engine._order_execution(order.order_id, fixture.subject_id)
    assert execution is not None
    pending = restarted_engine.poll_receipt(
        execution.execution_id, fixture.subject_id, actor="operator"
    )
    assert pending.status == "unknown"
    broadcast = restarted_engine.retry_unknown(
        execution.order_id,
        fixture.subject_id,
        actor="operator",
        reason="explicit recovery resend",
    )
    assert broadcast.status == "broadcast"
    execution = restarted_engine.get_execution(broadcast.execution_id, fixture.subject_id)
    assert execution.tx_hash is not None
    fixture.signer.set_receipt(execution.tx_hash, chain_id=1, status=1, block_number=12)
    assert (
        restarted.execute_ready(fixture.subject_id, actor="operator")[0].workflow.status == "open"
    )
    assert (
        restarted.execute_ready(fixture.subject_id, actor="operator")[0].workflow.status == "closed"
    )
    assert len(fixture.signer.requests) == 2
    assert fixture.signer.requests[0] == fixture.signer.requests[1]
    assert (
        restarted_engine.get_execution(execution.execution_id, fixture.subject_id).attempt_count
        == 2
    )
    with fixture.database.connection() as connection:
        settlements = connection.execute(
            "SELECT count(*) FROM wallet_ledger_journals WHERE journal_type='settlement'"
        ).fetchone()[0]
    assert settlements == 1


def test_signer_rejection_fails_closed_to_manual_intervention(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)

    class RejectingSigner(MockSigner):
        def sign_and_broadcast(self, transfer: Any, *, request_id: str) -> Any:
            del transfer, request_id
            raise WalletSignerError("policy rejected")

    signer = RejectingSigner(chain_id=1, source_address=fixture.source_address)
    engine = WalletPaymentExecutionEngine(fixture.database, signer, economy=fixture.economy)
    workflow = WalletRewardWorkflow(fixture.database, economy=fixture.economy, execution=engine)
    fixture.workflow = workflow
    opened = _open(fixture)
    submitted = workflow.submit_public(
        opened.workflow_id,
        fixture.subject_id,
        _submission(network_id=fixture.network_id),
    )
    assert submitted.submission is not None
    _accept(fixture, submitted.submission.submission_id)
    failed = workflow.execute_ready(fixture.subject_id, actor="operator")
    assert failed[0].execution_status == "failed"
    assert failed[0].workflow.status == "manual_intervention"
    assert failed[0].incident is not None
    assert failed[0].incident.kind == "signer_rejection"


def test_resolved_incident_recurs_as_a_new_open_incident(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    opened = _open(fixture)
    mismatched = _submission(network_id="walletnet_other")
    first = fixture.workflow.submit_public(
        opened.workflow_id,
        fixture.subject_id,
        mismatched,
    )
    assert first.incident is not None
    fixture.workflow.resolve_incident(
        first.incident.incident_id,
        fixture.subject_id,
        RewardIncidentResolutionInput(
            resolution="caller will retry with corrected provenance",
            idempotency_key="resolve-first-source-mismatch",
        ),
        actor="operator",
    )
    fixture.workflow.resume(
        opened.workflow_id,
        fixture.subject_id,
        actor="operator",
        reason="allow corrected retry",
    )

    repeated = fixture.workflow.submit_public(
        opened.workflow_id,
        fixture.subject_id,
        mismatched,
    )
    assert repeated.incident is not None
    assert repeated.incident.incident_id != first.incident.incident_id
    assert repeated.incident.status == "open"
    assert repeated.workflow.status == "manual_intervention"


def test_confirmed_receipt_reorganization_opens_incident_before_completion(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    opened = _open(fixture)
    submitted = fixture.workflow.submit_public(
        opened.workflow_id,
        fixture.subject_id,
        _submission(network_id=fixture.network_id),
    )
    assert submitted.submission is not None
    _accept(fixture, submitted.submission.submission_id)
    broadcast = fixture.workflow.execute_ready(fixture.subject_id, actor="operator")[0]
    assert broadcast.execution_id is not None
    engine = fixture.workflow.execution
    assert engine is not None
    execution = engine.get_execution(broadcast.execution_id, fixture.subject_id)
    assert execution.tx_hash is not None
    fixture.signer.set_receipt(execution.tx_hash, chain_id=1, status=1, block_number=20)
    confirmed = fixture.workflow.execute_ready(fixture.subject_id, actor="operator")[0]
    assert confirmed.workflow.status == "open"
    fixture.signer._receipts.pop(execution.tx_hash)
    reorganized = fixture.workflow.execute_ready(fixture.subject_id, actor="operator")[0]
    assert reorganized.workflow.status == "manual_intervention"
    assert reorganized.incident is not None
    assert reorganized.incident.kind == "chain_reorganization"


def test_native_inbound_submission_preserves_verified_event_provenance(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    opened = _open(fixture)
    transports = TransportStore(fixture.database, tmp_path / "transport-secrets")
    transport = transports.configure(
        fixture.subject_id,
        TransportInput(
            channel="webhook",
            label="reward webhook",
            endpoint="https://example.test/inbound",
            credentials={"webhook_secret": SecretStr("secret")},
        ),
        actor="operator",
    )
    inbound = InboundStore(fixture.database)
    inbound.bind(
        fixture.subject_id,
        transport.transport_id,
        external_account_id="reward-account",
        external_sender_id="human-42",
        role="participant",
    )
    content = json.dumps(
        {
            "kind": "noyra_reward_submission",
            "workflow_id": opened.workflow_id,
            "evidence": ["https://example.test/inbound-proof"],
            "recipient_address": RECIPIENT_ADDRESS,
            "network_id": fixture.network_id,
            "idempotency_key": "inbound-submission",
            "consent_version": 1,
        },
        separators=(",", ":"),
    )
    accepted = inbound.ingest(
        InboundEnvelope(
            channel="webhook",
            transport_id=transport.transport_id,
            provider_event_id="reward-event-1",
            external_account_id="reward-account",
            external_sender_id="human-42",
            conversation_id="reward-conversation",
            content=content,
        )
    )
    result = fixture.workflow.submit_inbound(accepted, external_counterparty="human-42")
    assert result.submission is not None
    assert result.submission.source_type == "inbound_event"
    assert result.submission.inbound_event_id == accepted.event_id
    assert result.submission.interaction_id == accepted.interaction_id
    assert result.submission.source_content_hash == content_hash(content)
    replay = fixture.workflow.submit_inbound(accepted, external_counterparty="human-42")
    assert replay.submission == result.submission


def test_subject_scope_and_hash_tampering_fail_closed(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    opened = _open(fixture)
    submitted = fixture.workflow.submit_public(
        opened.workflow_id,
        fixture.subject_id,
        _submission(network_id=fixture.network_id),
    )
    assert submitted.submission is not None
    other = "Noyra-other-reward-subject"
    IdentityStore(fixture.database).ensure(other, content_hash({"subject": other}))
    with pytest.raises(NotFoundError):
        fixture.workflow.decide(
            submitted.submission.submission_id,
            other,
            RewardEvidenceDecisionInput(
                accepted=True,
                criteria_results=[True, True],
                reason="cross-subject attempt",
                idempotency_key="cross-subject",
            ),
            actor="operator",
        )
    with fixture.database.transaction() as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE wallet_reward_workflows SET state_hash=? WHERE workflow_id=?",
            ("0" * 64, opened.workflow_id),
        )
    assert fixture.workflow.verify_integrity(fixture.subject_id)["wallet_reward_workflows"] == 1
