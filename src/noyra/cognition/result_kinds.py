"""Conservative cycle-result categories; paid outcomes stay in the model ledger."""

from __future__ import annotations

from typing import Literal

ResultKind = Literal["waiting", "no_change", "failed", "unverified"]

# Match complete suffixes, never arbitrary substrings. These are operational
# results, not provider outcomes or authorization to create another request.
WAIT_SUFFIXES = (
    "waiting_reconciliation",
    "waiting",
    "waited",
    "wait",
    "in_progress",
    "target_not_due",
    "context_changed",
    "context_unavailable",
    "budget_exhausted",
    "daily_retry_limit",
    "cooldown",
)
NO_CHANGE_SUFFIXES = ("no_change", "unchanged", "no_results", "replayed", "target_expired")
FAILURE_SUFFIXES = ("model_failed", "failed", "rejected", "target_missing", "target_invalid")


def cycle_result_kind(result: str) -> ResultKind:
    if any(result.endswith("_" + suffix) for suffix in WAIT_SUFFIXES):
        return "waiting"
    if any(result.endswith("_" + suffix) for suffix in NO_CHANGE_SUFFIXES):
        return "no_change"
    if any(result.endswith("_" + suffix) for suffix in FAILURE_SUFFIXES):
        return "failed"
    return "unverified"


def cycle_result_outcome(result: str) -> Literal["stagnant", "failed", "unknown"]:
    kind = cycle_result_kind(result)
    # Preserve the historical outcome vocabulary and profile formula.
    # Stagnant means this turn supplied no verified progress, including waits;
    # it does not assert an unknown paid call or failed provider request.
    if kind in {"waiting", "no_change"}:
        return "stagnant"
    return "failed" if kind == "failed" else "unknown"
