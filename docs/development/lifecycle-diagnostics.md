# Advisory lifecycle diagnostics

The preservation inventory lists explicitly reviewed schema object names and
domain membership. It documents a preservation baseline; it is not an executable
cleanup registry or proof that every logical reference has been signed off.
Unknown objects remain `unreviewed_preserve`, even when their name starts with a
known domain prefix or an automatic schema baseline has been refreshed.

`RetentionManager.lifecycle_catalog` reads only schema metadata in one read
snapshot. Results distinguish existing `RETENTION_REGISTRY` rules, documented
domain contracts, preservation-only objects and unreviewed objects. Physical
payload/archive/index contracts are named separately from logical retention.
The registered delete set, cleanup cursor/version and integrity admission policy
are unchanged. FTS shadows remain owned by their virtual-table rebuild contract.
A compatibility rule referring to an absent historical table is listed separately
from an actually missing object in the installation's inventory baseline.

The `policy_coverage` result is advisory. A gap does not authorize deletion or
create a P0/P1 finding. Existing structural inventory failures retain their
previous meaning. `structural_drift` compares table names to the stored inventory;
it is explicitly not complete table/index/trigger DDL verification. A missing or
invalid baseline reports `baseline_unavailable`, never a clean match.

`GET /api/v1/admin/retention/lifecycle?limit=50&cursor=...` is operator-only.
The `/api/admin/...` alias uses the same contract. The limit is 1..100; responses
include page scope and a next cursor. The cursor is bound to the preservation
catalog, current schema cookie/object list and structural baseline. A changed
catalog, schema or baseline rejects a stale cursor so the caller can restart.
Object names are parameters or metadata, never interpolated into business SQL.
No row content, credentials, host paths, model text or cleanup action is returned.

Observation bounds are 2,000 table objects, 256 characters per object name and
512 KiB of baseline metadata. The read uses a short SQL work deadline and a
200-ms SQLite busy timeout. An oversized/temporarily unavailable observation
returns an unavailable response, not a successful truncated coverage claim.
Individual discrepancy lists have counts and truncation flags. The existing
retention response embeds only coverage/drift summaries, and its other controls
remain readable when this optional observation is unavailable. The management
page labels policy gaps as maintenance information without blocking cognition.

No schema migration, row deletion, archive publication, restoration or new
scheduler/policy gate is introduced. Original cleanup and domain-specific
session/credential/privacy protocols remain authoritative.
