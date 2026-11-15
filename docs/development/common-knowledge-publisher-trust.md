# Common knowledge publisher trust

Common knowledge signatures bind a package to both its signing key ID and its
`publisher_subject_id`. A key ID by itself is not publisher authorization.

Operators must register the exact publisher subject together with its public
key. The HTTP endpoint `/api/v1/config/common-knowledge/trust` accepts
`public_key`, `publisher_subject_id`, and `label`. The Python API uses the same
binding:

```python
store.trust_key(
    publisher_public_key,
    publisher_subject_id="Publisher-Example",
    label="Example publisher",
    actor="operator",
)
```

Import, read-time usability checks, and peer synchronization
require the `(local subject, publisher subject, key ID)` binding to be
active. A package signed by a trusted key but claiming another publisher,
including the local subject, is rejected before package or import records are
written. Peer registration creates the matching explicit binding. Trusting a
key for one publisher does not authorize that key to speak as another.

Schema 118 adds publisher-scoped trust bindings. Migration preserves existing
configured peers as explicit bindings, but it does not infer publisher identity
from old key-only trust rows or package claims. Older accepted imports without a
binding remain stored and cryptographically inspectable, but are not returned as
usable knowledge until an operator explicitly binds that key to the package's
publisher subject. The local subject's own packages continue to use only the
local signing key.
Previously revoked keys retain revoked bindings during migration. Integrity
inspection still verifies historical signatures and provenance; it reports
`unbound_accepted_imports` separately while the runtime excludes those imports
from usable knowledge.

The operator review queue keeps these legacy imports visible with
`publisher_binding_required=true`, package/publisher/key identifiers and their
original import status. It omits the advisory payload until the publisher is
explicitly authorized; a missing binding does not break the configuration page.

Revoking a key deactivates all publisher bindings for that key in the database,
matching the existing database-wide key revocation. Re-trusting a publisher
reactivates only that local subject's specific publisher binding; it
does not restore other publisher identities associated with the same key.

This is a required API change: older key-only trust requests receive a validation
error until `publisher_subject_id` is supplied. There is no wildcard or
first-import binding. Migration uses the normal database backup and rollback
path, and preserves existing package, provenance, review and workflow rows.
Older binaries refuse schema118; deployment rollback must also restore the
matching pre-migration database backup while the service is stopped, after
accounting for any writes made since that backup.
