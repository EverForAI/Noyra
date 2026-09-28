from __future__ import annotations

import asyncio

from noyra.research.search import SearchExecutor
from noyra.research.types import SearchExecution, SearchProviderRecord


def provider(config_id: str, priority: int) -> SearchProviderRecord:
    return SearchProviderRecord(
        config_id=config_id,
        subject_id="subject",
        provider_type="brave",
        label=config_id,
        key_fingerprint="f" * 64,
        extras={},
        rate_limit_per_hour=20,
        status="active",
        created_at="2026-01-01T00:00:00+00:00",
        revoked_at=None,
        revoke_reason=None,
        priority=priority,
    )


class Health:
    def __init__(self, unavailable: set[str] | None = None):
        self.unavailable = unavailable or set()
        self.checked: list[str] = []

    def route_available(self, subject_id: str, kind: str, provider_id: str) -> bool:
        self.checked.append(provider_id)
        return provider_id not in self.unavailable


def executor_with_results(results: dict[str, SearchExecution], health: Health):
    executor = SearchExecutor.__new__(SearchExecutor)
    executor.provider_health = health
    calls: list[str] = []

    async def search(subject_id, config, query, **kwargs):
        calls.append(config.config_id)
        return results[config.config_id]

    executor.search = search
    executor.calls = calls
    return executor


def result(provider_id: str, status: str) -> SearchExecution:
    return SearchExecution("action-" + provider_id, provider_id, "brave", "query", (), status)


def test_search_uses_priority_order_and_fails_over_after_known_failure():
    primary = provider("primary", 0)
    secondary = provider("secondary", 10)
    executor = executor_with_results(
        {"primary": result("primary", "failed"), "secondary": result("secondary", "succeeded")},
        Health(),
    )

    execution = asyncio.run(
        executor.search_with_fallback(
            "subject",
            [secondary, primary],
            "query",
            preferred_provider_id="secondary",
            goal_id="goal",
            strategy_id="strategy",
            expected_outcome="sources",
            idempotency_key="research:one",
        )
    )

    assert execution is not None
    assert execution.provider_config_id == "secondary"
    assert executor.calls == ["primary", "secondary"]


def test_search_does_not_fail_over_when_first_result_is_unknown():
    primary = provider("primary", 0)
    secondary = provider("secondary", 10)
    executor = executor_with_results(
        {"primary": result("primary", "unknown"), "secondary": result("secondary", "succeeded")},
        Health(),
    )

    execution = asyncio.run(
        executor.search_with_fallback(
            "subject",
            [primary, secondary],
            "query",
            preferred_provider_id=None,
            goal_id="goal",
            strategy_id="strategy",
            expected_outcome="sources",
            idempotency_key="research:two",
        )
    )

    assert execution is not None and execution.status == "unknown"
    assert executor.calls == ["primary"]


def test_search_skips_provider_still_in_cooldown():
    primary = provider("primary", 0)
    secondary = provider("secondary", 10)
    health = Health({"primary"})
    executor = executor_with_results({"secondary": result("secondary", "succeeded")}, health)

    execution = asyncio.run(
        executor.search_with_fallback(
            "subject",
            [primary, secondary],
            "query",
            preferred_provider_id=None,
            goal_id="goal",
            strategy_id="strategy",
            expected_outcome="sources",
            idempotency_key="research:three",
        )
    )

    assert execution is not None and execution.provider_config_id == "secondary"
    assert executor.calls == ["secondary"]
    assert health.checked == ["primary", "secondary"]


def test_search_returns_none_when_every_provider_is_cooling_down():
    primary = provider("primary", 0)
    health = Health({"primary"})
    executor = executor_with_results({}, health)

    execution = asyncio.run(
        executor.search_with_fallback(
            "subject",
            [primary],
            "query",
            preferred_provider_id=None,
            goal_id="goal",
            strategy_id="strategy",
            expected_outcome="sources",
            idempotency_key="research:four",
        )
    )

    assert execution is None
    assert executor.calls == []


def test_search_weight_controls_selection_within_the_same_priority_tier():
    low_weight = provider("provider-a", 10)
    high_weight = provider("provider-b", 10)
    high_weight = SearchProviderRecord(**{**high_weight.__dict__, "weight": 3})
    executor = executor_with_results(
        {
            "provider-a": result("provider-a", "succeeded"),
            "provider-b": result("provider-b", "succeeded"),
        },
        Health(),
    )

    async def run_requests():
        for index in range(400):
            await executor.search_with_fallback(
                "subject",
                [low_weight, high_weight],
                "query",
                preferred_provider_id=None,
                goal_id="goal",
                strategy_id="strategy",
                expected_outcome="sources",
                idempotency_key=f"research:weighted:{index}",
            )

    asyncio.run(run_requests())

    selected_high_weight = executor.calls.count("provider-b")
    assert 270 <= selected_high_weight <= 330
