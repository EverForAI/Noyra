from __future__ import annotations

from noyra.interaction.public_contract import (
    PUBLIC_CONTRACT_ID,
    PUBLIC_DEFAULT_LIMIT,
    PUBLIC_ENDPOINTS,
    PUBLIC_MAX_RESPONSE_BYTES,
    PUBLIC_MAX_ROWS,
)


def test_public_contract_v1_has_stable_limits_and_endpoints() -> None:
    assert PUBLIC_CONTRACT_ID == "public-contract-v1"
    assert PUBLIC_DEFAULT_LIMIT == 100
    assert PUBLIC_MAX_ROWS == 1_000
    assert PUBLIC_MAX_RESPONSE_BYTES == 2_000_000
    assert PUBLIC_ENDPOINTS == (
        "/api/state",
        "/api/diary",
        "/api/behavior",
        "/api/interactions",
        "/api/public-posts",
    )
