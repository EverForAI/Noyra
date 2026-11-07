# Communication intent and verified delivery

`interactions.status='sent'` is the historical durable send-intent vocabulary.
It does not prove provider delivery. Its public label is `Recorded`; no historical
row, state hash or dispatch permission changes. The original status remains in
responses for compatibility.

Conversation messages and the public-channel projection add
`communication_status`, `delivery_reason`, `delivery_statuses` and
`deliveries_truncated`. Existing delivery rows are read in deterministic row order
within the caller's transaction. Their reconciliation history is checked for
ownership, contiguous sequence, valid hashes and terminal ordering before its
effective outcome is used. Corrupt evidence fails closed; a raw unknown row may
be shown as delivered/failed/cancelled only with valid immutable receipts.

With no delivery row, a normal web reply is `local_available`: the reply is
recorded locally, without claiming an external send. Other outgoing messages,
including help requests, are `not_scheduled` with `no_delivery_record`.
This explains what is known; it does not claim a configured transport is absent
or prevent the normal dispatcher from scheduling later. Incoming messages use
`not_applicable`. None of these read states authorizes another send.

Up to ten delivery states are returned for a message. Additional rows produce
`delivery_sample_incomplete` and an explicit truncation flag rather than an
unqualified delivered claim. If multiple states are present, unknown, sending,
queued or failed takes precedence over a successful route; individual states
remain available for inspection.

The lightweight runtime observer samples up to 100 recent raw unknown/failed
rows and validates their receipt histories. Confirmed failed receipts stay in
the attention sample; confirmed delivered/cancelled receipts leave it. The
sample reports its scope, row limit, sampled count and truncation. It is not the
complete inventory or a health/admission/billing claim. The management page
shows a reminder when older rows were not sampled, even if the recent sample
contains no unresolved delivery. Existing SQL work deadlines remain in force;
unavailable or corrupt evidence cannot masquerade as zero usage or a clean read.

No reader changes original ledger rows, creates a virtual delivery, reads
credentials, contacts a provider, retries unknown work or releases reservations.
