"""Read-only, bounded scheduling view over durable intrinsic attention history."""

from __future__ import annotations

from typing import Any

from noyra.core.database import Database

ATTENTION_BATCH_SIZE = 64


def attention_batch(database: Database, subject_id: str, now: str, interval: float) -> list[Any]:
    """Give old unserved questions a turn without changing their evidence or budgets.

    Equivalent question text is one scheduling group, not one stored record.
    A known successful response awaiting commit takes precedence. Unknown calls
    quarantine their group; this view never declares them failed or retries them.
    """
    with database.connection() as connection:
        return list(
            connection.execute(
                """WITH attention AS (
              SELECT a.*,
                CASE WHEN a.source_type IN ('question','self_model')
                     THEN lower(trim(a.topic)) ELSE a.agenda_id END AS topic_key,
                (SELECT MAX(t.created_at) FROM thought_episodes t
                  WHERE t.subject_id=a.subject_id AND t.agenda_id=a.agenda_id) AS served,
                (SELECT MAX(c.closed_at) FROM thought_round_closures c
                  WHERE c.subject_id=a.subject_id AND c.agenda_id=a.agenda_id) AS closed,
                (SELECT MAX(c.retry_after) FROM thought_round_closures c
                  WHERE c.subject_id=a.subject_id AND c.agenda_id=a.agenda_id) AS retry_after,
                EXISTS(SELECT 1 FROM model_calls m WHERE m.subject_id=a.subject_id
                  AND m.purpose >= 'intrinsic_thought:' || a.agenda_id || ':'
                  AND m.purpose < 'intrinsic_thought:' || a.agenda_id || ';'
                  AND m.status IN ('unknown','executing')) AS quarantined,
                EXISTS(SELECT 1 FROM model_calls m WHERE m.subject_id=a.subject_id
                  AND m.purpose >= 'intrinsic_thought:' || a.agenda_id || ':'
                  AND m.purpose < 'intrinsic_thought:' || a.agenda_id || ';'
                  AND m.status='succeeded' AND NOT EXISTS(SELECT 1 FROM thought_episodes t
                    WHERE t.subject_id=a.subject_id AND t.model_call_id=m.call_id)
                  AND NOT EXISTS(SELECT 1 FROM thought_round_closures c
                    WHERE c.subject_id=a.subject_id AND c.agenda_id=a.agenda_id
                    AND m.purpose='intrinsic_thought:' || a.agenda_id || ':' || c.round_number)
                  ) AS recoverable
              FROM thought_agenda_items a WHERE a.subject_id=?
                AND a.status IN ('open','cooling')
                AND (a.source_type!='goal' OR EXISTS(SELECT 1 FROM goals g
                  WHERE g.subject_id=a.subject_id AND g.goal_id=a.source_id
                    AND g.status IN ('candidate','active','paused','reconsidering')))
            ), grouped AS (
              SELECT *, MAX(quarantined) OVER w AS group_quarantined,
                MAX(julianday(served)) OVER w AS last_served,
                MAX(julianday(closed)) OVER w AS last_closed,
                MAX(julianday(retry_after)) OVER w AS group_retry,
                MAX(julianday(cooldown_until)) OVER w AS group_cooldown,
                MIN(julianday(created_at)) OVER w AS first_seen,
                ROW_NUMBER() OVER (PARTITION BY source_type,topic_key
                  ORDER BY recoverable DESC,created_at,agenda_id) AS representative
              FROM attention WINDOW w AS (PARTITION BY source_type,topic_key)
            ) SELECT * FROM grouped WHERE representative=1 AND group_quarantined=0
              AND (recoverable=1 OR (
                COALESCE(group_cooldown,0)<=julianday(?)
                AND COALESCE(group_retry,0)<=julianday(?)
                AND (last_served IS NULL OR (julianday(?)-last_served)*86400>=?-0.001)
              ))
              ORDER BY recoverable DESC,
                CASE WHEN last_served IS NULL AND last_closed IS NULL THEN 0 ELSE 1 END,
                MAX(COALESCE(last_served,first_seen),COALESCE(last_closed,first_seen)),
                urgency DESC,novelty DESC,agenda_id LIMIT ?""",
                (subject_id, now, now, now, interval, ATTENTION_BATCH_SIZE),
            ).fetchall()
        )
