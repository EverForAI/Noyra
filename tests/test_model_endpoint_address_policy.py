from __future__ import annotations

import ipaddress
import socket
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import httpcore
import pytest
from pydantic import SecretStr, ValidationError

from noyra.core import Database, IdentityStore
from noyra.core.errors import IntegrityError
from noyra.core.http import is_public_address
from noyra.core.types import content_hash
from noyra.model.config import OpenAICompatibleSettings
from noyra.model.errors import ProviderCallError
from noyra.model.openai_compatible import OpenAICompatibleProvider
from noyra.model.resources import (
    CognitiveResourceGroupInput,
    CognitiveResourceStore,
    _normalize_public_model_url,
)
from noyra.model.types import CompletionRequest, ModelMessage

UNSAFE = (
    "127.0.0.1",
    "10.0.0.1",
    "169.254.169.254",
    "0.0.0.0",
    "100.64.0.1",
    "198.18.0.1",
    "224.0.0.1",
    "192.0.0.9",
    "192.88.99.1",
    "::",
    "::1",
    "fe80::1",
    "fc00::1",
    "ff02::1",
    "64:ff9b::808:808",
    "64:ff9b:1::808:808",
    "::ffff:8.8.8.8",
    "2002:0808:0808::",
    "2001:0:4136:e378:8000:63bf:3fff:fdd2",
    "3fff::1",
    "5f00::1",
    "2606:4700:4700::1111%1",
    "2606:4700:4700::1111%25eth0",
)
PUBLIC = ("8.8.8.8", "1.1.1.1", "2606:4700:4700::1111", "2001:4860:4860::8888")


def endpoint(address: str) -> str:
    host = f"[{address}]" if ":" in address else address
    return f"https://{host}/v1"


def proposal(url: str) -> CognitiveResourceGroupInput:
    return CognitiveResourceGroupInput(
        pool="economy",
        label="synthetic-endpoint",
        base_url=url,
        model="synthetic",
        api_keys=(SecretStr("synthetic-key"),),
    )


def values(url: str) -> tuple[Any, ...]:
    # One otherwise-valid durable contract. No DB, DNS, provider or secret-file I/O.
    return (
        "synthetic-subject",
        "economy",
        "synthetic",
        "openai_compatible",
        url,
        "synthetic-model",
        0,
        1,
        100,
        10000,
        10000,
        10000,
        0,
        0,
        1,
        None,
    )


@pytest.mark.parametrize("address", UNSAFE)
def test_new_input_rejects_literals_without_rewriting_historical_contract(address: str) -> None:
    assert not is_public_address(ipaddress.ip_address(address))
    url = endpoint(address)
    with pytest.raises(ValidationError, match=r"public|local"):
        proposal(url)
    historical_literal = ipaddress.ip_address(address.split("%", 1)[0])
    if historical_literal.is_global:
        CognitiveResourceStore._validate_group_values(values(url), "active")
    else:
        with pytest.raises(IntegrityError, match="durable state"):
            CognitiveResourceStore._validate_group_values(values(url), "active")


@pytest.mark.parametrize("address", PUBLIC)
def test_public_literals_remain_valid_for_input_and_durable_state(address: str) -> None:
    assert is_public_address(ipaddress.ip_address(address))
    url = endpoint(address)
    assert proposal(url + "/").base_url == url
    CognitiveResourceStore._validate_group_values(values(url), "active")


@pytest.mark.parametrize("url", ["https://models.example/v1", "https://MODELS.example:443/v1"])
def test_saving_hostname_does_not_resolve_or_contact_it(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    lookup = Mock(side_effect=AssertionError("saving endpoints must not perform DNS"))
    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    assert _normalize_public_model_url(url) == url
    assert proposal(url).base_url == url
    CognitiveResourceStore._validate_group_values(values(url), "active")
    lookup.assert_not_called()


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:8000/v1", "http://[::1]:8000/v1", "http://localhost:8000/v1"]
)
def test_explicit_local_provider_settings_keep_their_separate_opt_in_contract(url: str) -> None:
    settings = OpenAICompatibleSettings(
        base_url=url,
        model="synthetic",
        api_key=SecretStr("synthetic"),
        allow_local_endpoint=True,
    )
    assert settings.base_url == url
    with pytest.raises(ValidationError, match="opt-in"):
        OpenAICompatibleSettings(base_url=url, model="synthetic", api_key=SecretStr("synthetic"))
    with pytest.raises(ValidationError, match=r"public|HTTPS|local"):
        proposal(url)


def test_previously_accepted_literal_can_still_be_read_disabled_and_revoked(tmp_path: Path) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    subject = "synthetic-legacy-model"
    IdentityStore(database).ensure(subject, content_hash(subject))
    store = CognitiveResourceStore(database, tmp_path / "secrets")
    url = endpoint("64:ff9b::808:808")
    assert ipaddress.ip_address("64:ff9b::808:808").is_global
    assert not is_public_address(ipaddress.ip_address("64:ff9b::808:808"))
    # Reproduce the old admission policy through the real configure path:
    # normal pydantic type, store writes, hashes, policies and revisions.
    legacy = proposal("https://models.example/v1").model_copy(update={"base_url": url})
    with patch("noyra.model.resources._normalize_public_model_url", return_value=url):
        group = store.configure(subject, legacy, actor="operator")
    with database.connection() as connection:
        before = [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM cognitive_resource_groups WHERE group_id=?", (group.group_id,)
            )
        ]
    assert store.get(group.group_id, subject_id=subject).base_url == url
    assert store.list(subject)[0].base_url == url
    store.verify_integrity(subject)
    with database.connection() as connection:
        assert before == [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM cognitive_resource_groups WHERE group_id=?", (group.group_id,)
            )
        ]
    assert (
        store.disable(
            group.group_id,
            subject_id=subject,
            actor="operator",
            reason="replace unsupported endpoint",
        ).status
        == "disabled"
    )
    assert (
        store.revoke(
            group.group_id,
            subject_id=subject,
            actor="operator",
            reason="replace unsupported endpoint",
        ).status
        == "revoked"
    )
    store.verify_integrity(subject)


def test_copied_input_cannot_bypass_new_configuration_policy(tmp_path: Path) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    subject = "synthetic-copied-model"
    IdentityStore(database).ensure(subject, content_hash(subject))
    store = CognitiveResourceStore(database, tmp_path / "secrets")
    unsafe = proposal("https://models.example/v1").model_copy(
        update={"base_url": endpoint("64:ff9b::808:808")}
    )
    with pytest.raises(IntegrityError, match="durable state"):
        store.configure(subject, unsafe, actor="operator")
    assert store.list(subject) == []
    assert not list((tmp_path / "secrets").glob("*.key"))


@pytest.mark.asyncio
@pytest.mark.parametrize("address", ["64:ff9b::808:808", "224.0.0.1", "2606:4700:4700::1111%1"])
async def test_legacy_endpoint_still_rejected_before_physical_model_connection(
    monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    connect = AsyncMock(side_effect=AssertionError("no physical connection may be attempted"))
    lookup = Mock(side_effect=AssertionError("literal address must not require DNS"))
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    provider = OpenAICompatibleProvider(
        OpenAICompatibleSettings(
            base_url=endpoint(address), model="synthetic", api_key=SecretStr("synthetic")
        )
    )
    try:
        with pytest.raises(ProviderCallError) as caught:
            await provider.complete(
                CompletionRequest(
                    model="synthetic",
                    messages=(ModelMessage(role="user", content="synthetic"),),
                    max_output_tokens=16,
                    temperature=0.0,
                    schema_name="synthetic",
                    output_schema={"type": "object"},
                )
            )
        assert caught.value.code == "provider_endpoint_rejected"
        assert not caught.value.retryable and not caught.value.outcome_unknown
        connect.assert_not_called()
        lookup.assert_not_called()
    finally:
        await provider.aclose()
