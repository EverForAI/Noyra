"""Focused validation tests for environment-backed model resources."""

import json

import pytest

from noyra.model import resource_groups_from_env
from noyra.model.errors import ConfigurationError


def _payload(api_keys: object) -> str:
    return json.dumps(
        [
            {
                "label": "environment-resource",
                "base_url": "https://models.example/v1",
                "model": "environment-model",
                "api_keys": api_keys,
            }
        ]
    )


def test_resource_groups_from_env_accepts_only_string_api_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOYRA_ECONOMY_MODEL_GROUPS_JSON", _payload(["first", "second"]))

    groups = resource_groups_from_env("economy")

    assert len(groups) == 1
    assert [key.get_secret_value() for key in groups[0].api_keys] == ["first", "second"]


@pytest.mark.parametrize(
    "api_keys",
    [None, True, 7, {"key": "value"}, ["valid", None], ["valid", False], ["valid", 7]],
)
def test_resource_groups_from_env_rejects_non_string_api_keys(
    monkeypatch: pytest.MonkeyPatch, api_keys: object
) -> None:
    monkeypatch.setenv("NOYRA_ECONOMY_MODEL_GROUPS_JSON", _payload(api_keys))

    with pytest.raises(ConfigurationError, match="invalid economy model group configuration"):
        resource_groups_from_env("economy")
