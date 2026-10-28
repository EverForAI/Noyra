# Historical SQL must retain its exact bytes; inherited from database.py.
# ruff: noqa: E501
"""Migrations v056 v069 definitions; execution remains in core.database."""

from __future__ import annotations

MIGRATIONS_V055_V069: dict[int, str] = {
    56: """
CREATE TABLE IF NOT EXISTS wallet_payment_executions (
    execution_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    order_id TEXT NOT NULL UNIQUE REFERENCES wallet_payment_orders(order_id),
    network_id TEXT NOT NULL REFERENCES wallet_networks(network_id),
    asset_id TEXT NOT NULL REFERENCES wallet_assets(asset_id),
    source_address TEXT NOT NULL CHECK(length(source_address)=42 AND substr(source_address,1,2)='0x'),
    recipient_address TEXT NOT NULL CHECK(length(recipient_address)=42 AND substr(recipient_address,1,2)='0x'),
    asset_type TEXT NOT NULL CHECK(asset_type IN ('native','token')),
    contract_address TEXT,
    amount TEXT NOT NULL CHECK(length(amount) BETWEEN 1 AND 256 AND amount NOT GLOB '*[^0-9]*' AND (amount='0' OR amount NOT GLOB '0*')),
    chain_id INTEGER NOT NULL CHECK(chain_id > 0),
    nonce INTEGER NOT NULL CHECK(nonce >= 0),
    gas_limit INTEGER NOT NULL CHECK(gas_limit BETWEEN 21000 AND 30000000),
    max_fee_per_gas TEXT NOT NULL CHECK(length(max_fee_per_gas) BETWEEN 1 AND 256 AND max_fee_per_gas NOT GLOB '*[^0-9]*' AND max_fee_per_gas NOT GLOB '0*'),
    request_id TEXT NOT NULL,
    request_hash TEXT NOT NULL CHECK(length(request_hash)=64 AND request_hash NOT GLOB '*[^0-9a-f]*'),
    signer_id TEXT NOT NULL CHECK(length(trim(signer_id)) BETWEEN 1 AND 256),
    status TEXT NOT NULL CHECK(status IN ('signing','broadcast','unknown','confirmed','failed')),
    tx_hash TEXT,
    error_code TEXT,
    receipt_status INTEGER CHECK(receipt_status IN (0,1)),
    receipt_block_number INTEGER CHECK(receipt_block_number IS NULL OR receipt_block_number >= 0),
    receipt_block_hash TEXT,
    receipt_confirmations INTEGER CHECK(receipt_confirmations IS NULL OR receipt_confirmations >= 0),
    receipt_effect_hash TEXT,
    attempt_count INTEGER NOT NULL CHECK(attempt_count >= 1),
    state_hash TEXT NOT NULL CHECK(length(state_hash)=64 AND state_hash NOT GLOB '*[^0-9a-f]*'),
    created_audit_id TEXT NOT NULL REFERENCES audit_records(audit_id),
    last_audit_id TEXT NOT NULL REFERENCES audit_records(audit_id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(subject_id,request_id)
);
CREATE INDEX IF NOT EXISTS idx_wallet_payment_executions_status
    ON wallet_payment_executions(subject_id,status,created_at,execution_id);
CREATE INDEX IF NOT EXISTS idx_wallet_payment_executions_history
    ON wallet_payment_executions(subject_id,created_at DESC,execution_id DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_wallet_payment_execution_nonce
    ON wallet_payment_executions(subject_id,network_id,source_address,nonce)
    WHERE status IN ('signing','broadcast','unknown','confirmed') OR tx_hash IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_wallet_payment_execution_tx_hash
    ON wallet_payment_executions(subject_id,network_id,tx_hash) WHERE tx_hash IS NOT NULL;
CREATE TABLE IF NOT EXISTS wallet_payment_execution_attempts (
    attempt_id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL REFERENCES wallet_payment_executions(execution_id),
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    attempt_number INTEGER NOT NULL CHECK(attempt_number >= 1),
    request_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('signing','broadcast','unknown','confirmed','failed')),
    tx_hash TEXT,
    error_code TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    state_hash TEXT NOT NULL CHECK(length(state_hash)=64 AND state_hash NOT GLOB '*[^0-9a-f]*'),
    created_audit_id TEXT NOT NULL REFERENCES audit_records(audit_id),
    UNIQUE(execution_id,attempt_number),
    UNIQUE(subject_id,request_id)
);
CREATE TRIGGER IF NOT EXISTS validate_wallet_execution_insert
BEFORE INSERT ON wallet_payment_executions
WHEN NOT EXISTS (
    SELECT 1 FROM wallet_payment_orders order_row
    WHERE order_row.order_id = NEW.order_id
      AND order_row.subject_id = NEW.subject_id
      AND order_row.network_id = NEW.network_id
      AND order_row.asset_id = NEW.asset_id
      AND order_row.recipient_address = NEW.recipient_address
      AND order_row.amount = NEW.amount
      AND order_row.status = 'signing'
)
BEGIN
    SELECT RAISE(ABORT, 'wallet payment execution reference mismatch');
END;
CREATE TRIGGER IF NOT EXISTS validate_wallet_execution_attempt_insert
BEFORE INSERT ON wallet_payment_execution_attempts
WHEN NOT EXISTS (
    SELECT 1 FROM wallet_payment_executions execution_row
    WHERE execution_row.execution_id = NEW.execution_id
      AND execution_row.subject_id = NEW.subject_id
      AND execution_row.status = 'signing'
      AND execution_row.attempt_count = NEW.attempt_number
)
BEGIN
    SELECT RAISE(ABORT, 'wallet payment execution attempt reference mismatch');
END;
CREATE INDEX IF NOT EXISTS idx_wallet_payment_execution_attempts
    ON wallet_payment_execution_attempts(subject_id,execution_id,attempt_number);
CREATE TRIGGER IF NOT EXISTS prevent_wallet_execution_delete
BEFORE DELETE ON wallet_payment_executions BEGIN SELECT RAISE(ABORT,'wallet payment executions cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_execution_attempt_delete
BEFORE DELETE ON wallet_payment_execution_attempts BEGIN SELECT RAISE(ABORT,'wallet payment execution attempts cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_execution_identity_update
BEFORE UPDATE OF execution_id,subject_id,order_id,network_id,asset_id,source_address,recipient_address,
                 asset_type,contract_address,amount,chain_id,nonce,gas_limit,max_fee_per_gas,
                 request_id,request_hash,signer_id,created_audit_id,created_at
ON wallet_payment_executions BEGIN SELECT RAISE(ABORT,'wallet payment execution identity is immutable'); END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_execution_attempt_identity_update
BEFORE UPDATE OF attempt_id,execution_id,subject_id,attempt_number,request_id,started_at,created_audit_id
ON wallet_payment_execution_attempts BEGIN SELECT RAISE(ABORT,'wallet payment execution attempt identity is immutable'); END;
DROP TRIGGER IF EXISTS validate_wallet_execution_transition;
CREATE TRIGGER validate_wallet_execution_transition
BEFORE UPDATE ON wallet_payment_executions
WHEN NOT (
    NEW.status IS OLD.status
        AND NEW.tx_hash IS OLD.tx_hash
        AND NEW.error_code IS OLD.error_code
        AND NEW.receipt_status IS OLD.receipt_status
        AND NEW.receipt_block_number IS OLD.receipt_block_number
        AND NEW.attempt_count IS OLD.attempt_count
        AND NEW.state_hash IS OLD.state_hash
        AND NEW.last_audit_id IS OLD.last_audit_id
        AND NEW.updated_at IS OLD.updated_at
    OR OLD.status='signing' AND NEW.status IN ('broadcast','unknown','failed')
    OR OLD.status='broadcast' AND NEW.status IN ('unknown','confirmed','failed')
    OR OLD.status='unknown' AND NEW.status IN ('signing','broadcast','confirmed','failed')
)
BEGIN SELECT RAISE(ABORT,'wallet payment execution transition is invalid'); END;
DROP TRIGGER IF EXISTS validate_wallet_execution_attempt_transition;
CREATE TRIGGER validate_wallet_execution_attempt_transition
BEFORE UPDATE ON wallet_payment_execution_attempts
WHEN NOT (
    NEW.status IS OLD.status
        AND NEW.tx_hash IS OLD.tx_hash
        AND NEW.error_code IS OLD.error_code
        AND NEW.completed_at IS OLD.completed_at
        AND NEW.state_hash IS OLD.state_hash
    OR OLD.status='signing' AND NEW.status IN ('broadcast','unknown','failed')
    OR OLD.status='broadcast' AND NEW.status IN ('unknown','confirmed','failed')
    OR OLD.status='unknown' AND NEW.status IN ('broadcast','confirmed','failed')
)
BEGIN SELECT RAISE(ABORT,'wallet payment execution attempt transition is invalid'); END;
DROP TRIGGER IF EXISTS validate_wallet_order_transition;
CREATE TRIGGER validate_wallet_order_transition
BEFORE UPDATE ON wallet_payment_orders
WHEN NOT (
    NEW.status = OLD.status AND NEW.state_hash = OLD.state_hash AND NEW.updated_at = OLD.updated_at
    OR OLD.status = 'pending_policy' AND NEW.status IN ('awaiting_confirmation','reserved','rejected','cancelled','expired')
    OR OLD.status = 'awaiting_confirmation' AND NEW.status IN ('reserved','rejected','cancelled','expired')
    OR OLD.status = 'reserved' AND NEW.status IN ('cancelled','expired','signing')
    OR OLD.status = 'signing' AND NEW.status IN ('broadcast','unknown','failed')
    OR OLD.status = 'broadcast' AND NEW.status IN ('unknown','confirmed','failed')
    OR OLD.status = 'unknown' AND NEW.status IN ('signing','broadcast','confirmed','failed','refunded')
    OR OLD.status = 'failed' AND NEW.status = 'refunded'
)
BEGIN SELECT RAISE(ABORT,'wallet payment order transition is invalid'); END;
""",
    55: """
CREATE TABLE IF NOT EXISTS wallet_bounties (
    bounty_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    project_id TEXT REFERENCES autonomous_projects(project_id),
    goal_id TEXT REFERENCES goals(goal_id),
    idempotency_key TEXT NOT NULL CHECK(length(trim(idempotency_key)) BETWEEN 1 AND 128),
    title TEXT NOT NULL CHECK(length(trim(title)) BETWEEN 1 AND 240),
    description TEXT NOT NULL CHECK(length(trim(description)) BETWEEN 1 AND 20000),
    acceptance_criteria_json TEXT NOT NULL,
    network_id TEXT NOT NULL REFERENCES wallet_networks(network_id),
    asset_id TEXT NOT NULL REFERENCES wallet_assets(asset_id),
    reward_amount TEXT NOT NULL CHECK(length(reward_amount) BETWEEN 1 AND 256 AND reward_amount NOT GLOB '*[^0-9]*' AND (reward_amount='0' OR reward_amount NOT GLOB '0*')),
    opens_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    max_submissions INTEGER NOT NULL CHECK(max_submissions BETWEEN 1 AND 10000),
    reward_slots INTEGER NOT NULL CHECK(reward_slots BETWEEN 1 AND 10000),
    status TEXT NOT NULL CHECK(status IN ('draft','published','closed','cancelled','expired')),
    state_hash TEXT NOT NULL,
    created_audit_id TEXT NOT NULL REFERENCES audit_records(audit_id),
    last_audit_id TEXT NOT NULL REFERENCES audit_records(audit_id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(subject_id,idempotency_key),
    CHECK(expires_at > opens_at)
);
CREATE INDEX IF NOT EXISTS idx_wallet_bounties_public ON wallet_bounties(subject_id,status,created_at DESC,bounty_id);
CREATE TABLE IF NOT EXISTS wallet_bounty_submissions (
    submission_id TEXT PRIMARY KEY,
    bounty_id TEXT NOT NULL REFERENCES wallet_bounties(bounty_id),
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    counterparty TEXT NOT NULL CHECK(length(trim(counterparty)) BETWEEN 1 AND 256),
    content TEXT NOT NULL CHECK(length(trim(content)) BETWEEN 1 AND 20000),
    evidence_json TEXT NOT NULL,
    recipient_address TEXT NOT NULL CHECK(length(recipient_address)=42 AND substr(recipient_address,1,2)='0x'),
    idempotency_key TEXT NOT NULL CHECK(length(trim(idempotency_key)) BETWEEN 1 AND 128),
    status TEXT NOT NULL CHECK(status IN ('submitted','accepted','rejected','withdrawn','expired')),
    decision_reason TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    state_hash TEXT NOT NULL,
    created_audit_id TEXT NOT NULL REFERENCES audit_records(audit_id),
    decision_audit_id TEXT REFERENCES audit_records(audit_id),
    UNIQUE(subject_id,bounty_id,idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_wallet_bounty_submissions_bounty ON wallet_bounty_submissions(subject_id,bounty_id,status,created_at,submission_id);
CREATE TABLE IF NOT EXISTS wallet_payment_policies (
    subject_id TEXT PRIMARY KEY REFERENCES subject_identity(subject_id),
    mode TEXT NOT NULL CHECK(mode IN ('disabled','conditional_confirmation','automatic')),
    allowed_network_ids_json TEXT NOT NULL,
    allowed_asset_ids_json TEXT NOT NULL,
    per_order_limit TEXT NOT NULL,
    daily_limit TEXT NOT NULL,
    monthly_limit TEXT NOT NULL,
    daily_order_limit INTEGER NOT NULL CHECK(daily_order_limit >= 0),
    monthly_order_limit INTEGER NOT NULL CHECK(monthly_order_limit >= 0),
    min_balance TEXT NOT NULL,
    max_observation_age_seconds INTEGER NOT NULL CHECK(max_observation_age_seconds >= 0),
    automatic_max_amount TEXT NOT NULL,
    anomaly_block INTEGER NOT NULL CHECK(anomaly_block IN (0,1)),
    emergency_paused INTEGER NOT NULL CHECK(emergency_paused IN (0,1)),
    recipient_allowlist_enabled INTEGER NOT NULL DEFAULT 0 CHECK(recipient_allowlist_enabled IN (0,1)),
    allowed_recipient_addresses_json TEXT NOT NULL DEFAULT '[]',
    policy_version INTEGER NOT NULL CHECK(policy_version > 0),
    updated_at TEXT NOT NULL,
    state_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wallet_payment_orders (
    order_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    bounty_id TEXT NOT NULL REFERENCES wallet_bounties(bounty_id),
    submission_id TEXT NOT NULL REFERENCES wallet_bounty_submissions(submission_id),
    network_id TEXT NOT NULL REFERENCES wallet_networks(network_id),
    asset_id TEXT NOT NULL REFERENCES wallet_assets(asset_id),
    recipient_address TEXT NOT NULL,
    amount TEXT NOT NULL CHECK(length(amount) BETWEEN 1 AND 256 AND amount NOT GLOB '*[^0-9]*' AND (amount='0' OR amount NOT GLOB '0*')),
    payment_mode TEXT NOT NULL CHECK(payment_mode IN ('disabled','conditional_confirmation','automatic')),
    policy_version INTEGER NOT NULL CHECK(policy_version > 0),
    idempotency_key TEXT NOT NULL,
    authorized_at TEXT,
    created_audit_id TEXT NOT NULL REFERENCES audit_records(audit_id),
    last_audit_id TEXT NOT NULL REFERENCES audit_records(audit_id),
    status TEXT NOT NULL CHECK(status IN ('pending_policy','awaiting_confirmation','reserved','rejected','cancelled','expired','signing','broadcast','unknown','confirmed','failed','refunded')),
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(subject_id,submission_id), UNIQUE(subject_id,idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_wallet_payment_orders_status ON wallet_payment_orders(subject_id,status,created_at,order_id);
CREATE TABLE IF NOT EXISTS wallet_ledger_journals (
    journal_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    order_id TEXT NOT NULL REFERENCES wallet_payment_orders(order_id),
    network_id TEXT NOT NULL REFERENCES wallet_networks(network_id),
    asset_id TEXT NOT NULL REFERENCES wallet_assets(asset_id),
    journal_type TEXT NOT NULL CHECK(journal_type IN ('reservation','release','settlement','refund')),
    amount TEXT NOT NULL CHECK(length(amount) BETWEEN 1 AND 256 AND amount NOT GLOB '*[^0-9]*' AND (amount='0' OR amount NOT GLOB '0*')),
    created_at TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    UNIQUE(order_id,journal_type)
);
CREATE TABLE IF NOT EXISTS wallet_ledger_entries (
    entry_id TEXT PRIMARY KEY,
    journal_id TEXT NOT NULL REFERENCES wallet_ledger_journals(journal_id),
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    order_id TEXT NOT NULL REFERENCES wallet_payment_orders(order_id),
    account TEXT NOT NULL CHECK(account IN ('available','reserved','paid','released')),
    direction TEXT NOT NULL CHECK(direction IN ('debit','credit')),
    amount TEXT NOT NULL CHECK(length(amount) BETWEEN 1 AND 256 AND amount NOT GLOB '*[^0-9]*' AND (amount='0' OR amount NOT GLOB '0*')),
    created_at TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    UNIQUE(journal_id,account,direction)
);
CREATE INDEX IF NOT EXISTS idx_wallet_ledger_subject ON wallet_ledger_journals(subject_id,created_at,journal_id);
CREATE TRIGGER IF NOT EXISTS prevent_wallet_bounty_delete BEFORE DELETE ON wallet_bounties BEGIN SELECT RAISE(ABORT,'wallet bounties cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_submission_delete BEFORE DELETE ON wallet_bounty_submissions BEGIN SELECT RAISE(ABORT,'wallet submissions cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_order_delete BEFORE DELETE ON wallet_payment_orders BEGIN SELECT RAISE(ABORT,'wallet payment orders cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_journal_update BEFORE UPDATE ON wallet_ledger_journals BEGIN SELECT RAISE(ABORT,'wallet ledger journals are append-only'); END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_journal_delete BEFORE DELETE ON wallet_ledger_journals BEGIN SELECT RAISE(ABORT,'wallet ledger journals cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_entry_update BEFORE UPDATE ON wallet_ledger_entries BEGIN SELECT RAISE(ABORT,'wallet ledger entries are append-only'); END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_entry_delete BEFORE DELETE ON wallet_ledger_entries BEGIN SELECT RAISE(ABORT,'wallet ledger entries cannot be deleted'); END;
INSERT OR IGNORE INTO wallet_payment_policies(
    subject_id, mode, allowed_network_ids_json, allowed_asset_ids_json,
    per_order_limit, daily_limit, monthly_limit, daily_order_limit, monthly_order_limit,
    min_balance, max_observation_age_seconds, automatic_max_amount, anomaly_block,
    emergency_paused, recipient_allowlist_enabled, allowed_recipient_addresses_json,
    policy_version, updated_at, state_hash
)
SELECT subject_id, 'disabled', '[]', '[]', '0', '0', '0', 0, 0, '0', 0, '0', 1, 0, 0, '[]',
       1, updated_at, 'bootstrap'
FROM subject_identity;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_bounty_identity_update
BEFORE UPDATE OF bounty_id,subject_id,project_id,goal_id,idempotency_key,title,description,
                 acceptance_criteria_json,network_id,asset_id,reward_amount,opens_at,expires_at,
                 max_submissions,reward_slots,created_audit_id,created_at ON wallet_bounties
BEGIN SELECT RAISE(ABORT,'wallet bounty identity is immutable'); END;
CREATE TRIGGER IF NOT EXISTS validate_wallet_bounty_transition
BEFORE UPDATE ON wallet_bounties
WHEN NOT (
    NEW.status = OLD.status AND NEW.state_hash = OLD.state_hash AND NEW.updated_at = OLD.updated_at
    OR OLD.status = 'draft' AND NEW.status IN ('published','cancelled','expired')
    OR OLD.status = 'published' AND NEW.status IN ('closed','cancelled','expired')
    OR OLD.status = 'closed' AND NEW.status = 'expired'
)
BEGIN SELECT RAISE(ABORT,'wallet bounty transition is invalid'); END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_submission_identity_update
BEFORE UPDATE OF submission_id,bounty_id,subject_id,counterparty,content,evidence_json,
                 recipient_address,idempotency_key,created_at,created_audit_id ON wallet_bounty_submissions
BEGIN SELECT RAISE(ABORT,'wallet submission identity is immutable'); END;
CREATE TRIGGER IF NOT EXISTS validate_wallet_submission_transition
BEFORE UPDATE ON wallet_bounty_submissions
WHEN NOT (
    NEW.status = OLD.status AND NEW.state_hash = OLD.state_hash
    OR OLD.status = 'submitted' AND NEW.status IN ('accepted','rejected','withdrawn','expired')
)
BEGIN SELECT RAISE(ABORT,'wallet submission transition is invalid'); END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_order_identity_update
BEFORE UPDATE OF order_id,subject_id,bounty_id,submission_id,network_id,asset_id,
                 recipient_address,amount,payment_mode,policy_version,idempotency_key,
                 created_audit_id,created_at ON wallet_payment_orders
BEGIN SELECT RAISE(ABORT,'wallet payment order identity is immutable'); END;
CREATE TRIGGER IF NOT EXISTS validate_wallet_order_transition
BEFORE UPDATE ON wallet_payment_orders
WHEN NOT (
    NEW.status = OLD.status AND NEW.state_hash = OLD.state_hash AND NEW.updated_at = OLD.updated_at
    OR OLD.status = 'pending_policy' AND NEW.status IN ('awaiting_confirmation','reserved','rejected','cancelled','expired')
    OR OLD.status = 'awaiting_confirmation' AND NEW.status IN ('reserved','rejected','cancelled','expired')
    OR OLD.status = 'reserved' AND NEW.status IN ('cancelled','expired','signing')
    OR OLD.status = 'signing' AND NEW.status IN ('broadcast','unknown','failed')
    OR OLD.status = 'broadcast' AND NEW.status IN ('unknown','confirmed','failed')
    OR OLD.status = 'unknown' AND NEW.status IN ('broadcast','confirmed','failed','refunded')
    OR OLD.status = 'failed' AND NEW.status = 'refunded'
)
BEGIN SELECT RAISE(ABORT,'wallet payment order transition is invalid'); END;
""",
    57: """
CREATE TABLE IF NOT EXISTS wallet_reward_workflows (
    workflow_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    assistance_request_id TEXT NOT NULL UNIQUE REFERENCES autonomous_project_assistance_requests(request_id),
    project_id TEXT NOT NULL REFERENCES autonomous_projects(project_id),
    phase_id TEXT NOT NULL REFERENCES autonomous_project_phases(phase_id),
    goal_id TEXT NOT NULL REFERENCES goals(goal_id),
    post_id TEXT NOT NULL UNIQUE REFERENCES public_posts(post_id),
    bounty_id TEXT NOT NULL UNIQUE REFERENCES wallet_bounties(bounty_id),
    idempotency_key TEXT NOT NULL CHECK(length(trim(idempotency_key)) BETWEEN 1 AND 128),
    status TEXT NOT NULL CHECK(status IN ('awaiting_publication','open','closed','cancelled','manual_intervention')),
    manual_reason TEXT,
    state_hash TEXT NOT NULL CHECK(length(state_hash)=64 AND state_hash NOT GLOB '*[^0-9a-f]*'),
    created_audit_id TEXT NOT NULL REFERENCES audit_records(audit_id),
    last_audit_id TEXT NOT NULL REFERENCES audit_records(audit_id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(subject_id,idempotency_key),
    CHECK((status='manual_intervention') = (manual_reason IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_wallet_reward_workflows_status ON wallet_reward_workflows(subject_id,status,created_at DESC,workflow_id DESC);
CREATE TABLE IF NOT EXISTS wallet_reward_submission_links (
    submission_id TEXT PRIMARY KEY REFERENCES wallet_bounty_submissions(submission_id),
    workflow_id TEXT NOT NULL REFERENCES wallet_reward_workflows(workflow_id),
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    source_type TEXT NOT NULL CHECK(source_type IN ('public_form','inbound_event')),
    inbound_event_id TEXT REFERENCES interaction_inbound_events(event_id),
    interaction_id TEXT REFERENCES interactions(interaction_id),
    claimed_network_id TEXT NOT NULL REFERENCES wallet_networks(network_id),
    source_content_hash TEXT NOT NULL CHECK(length(source_content_hash)=64 AND source_content_hash NOT GLOB '*[^0-9a-f]*'),
    verification_status TEXT NOT NULL CHECK(verification_status IN ('pending_verification','accepted','rejected','manual_intervention')),
    criteria_results_json TEXT,
    verification_reason TEXT,
    verified_by TEXT,
    created_audit_id TEXT NOT NULL REFERENCES audit_records(audit_id),
    decision_audit_id TEXT REFERENCES audit_records(audit_id),
    state_hash TEXT NOT NULL CHECK(length(state_hash)=64 AND state_hash NOT GLOB '*[^0-9a-f]*'),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(subject_id,inbound_event_id),
    CHECK((source_type='public_form' AND inbound_event_id IS NULL AND interaction_id IS NULL) OR (source_type='inbound_event' AND inbound_event_id IS NOT NULL AND interaction_id IS NOT NULL)),
    CHECK((verification_status='pending_verification' AND criteria_results_json IS NULL AND verification_reason IS NULL AND verified_by IS NULL AND decision_audit_id IS NULL) OR (verification_status!='pending_verification' AND criteria_results_json IS NOT NULL AND verification_reason IS NOT NULL AND verified_by IS NOT NULL AND decision_audit_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_wallet_reward_submission_links_status ON wallet_reward_submission_links(subject_id,workflow_id,verification_status,created_at,submission_id);
CREATE TABLE IF NOT EXISTS wallet_reward_incidents (
    incident_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    workflow_id TEXT NOT NULL REFERENCES wallet_reward_workflows(workflow_id),
    submission_id TEXT REFERENCES wallet_bounty_submissions(submission_id),
    execution_id TEXT REFERENCES wallet_payment_executions(execution_id),
    kind TEXT NOT NULL CHECK(kind IN ('source_mismatch','evidence_mismatch','payment_unknown','chain_reorganization','recovery_mismatch')),
    idempotency_key TEXT NOT NULL CHECK(length(trim(idempotency_key)) BETWEEN 1 AND 256),
    status TEXT NOT NULL CHECK(status IN ('open','resolved')),
    reason TEXT NOT NULL CHECK(length(trim(reason)) BETWEEN 1 AND 2000),
    resolution TEXT,
    opened_audit_id TEXT NOT NULL REFERENCES audit_records(audit_id),
    resolved_audit_id TEXT REFERENCES audit_records(audit_id),
    state_hash TEXT NOT NULL CHECK(length(state_hash)=64 AND state_hash NOT GLOB '*[^0-9a-f]*'),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(subject_id,idempotency_key),
    CHECK((status='open' AND resolution IS NULL AND resolved_audit_id IS NULL) OR (status='resolved' AND resolution IS NOT NULL AND resolved_audit_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_wallet_reward_incidents_open ON wallet_reward_incidents(subject_id,status,created_at,incident_id);
CREATE TRIGGER IF NOT EXISTS prevent_wallet_reward_workflow_delete BEFORE DELETE ON wallet_reward_workflows BEGIN SELECT RAISE(ABORT,'wallet reward workflows cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_reward_submission_link_delete BEFORE DELETE ON wallet_reward_submission_links BEGIN SELECT RAISE(ABORT,'wallet reward submission links cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_reward_incident_delete BEFORE DELETE ON wallet_reward_incidents BEGIN SELECT RAISE(ABORT,'wallet reward incidents cannot be deleted'); END;
""",
    58: """
-- Expand the bounded reward incident taxonomy while preserving existing rows.
-- SQLite cannot alter a CHECK constraint in place, so rebuild this append-only
-- table and retain all provenance, audit, and state-hash values verbatim.
DROP TRIGGER IF EXISTS prevent_wallet_reward_incident_delete;
CREATE TABLE wallet_reward_incidents_v58 (
    incident_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    workflow_id TEXT NOT NULL REFERENCES wallet_reward_workflows(workflow_id),
    submission_id TEXT REFERENCES wallet_bounty_submissions(submission_id),
    execution_id TEXT REFERENCES wallet_payment_executions(execution_id),
    kind TEXT NOT NULL CHECK(kind IN (
        'source_mismatch','evidence_mismatch','payment_unknown',
        'chain_reorganization','recovery_mismatch','signer_rejection',
        'broadcast_unknown','receipt_chain_unknown'
    )),
    idempotency_key TEXT NOT NULL CHECK(length(trim(idempotency_key)) BETWEEN 1 AND 256),
    status TEXT NOT NULL CHECK(status IN ('open','resolved')),
    reason TEXT NOT NULL CHECK(length(trim(reason)) BETWEEN 1 AND 2000),
    resolution TEXT,
    opened_audit_id TEXT NOT NULL REFERENCES audit_records(audit_id),
    resolved_audit_id TEXT REFERENCES audit_records(audit_id),
    state_hash TEXT NOT NULL CHECK(length(state_hash)=64 AND state_hash NOT GLOB '*[^0-9a-f]*'),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(subject_id,idempotency_key),
    CHECK((status='open' AND resolution IS NULL AND resolved_audit_id IS NULL) OR (status='resolved' AND resolution IS NOT NULL AND resolved_audit_id IS NOT NULL))
);
INSERT INTO wallet_reward_incidents_v58(
    incident_id,subject_id,workflow_id,submission_id,execution_id,kind,
    idempotency_key,status,reason,resolution,opened_audit_id,resolved_audit_id,
    state_hash,created_at,updated_at
)
SELECT incident_id,subject_id,workflow_id,submission_id,execution_id,kind,
       idempotency_key,status,reason,resolution,opened_audit_id,resolved_audit_id,
       state_hash,created_at,updated_at
FROM wallet_reward_incidents;
DROP TABLE wallet_reward_incidents;
ALTER TABLE wallet_reward_incidents_v58 RENAME TO wallet_reward_incidents;
CREATE INDEX IF NOT EXISTS idx_wallet_reward_incidents_open
    ON wallet_reward_incidents(subject_id,status,created_at,incident_id);
CREATE TRIGGER IF NOT EXISTS prevent_wallet_reward_incident_delete
BEFORE DELETE ON wallet_reward_incidents BEGIN
    SELECT RAISE(ABORT,'wallet reward incidents cannot be deleted');
END;
""",
    59: """
-- Submission consent is immutable provenance and participates in the durable
-- state hash. The column addition and hash backfill run in the Python hook so
-- replaying an interrupted migration remains idempotent.
DROP TRIGGER IF EXISTS prevent_wallet_submission_identity_update;
DROP TRIGGER IF EXISTS validate_wallet_submission_transition;
CREATE TRIGGER prevent_wallet_submission_identity_update
BEFORE UPDATE OF submission_id,bounty_id,subject_id,counterparty,content,evidence_json,
                 recipient_address,idempotency_key,consent_version,created_at,created_audit_id
ON wallet_bounty_submissions
BEGIN SELECT RAISE(ABORT,'wallet submission identity is immutable'); END;
CREATE TRIGGER validate_wallet_submission_transition
BEFORE UPDATE ON wallet_bounty_submissions
WHEN NOT (
    NEW.status = OLD.status AND NEW.state_hash = OLD.state_hash
    OR OLD.status = 'submitted' AND NEW.status IN ('accepted','rejected','withdrawn','expired')
)
BEGIN SELECT RAISE(ABORT,'wallet submission transition is invalid'); END;
""",
    60: """
-- Wallet payment policy hashes now cover every durable policy field. The
-- Python migration hook validates legacy hashes before backfilling the new
-- complete hash so a damaged row is never silently trusted.
CREATE INDEX IF NOT EXISTS idx_wallet_ledger_entries_subject
    ON wallet_ledger_entries(subject_id, journal_id, entry_id);
    CREATE INDEX IF NOT EXISTS idx_wallet_ledger_entries_journal_entry
    ON wallet_ledger_entries(journal_id, entry_id);
""",
    61: """
-- Payment order/execution chronology is backfilled by the Python migration
-- hook after validating the schema-60 hashes.  Keep this DDL idempotent.
DROP TRIGGER IF EXISTS prevent_wallet_order_identity_update;
CREATE TRIGGER prevent_wallet_order_identity_update
BEFORE UPDATE OF order_id,subject_id,bounty_id,submission_id,network_id,asset_id,
                 recipient_address,amount,payment_mode,policy_version,idempotency_key,
                 created_audit_id,created_at ON wallet_payment_orders
BEGIN SELECT RAISE(ABORT,'wallet payment order identity is immutable'); END;
DROP TRIGGER IF EXISTS validate_wallet_order_transition;
CREATE TRIGGER validate_wallet_order_transition
BEFORE UPDATE ON wallet_payment_orders
WHEN NOT (
    NEW.status = OLD.status AND NEW.state_hash = OLD.state_hash AND NEW.updated_at = OLD.updated_at
    OR OLD.status = 'pending_policy' AND NEW.status IN ('awaiting_confirmation','reserved','rejected','cancelled','expired')
    OR OLD.status = 'awaiting_confirmation' AND NEW.status IN ('reserved','rejected','cancelled','expired')
    OR OLD.status = 'reserved' AND NEW.status IN ('cancelled','expired','signing')
    OR OLD.status = 'signing' AND NEW.status IN ('broadcast','unknown','failed')
    OR OLD.status = 'broadcast' AND NEW.status IN ('unknown','confirmed','failed')
    OR OLD.status = 'unknown' AND NEW.status IN ('signing','broadcast','confirmed','failed','refunded')
    OR OLD.status = 'failed' AND NEW.status = 'refunded'
)
BEGIN SELECT RAISE(ABORT,'wallet payment order transition is invalid'); END;
""",
    62: """
-- Receipt evidence is durable: a block number alone cannot detect a same
-- height reorganization or prove an ERC-20 transfer effect.
DROP INDEX IF EXISTS uq_wallet_payment_execution_nonce;
CREATE UNIQUE INDEX uq_wallet_payment_execution_nonce
    ON wallet_payment_executions(subject_id,network_id,source_address,nonce)
    WHERE status IN ('signing','broadcast','unknown','confirmed') OR tx_hash IS NOT NULL;
DROP TRIGGER IF EXISTS validate_wallet_execution_transition;
CREATE TRIGGER validate_wallet_execution_transition
BEFORE UPDATE ON wallet_payment_executions
WHEN NOT (
    NEW.status IS OLD.status
        AND NEW.tx_hash IS OLD.tx_hash
        AND NEW.error_code IS OLD.error_code
        AND NEW.receipt_status IS OLD.receipt_status
        AND NEW.receipt_block_number IS OLD.receipt_block_number
        AND NEW.receipt_block_hash IS OLD.receipt_block_hash
        AND NEW.receipt_confirmations IS OLD.receipt_confirmations
        AND NEW.receipt_effect_hash IS OLD.receipt_effect_hash
        AND NEW.attempt_count IS OLD.attempt_count
        AND NEW.state_hash IS OLD.state_hash
        AND NEW.last_audit_id IS OLD.last_audit_id
        AND NEW.updated_at IS OLD.updated_at
    OR OLD.status='signing' AND NEW.status IN ('broadcast','unknown','failed')
    OR OLD.status='broadcast' AND NEW.status IN ('unknown','confirmed','failed')
    OR OLD.status='unknown' AND NEW.status IN ('signing','broadcast','confirmed','failed')
)
BEGIN SELECT RAISE(ABORT,'wallet payment execution transition is invalid'); END;
""",
    63: """
-- Revoked wallet addresses remain immutable audit history, but no longer
-- reserve their label or address forever. Rebuild the table because SQLite
-- cannot remove table-level UNIQUE constraints in place.
DROP TRIGGER IF EXISTS validate_wallet_address_creation_audit;
DROP TRIGGER IF EXISTS prevent_wallet_address_identity_update;
DROP TRIGGER IF EXISTS validate_wallet_address_transition;
DROP TRIGGER IF EXISTS prevent_wallet_address_delete;
DROP TRIGGER IF EXISTS validate_wallet_address_revision_insert;
DROP TRIGGER IF EXISTS prevent_wallet_address_revision_update;
DROP TRIGGER IF EXISTS prevent_wallet_address_revision_delete;
DROP TRIGGER IF EXISTS validate_wallet_network_transition;
DROP TRIGGER IF EXISTS validate_wallet_balance_snapshot_insert;
DROP TRIGGER IF EXISTS validate_wallet_balance_acquisition_run_insert;
DROP TRIGGER IF EXISTS validate_wallet_address_revisions_address_id_subject_insert;
DROP TRIGGER IF EXISTS validate_wallet_address_revisions_address_id_subject_update;
DROP TRIGGER IF EXISTS validate_wallet_balance_snapshots_address_id_subject_insert;
DROP TRIGGER IF EXISTS validate_wallet_balance_snapshots_address_id_subject_update;
DROP TRIGGER IF EXISTS validate_wallet_balance_acquisition_runs_address_id_subject_insert;
DROP TRIGGER IF EXISTS validate_wallet_balance_acquisition_runs_address_id_subject_update;
DROP TRIGGER IF EXISTS validate_wallet_balance_acquisition_attempts_address_id_subject_insert;
DROP TRIGGER IF EXISTS validate_wallet_balance_acquisition_attempts_address_id_subject_update;

CREATE TABLE wallet_addresses_v63 (
    address_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    network_id TEXT NOT NULL REFERENCES wallet_networks(network_id),
    label TEXT NOT NULL CHECK (label = trim(label) AND length(label) BETWEEN 1 AND 128),
    address TEXT NOT NULL CHECK (
        length(address) = 42
        AND substr(address, 1, 2) = '0x'
        AND lower(address) = address
        AND substr(address, 3) NOT GLOB '*[^0-9a-f]*'
    ),
    purpose TEXT NOT NULL CHECK (
        purpose IN ('treasury', 'spending', 'observation', 'external')
    ),
    status TEXT NOT NULL CHECK (status IN ('active', 'revoked')),
    state_hash TEXT NOT NULL CHECK (
        length(state_hash) = 64 AND state_hash NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    revoke_reason TEXT,
    created_audit_id TEXT NOT NULL REFERENCES audit_records(audit_id),
    revoked_audit_id TEXT REFERENCES audit_records(audit_id),
    CHECK (
        (status = 'active' AND revoked_at IS NULL AND revoke_reason IS NULL
         AND revoked_audit_id IS NULL)
        OR (status = 'revoked' AND revoked_at IS NOT NULL
            AND revoke_reason IS NOT NULL AND length(trim(revoke_reason)) > 0
            AND revoked_audit_id IS NOT NULL)
    )
);
INSERT INTO wallet_addresses_v63(
    address_id, subject_id, network_id, label, address, purpose, status,
    state_hash, created_at, revoked_at, revoke_reason,
    created_audit_id, revoked_audit_id
)
SELECT
    address_id, subject_id, network_id, label, address, purpose, status,
    state_hash, created_at, revoked_at, revoke_reason,
    created_audit_id, revoked_audit_id
FROM wallet_addresses;
DROP TABLE wallet_addresses;
ALTER TABLE wallet_addresses_v63 RENAME TO wallet_addresses;

CREATE UNIQUE INDEX uq_wallet_addresses_active_label
    ON wallet_addresses(subject_id, network_id, label)
    WHERE status = 'active';
CREATE UNIQUE INDEX uq_wallet_addresses_active_address
    ON wallet_addresses(subject_id, network_id, address)
    WHERE status = 'active';
CREATE INDEX idx_wallet_addresses_subject_network_status
    ON wallet_addresses(subject_id, network_id, status, created_at DESC, address_id);

CREATE TRIGGER validate_wallet_address_creation_audit
BEFORE INSERT ON wallet_addresses
WHEN NOT EXISTS (
    SELECT 1 FROM audit_records a
    WHERE a.audit_id = NEW.created_audit_id
      AND a.subject_id = NEW.subject_id
      AND a.action = 'wallet_address_registered'
)
BEGIN
    SELECT RAISE(ABORT, 'wallet address creation audit mismatch');
END;
CREATE TRIGGER prevent_wallet_address_identity_update
BEFORE UPDATE OF address_id, subject_id, network_id, label, address, purpose,
                 created_at, created_audit_id ON wallet_addresses
BEGIN
    SELECT RAISE(ABORT, 'wallet address identity is immutable');
END;
CREATE TRIGGER validate_wallet_address_transition
BEFORE UPDATE ON wallet_addresses
WHEN NOT (
    (NEW.status = OLD.status
     AND NEW.state_hash = OLD.state_hash
     AND NEW.revoked_at IS OLD.revoked_at
     AND NEW.revoke_reason IS OLD.revoke_reason
     AND NEW.revoked_audit_id IS OLD.revoked_audit_id)
    OR (
        OLD.status = 'active' AND NEW.status = 'revoked'
        AND NEW.state_hash <> OLD.state_hash
        AND NEW.revoked_at IS NOT NULL
        AND NEW.revoke_reason IS NOT NULL
        AND NEW.revoked_audit_id IS NOT NULL
        AND EXISTS (
            SELECT 1 FROM audit_records a
            WHERE a.audit_id = NEW.revoked_audit_id
              AND a.subject_id = NEW.subject_id
              AND a.action = 'wallet_address_revoked'
        )
    )
)
BEGIN
    SELECT RAISE(ABORT, 'wallet address transition is invalid');
END;
CREATE TRIGGER prevent_wallet_address_delete
BEFORE DELETE ON wallet_addresses
BEGIN
    SELECT RAISE(ABORT, 'wallet addresses cannot be deleted');
END;
CREATE TRIGGER validate_wallet_address_revision_insert
BEFORE INSERT ON wallet_address_revisions
WHEN NOT EXISTS (
    SELECT 1 FROM wallet_addresses a
    JOIN audit_records audit ON audit.audit_id = NEW.audit_id
    WHERE a.address_id = NEW.address_id
      AND a.subject_id = NEW.subject_id
      AND a.status = NEW.status
      AND audit.subject_id = NEW.subject_id
      AND ((NEW.status = 'active' AND audit.action = 'wallet_address_registered')
           OR (NEW.status = 'revoked' AND audit.action = 'wallet_address_revoked'))
) OR NEW.revision_number != COALESCE((
    SELECT MAX(r.revision_number) + 1 FROM wallet_address_revisions r
    WHERE r.address_id = NEW.address_id
), 1) OR (NEW.revision_number = 1 AND NEW.status != 'active')
OR (NEW.revision_number = 2 AND (
    NEW.status != 'revoked' OR NOT EXISTS (
        SELECT 1 FROM wallet_address_revisions r
        WHERE r.address_id = NEW.address_id AND r.revision_number = 1 AND r.status = 'active'
    )
))
BEGIN
    SELECT RAISE(ABORT, 'wallet address revision is invalid');
END;
CREATE TRIGGER prevent_wallet_address_revision_update
BEFORE UPDATE ON wallet_address_revisions
BEGIN
    SELECT RAISE(ABORT, 'wallet address revisions are append-only');
END;
CREATE TRIGGER prevent_wallet_address_revision_delete
BEFORE DELETE ON wallet_address_revisions
BEGIN
    SELECT RAISE(ABORT, 'wallet address revisions cannot be deleted');
END;

CREATE TRIGGER validate_wallet_network_transition
BEFORE UPDATE ON wallet_networks
WHEN NOT (
    (NEW.status = OLD.status
     AND NEW.state_hash = OLD.state_hash
     AND NEW.revoked_at IS OLD.revoked_at
     AND NEW.revoke_reason IS OLD.revoke_reason
     AND NEW.revoked_audit_id IS OLD.revoked_audit_id)
    OR (
        OLD.status = 'active' AND NEW.status = 'revoked'
        AND NEW.state_hash <> OLD.state_hash
        AND NEW.revoked_at IS NOT NULL
        AND NEW.revoke_reason IS NOT NULL
        AND NEW.revoked_audit_id IS NOT NULL
        AND EXISTS (
            SELECT 1 FROM audit_records a
            WHERE a.audit_id = NEW.revoked_audit_id
              AND a.subject_id = NEW.subject_id
              AND a.action = 'wallet_network_revoked'
        )
        AND NOT EXISTS (
            SELECT 1 FROM wallet_assets a
            WHERE a.subject_id = NEW.subject_id AND a.network_id = NEW.network_id
              AND a.status = 'active'
        )
        AND NOT EXISTS (
            SELECT 1 FROM wallet_addresses a
            WHERE a.subject_id = NEW.subject_id AND a.network_id = NEW.network_id
              AND a.status = 'active'
        )
    )
)
BEGIN
    SELECT RAISE(ABORT, 'wallet network transition is invalid');
END;
CREATE TRIGGER validate_wallet_balance_snapshot_insert
BEFORE INSERT ON wallet_balance_snapshots
WHEN NOT EXISTS (
    SELECT 1 FROM wallet_networks n
    JOIN wallet_assets a ON a.asset_id = NEW.asset_id
    JOIN wallet_addresses d ON d.address_id = NEW.address_id
    WHERE n.network_id = NEW.network_id
      AND n.subject_id = NEW.subject_id
      AND a.subject_id = NEW.subject_id AND a.network_id = NEW.network_id
      AND d.subject_id = NEW.subject_id AND d.network_id = NEW.network_id
)
BEGIN
    SELECT RAISE(ABORT, 'wallet balance snapshot reference mismatch');
END;
CREATE TRIGGER validate_wallet_balance_acquisition_run_insert
BEFORE INSERT ON wallet_balance_acquisition_runs
WHEN NOT EXISTS (
    SELECT 1 FROM wallet_networks n
    JOIN wallet_assets a ON a.asset_id = NEW.asset_id
    JOIN wallet_addresses d ON d.address_id = NEW.address_id
    JOIN audit_records audit ON audit.audit_id = NEW.created_audit_id
    WHERE n.network_id = NEW.network_id
      AND n.subject_id = NEW.subject_id
      AND a.subject_id = NEW.subject_id AND a.network_id = NEW.network_id
      AND d.subject_id = NEW.subject_id AND d.network_id = NEW.network_id
      AND audit.subject_id = NEW.subject_id
      AND audit.action = 'wallet_balance_acquisition_queued'
)
BEGIN
    SELECT RAISE(ABORT, 'wallet balance acquisition run reference mismatch');
END;
""",
    64: """
CREATE TABLE IF NOT EXISTS search_routing_settings (
    subject_id TEXT PRIMARY KEY REFERENCES subject_identity(subject_id),
    mode TEXT NOT NULL CHECK (mode IN ('model_first', 'api_first', 'auto')),
    state_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS search_provider_controls (
    config_id TEXT PRIMARY KEY REFERENCES search_provider_configs(config_id),
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    state_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_search_provider_controls_config
    ON search_provider_controls(config_id, enabled);
""",
    65: """
CREATE TABLE IF NOT EXISTS search_provider_controls_v65 (
    config_id TEXT PRIMARY KEY REFERENCES search_provider_configs(config_id),
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    state_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
INSERT OR REPLACE INTO search_provider_controls_v65(config_id, enabled, state_hash, updated_at)
SELECT config_id, enabled, state_hash, updated_at FROM search_provider_controls;
DROP TABLE IF EXISTS search_provider_controls;
ALTER TABLE search_provider_controls_v65 RENAME TO search_provider_controls;
CREATE INDEX IF NOT EXISTS idx_search_provider_controls_config
    ON search_provider_controls(config_id, enabled);
""",
    66: """
-- Optional recipient allow-list fields are added and included in the policy
-- state hash by the Python migration hook.  Keeping the DDL marker empty
-- makes the migration replay-safe for fresh schemas whose base DDL already
-- contains the columns.
SELECT 1;
""",
    67: """
-- Search-use rows only support the rolling one-hour provider rate limit.
-- Preserve a two-hour minimum window while allowing retention to remove older
-- operational detail; the action ledger remains the durable audit record.
DROP TRIGGER IF EXISTS prevent_search_provider_use_delete;
CREATE TRIGGER prevent_search_provider_use_delete
BEFORE DELETE ON search_provider_uses
WHEN julianday(OLD.created_at) IS NULL
  OR julianday(OLD.created_at) >= julianday('now', '-2 hours')
BEGIN
    SELECT RAISE(ABORT, 'recent search provider uses cannot be deleted');
END;
    """,
    68: """
CREATE TABLE IF NOT EXISTS provider_health_buckets (
    subject_id TEXT NOT NULL,
    provider_kind TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    bucket_start TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    latency_total_ms INTEGER NOT NULL DEFAULT 0,
    last_success_at TEXT,
    last_failure_at TEXT,
    state_hash TEXT NOT NULL,
    PRIMARY KEY(subject_id, provider_kind, provider_id, bucket_start)
);
CREATE TABLE IF NOT EXISTS provider_health_state (
    subject_id TEXT NOT NULL,
    provider_kind TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    state TEXT NOT NULL,
    cooldown_until TEXT,
    probe_token TEXT,
    probe_started_at TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_success_at TEXT,
    last_failure_at TEXT,
    updated_at TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    PRIMARY KEY(subject_id, provider_kind, provider_id)
);
CREATE TABLE IF NOT EXISTS retention_runs (
    run_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    deleted_by_table_json TEXT NOT NULL,
    protected_rows INTEGER NOT NULL DEFAULT 0,
    failed_reason TEXT,
    next_cursor TEXT,
    state_hash TEXT NOT NULL,
    failure_stage TEXT,
    retry_at TEXT,
    failure_count INTEGER NOT NULL DEFAULT 0,
    protected_rows_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_retention_runs_subject_time
    ON retention_runs(subject_id, started_at DESC);
""",
    69: """
-- Provider health metric columns are added by the idempotent migration hook.
-- Keeping the SQL marker replay-safe matters for databases whose schema marker
-- is deliberately rewound during migration verification.
SELECT 1;
""",
}
