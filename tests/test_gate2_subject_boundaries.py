from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from noyra.core import Database, IdentityStore
from noyra.core.errors import NotFoundError
from noyra.core.types import content_hash, utc_now
from noyra.interaction import TransportInput, TransportStore
from noyra.model.embedding_resources import EmbeddingResourceInput, EmbeddingResourceStore
from noyra.model.resources import CognitiveResourceGroupInput, CognitiveResourceStore
from noyra.research import SearchProviderInput, SearchProviderStore
from noyra.world import ClaimProposal, FetchedDocument, ObservationStore, SourceRegistry
from noyra.world.store import WorldClaimStore


def _database(tmp_path: Path) -> tuple[Database, str, str]:
    database = Database(tmp_path / "noyra.sqlite3")
    owner = "Noyra-gate2-owner"
    foreign = "Noyra-gate2-foreign"
    identities = IdentityStore(database)
    identities.ensure(owner, content_hash({"subject": owner}))
    identities.ensure(foreign, content_hash({"subject": foreign}))
    return database, owner, foreign


def test_world_reads_and_revisions_require_the_owner(tmp_path: Path) -> None:
    database, owner, foreign = _database(tmp_path)
    source = SourceRegistry(database).register(
        owner,
        "owner source",
        "https://example.com/owner",
        "news",
        status="active",
        reason="boundary fixture",
    )
    with pytest.raises(NotFoundError):
        SourceRegistry(database).get(source.source_id, subject_id=foreign)
    with pytest.raises(NotFoundError):
        SourceRegistry(database).revise(
            source.source_id,
            subject_id=foreign,
            trust_score=0.4,
            status="blocked",
            reason="foreign subject must not revise",
        )

    document = FetchedDocument(
        url=source.url,
        title="owner document",
        content="owner content",
        content_hash=content_hash("owner content"),
        media_type="text/plain",
        injection_signals=(),
        etag=None,
        last_modified=None,
        fetched_at=utc_now(),
    )
    observation, _ = ObservationStore(database).record(owner, source.source_id, document)
    with pytest.raises(NotFoundError):
        ObservationStore(database).get(observation.observation_id, subject_id=foreign)
    claim = WorldClaimStore(database).create(
        owner,
        ClaimProposal(proposition="owner claim", confidence=0.7),
        evidence_observation_ids=(observation.observation_id,),
    )
    with pytest.raises(NotFoundError):
        WorldClaimStore(database).get(claim.claim_id, subject_id=foreign)
    with pytest.raises(NotFoundError):
        WorldClaimStore(database).revise(
            claim.claim_id,
            ClaimProposal(proposition="foreign update", confidence=0.2),
            subject_id=foreign,
            status="contested",
            evidence_observation_ids=(observation.observation_id,),
            reason="foreign subject must not revise",
        )


def test_resource_reads_are_subject_scoped(tmp_path: Path) -> None:
    database, owner, foreign = _database(tmp_path)
    cognitive = CognitiveResourceStore(database, tmp_path / "cognitive")
    group = cognitive.configure(
        owner,
        CognitiveResourceGroupInput(
            pool="economy",
            label="owner model",
            base_url="https://models.example/v1",
            model="owner-model",
            api_keys=(SecretStr("owner-model-key"),),
        ),
        actor="operator",
    )
    with pytest.raises(NotFoundError):
        cognitive.get(group.group_id, subject_id=foreign)
    with pytest.raises(NotFoundError):
        cognitive.keys(group.group_id, subject_id=foreign)

    embedding = EmbeddingResourceStore(database, tmp_path / "embedding")
    embedding_record = embedding.configure(
        owner,
        EmbeddingResourceInput(
            label="owner embedding",
            base_url="https://embeddings.example/v1",
            model="owner-embedding",
            api_key=SecretStr("owner-embedding-key"),
        ),
        actor="operator",
    )
    with pytest.raises(NotFoundError):
        embedding.get(embedding_record.config_id, subject_id=foreign)

    search = SearchProviderStore(database, tmp_path / "search")
    provider = search.configure(
        owner,
        SearchProviderInput(
            provider_type="brave",
            label="owner search",
            api_key="owner-search-key",
        ),
        actor="operator",
    )
    with pytest.raises(NotFoundError):
        search.get(provider.config_id, subject_id=foreign)
    with pytest.raises(NotFoundError):
        search.api_key(provider.config_id, subject_id=foreign)


def test_transport_reads_are_subject_scoped(tmp_path: Path) -> None:
    database, owner, foreign = _database(tmp_path)
    transports = TransportStore(database, tmp_path / "transport")
    transport = transports.configure(
        owner,
        TransportInput(
            channel="webhook",
            label="owner webhook",
            endpoint="https://example.com/hook",
            credentials={"token": SecretStr("owner-token")},
        ),
        actor="operator",
    )
    with pytest.raises(NotFoundError):
        transports.get(transport.transport_id, subject_id=foreign)
    with pytest.raises(NotFoundError):
        transports.settings(transport.transport_id, subject_id=foreign)
    with pytest.raises(NotFoundError):
        transports.secret(transport.transport_id, subject_id=foreign)
    with pytest.raises(NotFoundError):
        transports.delivery_counterparty(
            transport.transport_id,
            {"channel": "web", "kind": "help_request", "counterparty": "fallback"},
            subject_id=foreign,
        )
