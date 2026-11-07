# ruff: noqa: E501 -- frozen versioned SQL
"""Additive increment metadata. No file publication or cleanup is enabled here."""

WORKFLOW_INCREMENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_increment_publications (
 publication_id TEXT PRIMARY KEY,
 subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
 workflow_id TEXT NOT NULL,
 parent_base_id TEXT REFERENCES workflow_export_publications(publication_id),
 parent_increment_id TEXT REFERENCES workflow_increment_publications(publication_id),
 parent_ref_json TEXT NOT NULL CHECK(length(parent_ref_json)<=16000 AND json_valid(parent_ref_json)),
 source_json TEXT NOT NULL CHECK(length(source_json)<=32000 AND json_valid(source_json)),
 source_hash TEXT NOT NULL CHECK(length(source_hash)=64),
 first_version INTEGER NOT NULL CHECK(typeof(first_version)='integer' AND first_version>=2),
 last_version INTEGER NOT NULL CHECK(typeof(last_version)='integer' AND last_version>=first_version),
 chain_depth INTEGER NOT NULL CHECK(typeof(chain_depth)='integer' AND chain_depth BETWEEN 1 AND 15),
 encryption_key_id TEXT NOT NULL,
 encryption_key_fingerprint TEXT NOT NULL CHECK(length(encryption_key_fingerprint)=64),
 key_generation INTEGER NOT NULL CHECK(typeof(key_generation)='integer' AND key_generation>=1),
 status TEXT NOT NULL CHECK(status IN ('prepared','published_durability_unknown','accepted')),
 manifest_json TEXT CHECK(manifest_json IS NULL OR (length(manifest_json)<=8192 AND json_valid(manifest_json))),
 receipt_event_id TEXT REFERENCES events(event_id),
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 state_hash TEXT NOT NULL CHECK(length(state_hash)=64),
 CHECK((parent_base_id IS NOT NULL AND parent_increment_id IS NULL)
    OR (parent_base_id IS NULL AND parent_increment_id IS NOT NULL)),
 CHECK(publication_id IS NOT parent_increment_id),
 CHECK((status='prepared' AND manifest_json IS NULL AND receipt_event_id IS NULL)
    OR (status='published_durability_unknown' AND manifest_json IS NOT NULL AND receipt_event_id IS NULL)
    OR (status='accepted' AND manifest_json IS NOT NULL AND receipt_event_id IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_workflow_increment_identity
 ON workflow_increment_publications(subject_id,workflow_id,COALESCE(parent_base_id,parent_increment_id),source_hash);
CREATE INDEX IF NOT EXISTS idx_workflow_increment_subject_time
 ON workflow_increment_publications(subject_id,created_at DESC,publication_id DESC);
CREATE INDEX IF NOT EXISTS idx_workflow_increment_base_dependency
 ON workflow_increment_publications(parent_base_id) WHERE parent_base_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_workflow_increment_delta_dependency
 ON workflow_increment_publications(parent_increment_id) WHERE parent_increment_id IS NOT NULL;
CREATE TRIGGER IF NOT EXISTS preserve_workflow_increment_source
BEFORE UPDATE OF publication_id,subject_id,workflow_id,parent_base_id,parent_increment_id,
 parent_ref_json,source_json,source_hash,first_version,last_version,chain_depth,
 encryption_key_id,encryption_key_fingerprint,key_generation,created_at ON workflow_increment_publications
BEGIN SELECT RAISE(ABORT,'workflow increment source is immutable'); END;
CREATE TRIGGER IF NOT EXISTS preserve_accepted_workflow_increment
BEFORE UPDATE ON workflow_increment_publications WHEN OLD.status='accepted'
BEGIN SELECT RAISE(ABORT,'accepted workflow increment is immutable'); END;
CREATE TRIGGER IF NOT EXISTS preserve_workflow_increment_transition
BEFORE UPDATE ON workflow_increment_publications
WHEN NOT ((OLD.status='prepared' AND NEW.status IN ('accepted','published_durability_unknown'))
 OR (OLD.status='published_durability_unknown' AND NEW.status='accepted' AND NEW.manifest_json IS OLD.manifest_json))
BEGIN SELECT RAISE(ABORT,'workflow increment transition or frozen manifest is invalid'); END;
CREATE TRIGGER IF NOT EXISTS preserve_workflow_increment_delete
BEFORE DELETE ON workflow_increment_publications
BEGIN SELECT RAISE(ABORT,'workflow increments cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS validate_workflow_increment_parent
BEFORE INSERT ON workflow_increment_publications
WHEN NEW.status!='prepared'
 OR json_type(NEW.source_json,'$.records') IS NOT 'integer'
 OR json_extract(NEW.source_json,'$.records')!=NEW.last_version
 OR NOT EXISTS(SELECT 1 FROM workflow_checkpoints WHERE workflow_id=NEW.workflow_id AND subject_id=NEW.subject_id)
 OR (NEW.parent_base_id IS NOT NULL AND NOT EXISTS(
    SELECT 1 FROM workflow_export_publications p WHERE p.publication_id=NEW.parent_base_id
    AND p.status='accepted' AND p.subject_id=NEW.subject_id AND p.workflow_id=NEW.workflow_id
    AND NEW.chain_depth=1 AND NEW.first_version=json_extract(p.source_json,'$.records')+1
    AND json_extract(p.source_json,'$.genesis_hash') IS json_extract(NEW.source_json,'$.genesis_hash')
    AND json_extract(p.source_json,'$.schema_version') IS json_extract(NEW.source_json,'$.schema_version')
    AND json_extract(p.source_json,'$.schema_ddl_hash') IS json_extract(NEW.source_json,'$.schema_ddl_hash')))
 OR (NEW.parent_increment_id IS NOT NULL AND NOT EXISTS(
    SELECT 1 FROM workflow_increment_publications p WHERE p.publication_id=NEW.parent_increment_id
    AND p.status='accepted' AND p.subject_id=NEW.subject_id AND p.workflow_id=NEW.workflow_id
    AND NEW.chain_depth=p.chain_depth+1 AND NEW.first_version=p.last_version+1
    AND json_extract(p.source_json,'$.genesis_hash') IS json_extract(NEW.source_json,'$.genesis_hash')
    AND json_extract(p.source_json,'$.schema_version') IS json_extract(NEW.source_json,'$.schema_version')
    AND json_extract(p.source_json,'$.schema_ddl_hash') IS json_extract(NEW.source_json,'$.schema_ddl_hash')))
BEGIN SELECT RAISE(ABORT,'workflow increment requires an accepted compatible parent'); END;
CREATE TABLE IF NOT EXISTS workflow_increment_requests (
 subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
 request_hash TEXT NOT NULL CHECK(length(request_hash)=64),
 publication_id TEXT NOT NULL REFERENCES workflow_increment_publications(publication_id),
 workflow_id TEXT NOT NULL,
 parent_id TEXT NOT NULL,
 expected_version INTEGER NOT NULL CHECK(typeof(expected_version)='integer' AND expected_version>=2),
 receipt_event_id TEXT NOT NULL REFERENCES events(event_id),
 created_at TEXT NOT NULL,
 state_hash TEXT NOT NULL CHECK(length(state_hash)=64),
 PRIMARY KEY(subject_id,request_hash)
);
CREATE INDEX IF NOT EXISTS idx_workflow_increment_requests_publication
 ON workflow_increment_requests(publication_id);
CREATE TRIGGER IF NOT EXISTS preserve_workflow_increment_request_update
BEFORE UPDATE ON workflow_increment_requests
BEGIN SELECT RAISE(ABORT,'workflow increment requests are immutable'); END;
CREATE TRIGGER IF NOT EXISTS preserve_workflow_increment_request_delete
BEFORE DELETE ON workflow_increment_requests
BEGIN SELECT RAISE(ABORT,'workflow increment requests cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS validate_workflow_increment_request
BEFORE INSERT ON workflow_increment_requests
WHEN NOT EXISTS(SELECT 1 FROM workflow_increment_publications p WHERE p.publication_id=NEW.publication_id
 AND p.subject_id=NEW.subject_id AND p.workflow_id=NEW.workflow_id AND p.last_version=NEW.expected_version
 AND COALESCE(p.parent_base_id,p.parent_increment_id)=NEW.parent_id)
BEGIN SELECT RAISE(ABORT,'workflow increment request intent differs'); END;
"""
