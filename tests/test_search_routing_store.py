from __future__ import annotations

from typing import Any

import pytest

from noyra.core import Database, IdentityStore
from noyra.core.errors import IntegrityError
from noyra.core.types import content_hash
from noyra.research.provider import SearchProviderStore
from noyra.research.types import SearchProviderInput


def test_route_update_does_not_overwrite_a_corrupt_existing_state(tmp_path: Any) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    subject = "Noyra-route-state"
    IdentityStore(database).ensure(subject, content_hash({"subject": subject}))
    providers = SearchProviderStore(database, tmp_path / "secrets")
    provider = providers.configure(
        subject,
        SearchProviderInput(
            provider_type="brave",
            label="route integrity",
            api_key="route-integrity-key",
        ),
        actor="operator",
    )
    with database.transaction() as connection:
        connection.execute(
            "UPDATE search_provider_routing SET state_hash=? WHERE config_id=?",
            ("0" * 64, provider.config_id),
        )

    with pytest.raises(IntegrityError):
        providers.set_routing(provider.config_id, subject, priority=4, weight=5)

    with database.connection() as connection:
        row = connection.execute(
            "SELECT priority, weight, state_hash FROM search_provider_routing WHERE config_id=?",
            (provider.config_id,),
        ).fetchone()
    assert (row["priority"], row["weight"], row["state_hash"]) == (
        100,
        1,
        "0" * 64,
    )
