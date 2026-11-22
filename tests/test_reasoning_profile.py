from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import httpx
import pytest
from pydantic import BaseModel, SecretStr

from noyra.core import Database, IdentityStore
from noyra.core.errors import IntegrityError
from noyra.core.types import content_hash
from noyra.model.capabilities import CapabilityProfileStore
from noyra.model.config import OpenAICompatibleSettings
from noyra.model.errors import BudgetExhaustedError, ProviderCallError, StructuredOutputError
from noyra.model.fake import FakeProvider
from noyra.model.openai_compatible import OpenAICompatibleProvider
from noyra.model.reasoning import (
    normalize_reasoning_effort,
    reasoning_profile,
    reasoning_profile_payload,
)
from noyra.model.resources import (
    CognitiveResourceGroupInput,
    CognitiveResourceStore,
    RoutedModelGateway,
)
from noyra.model.types import CompletionRequest, ModelMessage, ModelUsage, ProviderResponse
from noyra.service import NoyraService

_SUBJECT = "revalidation"
_URL = "https://models.example/v1"
_MODEL = "provider-alias"
_IDENTITY = (_SUBJECT, "openai_compatible", _URL, _MODEL)


class _Clock(datetime):
    value: ClassVar[datetime]

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> _Clock:
        return cls.fromtimestamp(cls.value.timestamp(), tz)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> type[_Clock]:
    _Clock.value = datetime(2026, 10, 1, tzinfo=UTC)
    monkeypatch.setattr("noyra.model.capabilities.datetime", _Clock)
    monkeypatch.setattr("noyra.model.resources.datetime", _Clock)
    monkeypatch.setattr("noyra.model.resources.utc_now", lambda: _Clock.value.isoformat())
    monkeypatch.setattr("noyra.core.provider_health.datetime", _Clock)
    return _Clock


class _Output(BaseModel):
    ok: bool


def _router(
    tmp_path: Path,
    provider: FakeProvider,
    *,
    budget: int = 50,
    max_attempts: int = 1,
    key_count: int = 1,
) -> tuple[RoutedModelGateway, CapabilityProfileStore, str]:
    database = Database(tmp_path / "revalidation.sqlite3")
    IdentityStore(database).ensure(_SUBJECT, content_hash(_SUBJECT))
    resources = CognitiveResourceStore(database, tmp_path / "secrets")
    record = resources.configure(
        _SUBJECT,
        CognitiveResourceGroupInput(
            pool="economy",
            label="revalidation",
            base_url=_URL,
            model=_MODEL,
            reasoning_effort="high",
            api_keys=tuple(SecretStr(f"fixture-key-{index}") for index in range(key_count)),
            daily_attempts=budget,
            max_attempts=max_attempts,
        ),
        actor="operator",
    )
    return (
        RoutedModelGateway(database, _SUBJECT, resources, provider_factory=lambda _: provider),
        CapabilityProfileStore(database),
        record.group_id,
    )


async def _complete(router: RoutedModelGateway, key: str) -> bool:
    result = await router.complete_structured(
        _SUBJECT,
        f"research_plan:{key}",
        (ModelMessage(role="user", content="fixture"),),
        _Output,
        idempotency_key=key,
    )
    return result.cached


def test_known_reasoning_models_expose_bounded_efforts() -> None:
    profile = reasoning_profile("gpt-5-mini")
    assert profile.supported is True
    assert profile.efforts == ("low", "medium", "high", "xhigh")
    assert profile.parameter == "reasoning_effort"
    assert normalize_reasoning_effort("gpt-5-mini", "high") == "high"


def test_unknown_models_fail_closed_without_reasoning() -> None:
    profile = reasoning_profile("deepseek-chat")
    assert profile.supported is False
    assert profile.candidate_efforts == ("low", "medium", "high", "xhigh")
    assert normalize_reasoning_effort("deepseek-chat", "high") is None


def test_known_model_rejects_unknown_effort() -> None:
    with pytest.raises(ValueError, match="not supported"):
        normalize_reasoning_effort("o3-mini", "extreme")


def test_public_profile_is_server_owned_and_clears_unsupported_selection() -> None:
    supported = reasoning_profile_payload("gpt-5-mini", selected="high")
    unsupported = reasoning_profile_payload("deepseek-chat", selected="high")
    assert supported["supported"] is True
    assert supported["selected"] == "high"
    assert isinstance(supported["revision"], str)
    assert unsupported["supported"] is False
    assert unsupported["efforts"] == []
    assert unsupported["candidate_efforts"] == ["low", "medium", "high", "xhigh"]
    assert unsupported["selected"] is None


def test_capability_observations_are_subject_endpoint_and_model_scoped(tmp_path: Any) -> None:
    database = Database(tmp_path / "capabilities.sqlite3")
    IdentityStore(database).ensure("Noyra-capabilities", content_hash("capabilities"))
    store = CapabilityProfileStore(database)
    first = store.record_probe_success(
        "Noyra-capabilities",
        "openai_compatible",
        "https://models.example/v1",
        "gpt-5-mini",
        "high",
    )
    assert first is not None
    assert first.status == "known"
    assert first.tested_efforts == ("high",)
    assert first.efforts == ("low", "medium", "high", "xhigh")
    assert (
        store.get("another-subject", "openai_compatible", "https://models.example/v1", "gpt-5-mini")
        is None
    )


def test_stale_capability_public_payload_clears_effective_selection(tmp_path: Any) -> None:
    database = Database(tmp_path / "capability-public.sqlite3")
    subject_id = "Noyra-capability-public"
    IdentityStore(database).ensure(subject_id, content_hash("public"))
    store = CapabilityProfileStore(database)
    observed = store.record_probe_success(
        subject_id,
        "openai_compatible",
        "https://models.example/v1",
        "provider-alias",
        "high",
    )
    assert observed is not None
    with database.transaction() as connection:
        row = connection.execute(
            "SELECT * FROM model_capability_profiles WHERE subject_id=?", (subject_id,)
        ).fetchone()
        state = dict(row)
        state.pop("state_hash")
        expired = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        state["expires_at"] = expired
        legacy = json.loads(state["capabilities_json"])
        legacy.pop("effort_expiries", None)
        legacy.pop("rejected_efforts", None)
        state["capabilities_json"] = json.dumps(legacy)
        connection.execute(
            "UPDATE model_capability_profiles SET expires_at=?, capabilities_json=?, "
            "state_hash=? WHERE subject_id=?",
            (expired, state["capabilities_json"], content_hash(state), subject_id),
        )
    stale = store.get(
        subject_id, "openai_compatible", "https://models.example/v1", "provider-alias"
    )
    assert stale is not None
    payload = stale.public(selected="high")
    assert payload["status"] == "stale"
    assert payload["selected"] is None
    assert payload["configured_selected"] == "high"
    assert payload["effective_status"] == "probe_required"
    assert stale.public(selected="high", existing_resource=True)["selected"] == "high"
    assert stale.public(selected="high", existing_resource=True)["effective_status"] == "grace"
    assert (
        store.get(
            "Noyra-capabilities", "openai_compatible", "https://other.example/v1", "gpt-5-mini"
        )
        is None
    )
    assert (
        store.get(
            "Noyra-capabilities", "openai_compatible", "https://models.example/v1", "other-model"
        )
        is None
    )


def test_successful_probes_accumulate_only_current_observations(tmp_path: Any) -> None:
    database = Database(tmp_path / "capability-revisions.sqlite3")
    subject_id = "Noyra-capability-revisions"
    IdentityStore(database).ensure(subject_id, content_hash("revision"))
    store = CapabilityProfileStore(database)
    first = store.record_probe_success(
        subject_id,
        "openai_compatible",
        "https://models.example/v1",
        "gpt-5-mini",
        "low",
    )
    second = store.record_probe_success(
        subject_id,
        "openai_compatible",
        "https://models.example/v1",
        "gpt-5-mini",
        "high",
    )

    assert first is not None and second is not None
    assert first.revision == 1
    assert second.revision == 2
    assert second.tested_efforts == ("low", "high")


def test_unknown_model_probe_persists_endpoint_observation(tmp_path: Any) -> None:
    database = Database(tmp_path / "capability-unsupported.sqlite3")
    subject_id = "Noyra-capability-unsupported"
    IdentityStore(database).ensure(subject_id, content_hash("unsupported"))
    store = CapabilityProfileStore(database)

    observed = store.record_probe_success(
        subject_id,
        "openai_compatible",
        "https://models.example/v1",
        "deepseek-chat",
        "high",
    )
    assert observed is not None
    assert observed.supported is True
    assert observed.efforts == ("high",)
    assert observed.tested_efforts == ("high",)

    additional = store.record_probe_success(
        subject_id,
        "openai_compatible",
        "https://models.example/v1",
        "deepseek-chat",
        "low",
    )
    assert additional is not None
    assert additional.efforts == ("low", "high")
    assert additional.tested_efforts == ("low", "high")


def test_capability_profile_export_import_provisions_environment_startup(tmp_path: Any) -> None:
    source_database = Database(tmp_path / "capability-export-source.sqlite3")
    source_subject = "Noyra-capability-export-source"
    IdentityStore(source_database).ensure(source_subject, content_hash("export-source"))
    source_store = CapabilityProfileStore(source_database)
    observed = source_store.record_probe_success(
        source_subject,
        "openai_compatible",
        "https://models.example/v1",
        "provider-alias",
        "high",
    )
    assert observed is not None
    exported = observed.export(
        provider_type="openai_compatible",
        base_url="https://models.example/v1",
        model="provider-alias",
    )

    target_database = Database(tmp_path / "capability-export-target.sqlite3")
    target_subject = "Noyra-capability-export-target"
    IdentityStore(target_database).ensure(target_subject, content_hash("export-target"))
    target_store = CapabilityProfileStore(target_database)
    assert target_store.import_profiles(target_subject, {"profiles": [exported]}) == 1
    imported = target_store.get(
        target_subject,
        "openai_compatible",
        "https://models.example/v1",
        "provider-alias",
    )
    assert imported is not None
    assert imported.source == "operator"
    assert imported.tested_efforts == ("high",)
    assert imported.profile_digest == observed.profile_digest


def test_unknown_alias_requires_endpoint_probe_before_resource_configuration(tmp_path: Any) -> None:
    database = Database(tmp_path / "alias.sqlite3")
    subject_id = "Noyra-alias"
    IdentityStore(database).ensure(subject_id, content_hash("alias"))
    store = CognitiveResourceStore(database, tmp_path / "secrets")
    proposal = CognitiveResourceGroupInput(
        pool="economy",
        label="alias",
        base_url="https://models.example/v1",
        model="provider-alias",
        reasoning_effort="high",
        api_keys=(SecretStr("private-key"),),
    )
    record = store.configure(subject_id, proposal, actor="operator")
    router = RoutedModelGateway(database, subject_id, store)
    key = store.keys(record.group_id, subject_id=subject_id)[0]

    capabilities = CapabilityProfileStore(database)
    with pytest.raises(ProviderCallError, match="model_capability_probe_required"):
        router._gateway(record, key)

    capabilities.record_probe_success(
        subject_id, "openai_compatible", "https://models.example/v1", "provider-alias", "high"
    )
    assert proposal.reasoning_effort == "high"
    gateway = router._gateway(record, key)
    assert gateway.reasoning_capability_verified is True


def test_expired_capability_requires_each_effort_to_be_retested(
    tmp_path: Path, clock: type[_Clock]
) -> None:
    database = Database(tmp_path / "capability-expiry.sqlite3")
    subject_id = "Noyra-capability-expiry"
    IdentityStore(database).ensure(subject_id, content_hash("expiry"))
    store = CapabilityProfileStore(database)
    initial = store.record_probe_success(
        subject_id,
        "openai_compatible",
        "https://models.example/v1",
        "gpt-5-mini",
        "low",
    )
    assert initial is not None

    clock.value += timedelta(days=61)
    stale = store.get(subject_id, "openai_compatible", "https://models.example/v1", "gpt-5-mini")
    assert stale is not None and stale.status == "stale"
    refreshed = store.record_probe_success(
        subject_id,
        "openai_compatible",
        "https://models.example/v1",
        "gpt-5-mini",
        "high",
    )
    assert refreshed is not None
    assert refreshed.revision == 2
    assert refreshed.effort_status("high") == "ready"
    assert refreshed.effort_status("low") == "grace"
    assert refreshed.effort_expiry("low") == initial.effort_expiry("low")
    assert refreshed.public()["fresh_efforts"] == ["high"]


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_period", [30, 60])
async def test_live_call_refreshes_only_selected_effort_and_cache_does_not_refresh(
    tmp_path: Path, clock: type[_Clock], monkeypatch: pytest.MonkeyPatch, initial_period: int
) -> None:
    success = ProviderResponse('{"ok":true}', ModelUsage(3, 3))
    provider = FakeProvider([success, success, success])
    router, store, _ = _router(tmp_path, provider)
    with monkeypatch.context() as prior_release:
        if initial_period == 30:
            prior_release.setattr("noyra.model.capabilities.CAPABILITY_PROBE_TTL_DAYS", 30)
        medium = store.record_probe_success(*_IDENTITY, "medium")
        initial = store.record_probe_success(*_IDENTITY, "high")
    assert medium and initial
    initial_deadline = clock.value + timedelta(days=initial_period)
    assert initial.effort_expiry("high") == initial_deadline.isoformat()
    clock.value += timedelta(days=initial_period - 8)
    assert await _complete(router, "first") is False
    assert store.get(*_IDENTITY) == initial

    clock.value += timedelta(days=1)
    due = store.get(*_IDENTITY)
    assert due and due.effort_status("high") == "refresh_due"
    assert await _complete(router, "first") is True
    assert store.get(*_IDENTITY) == due
    assert await _complete(router, "second") is False
    refreshed = store.get(*_IDENTITY)
    assert refreshed and refreshed.revision == due.revision + 1
    assert refreshed.effort_status("high") == "ready"
    assert refreshed.effort_expiry("high") == (clock.value + timedelta(days=60)).isoformat()
    assert refreshed.effort_expiry("medium") == medium.effort_expiry("medium")

    clock.value += timedelta(days=66)
    grace = store.get(*_IDENTITY)
    assert grace and grace.effort_status("high") == "grace"
    assert await _complete(router, "third") is False
    renewed = store.get(*_IDENTITY)
    assert renewed and renewed.revision == refreshed.revision + 1
    assert renewed.effort_expiry("high") == (clock.value + timedelta(days=60)).isoformat()
    assert renewed.effort_expiry("medium") == medium.effort_expiry("medium")
    assert [req.reasoning_effort for req in provider.requests] == ["high", "high", "high"]


@pytest.mark.asyncio
async def test_expired_effort_revalidates_on_the_first_successful_normal_call(
    tmp_path: Path, clock: type[_Clock]
) -> None:
    provider = FakeProvider([ProviderResponse('{"ok":true}', ModelUsage(3, 3))])
    router, store, _ = _router(tmp_path, provider, budget=1)
    initial = store.record_probe_success(*_IDENTITY, "high")
    assert initial is not None
    clock.value += timedelta(days=68)
    expired = store.get(*_IDENTITY)
    assert expired and expired.effort_status("high") == "expired"
    assert expired.public(selected="high")["effective_status"] == "probe_required"
    assert expired.public(selected="high", existing_resource=True)["effective_status"] == "expired"

    assert await _complete(router, "needed-after-expiry") is False
    renewed = store.get(*_IDENTITY)
    assert renewed and renewed.revision == initial.revision + 1
    assert renewed.effort_expiry("high") == (clock.value + timedelta(days=60)).isoformat()
    assert len(provider.requests) == 1
    assert provider.requests[0].reasoning_effort == "high"
    assert (
        router.ledger.budget_status(
            _SUBJECT, router.limits_for_pool("economy"), resource_pool="economy"
        ).attempts
        == 1
    )

    assert await _complete(router, "needed-after-expiry") is True
    assert store.get(*_IDENTITY) == renewed
    assert len(provider.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_status", "expected_revision"),
    [
        (
            ProviderCallError("provider_http_500", retryable=True, outcome_unknown=False),
            "expired",
            1,
        ),
        (
            ProviderCallError(
                "provider_reasoning_effort_rejected",
                retryable=False,
                outcome_unknown=False,
                status_code=400,
            ),
            "rejected",
            2,
        ),
    ],
)
async def test_expired_effort_failure_never_renews_or_overrides_provider_rejection(
    tmp_path: Path,
    clock: type[_Clock],
    error: ProviderCallError,
    expected_status: str,
    expected_revision: int,
) -> None:
    provider = FakeProvider([error])
    router, store, _ = _router(tmp_path, provider, max_attempts=3, key_count=2)
    initial = store.record_probe_success(*_IDENTITY, "high")
    assert initial is not None
    clock.value += timedelta(days=68)

    key = f"failed-expired-{expected_status}"
    with pytest.raises(ProviderCallError, match=error.code):
        await _complete(router, key)
    current = store.get(*_IDENTITY)
    assert current and current.revision == expected_revision
    assert current.effort_status("high") == expected_status
    if expected_status == "expired":
        assert current.effort_expiry("high") == initial.effort_expiry("high")
        with pytest.raises(ProviderCallError, match="economy_pool_waiting"):
            await _complete(router, key)
    else:
        assert "high" not in current.tested_efforts
        with pytest.raises(ProviderCallError, match="model_capability_probe_required"):
            await _complete(router, key)
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_expired_effort_revalidation_respects_budget(
    tmp_path: Path, clock: type[_Clock]
) -> None:
    provider = FakeProvider([])
    router, store, _ = _router(tmp_path, provider, budget=0)
    assert store.record_probe_success(*_IDENTITY, "high") is not None
    clock.value += timedelta(days=68)
    before = store.get(*_IDENTITY)

    with pytest.raises(BudgetExhaustedError):
        await _complete(router, "expired-without-budget")

    assert store.get(*_IDENTITY) == before
    assert not provider.requests


@pytest.mark.asyncio
async def test_observation_write_failure_does_not_change_settled_call(
    tmp_path: Path, clock: type[_Clock], monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = FakeProvider([ProviderResponse('{"ok":true}', ModelUsage(3, 3))])
    router, store, _ = _router(tmp_path, provider)
    assert store.record_probe_success(*_IDENTITY, "high") is not None
    clock.value += timedelta(days=53)
    before = store.get(*_IDENTITY)

    def unavailable(*args: Any, **kwargs: Any) -> None:
        raise sqlite3.OperationalError("fixture capability write failure")

    monkeypatch.setattr(CapabilityProfileStore, "record_runtime_observation", unavailable)
    assert await _complete(router, "write-failure") is False
    assert await _complete(router, "write-failure") is True
    assert store.get(*_IDENTITY) == before
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_unknown_outcome_does_not_renew_or_replay_expired_effort(
    tmp_path: Path, clock: type[_Clock]
) -> None:
    timeout = ProviderCallError("provider_timeout", retryable=False, outcome_unknown=True)
    provider = FakeProvider([timeout, ProviderResponse('{"ok":true}', ModelUsage(3, 3))])
    router, store, group_id = _router(tmp_path, provider)
    initial = store.record_probe_success(*_IDENTITY, "high")
    assert initial
    clock.value += timedelta(days=61)
    with pytest.raises(ProviderCallError, match="provider_timeout"):
        await _complete(router, "timeout")
    stale = store.get(*_IDENTITY)
    assert stale and stale.effort_status("high") == "grace"
    assert stale.revision == initial.revision
    clock.value += timedelta(days=6)
    expired = store.get(*_IDENTITY)
    assert expired and expired.effort_status("high") == "expired"
    with pytest.raises(ProviderCallError, match="routed_model_call_quarantined"):
        await _complete(router, "timeout")
    assert len(provider.requests) == 1
    result = await router.test_resource(group_id, subject_id=_SUBJECT)
    assert result["ok"] and len(provider.requests) == 2
    assert provider.requests[-1].reasoning_effort == "high"
    assert (current := store.get(*_IDENTITY)) and current.effort_status("high") == "ready"


@pytest.mark.asyncio
async def test_rejection_blocks_only_the_rejected_effort(tmp_path: Path) -> None:
    denial = ProviderCallError(
        "provider_reasoning_effort_rejected",
        retryable=False,
        outcome_unknown=False,
        status_code=400,
    )
    provider = FakeProvider([denial])
    router, store, group_id = _router(tmp_path, provider)
    store.record_probe_success(*_IDENTITY, "medium")
    store.record_probe_success(*_IDENTITY, "high")
    group = router.resources.get(group_id, subject_id=_SUBJECT)
    prepared_gateway = router._gateway(
        group, router.resources.keys(group_id, subject_id=_SUBJECT)[0]
    )
    with pytest.raises(ProviderCallError, match="provider_reasoning_effort_rejected"):
        await _complete(router, "rejected")
    profile = store.get(*_IDENTITY)
    assert profile and profile.effort_status("high") == "rejected"
    assert profile.effort_status("medium") == "ready"
    with pytest.raises(ProviderCallError, match="model_capability_probe_required"):
        await prepared_gateway.complete_structured(
            _SUBJECT,
            "research_plan:prepared",
            (ModelMessage(role="user", content="fixture"),),
            _Output,
            idempotency_key="prepared",
        )
    with pytest.raises(ProviderCallError, match="model_capability_probe_required"):
        await _complete(router, "still-rejected")
    assert len(provider.requests) == 1


def test_late_refusal_cannot_overwrite_a_newer_probe_or_survive_as_positive_evidence(
    tmp_path: Path, clock: type[_Clock]
) -> None:
    router, store, _ = _router(tmp_path, FakeProvider([]))
    before = store.record_probe_success(*_IDENTITY, "high")
    assert before is not None
    clock.value += timedelta(days=1)
    fresh = store.record_probe_success(*_IDENTITY, "high")
    assert fresh is not None
    store.record_runtime_observation(*_IDENTITY, "high", rejected=True, expected=before)
    assert store.get(*_IDENTITY) == fresh
    rejected = store.record_runtime_observation(*_IDENTITY, "high", rejected=True, expected=fresh)
    assert rejected and rejected.effort_status("high") == "rejected"
    store.record_runtime_observation(*_IDENTITY, "high", expected=fresh)
    assert CapabilityProfileStore(router.database).get(*_IDENTITY) == rejected

    target = Database(tmp_path / "import.sqlite3")
    IdentityStore(target).ensure(_SUBJECT, content_hash(_SUBJECT))
    imported = CapabilityProfileStore(target)
    assert (
        imported.import_profiles(
            _SUBJECT,
            [rejected.export(provider_type="openai_compatible", base_url=_URL, model=_MODEL)],
        )
        == 1
    )
    persisted = imported.get(*_IDENTITY)
    assert persisted and persisted.effort_status("high") == "rejected"


def test_environment_restart_keeps_existing_group_but_not_new_stale_configuration(
    tmp_path: Path, clock: type[_Clock]
) -> None:
    database = Database(tmp_path / "restart.sqlite3")
    IdentityStore(database).ensure(_SUBJECT, content_hash(_SUBJECT))
    capabilities = CapabilityProfileStore(database)
    evidence = capabilities.record_probe_success(*_IDENTITY, "high")
    assert evidence is not None
    resources = CognitiveResourceStore(database, tmp_path / "restart-secrets")
    proposal = CognitiveResourceGroupInput(
        pool="economy",
        label="provisioned",
        base_url=_URL,
        model=_MODEL,
        reasoning_effort="high",
        api_keys=(SecretStr("fixture-key"),),
        capability_revision=evidence.revision,
        capability_digest=evidence.profile_digest,
    )
    owner = cast(
        NoyraService,
        SimpleNamespace(
            kernel=SimpleNamespace(database=database, subject_id=_SUBJECT),
            cognitive_resources=resources,
        ),
    )
    NoyraService._sync_cognitive_resources(owner, (proposal,))
    clock.value += timedelta(days=68)
    NoyraService._sync_cognitive_resources(owner, (proposal,))
    with pytest.raises(RuntimeError, match="environment_model_capability_profile_required"):
        NoyraService._sync_cognitive_resources(
            owner, (proposal.model_copy(update={"label": "new-resource"}),)
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", [0, 1])
async def test_saved_probe_does_not_bypass_budget_or_learn_from_failed_probe(
    tmp_path: Path, budget: int
) -> None:
    provider = FakeProvider([ProviderResponse('{"ok":false}', ModelUsage(3, 3))])
    router, store, group_id = _router(tmp_path, provider, budget=budget)
    with pytest.raises(BudgetExhaustedError if budget == 0 else StructuredOutputError):
        await router.test_resource(group_id, subject_id=_SUBJECT)
    assert len(provider.requests) == budget
    assert store.get(*_IDENTITY) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("param", "code", "expected"),
    [
        ("reasoning_effort", "unsupported_value", "provider_reasoning_effort_rejected"),
        ("temperature", "unsupported_value", "provider_http_400"),
        ("reasoning_effort", "invalid_request_error", "provider_http_400"),
    ],
)
async def test_reasoning_rejection_requires_structured_parameter_evidence(
    param: str, code: str, expected: str
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"code": code, "param": param}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleProvider(
            OpenAICompatibleSettings(base_url=_URL, model=_MODEL, api_key=SecretStr("fixture")),
            client=client,
        )
        with pytest.raises(ProviderCallError, match=expected) as exc:
            await provider.complete(
                CompletionRequest(
                    model=_MODEL,
                    messages=(ModelMessage(role="user", content="fixture"),),
                    max_output_tokens=64,
                    temperature=0,
                    schema_name="Output",
                    output_schema={"type": "object"},
                    reasoning_effort="high",
                )
            )
        assert not exc.value.retryable and not exc.value.outcome_unknown


def test_capability_profile_detects_unhashed_mutation(tmp_path: Any) -> None:
    database = Database(tmp_path / "capability-integrity.sqlite3")
    subject_id = "Noyra-capability-integrity"
    IdentityStore(database).ensure(subject_id, content_hash("integrity"))
    store = CapabilityProfileStore(database)
    store.record_probe_success(
        subject_id,
        "openai_compatible",
        "https://models.example/v1",
        "gpt-5-mini",
        "high",
    )
    with database.transaction() as connection:
        connection.execute(
            "UPDATE model_capability_profiles SET capabilities_json='{}' WHERE subject_id=?",
            (subject_id,),
        )

    with pytest.raises(IntegrityError, match="hash mismatch"):
        store.get(subject_id, "openai_compatible", "https://models.example/v1", "gpt-5-mini")


def test_provider_payload_only_contains_optional_field_when_selected() -> None:
    base: dict[str, Any] = dict(
        model="gpt-5-mini",
        messages=(ModelMessage(role="user", content="hello"),),
        max_output_tokens=8,
        temperature=0,
        schema_name="Output",
        output_schema={"type": "object"},
    )
    without = OpenAICompatibleProvider._request_payload(CompletionRequest(**base))
    with_effort = OpenAICompatibleProvider._request_payload(
        CompletionRequest(**base, reasoning_effort="high")
    )
    assert "reasoning_effort" not in without
    assert with_effort["reasoning_effort"] == "high"
    json.dumps(with_effort)
