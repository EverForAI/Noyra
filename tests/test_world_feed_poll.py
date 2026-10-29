from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from noyra.core import Database, IdentityStore
from noyra.core.errors import IntegrityError, SchemaContractError
from noyra.core.types import content_hash
from noyra.world import FetchedDocument, ObservationStore, SafeWebReader, SourceRegistry
from noyra.world.errors import FetchError, ObservationBudgetError
from noyra.world.feed import MAX_FEED_ITEMS, FeedItem, feed_content, parse_feed
from noyra.world.poll import SourcePollStore
from noyra.world.types import SourceRecord

SUBJECT = "feed-test"
T1 = "2026-08-11T00:00:00.000+00:00"
T2 = "2026-08-11T00:15:00.000+00:00"
T3 = "2026-08-11T00:30:00.000+00:00"


def rss(*entries: str, build: str = "one") -> str:
    return (
        f"<rss><channel><title>News</title><lastBuildDate>{build}</lastBuildDate>"
        + "".join(entries)
        + "</channel></rss>"
    )


def item(guid: str = "one", text: str = "A discovery", title: str = "Update") -> str:
    return f"<item><guid>{guid}</guid><link>https://example.com/{guid}</link><title>{title}</title><description>{text}</description></item>"


def rdf_feed(body: str = "A discovery") -> str:
    return f"""
    <rdf:RDF
        xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
        xmlns="http://purl.org/rss/1.0/"
        xmlns:content="http://purl.org/rss/1.0/modules/content/">
      <channel rdf:about="https://example.com/rss">
        <title>News</title>
        <link>https://example.com/</link>
        <description>Feed description</description>
        <items><rdf:Seq><rdf:li rdf:resource="https://example.com/one" /></rdf:Seq></items>
      </channel>
      <item rdf:about="https://example.com/one">
        <title>Update</title>
        <link>https://example.com/one</link>
        <description>A discovery</description>
        <content:encoded><![CDATA[{body}]]></content:encoded>
      </item>
    </rdf:RDF>
    """


@pytest.fixture
def context(tmp_path: Path) -> tuple[Database, SourceRecord, ObservationStore]:
    database = Database(tmp_path / "test.sqlite3")
    IdentityStore(database).ensure(SUBJECT, content_hash("feed-seed"))
    source = SourceRegistry(database).register(
        SUBJECT, "News", "https://example.com/rss", "rss", status="active"
    )
    return database, source, ObservationStore(database)


def document(source: SourceRecord, xml: str, at: str = T1) -> FetchedDocument:
    title, entries = parse_feed(xml, source.url)
    content = feed_content(entries)
    return FetchedDocument(
        source.url,
        title,
        content,
        content_hash(content),
        "application/rss+xml",
        (),
        None,
        None,
        at,
        entries,
    )


def state(database: Database, source: SourceRecord) -> dict[str, object]:
    with database.connection() as connection:
        result = SourcePollStore._read(connection, SUBJECT, source.source_id)
    assert result is not None
    return result


def test_timestamp_reorder_and_restart_do_not_create_observations(
    context: tuple[Database, SourceRecord, ObservationStore],
) -> None:
    database, source, store = context
    first, created = store.ingest(
        SUBJECT, source.source_id, document(source, rss(item(), item("two")))
    )
    assert first is not None and created
    for at, entries in ((T2, (item("two"), item())), (T3, (item(), item("two")))):
        reopened = ObservationStore(Database(database.path))
        assert reopened.ingest(
            SUBJECT, source.source_id, document(source, rss(*entries, build=at), at)
        ) == (None, False)
    current = state(database, source)
    assert current["last_success_at"] == T3
    assert current["last_change_at"] == T1
    assert current["checks"] == 3 and current["unchanged_checks"] == 2
    assert current["new_item_versions"] == 2
    assert current["baseline"] == "fresh"
    assert SourcePollStore(database).last_checked_at(SUBJECT, source.source_id) == T3
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1


def test_only_new_entry_versions_enter_the_next_observation(
    context: tuple[Database, SourceRecord, ObservationStore],
) -> None:
    database, source, store = context
    store.ingest(SUBJECT, source.source_id, document(source, rss(item())))
    observation, created = store.ingest(
        SUBJECT, source.source_id, document(source, rss(item(), item("two", "New evidence")), T2)
    )
    assert observation is not None and created
    assert "New evidence" in observation.content
    assert "A discovery" not in observation.content
    changed, created = store.ingest(
        SUBJECT,
        source.source_id,
        document(source, rss(item(text="Corrected evidence"), item("two", "New evidence")), T3),
    )
    assert changed is not None and created and "Corrected evidence" in changed.content
    assert state(database, source)["new_item_versions"] == 3


def test_reverting_to_seen_revision_is_not_new(
    context: tuple[Database, SourceRecord, ObservationStore],
) -> None:
    _, source, store = context
    store.ingest(SUBJECT, source.source_id, document(source, rss(item())))
    store.ingest(SUBJECT, source.source_id, document(source, rss(item(text="Revised")), T2))
    assert store.ingest(SUBJECT, source.source_id, document(source, rss(item()), T3)) == (
        None,
        False,
    )


def test_large_feed_is_batched_before_entries_are_marked_seen(
    context: tuple[Database, SourceRecord, ObservationStore],
) -> None:
    database, source, store = context
    xml = rss(
        item("one", "A" * 500),
        item("two", "B" * 500),
        item("three", "C" * 500),
    )
    fetched = document(source, xml)
    assert fetched.feed_items is not None
    max_chars = max(len(feed_content((entry,))) for entry in fetched.feed_items)

    first, created = store.ingest(
        SUBJECT,
        source.source_id,
        fetched,
        max_content_chars=max_chars,
    )

    assert first is not None and created
    assert store.ingest(
        SUBJECT,
        source.source_id,
        fetched,
        max_content_chars=max_chars,
    ) == (None, False)
    with database.connection() as connection:
        observations = [
            str(row[0])
            for row in connection.execute(
                "SELECT content FROM observations WHERE source_id=? ORDER BY rowid",
                (source.source_id,),
            ).fetchall()
        ]
        seen_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM world_feed_seen WHERE source_id=?",
                (source.source_id,),
            ).fetchone()[0]
        )
    assert len(observations) == 3
    assert all(len(content) <= max_chars for content in observations)
    combined = "".join(observations)
    assert all(entry.link in combined for entry in fetched.feed_items)
    assert all(marker in combined for marker in ("A" * 500, "B" * 500, "C" * 500))
    assert seen_count == 3
    assert state(database, source)["new_item_versions"] == 3


def test_oversized_feed_item_is_not_marked_seen(
    context: tuple[Database, SourceRecord, ObservationStore],
) -> None:
    database, source, store = context
    fetched = document(source, rss(item("large", "X" * 3_000)))

    with pytest.raises(ObservationBudgetError, match="exceeds"):
        store.ingest(
            SUBJECT,
            source.source_id,
            fetched,
            max_content_chars=1_024,
        )

    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM world_feed_seen").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM world_poll_state").fetchone()[0] == 0


def test_empty_feed_is_a_successful_heartbeat(
    context: tuple[Database, SourceRecord, ObservationStore],
) -> None:
    database, source, store = context
    assert store.ingest(SUBJECT, source.source_id, document(source, rss())) == (None, False)
    assert state(database, source)["outcome"] == "unchanged"
    assert state(database, source)["last_success_at"] == T1


def test_legacy_extraction_does_not_silently_swallow_new_entries(
    context: tuple[Database, SourceRecord, ObservationStore],
) -> None:
    database, source, store = context
    legacy = FetchedDocument(
        source.url,
        "Old feed",
        "Old flattened content",
        content_hash("Old flattened content"),
        "text/xml",
        (),
        None,
        None,
        T1,
    )
    old, _ = store.record(SUBJECT, source.source_id, legacy)
    new, created = store.ingest(
        SUBJECT, source.source_id, document(source, rss(item(), item("new")), T2)
    )
    assert new is not None and created and "https://example.com/new" in new.content
    assert state(database, source)["baseline"] == "legacy_unavailable"
    assert store.get(old.observation_id, subject_id=SUBJECT) == old
    assert store.ingest(
        SUBJECT, source.source_id, document(source, rss(item(), item("new")), T3)
    ) == (None, False)


def test_failed_poll_backs_off_without_claiming_success(
    context: tuple[Database, SourceRecord, ObservationStore],
) -> None:
    database, source, store = context
    store.ingest(SUBJECT, source.source_id, document(source, rss(item())))
    SourcePollStore(database).failed(SUBJECT, source.source_id, T2)
    current = state(database, source)
    assert current["outcome"] == "failed"
    assert current["last_success_at"] == T1 and current["last_attempt_at"] == T2


def test_poll_and_observation_rollback_together(
    context: tuple[Database, SourceRecord, ObservationStore],
) -> None:
    database, source, store = context
    with (
        patch.object(SourcePollStore, "complete", side_effect=RuntimeError("simulated crash")),
        pytest.raises(RuntimeError),
    ):
        store.ingest(SUBJECT, source.source_id, document(source, rss(item())))
    with database.connection() as connection:
        for table in ("observations", "world_feed_seen", "world_poll_state"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_poll_hash_drift_and_missing_schema_fail_closed(
    context: tuple[Database, SourceRecord, ObservationStore],
) -> None:
    database, source, store = context
    store.ingest(SUBJECT, source.source_id, document(source, rss(item())))
    with database.transaction() as connection:
        connection.execute("UPDATE world_poll_state SET checks=500")
    with pytest.raises(IntegrityError):
        SourcePollStore(database).last_checked_at(SUBJECT, source.source_id)
    with database.transaction() as connection:
        connection.execute("DROP TABLE world_poll_state")
    with pytest.raises(sqlite3.OperationalError):
        SourcePollStore(database).last_checked_at(SUBJECT, source.source_id)


@pytest.mark.parametrize(
    "payload",
    [
        '<!DOCTYPE rss [<!ENTITY x "boom">]><rss><channel/></rss>',
        '<!DOCTYPE rss SYSTEM "https://example.com/evil"><rss><channel/></rss>',
        '<!ENTITY x "boom"><rss><channel/></rss>',
        "<rss>" + "<x>" * 40 + "</x>" * 40 + "</rss>",
        rss(*([item()] * (MAX_FEED_ITEMS + 1))),
        "<rss><channel>",
    ],
    ids=["internal-dtd", "external-dtd", "entity", "depth-limit", "item-limit", "malformed"],
)
def test_hostile_xml_is_rejected(payload: str) -> None:
    with pytest.raises(FetchError):
        parse_feed(payload, "https://example.com/rss")


def test_rss_10_rdf_is_supported_with_stable_identity_and_fingerprint() -> None:
    title, entries = parse_feed(rdf_feed(), "https://example.com/rss")
    changed_title, changed = parse_feed(rdf_feed("A changed discovery"), "https://example.com/rss")

    assert title == changed_title == "News"
    assert len(entries) == len(changed) == 1
    assert entries[0].summary == "A discovery"
    assert entries[0].body == "A discovery"
    assert entries[0].item_key == changed[0].item_key
    assert entries[0].fingerprint != changed[0].fingerprint


def test_rss_10_rdf_keeps_xml_hardening() -> None:
    payload = rdf_feed()
    payload = payload.replace(
        "<rdf:RDF",
        '<!DOCTYPE rdf:RDF [<!ENTITY x "blocked">]><rdf:RDF',
        1,
    )
    with pytest.raises(FetchError, match="world_feed_dtd_rejected"):
        parse_feed(payload, "https://example.com/rss")


def test_atom_content_and_unicode_normalization() -> None:
    xml = (
        '<feed xmlns="http://www.w3.org/2005/Atom"><title>News</title><updated>one</u'
        'pdated><entry><id>item</id><link href="/news"/><title>Café</title><content '
        'type="html">A  discovery</content></entry></feed>'
    )
    title, entries = parse_feed(xml, "https://example.com/rss")
    _, same = parse_feed(
        xml.replace("one", "two")
        .replace("Cafe\u0301", "Café")
        .replace("A  discovery", "A discovery"),
        "https://example.com/rss",
    )
    assert title == "News" and entries == same
    assert entries[0].link == "https://example.com/news"
    assert entries[0].body == "A discovery"


def test_atom_text_construct_preserves_literal_markup_and_detects_revisions() -> None:
    template = (
        '<feed xmlns="http://www.w3.org/2005/Atom"><title>News</title>'
        '<entry><id>item</id><title type="text">Use &lt;{word}&gt; for storage</title>'
        '<content type="text">Use &lt;{word}&gt; for storage</content></entry></feed>'
    )
    _, vector = parse_feed(template.format(word="vector"), "https://example.com/rss")
    _, deque = parse_feed(template.format(word="deque"), "https://example.com/rss")

    assert vector[0].title == "Use <vector> for storage"
    assert vector[0].body == "Use <vector> for storage"
    assert vector[0].item_key == deque[0].item_key
    assert vector[0].fingerprint != deque[0].fingerprint


def test_atom_semantic_correction_emits_one_bounded_upgrade_version(
    context: tuple[Database, SourceRecord, ObservationStore],
) -> None:
    database, source, store = context
    xml = (
        '<feed xmlns="http://www.w3.org/2005/Atom"><title>News</title><entry>'
        '<id>item</id><title type="text">Use &lt;vector&gt; for storage</title>'
        '<content type="text">Use &lt;vector&gt; for storage</content></entry></feed>'
    )
    title, entries = parse_feed(xml, source.url)
    content = feed_content(entries)
    document_value = FetchedDocument(
        source.url,
        title,
        content,
        content_hash(content),
        "application/atom+xml",
        (),
        None,
        None,
        T1,
        entries,
    )
    old_payload = {"title": "Use for storage", "link": "", "summary": "", "body": "Use for storage"}
    old_identity = {"id": "item", "link": ""}
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO world_feed_seen(source_id,subject_id,fingerprint_version,"
            "item_key,fingerprint,first_seen_at) VALUES (?,?,?,?,?,?)",
            (
                source.source_id,
                SUBJECT,
                1,
                content_hash({"version": 1, "identity": old_identity}),
                content_hash({"version": 1, "content": old_payload}),
                T1,
            ),
        )

    first, created = store.ingest(SUBJECT, source.source_id, document_value)
    second, second_created = store.ingest(
        SUBJECT, source.source_id, replace(document_value, fetched_at=T2)
    )
    assert first is not None and created
    assert second is None and not second_created
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1


def test_atom_xhtml_skips_hidden_subtrees_and_decodes_html_once() -> None:
    xml = (
        '<feed xmlns="http://www.w3.org/2005/Atom"><title>News</title><entry>'
        '<id>item</id><content type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml">'
        "Hello <script>hidden instruction</script>world<style>hidden style</style>"
        "<span>visible</span></div></content></entry></feed>"
    )
    _, xhtml = parse_feed(xml, "https://example.com/rss")
    html = (
        '<feed xmlns="http://www.w3.org/2005/Atom"><title>News</title><entry>'
        '<id>item</id><content type="html">&amp;lt;vector&amp;gt;</content>'
        "</entry></feed>"
    )
    _, escaped = parse_feed(html, "https://example.com/rss")

    assert xhtml[0].body == "Hello world visible"
    assert "hidden instruction" not in xhtml[0].body
    assert "hidden style" not in xhtml[0].body
    assert escaped[0].body == "<vector>"


def test_duplicate_or_missing_ids_do_not_merge_different_items() -> None:
    _, entries = parse_feed(
        rss(item(), item("two").replace("<guid>two</guid>", "<guid>one</guid>")),
        "https://example.com/rss",
    )
    assert len(entries) == 2 and entries[0].item_key != entries[1].item_key
    _, anonymous = parse_feed(
        rss(
            "<item><title>A|B</title><description>C</description></item>",
            "<item><title>A</title><description>B|C</description></item>",
        ),
        "https://example.com/rss",
    )
    assert len(anonymous) == 2 and anonymous[0].fingerprint != anonymous[1].fingerprint


def test_seen_cache_eviction_uses_first_appearance(
    context: tuple[Database, SourceRecord, ObservationStore],
) -> None:
    database, source, store = context
    with patch("noyra.world.poll.MAX_SEEN_VERSIONS", 2):
        store.ingest(SUBJECT, source.source_id, document(source, rss(item(), item("two"))))
        store.ingest(SUBJECT, source.source_id, document(source, rss(item("two"), item()), T2))
        store.ingest(SUBJECT, source.source_id, document(source, rss(item("three")), T3))
    with database.connection() as connection:
        times = [r[0] for r in connection.execute("SELECT first_seen_at FROM world_feed_seen")]
    assert len(times) == 2 and T2 not in times and T3 in times


@pytest.mark.asyncio
async def test_reader_uses_feed_parser_on_200_without_validators(
    context: tuple[Database, SourceRecord, ObservationStore],
) -> None:
    _, source, store = context
    counter = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal counter
        counter += 1
        return httpx.Response(
            200,
            headers={"Content-Type": "application/rss+xml"},
            text=rss(item(), build=str(counter)),
        )

    async def resolver(host: str, port: int) -> tuple[str, ...]:
        return ("93.184.216.34",)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        reader = SafeWebReader(client=client, resolver=resolver, verify_peer_address=False)
        one = await reader.fetch(source)
        two = await reader.fetch(source)
    assert one.etag is None and two.last_modified is None
    assert one.content_hash == two.content_hash and one.feed_items
    assert store.ingest(SUBJECT, source.source_id, one)[1]
    assert store.ingest(SUBJECT, source.source_id, two) == (None, False)


def test_v107_upgrade_preserves_observations_and_matches_new_contract(
    context: tuple[Database, SourceRecord, ObservationStore],
) -> None:
    database, source, store = context
    old = FetchedDocument(
        source.url,
        "Legacy",
        "Legacy content",
        content_hash("Legacy content"),
        "text/xml",
        (),
        None,
        None,
        T1,
    )
    observation, _ = store.record(SUBJECT, source.source_id, old)
    with database.transaction() as connection:
        connection.execute("DROP TABLE world_feed_seen")
        connection.execute("DROP TABLE world_poll_state")
        connection.execute("DELETE FROM persistent_features WHERE feature_id='world_poll_state'")
        connection.execute("UPDATE schema_meta SET value='107' WHERE key='schema_version'")
        connection.execute("DELETE FROM schema_meta WHERE key='schema_ddl_contract'")
    upgraded = Database(database.path)
    upgraded.assert_schema_contract()
    assert (
        ObservationStore(upgraded).get(observation.observation_id, subject_id=SUBJECT)
        == observation
    )
    with upgraded.transaction() as connection:
        connection.execute("DROP TABLE world_poll_state")
    with pytest.raises(SchemaContractError):
        Database(database.path, initialize=False, strict_schema_contract=True)


def test_acquisition_export_preserves_subject_isolation(
    context: tuple[Database, SourceRecord, ObservationStore],
) -> None:
    from io import BytesIO
    from zipfile import ZipFile

    from noyra.core.runtime_export import RuntimeLogExporter
    from noyra.core.types import strict_json_loads

    database, source, store = context
    store.ingest(SUBJECT, source.source_id, document(source, rss(item())))
    other_subject = "other-feed-subject"
    IdentityStore(database).ensure(other_subject, content_hash(other_subject))
    other_source = SourceRegistry(database).register(
        other_subject, "Other source", "https://example.org/rss", "rss", status="active"
    )
    store.ingest(other_subject, other_source.source_id, document(other_source, rss(item("other"))))
    artifact = RuntimeLogExporter(database).export(SUBJECT, actor="synthetic-test")
    with ZipFile(BytesIO(artifact.content)) as archive:
        for name in ("world_poll_state", "world_feed_seen"):
            rows = [
                strict_json_loads(line.decode())
                for line in archive.read(f"tables/{name}.jsonl").splitlines()
            ]
            assert len(rows) == 1
            assert rows[0]["subject_id"] == SUBJECT
            assert rows[0]["source_id"] == source.source_id


def test_older_runtime_rejects_new_schema_without_silently_polling(
    context: tuple[Database, SourceRecord, ObservationStore],
) -> None:
    database, source, store = context
    store.ingest(SUBJECT, source.source_id, document(source, rss(item())))
    with (
        patch("noyra.core.database.CURRENT_SCHEMA_VERSION", 107),
        pytest.raises(RuntimeError, match="newer than runtime"),
    ):
        Database(database.path)
    current = Database(database.path)
    current.assert_schema_contract()
    assert state(current, source)["checks"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("media_type", ["text/xml", "application/xml"])
async def test_generic_xml_feed_is_detected_without_rss_source_label(
    context: tuple[Database, SourceRecord, ObservationStore],
    media_type: str,
) -> None:
    _, source, _ = context

    async def resolver(host: str, port: int) -> tuple[str, ...]:
        return ("93.184.216.34",)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"Content-Type": media_type}, text=rss(item())
            )
        )
    ) as client:
        reader = SafeWebReader(client=client, resolver=resolver, verify_peer_address=False)
        result = await reader.fetch(replace(source, source_type="web"))
    assert result.feed_items and len(result.feed_items) == 1


@pytest.mark.asyncio
async def test_valid_non_feed_xml_keeps_generic_api_extraction(
    context: tuple[Database, SourceRecord, ObservationStore],
) -> None:
    _, source, _ = context

    async def resolver(host: str, port: int) -> tuple[str, ...]:
        return ("93.184.216.34",)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"Content-Type": "application/xml"},
                text="<weather><summary>Cloudy</summary></weather>",
            )
        )
    ) as client:
        reader = SafeWebReader(client=client, resolver=resolver, verify_peer_address=False)
        result = await reader.fetch(replace(source, source_type="api"))
    assert result.feed_items is None and result.content == "Cloudy"


def namespaced_feed(
    kind: str, body: str = "Core body", *, extensions: bool = False, before: bool = False
) -> str:
    declarations = (
        ' xmlns:e="urn:example:extension" xmlns:c="http://purl.org/rss/1.0/modules/content/"'
    )
    foreign = (
        (
            "<e:title>Foreign title</e:title><e:description>Foreign summary</e:description>"
            "<e:summary>Foreign summary</e:summary><e:content>Foreign body</e:content>"
            "<e:encoded>Foreign encoded</e:encoded><e:id>foreign-id</e:id>"
            '<e:guid>foreign-guid</e:guid><e:link href="https://example.com/foreign"/>'
        )
        if extensions
        else ""
    )
    foreign_title = "<e:title>Foreign feed title</e:title>" if extensions else ""
    if kind == "atom":
        core = (
            "<a:id>core-id</a:id><a:title>Core title</a:title>"
            '<a:link href="https://example.com/core"/>'
            f'<a:summary>Core summary</a:summary><a:content type="text">{body}</a:content>'
        )
        entry = foreign + core if before else core + foreign
        return (
            '<a:feed xmlns:a="http://www.w3.org/2005/Atom"'
            + declarations
            + ">"
            + foreign_title
            + "<a:title>Core feed</a:title><a:entry>"
            + entry
            + "</a:entry></a:feed>"
        )
    prefix = "r:" if kind == "rdf" else ""
    core = (
        f"<{prefix}title>Core title</{prefix}title>"
        f"<{prefix}link>https://example.com/core</{prefix}link>"
        f"<{prefix}description>Core summary</{prefix}description>"
        f"<c:encoded>{body}</c:encoded>"
    )
    if kind == "rss":
        core += "<guid>core-id</guid>"
    entry = foreign + core if before else core + foreign
    if kind == "rdf":
        return (
            '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"'
            ' xmlns:r="http://purl.org/rss/1.0/"' + declarations + ">"
            '<r:channel rdf:about="https://example.com/rss">'
            + foreign_title
            + "<r:title>Core feed</r:title></r:channel>"
            '<r:item rdf:about="core-id">' + entry + "</r:item></rdf:RDF>"
        )
    return (
        "<rss"
        + declarations
        + "><channel>"
        + foreign_title
        + "<title>Core feed</title><item>"
        + entry
        + "</item></channel></rss>"
    )


@pytest.mark.parametrize("kind", ["rss", "rdf", "atom"])
@pytest.mark.parametrize("before", [False, True], ids=["extension-last", "extension-first"])
def test_core_feed_fields_ignore_foreign_namespaces(kind: str, before: bool) -> None:
    expected = parse_feed(namespaced_feed(kind), "https://example.com/rss")
    actual = parse_feed(
        namespaced_feed(kind, extensions=True, before=before), "https://example.com/rss"
    )
    assert actual == expected
    assert actual[0] == "Core feed"
    assert actual[1][0].body == "Core body"
    assert actual[1][0].link == "https://example.com/core"


@pytest.mark.parametrize("kind", ["rss", "rdf", "atom"])
def test_foreign_fields_cannot_hide_core_content_revision(kind: str) -> None:
    _, first = parse_feed(
        namespaced_feed(kind, "Original core", extensions=True), "https://example.com/rss"
    )
    _, revised = parse_feed(
        namespaced_feed(kind, "Revised core", extensions=True), "https://example.com/rss"
    )
    assert first[0].item_key == revised[0].item_key
    assert first[0].fingerprint != revised[0].fingerprint
    assert revised[0].body == "Revised core"


@pytest.mark.parametrize("kind", ["rss", "rdf", "atom"])
def test_namespace_prefix_spelling_does_not_change_fingerprints(kind: str) -> None:
    xml = namespaced_feed(kind, extensions=True)
    renamed = xml
    for old, new in (("a", "atom"), ("r", "rss1"), ("e", "metadata")):
        renamed = renamed.replace(f"xmlns:{old}=", f"xmlns:{new}=")
        renamed = renamed.replace(f"<{old}:", f"<{new}:").replace(f"</{old}:", f"</{new}:")
    assert parse_feed(renamed, "https://example.com/rss") == parse_feed(
        xml, "https://example.com/rss"
    )


@pytest.mark.parametrize("kind", ["rss", "rdf", "atom"])
def test_core_namespaces_preserve_existing_v1_fingerprints(kind: str) -> None:
    # Captured from the pre-repair parser on canonical, extension-free input.
    _, entries = parse_feed(namespaced_feed(kind), "https://example.com/rss")
    assert entries[0].item_key == "f5073c64ea2885cbd92d2f5db92c8c881238a3bde4579e22b3e132ec42d894a0"
    assert (
        entries[0].fingerprint == "c78959fdcd4238f154efb93d5fbd57257e0696c7ae28b201204c23dd3aaa31b4"
    )


@pytest.mark.parametrize("kind", ["rss", "rdf", "atom"])
def test_foreign_entry_resources_are_not_feed_items(kind: str) -> None:
    xml = namespaced_feed(kind)
    fake = "<e:item><e:title>Not an RSS item</e:title></e:item>"
    if kind == "atom":
        xml = xml.replace("</a:feed>", fake.replace("item", "entry") + "</a:feed>")
    elif kind == "rdf":
        xml = xml.replace("</rdf:RDF>", fake + "</rdf:RDF>")
    else:
        xml = xml.replace("</channel>", fake + "</channel>")
        xml = xml.replace("<channel>", "<e:channel/><channel>")
    assert parse_feed(xml, "https://example.com/rss") == parse_feed(
        namespaced_feed(kind), "https://example.com/rss"
    )


@pytest.mark.parametrize("kind", ["rss", "rdf", "atom"])
def test_namespace_correction_keeps_old_evidence_and_emits_once(
    context: tuple[Database, SourceRecord, ObservationStore], kind: str
) -> None:
    database, source, store = context
    legacy_payload = {
        "title": "Foreign title",
        "link": "https://example.com/core" if kind == "atom" else "https://example.com/foreign",
        "summary": "Foreign summary",
        "body": "Foreign encoded",
    }
    legacy_key = content_hash(
        {
            "version": 1,
            "identity": {
                "id": "foreign-guid",
                "link": legacy_payload["link"],
            },
        }
    )
    legacy_fingerprint = content_hash({"version": 1, "content": legacy_payload})
    legacy_item = FeedItem(legacy_key, legacy_fingerprint, **legacy_payload)
    current = document(source, namespaced_feed(kind, extensions=True), T2)
    legacy_content = feed_content((legacy_item,))
    legacy_document = replace(
        current,
        title="Foreign feed title",
        content=legacy_content,
        content_hash=content_hash(legacy_content),
        feed_items=(legacy_item,),
        fetched_at=T1,
    )
    old, created = store.ingest(SUBJECT, source.source_id, legacy_document)
    assert old is not None and created
    corrected, created = store.ingest(SUBJECT, source.source_id, current)
    assert corrected is not None and created and "Core body" in corrected.content
    assert store.ingest(SUBJECT, source.source_id, replace(current, fetched_at=T3)) == (None, False)
    assert store.get(old.observation_id, subject_id=SUBJECT) == old
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM world_feed_seen").fetchone()[0] == 2
        assert (
            connection.execute("SELECT fingerprint_version FROM world_poll_state").fetchone()[0]
            == 1
        )
