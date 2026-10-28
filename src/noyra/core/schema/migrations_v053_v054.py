"""Migrations v053 v054 definitions; execution remains in core.database."""

from __future__ import annotations

MIGRATIONS_V053_V054: dict[int, str] = {
    53: """
-- Stage 4-A-1 intentionally registers only public chain metadata, public
-- addresses, and observed balances. No signer, transaction, or credential
-- material belongs in this schema.
CREATE TABLE IF NOT EXISTS wallet_networks (
    network_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    label TEXT NOT NULL CHECK (label = trim(label) AND length(label) BETWEEN 1 AND 128),
    chain_family TEXT NOT NULL CHECK (chain_family IN ('evm')),
    chain_id INTEGER NOT NULL CHECK (chain_id >= 1),
    native_symbol TEXT NOT NULL CHECK (
        length(native_symbol) BETWEEN 1 AND 32
        AND native_symbol = upper(native_symbol)
        AND native_symbol NOT GLOB '*[^A-Z0-9._-]*'
    ),
    rpc_url TEXT CHECK (rpc_url IS NULL OR length(rpc_url) BETWEEN 1 AND 512),
    status TEXT NOT NULL CHECK (status IN ('active', 'revoked')),
    state_hash TEXT NOT NULL CHECK (
        length(state_hash) = 64 AND state_hash NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    revoke_reason TEXT,
    created_audit_id TEXT NOT NULL REFERENCES audit_records(audit_id),
    revoked_audit_id TEXT REFERENCES audit_records(audit_id),
    UNIQUE(subject_id, label),
    UNIQUE(subject_id, chain_family, chain_id),
    CHECK (
        (status = 'active' AND revoked_at IS NULL AND revoke_reason IS NULL
         AND revoked_audit_id IS NULL)
        OR (status = 'revoked' AND revoked_at IS NOT NULL
            AND revoke_reason IS NOT NULL AND length(trim(revoke_reason)) > 0
            AND revoked_audit_id IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_wallet_networks_subject_status
    ON wallet_networks(subject_id, status, created_at DESC, network_id);

CREATE TABLE IF NOT EXISTS wallet_network_revisions (
    revision_id TEXT PRIMARY KEY,
    network_id TEXT NOT NULL REFERENCES wallet_networks(network_id),
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    revision_number INTEGER NOT NULL CHECK (revision_number BETWEEN 1 AND 2),
    status TEXT NOT NULL CHECK (status IN ('active', 'revoked')),
    reason TEXT NOT NULL CHECK (length(trim(reason)) BETWEEN 1 AND 2000),
    audit_id TEXT NOT NULL REFERENCES audit_records(audit_id),
    state_hash TEXT NOT NULL CHECK (
        length(state_hash) = 64 AND state_hash NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL,
    UNIQUE(network_id, revision_number)
);
CREATE INDEX IF NOT EXISTS idx_wallet_network_revisions_subject
    ON wallet_network_revisions(subject_id, network_id, revision_number);

CREATE TABLE IF NOT EXISTS wallet_assets (
    asset_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    network_id TEXT NOT NULL REFERENCES wallet_networks(network_id),
    asset_type TEXT NOT NULL CHECK (asset_type IN ('native', 'token')),
    contract_address TEXT,
    name TEXT NOT NULL CHECK (name = trim(name) AND length(name) BETWEEN 1 AND 128),
    symbol TEXT NOT NULL CHECK (
        length(symbol) BETWEEN 1 AND 32
        AND symbol = upper(symbol)
        AND symbol NOT GLOB '*[^A-Z0-9._-]*'
    ),
    decimals INTEGER NOT NULL CHECK (decimals BETWEEN 0 AND 255),
    status TEXT NOT NULL CHECK (status IN ('active', 'revoked')),
    state_hash TEXT NOT NULL CHECK (
        length(state_hash) = 64 AND state_hash NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    revoke_reason TEXT,
    created_audit_id TEXT NOT NULL REFERENCES audit_records(audit_id),
    revoked_audit_id TEXT REFERENCES audit_records(audit_id),
    UNIQUE(subject_id, network_id, contract_address),
    CHECK (
        (asset_type = 'native' AND contract_address IS NULL)
        OR (asset_type = 'token' AND contract_address IS NOT NULL
            AND length(contract_address) = 42
            AND substr(contract_address, 1, 2) = '0x'
            AND lower(contract_address) = contract_address
            AND substr(contract_address, 3) NOT GLOB '*[^0-9a-f]*')
    ),
    CHECK (
        (status = 'active' AND revoked_at IS NULL AND revoke_reason IS NULL
         AND revoked_audit_id IS NULL)
        OR (status = 'revoked' AND revoked_at IS NOT NULL
            AND revoke_reason IS NOT NULL AND length(trim(revoke_reason)) > 0
            AND revoked_audit_id IS NOT NULL)
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_wallet_assets_native_network
    ON wallet_assets(subject_id, network_id) WHERE asset_type = 'native';
CREATE INDEX IF NOT EXISTS idx_wallet_assets_subject_network_status
    ON wallet_assets(subject_id, network_id, status, created_at DESC, asset_id);

CREATE TABLE IF NOT EXISTS wallet_asset_revisions (
    revision_id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES wallet_assets(asset_id),
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    revision_number INTEGER NOT NULL CHECK (revision_number BETWEEN 1 AND 2),
    status TEXT NOT NULL CHECK (status IN ('active', 'revoked')),
    reason TEXT NOT NULL CHECK (length(trim(reason)) BETWEEN 1 AND 2000),
    audit_id TEXT NOT NULL REFERENCES audit_records(audit_id),
    state_hash TEXT NOT NULL CHECK (
        length(state_hash) = 64 AND state_hash NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL,
    UNIQUE(asset_id, revision_number)
);
CREATE INDEX IF NOT EXISTS idx_wallet_asset_revisions_subject
    ON wallet_asset_revisions(subject_id, asset_id, revision_number);

CREATE TABLE IF NOT EXISTS wallet_addresses (
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
    UNIQUE(subject_id, network_id, label),
    UNIQUE(subject_id, network_id, address),
    CHECK (
        (status = 'active' AND revoked_at IS NULL AND revoke_reason IS NULL
         AND revoked_audit_id IS NULL)
        OR (status = 'revoked' AND revoked_at IS NOT NULL
            AND revoke_reason IS NOT NULL AND length(trim(revoke_reason)) > 0
            AND revoked_audit_id IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_wallet_addresses_subject_network_status
    ON wallet_addresses(subject_id, network_id, status, created_at DESC, address_id);

CREATE TABLE IF NOT EXISTS wallet_address_revisions (
    revision_id TEXT PRIMARY KEY,
    address_id TEXT NOT NULL REFERENCES wallet_addresses(address_id),
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    revision_number INTEGER NOT NULL CHECK (revision_number BETWEEN 1 AND 2),
    status TEXT NOT NULL CHECK (status IN ('active', 'revoked')),
    reason TEXT NOT NULL CHECK (length(trim(reason)) BETWEEN 1 AND 2000),
    audit_id TEXT NOT NULL REFERENCES audit_records(audit_id),
    state_hash TEXT NOT NULL CHECK (
        length(state_hash) = 64 AND state_hash NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL,
    UNIQUE(address_id, revision_number)
);
CREATE INDEX IF NOT EXISTS idx_wallet_address_revisions_subject
    ON wallet_address_revisions(subject_id, address_id, revision_number);

CREATE TABLE IF NOT EXISTS wallet_balance_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    network_id TEXT NOT NULL REFERENCES wallet_networks(network_id),
    asset_id TEXT NOT NULL REFERENCES wallet_assets(asset_id),
    address_id TEXT NOT NULL REFERENCES wallet_addresses(address_id),
    balance TEXT NOT NULL CHECK (
        length(balance) BETWEEN 1 AND 256
        AND (balance = '0' OR (balance GLOB '[1-9]*' AND balance NOT GLOB '*[^0-9]*'))
    ),
    source TEXT NOT NULL CHECK (
        length(source) BETWEEN 1 AND 64
        AND source GLOB '[a-z]*' AND source NOT GLOB '*[^a-z0-9_]*'
    ),
    observed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    state_hash TEXT NOT NULL CHECK (
        length(state_hash) = 64 AND state_hash NOT GLOB '*[^0-9a-f]*'
    )
);
CREATE INDEX IF NOT EXISTS idx_wallet_balance_snapshots_subject_lookup
    ON wallet_balance_snapshots(
        subject_id, network_id, asset_id, address_id, observed_at DESC, snapshot_id DESC
    );

CREATE TRIGGER IF NOT EXISTS validate_wallet_network_creation_audit
BEFORE INSERT ON wallet_networks
WHEN NOT EXISTS (
    SELECT 1 FROM audit_records a
    WHERE a.audit_id = NEW.created_audit_id
      AND a.subject_id = NEW.subject_id
      AND a.action = 'wallet_network_registered'
)
BEGIN
    SELECT RAISE(ABORT, 'wallet network creation audit mismatch');
END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_network_identity_update
BEFORE UPDATE OF network_id, subject_id, label, chain_family, chain_id,
                 native_symbol, rpc_url, created_at, created_audit_id ON wallet_networks
BEGIN
    SELECT RAISE(ABORT, 'wallet network identity is immutable');
END;
CREATE TRIGGER IF NOT EXISTS validate_wallet_network_transition
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
CREATE TRIGGER IF NOT EXISTS prevent_wallet_network_delete
BEFORE DELETE ON wallet_networks
BEGIN
    SELECT RAISE(ABORT, 'wallet networks cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS validate_wallet_network_revision_insert
BEFORE INSERT ON wallet_network_revisions
WHEN NOT EXISTS (
    SELECT 1 FROM wallet_networks n
    JOIN audit_records a ON a.audit_id = NEW.audit_id
    WHERE n.network_id = NEW.network_id
      AND n.subject_id = NEW.subject_id
      AND n.status = NEW.status
      AND a.subject_id = NEW.subject_id
      AND ((NEW.status = 'active' AND a.action = 'wallet_network_registered')
           OR (NEW.status = 'revoked' AND a.action = 'wallet_network_revoked'))
) OR NEW.revision_number != COALESCE((
    SELECT MAX(r.revision_number) + 1 FROM wallet_network_revisions r
    WHERE r.network_id = NEW.network_id
), 1) OR (NEW.revision_number = 1 AND NEW.status != 'active')
OR (NEW.revision_number = 2 AND (
    NEW.status != 'revoked' OR NOT EXISTS (
        SELECT 1 FROM wallet_network_revisions r
        WHERE r.network_id = NEW.network_id AND r.revision_number = 1 AND r.status = 'active'
    )
))
BEGIN
    SELECT RAISE(ABORT, 'wallet network revision is invalid');
END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_network_revision_update
BEFORE UPDATE ON wallet_network_revisions
BEGIN
    SELECT RAISE(ABORT, 'wallet network revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_network_revision_delete
BEFORE DELETE ON wallet_network_revisions
BEGIN
    SELECT RAISE(ABORT, 'wallet network revisions cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS validate_wallet_asset_creation_audit
BEFORE INSERT ON wallet_assets
WHEN NOT EXISTS (
    SELECT 1 FROM audit_records a
    WHERE a.audit_id = NEW.created_audit_id
      AND a.subject_id = NEW.subject_id
      AND a.action = 'wallet_asset_registered'
)
BEGIN
    SELECT RAISE(ABORT, 'wallet asset creation audit mismatch');
END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_asset_identity_update
BEFORE UPDATE OF asset_id, subject_id, network_id, asset_type, contract_address,
                 name, symbol, decimals, created_at, created_audit_id ON wallet_assets
BEGIN
    SELECT RAISE(ABORT, 'wallet asset identity is immutable');
END;
CREATE TRIGGER IF NOT EXISTS validate_wallet_asset_transition
BEFORE UPDATE ON wallet_assets
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
              AND a.action = 'wallet_asset_revoked'
        )
    )
)
BEGIN
    SELECT RAISE(ABORT, 'wallet asset transition is invalid');
END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_asset_delete
BEFORE DELETE ON wallet_assets
BEGIN
    SELECT RAISE(ABORT, 'wallet assets cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS validate_wallet_asset_revision_insert
BEFORE INSERT ON wallet_asset_revisions
WHEN NOT EXISTS (
    SELECT 1 FROM wallet_assets a
    JOIN audit_records audit ON audit.audit_id = NEW.audit_id
    WHERE a.asset_id = NEW.asset_id
      AND a.subject_id = NEW.subject_id
      AND a.status = NEW.status
      AND audit.subject_id = NEW.subject_id
      AND ((NEW.status = 'active' AND audit.action = 'wallet_asset_registered')
           OR (NEW.status = 'revoked' AND audit.action = 'wallet_asset_revoked'))
) OR NEW.revision_number != COALESCE((
    SELECT MAX(r.revision_number) + 1 FROM wallet_asset_revisions r
    WHERE r.asset_id = NEW.asset_id
), 1) OR (NEW.revision_number = 1 AND NEW.status != 'active')
OR (NEW.revision_number = 2 AND (
    NEW.status != 'revoked' OR NOT EXISTS (
        SELECT 1 FROM wallet_asset_revisions r
        WHERE r.asset_id = NEW.asset_id AND r.revision_number = 1 AND r.status = 'active'
    )
))
BEGIN
    SELECT RAISE(ABORT, 'wallet asset revision is invalid');
END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_asset_revision_update
BEFORE UPDATE ON wallet_asset_revisions
BEGIN
    SELECT RAISE(ABORT, 'wallet asset revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_asset_revision_delete
BEFORE DELETE ON wallet_asset_revisions
BEGIN
    SELECT RAISE(ABORT, 'wallet asset revisions cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS validate_wallet_address_creation_audit
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
CREATE TRIGGER IF NOT EXISTS prevent_wallet_address_identity_update
BEFORE UPDATE OF address_id, subject_id, network_id, label, address, purpose,
                 created_at, created_audit_id ON wallet_addresses
BEGIN
    SELECT RAISE(ABORT, 'wallet address identity is immutable');
END;
CREATE TRIGGER IF NOT EXISTS validate_wallet_address_transition
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
CREATE TRIGGER IF NOT EXISTS prevent_wallet_address_delete
BEFORE DELETE ON wallet_addresses
BEGIN
    SELECT RAISE(ABORT, 'wallet addresses cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS validate_wallet_address_revision_insert
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
CREATE TRIGGER IF NOT EXISTS prevent_wallet_address_revision_update
BEFORE UPDATE ON wallet_address_revisions
BEGIN
    SELECT RAISE(ABORT, 'wallet address revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_address_revision_delete
BEFORE DELETE ON wallet_address_revisions
BEGIN
    SELECT RAISE(ABORT, 'wallet address revisions cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS validate_wallet_balance_snapshot_insert
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
CREATE TRIGGER IF NOT EXISTS prevent_wallet_balance_snapshot_update
BEFORE UPDATE ON wallet_balance_snapshots
BEGIN
    SELECT RAISE(ABORT, 'wallet balance snapshots are append-only');
END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_balance_snapshot_delete
BEFORE DELETE ON wallet_balance_snapshots
BEGIN
    SELECT RAISE(ABORT, 'wallet balance snapshots cannot be deleted');
END;
""",
    54: """
-- Stage 4-A-3 adds only a durable scheduler for the bounded read-only RPC
-- acquisition introduced in 4-A-2.  Runs reserve at most two JSON-RPC calls
-- (chain identity plus one balance read); no transaction or signer material is
-- represented here.
CREATE TABLE IF NOT EXISTS wallet_balance_acquisition_runs (
    run_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    network_id TEXT NOT NULL REFERENCES wallet_networks(network_id),
    asset_id TEXT NOT NULL REFERENCES wallet_assets(asset_id),
    address_id TEXT NOT NULL REFERENCES wallet_addresses(address_id),
    idempotency_key TEXT NOT NULL CHECK (
        length(idempotency_key) BETWEEN 1 AND 128
        AND idempotency_key NOT GLOB '*[^A-Za-z0-9._:-]*'
    ),
    status TEXT NOT NULL CHECK (
        status IN (
            'queued', 'running', 'retry_wait', 'succeeded',
            'failed', 'unknown', 'cancelled'
        )
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count BETWEEN 0 AND 5),
    max_attempts INTEGER NOT NULL CHECK (max_attempts BETWEEN 1 AND 5),
    next_attempt_at TEXT,
    last_error_code TEXT CHECK (
        last_error_code IS NULL OR (
            length(last_error_code) BETWEEN 1 AND 96
            AND last_error_code GLOB 'wallet_rpc_*'
            AND last_error_code NOT GLOB '*[^a-z0-9_]*'
        )
    ),
    snapshot_id TEXT UNIQUE REFERENCES wallet_balance_snapshots(snapshot_id),
    claim_token TEXT UNIQUE,
    lease_owner TEXT CHECK (
        lease_owner IS NULL OR length(trim(lease_owner)) BETWEEN 1 AND 128
    ),
    lease_expires_at TEXT,
    created_audit_id TEXT NOT NULL REFERENCES audit_records(audit_id),
    state_hash TEXT NOT NULL CHECK (
        length(state_hash) = 64 AND state_hash NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL,
    CHECK (attempt_count <= max_attempts),
    CHECK (
        (status = 'queued' AND attempt_count < max_attempts AND next_attempt_at IS NOT NULL
         AND last_error_code IS NULL AND snapshot_id IS NULL
         AND claim_token IS NULL AND lease_owner IS NULL AND lease_expires_at IS NULL
         AND completed_at IS NULL)
        OR (status = 'running' AND attempt_count >= 1 AND next_attempt_at IS NULL
            AND snapshot_id IS NULL AND claim_token IS NOT NULL
            AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL
            AND started_at IS NOT NULL AND completed_at IS NULL)
        OR (status = 'retry_wait' AND attempt_count >= 1
            AND attempt_count < max_attempts AND next_attempt_at IS NOT NULL
            AND last_error_code IS NOT NULL AND snapshot_id IS NULL
            AND claim_token IS NULL AND lease_owner IS NULL AND lease_expires_at IS NULL
            AND started_at IS NOT NULL AND completed_at IS NULL)
        OR (status = 'succeeded' AND attempt_count >= 1 AND next_attempt_at IS NULL
            AND last_error_code IS NULL AND snapshot_id IS NOT NULL
            AND claim_token IS NULL AND lease_owner IS NULL AND lease_expires_at IS NULL
            AND started_at IS NOT NULL AND completed_at IS NOT NULL)
        OR (status IN ('failed', 'unknown') AND attempt_count >= 1
            AND next_attempt_at IS NULL AND last_error_code IS NOT NULL
            AND snapshot_id IS NULL AND claim_token IS NULL AND lease_owner IS NULL
            AND lease_expires_at IS NULL AND started_at IS NOT NULL
            AND completed_at IS NOT NULL)
        OR (status = 'cancelled' AND next_attempt_at IS NULL
            AND last_error_code IS NOT NULL AND snapshot_id IS NULL
            AND claim_token IS NULL AND lease_owner IS NULL AND lease_expires_at IS NULL
            AND completed_at IS NOT NULL)
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_wallet_balance_acquisition_idempotency
    ON wallet_balance_acquisition_runs(subject_id, idempotency_key);
CREATE INDEX IF NOT EXISTS idx_wallet_balance_acquisition_runs_due
    ON wallet_balance_acquisition_runs(
        subject_id, status, next_attempt_at, created_at, run_id
    );
CREATE INDEX IF NOT EXISTS idx_wallet_balance_acquisition_runs_network_lease
    ON wallet_balance_acquisition_runs(
        subject_id, network_id, status, lease_expires_at, run_id
    );
CREATE UNIQUE INDEX IF NOT EXISTS uq_wallet_balance_acquisition_active_target
    ON wallet_balance_acquisition_runs(subject_id, asset_id, address_id)
    WHERE status IN ('queued', 'running', 'retry_wait');

CREATE TABLE IF NOT EXISTS wallet_balance_acquisition_attempts (
    attempt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES wallet_balance_acquisition_runs(run_id),
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    network_id TEXT NOT NULL REFERENCES wallet_networks(network_id),
    asset_id TEXT NOT NULL REFERENCES wallet_assets(asset_id),
    address_id TEXT NOT NULL REFERENCES wallet_addresses(address_id),
    attempt_number INTEGER NOT NULL CHECK (attempt_number BETWEEN 1 AND 5),
    status TEXT NOT NULL CHECK (status IN ('executing', 'succeeded', 'failed', 'unknown')),
    reserved_request_count INTEGER NOT NULL CHECK (reserved_request_count = 2),
    request_count INTEGER CHECK (request_count IS NULL OR request_count BETWEEN 0 AND 2),
    error_code TEXT CHECK (
        error_code IS NULL OR (
            length(error_code) BETWEEN 1 AND 96
            AND error_code GLOB 'wallet_rpc_*'
            AND error_code NOT GLOB '*[^a-z0-9_]*'
        )
    ),
    snapshot_id TEXT UNIQUE REFERENCES wallet_balance_snapshots(snapshot_id),
    claim_token TEXT NOT NULL,
    lease_owner TEXT NOT NULL CHECK (length(trim(lease_owner)) BETWEEN 1 AND 128),
    state_hash TEXT NOT NULL CHECK (
        length(state_hash) = 64 AND state_hash NOT GLOB '*[^0-9a-f]*'
    ),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(run_id, attempt_number),
    UNIQUE(run_id, claim_token),
    CHECK (
        (status = 'executing' AND request_count IS NULL AND error_code IS NULL
         AND snapshot_id IS NULL AND completed_at IS NULL)
        OR (status = 'succeeded' AND request_count = 2 AND error_code IS NULL
            AND snapshot_id IS NOT NULL AND completed_at IS NOT NULL)
        OR (status = 'failed' AND request_count IS NOT NULL AND error_code IS NOT NULL
            AND snapshot_id IS NULL AND completed_at IS NOT NULL)
        OR (status = 'unknown' AND request_count IS NULL AND error_code IS NOT NULL
            AND snapshot_id IS NULL AND completed_at IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_wallet_balance_acquisition_attempts_budget
    ON wallet_balance_acquisition_attempts(
        subject_id, network_id, started_at, attempt_id
    );
CREATE INDEX IF NOT EXISTS idx_wallet_balance_acquisition_attempts_run
    ON wallet_balance_acquisition_attempts(run_id, attempt_number, attempt_id);

DROP TRIGGER IF EXISTS validate_wallet_balance_acquisition_run_insert;
DROP TRIGGER IF EXISTS prevent_wallet_balance_acquisition_run_identity_update;
DROP TRIGGER IF EXISTS prevent_wallet_balance_acquisition_terminal_update;
DROP TRIGGER IF EXISTS validate_wallet_balance_acquisition_run_transition;
DROP TRIGGER IF EXISTS validate_wallet_balance_acquisition_attempt_increment;
DROP TRIGGER IF EXISTS validate_wallet_balance_acquisition_run_completion;
DROP TRIGGER IF EXISTS prevent_wallet_balance_acquisition_run_delete;
DROP TRIGGER IF EXISTS validate_wallet_balance_acquisition_attempt_insert;
DROP TRIGGER IF EXISTS prevent_wallet_balance_acquisition_attempt_identity_update;
DROP TRIGGER IF EXISTS validate_wallet_balance_acquisition_attempt_transition;
DROP TRIGGER IF EXISTS validate_wallet_balance_acquisition_attempt_snapshot;
DROP TRIGGER IF EXISTS prevent_wallet_balance_acquisition_attempt_delete;

CREATE TRIGGER IF NOT EXISTS validate_wallet_balance_acquisition_run_insert
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
CREATE TRIGGER IF NOT EXISTS prevent_wallet_balance_acquisition_run_identity_update
BEFORE UPDATE OF run_id, subject_id, network_id, asset_id, address_id,
                 idempotency_key, max_attempts, created_audit_id, created_at
ON wallet_balance_acquisition_runs
BEGIN
    SELECT RAISE(ABORT, 'wallet balance acquisition run identity is immutable');
END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_balance_acquisition_terminal_update
BEFORE UPDATE ON wallet_balance_acquisition_runs
WHEN OLD.status IN ('succeeded', 'failed', 'cancelled')
  OR (OLD.status = 'unknown' AND NOT (
      NEW.status = 'queued'
      AND NEW.attempt_count = OLD.attempt_count
      AND NEW.max_attempts = OLD.max_attempts
      AND NEW.next_attempt_at IS NOT NULL
      AND NEW.last_error_code IS NULL
      AND NEW.snapshot_id IS NULL
      AND NEW.claim_token IS NULL
      AND NEW.lease_owner IS NULL
      AND NEW.lease_expires_at IS NULL
      AND NEW.started_at IS OLD.started_at
      AND NEW.completed_at IS NULL
      AND NEW.state_hash <> OLD.state_hash
  ))
BEGIN
    SELECT RAISE(ABORT, 'terminal wallet balance acquisition runs are immutable');
END;
CREATE TRIGGER IF NOT EXISTS validate_wallet_balance_acquisition_run_transition
BEFORE UPDATE OF status ON wallet_balance_acquisition_runs
WHEN NOT (
    (OLD.status IN ('queued', 'retry_wait') AND NEW.status = 'running')
    OR (OLD.status IN ('queued', 'retry_wait') AND NEW.status = 'cancelled')
    OR (OLD.status = 'running' AND NEW.status IN (
        'retry_wait', 'succeeded', 'failed', 'unknown'
    ))
    OR (OLD.status = 'unknown' AND NEW.status = 'queued')
)
BEGIN
    SELECT RAISE(ABORT, 'wallet balance acquisition run transition is invalid');
END;
CREATE TRIGGER IF NOT EXISTS validate_wallet_balance_acquisition_attempt_increment
BEFORE UPDATE OF attempt_count ON wallet_balance_acquisition_runs
WHEN NEW.attempt_count != OLD.attempt_count AND NOT (
    OLD.status IN ('queued', 'retry_wait')
    AND NEW.status = 'running'
    AND NEW.attempt_count = OLD.attempt_count + 1
)
BEGIN
    SELECT RAISE(ABORT, 'wallet balance acquisition attempt count is invalid');
END;
CREATE TRIGGER IF NOT EXISTS validate_wallet_balance_acquisition_run_completion
BEFORE UPDATE ON wallet_balance_acquisition_runs
WHEN NEW.status IN ('retry_wait', 'succeeded', 'failed', 'unknown') AND NOT EXISTS (
    SELECT 1 FROM wallet_balance_acquisition_attempts attempt
    WHERE attempt.run_id = NEW.run_id
      AND attempt.subject_id = NEW.subject_id
      AND attempt.attempt_number = NEW.attempt_count
      AND (
          (NEW.status = 'retry_wait' AND attempt.status = 'failed')
          OR (NEW.status != 'retry_wait' AND attempt.status = NEW.status)
      )
      AND attempt.error_code IS NEW.last_error_code
      AND attempt.snapshot_id IS NEW.snapshot_id
)
BEGIN
    SELECT RAISE(ABORT, 'wallet balance acquisition completion evidence is missing');
END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_balance_acquisition_run_delete
BEFORE DELETE ON wallet_balance_acquisition_runs
BEGIN
    SELECT RAISE(ABORT, 'wallet balance acquisition runs cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS validate_wallet_balance_acquisition_attempt_insert
BEFORE INSERT ON wallet_balance_acquisition_attempts
WHEN NOT EXISTS (
    SELECT 1 FROM wallet_balance_acquisition_runs run
    WHERE run.run_id = NEW.run_id
      AND run.subject_id = NEW.subject_id
      AND run.network_id = NEW.network_id
      AND run.asset_id = NEW.asset_id
      AND run.address_id = NEW.address_id
      AND run.status = 'running'
      AND run.attempt_count = NEW.attempt_number
      AND run.claim_token = NEW.claim_token
      AND run.lease_owner = NEW.lease_owner
)
BEGIN
    SELECT RAISE(ABORT, 'wallet balance acquisition attempt reference mismatch');
END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_balance_acquisition_attempt_identity_update
BEFORE UPDATE OF attempt_id, run_id, subject_id, network_id, asset_id, address_id,
                 attempt_number, reserved_request_count, claim_token, lease_owner, started_at
ON wallet_balance_acquisition_attempts
BEGIN
    SELECT RAISE(ABORT, 'wallet balance acquisition attempt identity is immutable');
END;
CREATE TRIGGER IF NOT EXISTS validate_wallet_balance_acquisition_attempt_transition
BEFORE UPDATE ON wallet_balance_acquisition_attempts
WHEN NOT (
    OLD.status = 'executing'
    AND NEW.status IN ('succeeded', 'failed', 'unknown')
    AND NEW.state_hash <> OLD.state_hash
)
BEGIN
    SELECT RAISE(ABORT, 'wallet balance acquisition attempt transition is invalid');
END;
CREATE TRIGGER IF NOT EXISTS validate_wallet_balance_acquisition_attempt_snapshot
BEFORE UPDATE ON wallet_balance_acquisition_attempts
WHEN NEW.status = 'succeeded' AND NOT EXISTS (
    SELECT 1 FROM wallet_balance_snapshots snapshot
    WHERE snapshot.snapshot_id = NEW.snapshot_id
      AND snapshot.subject_id = NEW.subject_id
      AND snapshot.network_id = NEW.network_id
      AND snapshot.asset_id = NEW.asset_id
      AND snapshot.address_id = NEW.address_id
      AND snapshot.source = 'evm_rpc'
)
BEGIN
    SELECT RAISE(ABORT, 'wallet balance acquisition snapshot mismatch');
END;
CREATE TRIGGER IF NOT EXISTS prevent_wallet_balance_acquisition_attempt_delete
BEFORE DELETE ON wallet_balance_acquisition_attempts
BEGIN
    SELECT RAISE(ABORT, 'wallet balance acquisition attempts cannot be deleted');
END;
""",
}
