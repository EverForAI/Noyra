"""Bounded properties exercise the real admission gate, HTTP server and SQL definitions."""

from __future__ import annotations

import hashlib
import itertools
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from noyra.core.admission import OperationInvalidated, OperationLease, RuntimeAdmissionGate
from noyra.core.database import CURRENT_SCHEMA_VERSION, MIGRATIONS, SCHEMA_SQL
from noyra.service import NoyraHTTPServer
from test_participant_http import Client
from test_participant_http import http as base_http


@pytest.fixture
def http(tmp_path: Path) -> Iterator[tuple[NoyraHTTPServer, str]]:
    yield from cast(Any, base_http).__wrapped__(tmp_path)


def test_old_migration_definitions_are_immutable() -> None:
    contract = json.loads(
        (Path(__file__).parent / "contracts/database-definition-contract.json").read_text()
    )
    for version, digest in contract["migration_sha256"].items():
        assert hashlib.sha256(MIGRATIONS[int(version)].encode()).hexdigest() == digest, version
    if contract["schema_version"] == CURRENT_SCHEMA_VERSION:
        assert hashlib.sha256(SCHEMA_SQL.encode()).hexdigest() == contract["schema_sha256"]


def test_invalidated_leases_never_revive_in_generated_lifecycle_sequences() -> None:
    # Exhaustively enumerate short histories, including repeated reopening and
    # invalidation. Old work must never regain permission after an epoch change.
    for sequence in itertools.product(("open", "pause", "quarantine", "drain"), repeat=4):
        gate = RuntimeAdmissionGate("architecture-subject")
        leases: list[OperationLease] = []
        stale: list[OperationLease] = []
        for action in sequence:
            if gate.accepting and not gate.quarantined:
                leases.append(gate.begin("generated-operation"))
            before = gate.epoch
            if action == "open":
                if not gate.closed:
                    gate.clear_quarantine()
                    gate.open()
            elif action == "pause":
                gate.invalidate()
            elif action == "quarantine":
                gate.quarantine()
            else:
                gate.begin_drain()
            if gate.epoch != before:
                stale = list(leases)
            for lease in stale:
                with pytest.raises(OperationInvalidated):
                    lease.assert_current()
            assert gate.active_operations == len(leases)
        for lease in leases:
            gate.finish(lease)
        assert gate.wait_for_drain(timeout=0)


def test_lease_cannot_authorize_another_subject() -> None:
    first = RuntimeAdmissionGate("architecture-first")
    second = RuntimeAdmissionGate("architecture-second")
    with first.operation("cross-subject") as lease:
        with pytest.raises(OperationInvalidated):
            second.assert_current(lease)
        lease.assert_current()


@pytest.mark.parametrize("prefix", ["/api/", "/api/v1/"])
@pytest.mark.parametrize(
    "suffix",
    [
        "admin/migration/policy",
        "admin/export-jobs",
        "config/model-resources",
        "admin/retention",
    ],
)
def test_management_reads_require_operator_identity(
    http: tuple[NoyraHTTPServer, str], prefix: str, suffix: str
) -> None:
    owner, base = http
    anonymous = Client(base)
    assert anonymous.request(prefix + suffix)[0] == 401
    # A real participant session is still not an operator session.
    from noyra.interaction.accounts import AccountStore

    AccountStore(owner.kernel.database, owner.kernel.subject_id).register(
        "archhuman", "correct horse battery", "Human"
    )
    anonymous.login("archhuman")
    assert anonymous.request(prefix + suffix)[0] == 401


@pytest.mark.parametrize("suffix", ["state", "diary"])
def test_public_read_aliases_preserve_payload_and_headers(
    http: tuple[NoyraHTTPServer, str], suffix: str
) -> None:
    from urllib.request import urlopen

    _, base = http
    with (
        urlopen(base + "/api/" + suffix, timeout=10) as plain,
        urlopen(base + "/api/v1/" + suffix, timeout=10) as versioned,
    ):
        assert plain.status == versioned.status == 200
        assert json.loads(plain.read()) == json.loads(versioned.read())
        for key in (
            "Content-Type",
            "Cache-Control",
            "X-Content-Type-Options",
            "Content-Security-Policy",
        ):
            assert plain.headers.get(key) == versioned.headers.get(key)
