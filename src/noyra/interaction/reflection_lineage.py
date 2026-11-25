"""Follow durable memory links and belief revision histories for private review."""

from __future__ import annotations

import re
import sqlite3
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from noyra.core.errors import IntegrityError, PayloadLimitError
from noyra.core.types import canonical_json, content_hash, strict_json_loads
from noyra.mind.types import BeliefRecord

_MAX_MEMORY_REFERENCES_BYTES = 128 * 1024
_MAX_TOTAL_REFERENCE_BYTES = 8 * 1024 * 1024
_CLUSTER_PREFIX = "Consolidated memory cluster: "
_CLUSTER_MEMORY_ID = re.compile(r"mem_[0-9a-f]{32}")


@dataclass(frozen=True)
class _MemoryUse:
    kind: str
    operation_id: str
    parents: tuple[str, ...]
    output: str | None


def _check_bound(index: int, limit: int, deadline: float) -> None:
    if index > limit or time.monotonic() >= deadline:
        raise PayloadLimitError("sleep lineage exceeds the review bound")


def _memory_counts(
    connection: sqlite3.Connection, subject: str, seeds: set[str], limit: int, deadline: float
) -> dict[str, int]:
    counts = {
        "sleep_memory_records": len(seeds),
        "later_revisions_of_sleep_memories": 0,
        "memory_integrations_using_lineage": 0,
        "memory_integrations_with_other_parents": 0,
        "memory_consolidations_using_lineage": 0,
        "memory_consolidations_with_other_members": 0,
        "downstream_memory_records": 0,
        "downstream_memories_with_other_parents": 0,
    }
    if not seeds:
        return counts
    seed_rows = connection.execute(
        "SELECT m.current_revision,COUNT(r.revision_id) AS revisions,"
        "MAX(r.revision_number) AS latest FROM json_each(?) seed "
        "JOIN memories m ON m.memory_id=seed.value AND m.subject_id=? "
        "LEFT JOIN memory_revisions r ON r.memory_id=m.memory_id GROUP BY m.memory_id",
        (canonical_json(sorted(seeds)), subject),
    ).fetchall()
    if len(seed_rows) != len(seeds) or any(
        not 1 <= row["revisions"] == row["current_revision"] == row["latest"] for row in seed_rows
    ):
        raise IntegrityError("sleep memory receipt target history is unavailable")
    counts["later_revisions_of_sleep_memories"] = sum(row["revisions"] - 1 for row in seed_rows)

    uses: list[_MemoryUse] = []
    summary_runs: dict[str, set[str]] = defaultdict(set)
    references = 0
    reference_bytes = 0
    rows = connection.execute(
        "SELECT 'integration' AS kind,i.integration_id AS operation_id,"
        "CASE WHEN length(CAST(i.source_memory_ids_json AS BLOB))<=? "
        "THEN i.source_memory_ids_json END AS parents,i.output_memory_id AS output,"
        "o.subject_id AS output_subject FROM memory_integrations i "
        "LEFT JOIN memories o ON o.memory_id=i.output_memory_id WHERE i.subject_id=? "
        "UNION ALL "
        "SELECT 'consolidation',c.consolidation_id,json_array(c.source_memory_id),"
        "CASE WHEN c.disposition='summarized' THEN c.result_memory_id END,o.subject_id "
        "FROM memory_consolidation_members c "
        "JOIN memory_consolidation_runs r ON r.consolidation_id=c.consolidation_id "
        "LEFT JOIN memories o ON o.memory_id=c.result_memory_id WHERE r.subject_id=? LIMIT ?",
        (_MAX_MEMORY_REFERENCES_BYTES, subject, subject, limit + 1),
    )
    for index, row in enumerate(rows, start=1):
        _check_bound(index, limit, deadline)
        raw_parents = row["parents"]
        if raw_parents is None:
            raise PayloadLimitError("memory parent references exceed the review bound")
        if not isinstance(raw_parents, str):
            raise IntegrityError("memory parent references are invalid")
        reference_bytes += len(raw_parents.encode("utf-8"))
        _check_bound(reference_bytes, _MAX_TOTAL_REFERENCE_BYTES, deadline)
        try:
            parents = strict_json_loads(raw_parents)
        except (ValueError, RecursionError) as error:
            raise IntegrityError("memory parent references are invalid") from error
        if (
            not isinstance(parents, list)
            or not parents
            or any(not isinstance(parent, str) or not parent for parent in parents)
            or len(set(parents)) != len(parents)
        ):
            raise IntegrityError("memory parent references are invalid")
        if row["output"] is not None and row["output_subject"] != subject:
            raise IntegrityError("memory lineage output has an invalid subject")
        references += len(parents)
        _check_bound(references, limit * 4, deadline)
        output = row["output"]
        if row["kind"] == "consolidation" and output is not None:
            summary_runs[output].add(row["operation_id"])
            output = None
        uses.append(_MemoryUse(row["kind"], row["operation_id"], tuple(parents), output))

    # The member rows record which entries were archived. The original summary
    # also names cluster entries retained by the active-memory floor.
    for output, parents in _summary_sources(connection, summary_runs).items():
        for run_id in summary_runs[output]:
            references += len(parents)
            _check_bound(references, limit * 4, deadline)
            uses.append(_MemoryUse("consolidation", run_id, parents, output))
    uses_by_source: dict[str, list[int]] = defaultdict(list)
    for index, use in enumerate(uses):
        for parent in use.parents:
            uses_by_source[parent].append(index)
    invalid_parent = connection.execute(
        "SELECT 1 FROM json_each(?) sources LEFT JOIN memories m ON m.memory_id=sources.value "
        "AND m.subject_id=? WHERE m.memory_id IS NULL LIMIT 1",
        (canonical_json(sorted(uses_by_source)), subject),
    ).fetchone()
    if invalid_parent:
        raise IntegrityError("memory lineage parent has an invalid subject")

    # A duplicate's keeper already existed. Only summaries and integration
    # outputs carry forward a known link. Other parents do not become seeds.
    reached = set(seeds)
    pending = deque(sorted(seeds))
    visited: set[int] = set()
    matched_operations: dict[str, set[str]] = defaultdict(set)
    while pending:
        for index in uses_by_source.get(pending.popleft(), ()):
            if index in visited:
                continue
            visited.add(index)
            use = uses[index]
            matched_operations[use.kind].add(use.operation_id)
            if use.output is not None and use.output not in reached:
                reached.add(use.output)
                _check_bound(len(reached), limit, deadline)
                pending.append(use.output)
        _check_bound(len(visited), limit * 2, deadline)
    downstream = reached - seeds
    mixed_uses = [uses[index] for index in visited if set(uses[index].parents) - reached]
    counts.update(
        memory_integrations_using_lineage=len(matched_operations["integration"]),
        memory_integrations_with_other_parents=len(
            {use.operation_id for use in mixed_uses if use.kind == "integration"}
        ),
        memory_consolidations_using_lineage=len(matched_operations["consolidation"]),
        memory_consolidations_with_other_members=len(
            {use.operation_id for use in mixed_uses if use.kind == "consolidation"}
        ),
        downstream_memory_records=len(downstream),
        downstream_memories_with_other_parents=len(
            {use.output for use in mixed_uses if use.output in downstream}
        ),
    )
    return counts


def _summary_sources(
    connection: sqlite3.Connection, summaries: dict[str, set[str]]
) -> dict[str, tuple[str, ...]]:
    result: dict[str, tuple[str, ...]] = {}
    rows = connection.execute(
        "SELECT refs.value AS memory_id,"
        "CASE WHEN length(CAST(r.content AS BLOB))<=512 THEN r.content END AS cluster,"
        "r.content_hash FROM json_each(?) refs LEFT JOIN memory_revisions r "
        "ON r.memory_id=refs.value AND r.revision_number=1",
        (canonical_json(sorted(summaries)),),
    )
    for row in rows:
        cluster = row["cluster"]
        if (
            not isinstance(cluster, str)
            or not cluster.startswith(_CLUSTER_PREFIX)
            or content_hash(cluster) != row["content_hash"]
        ):
            raise IntegrityError("original memory consolidation cluster is unavailable")
        parents = tuple(cluster[len(_CLUSTER_PREFIX) :].split(", "))
        if (
            not 2 <= len(parents) <= 8
            or parents != tuple(sorted(set(parents)))
            or row["memory_id"] in parents
            or any(_CLUSTER_MEMORY_ID.fullmatch(parent) is None for parent in parents)
        ):
            raise IntegrityError("original memory consolidation cluster is invalid")
        result[row["memory_id"]] = parents
    return result


def _belief_counts(
    connection: sqlite3.Connection,
    subject: str,
    receipts: dict[str, set[str]],
    limit: int,
    deadline: float,
) -> dict[str, int]:
    matched: dict[str, set[str]] = defaultdict(set)
    first_revision: dict[str, int] = {}
    later_revisions = 0
    beliefs_with_later_revisions: set[str] = set()
    rows = connection.execute(
        "SELECT b.belief_id,b.scope,b.created_at AS belief_created_at,"
        "r.proposition,r.confidence,r.status,r.revision_number,r.created_at "
        "FROM json_each(?) seeds JOIN beliefs b ON b.belief_id=seeds.value AND b.subject_id=? "
        "JOIN belief_revisions r ON r.belief_id=b.belief_id "
        "ORDER BY b.belief_id,r.revision_number LIMIT ?",
        (canonical_json(sorted(receipts)), subject, limit + 1),
    )
    for index, row in enumerate(rows, start=1):
        _check_bound(index, limit, deadline)
        belief_id = row["belief_id"]
        revision = row["revision_number"]
        if belief_id in first_revision:
            later_revisions += 1
            beliefs_with_later_revisions.add(belief_id)
        # Rebuild the public data structure hashed by SleepEngine at commit.
        # The revision number disambiguates same-time, same-content revisions.
        record = BeliefRecord(
            belief_id,
            subject,
            row["proposition"],
            float(row["confidence"]),
            row["scope"],
            row["status"],
            revision,
            row["belief_created_at"],
            row["created_at"],
        )
        receipt_hash = content_hash(record.__dict__)
        if receipt_hash in receipts[belief_id]:
            matched[belief_id].add(receipt_hash)
            first_revision.setdefault(belief_id, revision)
    return {
        "sleep_beliefs_with_matched_revisions": len(first_revision),
        "beliefs_with_later_revisions_to_review": len(beliefs_with_later_revisions),
        "later_belief_revisions_to_review": later_revisions,
        "unmatched_sleep_belief_receipts": sum(
            len(hashes - matched[belief_id]) for belief_id, hashes in receipts.items()
        ),
    }


def sleep_lineage_counts(
    connection: sqlite3.Connection,
    subject: str,
    memory_ids: set[str],
    belief_receipts: dict[str, set[str]],
    row_limit: int,
    deadline: float,
) -> dict[str, int] | None:
    try:
        counts = _memory_counts(connection, subject, memory_ids, row_limit, deadline)
        counts.update(_belief_counts(connection, subject, belief_receipts, row_limit, deadline))
        return counts
    except PayloadLimitError:
        return None
