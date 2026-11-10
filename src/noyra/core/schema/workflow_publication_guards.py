"""Schema117 freezes published descriptors and validates catalog write shapes."""

WORKFLOW_PUBLICATION_GUARDS_SCHEMA = """
DROP TRIGGER preserve_workflow_export_transition;
CREATE TRIGGER preserve_workflow_export_transition
BEFORE UPDATE ON workflow_export_publications
WHEN NOT ((OLD.status='prepared' AND NEW.status IN ('accepted','published_durability_unknown'))
 OR (OLD.status='published_durability_unknown' AND NEW.status='accepted'
     AND NEW.manifest_json IS OLD.manifest_json))
BEGIN SELECT RAISE(ABORT,'workflow export transition or frozen manifest is invalid'); END;
DROP TRIGGER IF EXISTS validate_workflow_export_initial_state;
CREATE TRIGGER validate_workflow_export_initial_state
BEFORE INSERT ON workflow_export_publications
WHEN NEW.status!='prepared' OR typeof(NEW.key_generation)!='integer'
BEGIN SELECT RAISE(ABORT,'workflow export requires prepared initial state'); END;

DROP TRIGGER IF EXISTS validate_workflow_export_acceptance;
CREATE TRIGGER validate_workflow_export_acceptance
BEFORE UPDATE ON workflow_export_publications
WHEN NEW.status='accepted' AND (
 NEW.receipt_event_id IS NOT ('evt_wexport_accepted_' || NEW.source_hash)
 OR NOT EXISTS(SELECT 1 FROM events e WHERE e.event_id=NEW.receipt_event_id
  AND e.subject_id=NEW.subject_id AND e.event_type='workflow_export_accepted'
  AND e.source='workflow_export' AND e.privacy_level='private'))
BEGIN SELECT RAISE(ABORT,'workflow export acceptance metadata is invalid'); END;

DROP TRIGGER IF EXISTS validate_workflow_export_manifest_insert;
CREATE TRIGGER validate_workflow_export_manifest_insert
BEFORE INSERT ON workflow_export_publications
WHEN NEW.manifest_json IS NOT NULL AND (CASE WHEN json_valid(NEW.manifest_json) THEN (
 json_type(NEW.manifest_json) IS NOT 'object'
 OR (SELECT count(*) FROM json_each(NEW.manifest_json))!=4
 OR json_type(NEW.manifest_json,'$.name') IS NOT 'text'
 OR json_extract(NEW.manifest_json,'$.name') IS NOT 'manifest.enc'
 OR json_type(NEW.manifest_json,'$.stored_bytes') IS NOT 'integer'
 OR json_extract(NEW.manifest_json,'$.stored_bytes') NOT BETWEEN 37 AND 1048613
 OR json_type(NEW.manifest_json,'$.stored_hash') IS NOT 'text'
 OR length(json_extract(NEW.manifest_json,'$.stored_hash'))!=64
 OR length(CAST(json_extract(NEW.manifest_json,'$.stored_hash') AS BLOB))!=64
 OR json_extract(NEW.manifest_json,'$.stored_hash') GLOB '*[^0-9a-f]*'
 OR json_type(NEW.manifest_json,'$.plaintext_hash') IS NOT 'text'
 OR length(json_extract(NEW.manifest_json,'$.plaintext_hash'))!=64
 OR length(CAST(json_extract(NEW.manifest_json,'$.plaintext_hash') AS BLOB))!=64
 OR json_extract(NEW.manifest_json,'$.plaintext_hash') GLOB '*[^0-9a-f]*'
) ELSE 1 END)
BEGIN SELECT RAISE(ABORT,'workflow manifest descriptor is invalid'); END;

DROP TRIGGER IF EXISTS validate_workflow_export_manifest_update;
CREATE TRIGGER validate_workflow_export_manifest_update
BEFORE UPDATE ON workflow_export_publications
WHEN NEW.manifest_json IS NOT NULL AND (CASE WHEN json_valid(NEW.manifest_json) THEN (
 json_type(NEW.manifest_json) IS NOT 'object'
 OR (SELECT count(*) FROM json_each(NEW.manifest_json))!=4
 OR json_type(NEW.manifest_json,'$.name') IS NOT 'text'
 OR json_extract(NEW.manifest_json,'$.name') IS NOT 'manifest.enc'
 OR json_type(NEW.manifest_json,'$.stored_bytes') IS NOT 'integer'
 OR json_extract(NEW.manifest_json,'$.stored_bytes') NOT BETWEEN 37 AND 1048613
 OR json_type(NEW.manifest_json,'$.stored_hash') IS NOT 'text'
 OR length(json_extract(NEW.manifest_json,'$.stored_hash'))!=64
 OR length(CAST(json_extract(NEW.manifest_json,'$.stored_hash') AS BLOB))!=64
 OR json_extract(NEW.manifest_json,'$.stored_hash') GLOB '*[^0-9a-f]*'
 OR json_type(NEW.manifest_json,'$.plaintext_hash') IS NOT 'text'
 OR length(json_extract(NEW.manifest_json,'$.plaintext_hash'))!=64
 OR length(CAST(json_extract(NEW.manifest_json,'$.plaintext_hash') AS BLOB))!=64
 OR json_extract(NEW.manifest_json,'$.plaintext_hash') GLOB '*[^0-9a-f]*'
) ELSE 1 END)
BEGIN SELECT RAISE(ABORT,'workflow manifest descriptor is invalid'); END;

DROP TRIGGER IF EXISTS validate_workflow_increment_acceptance;
CREATE TRIGGER validate_workflow_increment_acceptance
BEFORE UPDATE ON workflow_increment_publications
WHEN NEW.status='accepted' AND (
 NEW.receipt_event_id IS NOT ('evt_wincrement_accepted_' || NEW.publication_id)
 OR NOT EXISTS(SELECT 1 FROM events e WHERE e.event_id=NEW.receipt_event_id
  AND e.subject_id=NEW.subject_id AND e.event_type='workflow_increment_accepted'
  AND e.source='workflow_increment' AND e.privacy_level='private'))
BEGIN SELECT RAISE(ABORT,'workflow increment acceptance metadata is invalid'); END;

DROP TRIGGER IF EXISTS validate_workflow_increment_manifest_insert;
CREATE TRIGGER validate_workflow_increment_manifest_insert
BEFORE INSERT ON workflow_increment_publications
WHEN NEW.manifest_json IS NOT NULL AND (CASE WHEN json_valid(NEW.manifest_json) THEN (
 json_type(NEW.manifest_json) IS NOT 'object'
 OR (SELECT count(*) FROM json_each(NEW.manifest_json))!=4
 OR json_type(NEW.manifest_json,'$.name') IS NOT 'text'
 OR json_extract(NEW.manifest_json,'$.name') IS NOT 'manifest.enc'
 OR json_type(NEW.manifest_json,'$.stored_bytes') IS NOT 'integer'
 OR json_extract(NEW.manifest_json,'$.stored_bytes') NOT BETWEEN 37 AND 1048613
 OR json_type(NEW.manifest_json,'$.stored_hash') IS NOT 'text'
 OR length(json_extract(NEW.manifest_json,'$.stored_hash'))!=64
 OR length(CAST(json_extract(NEW.manifest_json,'$.stored_hash') AS BLOB))!=64
 OR json_extract(NEW.manifest_json,'$.stored_hash') GLOB '*[^0-9a-f]*'
 OR json_type(NEW.manifest_json,'$.plaintext_hash') IS NOT 'text'
 OR length(json_extract(NEW.manifest_json,'$.plaintext_hash'))!=64
 OR length(CAST(json_extract(NEW.manifest_json,'$.plaintext_hash') AS BLOB))!=64
 OR json_extract(NEW.manifest_json,'$.plaintext_hash') GLOB '*[^0-9a-f]*'
) ELSE 1 END)
BEGIN SELECT RAISE(ABORT,'workflow manifest descriptor is invalid'); END;

DROP TRIGGER IF EXISTS validate_workflow_increment_manifest_update;
CREATE TRIGGER validate_workflow_increment_manifest_update
BEFORE UPDATE ON workflow_increment_publications
WHEN NEW.manifest_json IS NOT NULL AND (CASE WHEN json_valid(NEW.manifest_json) THEN (
 json_type(NEW.manifest_json) IS NOT 'object'
 OR (SELECT count(*) FROM json_each(NEW.manifest_json))!=4
 OR json_type(NEW.manifest_json,'$.name') IS NOT 'text'
 OR json_extract(NEW.manifest_json,'$.name') IS NOT 'manifest.enc'
 OR json_type(NEW.manifest_json,'$.stored_bytes') IS NOT 'integer'
 OR json_extract(NEW.manifest_json,'$.stored_bytes') NOT BETWEEN 37 AND 1048613
 OR json_type(NEW.manifest_json,'$.stored_hash') IS NOT 'text'
 OR length(json_extract(NEW.manifest_json,'$.stored_hash'))!=64
 OR length(CAST(json_extract(NEW.manifest_json,'$.stored_hash') AS BLOB))!=64
 OR json_extract(NEW.manifest_json,'$.stored_hash') GLOB '*[^0-9a-f]*'
 OR json_type(NEW.manifest_json,'$.plaintext_hash') IS NOT 'text'
 OR length(json_extract(NEW.manifest_json,'$.plaintext_hash'))!=64
 OR length(CAST(json_extract(NEW.manifest_json,'$.plaintext_hash') AS BLOB))!=64
 OR json_extract(NEW.manifest_json,'$.plaintext_hash') GLOB '*[^0-9a-f]*'
) ELSE 1 END)
BEGIN SELECT RAISE(ABORT,'workflow manifest descriptor is invalid'); END;
"""
