# Complete workflow export format

The base bundle contains only one subject's complete workflow history and its
clock-exception receipts. It is not a full instance backup, a restore command or
authority to send requests, resume work, settle unknown calls or release fees.
Original checkpoint rows are preserved in the source database.

Source capture uses a coherent SQLite backup including committed WAL data. It
checks the installed schema contract, source identity, complete version/type/
status/state hashes and clock receipts, and the subject's event-chain metadata
in the same snapshot. The canonical row-array digest is computed incrementally
and remains identical to the existing full-history fingerprint. Receipt content
and the source event head are independently bound alongside the history.

The versioned format binds subject/workflow/genesis, schema version and DDL hash,
record/UTF-8 byte counts, original row/receipt digests, final-row hash and clock
high-water. Each ordered segment declares its exact name, range, count and
stored/plaintext digest. The manifest source is compared against the protected
publication record, never accepted from its own self-description.

All objects use the existing archive keyring and encrypted local provider. AAD
includes the source-derived publication identity and canonical object name.
Windows and POSIX private-storage permission checks remain mandatory; symlinks,
reparse components, hard-linked objects and escaped names are refused. Long
Windows paths use the native extended path representation. No key material,
checkpoint state or local path is returned by the management directory.

Budgets bound export work to 1,000,000 checkpoints, 256 MiB of original UTF-8
state, 4 MiB for a state row, 256 records per chunk, a 512-KiB chunk target,
16-MiB hard chunk limit, 2,048 segments and a 1-MiB manifest. Large legal records
may occupy a single chunk. A bound/deadline failure preserves the source and
cannot produce acceptance. There is no arbitrary candidate-prefix cutoff.

Every segment and the manifest are read back, their exact stored bytes are
hashed and decrypted, and the complete history, transitions, scope and clock
receipts are verified again. This reader is publication verification, not a
production database restore or a cache that skips historical evidence.