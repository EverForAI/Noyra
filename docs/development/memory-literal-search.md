# Memory literal search

`MemoryStore.search` is the bounded, literal substring helper used by memory
formation and maintenance paths. It is separate from `MemoryStore.recall`,
which owns hybrid FTS, semantic, entity, causal and access scoring.

The query is stripped and Python `casefold()` is applied. Queries over 1,000
characters are rejected before opening SQLite. For a nonempty query, SQLite
registers a request-local deterministic `noyra_memory_casefold` function and
filters `subject_id`, optional `memory_type`, active status, and literal
`instr(casefold(content), query)` before `ORDER BY` and the existing maximum
200 result bound. `%`, `_`, quotes, backslashes and NUL are values, never SQL
wildcards or syntax. Empty queries keep the original salience, confidence and
updated-time ordering and materialize only the bounded rows.

This preserves Python Unicode casefold semantics, including its deliberate
lack of Unicode normalization: a decomposed `cafe\u0301` does not equal a
precomposed `café` unless their casefolded strings are equal. No LIKE or FTS
operator is substituted because those operators do not implement the exact
literal contract for all supported text.

The query can scan all rows that pass the subject/type/status filter because
SQLite cannot use the existing `(subject_id, memory_type, status)` index to
find arbitrary casefolded substrings. The query plan still uses that index and
only materializes at most 200 result records in Python; it may use a temporary
B-tree for the requested order. Local measurements are evidence for this
checkout only, not a universal latency guarantee. A future optimization must
preserve exact Unicode semantics, subject isolation, lifecycle filtering and
bounded result materialization before changing the storage schema.

Returned records still pass `MemoryStore._from_row`, so content/state hashes
are validated. The operation is read-only: it does not record access, revise
memories, create indexes, or mutate schema. Detailed benchmark outputs and
raw audit material remain outside the public source tree.
