from __future__ import annotations

import base64
import json
import secrets
import sqlite3
import tempfile
import unittest
import zlib
from collections.abc import Sequence
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from pydantic import ValidationError

from noyra.core import Database, IdentityStore
from noyra.core.database import public_post_identity_hash
from noyra.core.errors import IntegrityError
from noyra.core.types import content_hash
from noyra.interaction import (
    InteractionIntegrity,
    PublicPostCapacityError,
    PublicPostCaptchaError,
    PublicPostConflictError,
    PublicPostInput,
    PublicPostQueueFullError,
    PublicPostRateLimitError,
    PublicPostRecord,
    PublicPostStore,
    PublicProjection,
)
from noyra.service import NoyraService, ServiceSettings

_REAL_CHOICE = secrets.choice


class PublicPostAbuseTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "subject.sqlite3")
        self.subject_id = "Noyra-public-post-test"
        IdentityStore(self.database).ensure(
            self.subject_id, content_hash({"subject": self.subject_id})
        )
        self.store = PublicPostStore(self.database)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def _fixed_choice(sequence: Sequence[object]) -> object:
        # Keep image noise random while making the six-character answer known.
        if isinstance(sequence, str):
            return "A"
        return _REAL_CHOICE(sequence)

    def _challenge(
        self,
        *,
        ip: str = "192.0.2.1",
        ttl_seconds: int = 300,
        max_attempts: int = 5,
        mode: str = "alphanumeric",
        issue_limit_per_hour: int | None = None,
        global_rate_per_minute: int | None = None,
        queue_cap: int | None = None,
        storage_cap_bytes: int | None = None,
    ) -> dict[str, object]:
        with patch("noyra.interaction.posts.secrets.choice", side_effect=self._fixed_choice):
            return self.store.issue_captcha(
                self.subject_id,
                ip,
                ttl_seconds=ttl_seconds,
                max_attempts=max_attempts,
                mode=mode,
                issue_limit_per_hour=issue_limit_per_hour,
                global_rate_per_minute=global_rate_per_minute,
                queue_cap=queue_cap,
                storage_cap_bytes=storage_cap_bytes,
            )

    def _submit(
        self,
        title: str,
        challenge: dict[str, object],
        *,
        ip: str = "192.0.2.1",
    ) -> PublicPostRecord:
        return self.store.create(
            self.subject_id,
            PublicPostInput(kind="post", title=title, content="bounded public content"),
            client_ip=ip,
            captcha_id=str(challenge["challenge_id"]),
            captcha_answer="AAAAAA",
        )

    def _downgrade_to_schema_51(self) -> None:
        """Build an exact schema-51 fixture, including its legacy hash contracts."""
        with self.database.transaction() as connection:
            for trigger in (
                "validate_public_post_moderation_transition",
                "validate_public_post_identity_insert",
                "prevent_public_post_identity_update",
                "prevent_public_post_moderation_update",
            ):
                connection.execute(f"DROP TRIGGER {trigger}")
            for index in (
                "uq_public_post_moderation_revision",
                "uq_public_post_moderation_idempotency",
            ):
                connection.execute(f"DROP INDEX {index}")
            events = connection.execute("SELECT * FROM public_post_moderation_events").fetchall()
            for event in events:
                legacy_state = {
                    key: event[key]
                    for key in (
                        "event_id",
                        "subject_id",
                        "post_id",
                        "from_status",
                        "to_status",
                        "actor",
                        "reason",
                        "created_at",
                    )
                }
                connection.execute(
                    "UPDATE public_post_moderation_events SET state_hash = ? WHERE event_id = ?",
                    (content_hash(legacy_state), event["event_id"]),
                )
            controls = connection.execute("SELECT * FROM public_post_controls").fetchall()
            for control in controls:
                legacy_state = {
                    key: control[key]
                    for key in (
                        "subject_id",
                        "rate_limit_per_hour",
                        "queue_cap",
                        "captcha_ttl_seconds",
                        "captcha_max_attempts",
                        "captcha_mode",
                        "updated_at",
                        "updated_by",
                    )
                }
                connection.execute(
                    "UPDATE public_post_controls SET state_hash = ? WHERE subject_id = ?",
                    (content_hash(legacy_state), control["subject_id"]),
                )
            for column in ("idempotency_key", "previous_event_id", "revision"):
                connection.execute(
                    f"ALTER TABLE public_post_moderation_events DROP COLUMN {column}"
                )
            for column in ("author_provenance", "identity_hash"):
                connection.execute(f"ALTER TABLE public_posts DROP COLUMN {column}")
            for column in (
                "captcha_global_rate_per_minute",
                "captcha_issue_limit_per_hour",
                "storage_cap_bytes",
            ):
                connection.execute(f"ALTER TABLE public_post_controls DROP COLUMN {column}")
            connection.execute("UPDATE schema_meta SET value = '51' WHERE key = 'schema_version'")

    def test_captcha_modes_and_png_do_not_expose_answer(self) -> None:
        for mode in ("letters", "digits", "alphanumeric"):
            challenge = self._challenge(mode=mode)
            image = str(challenge["image"])
            self.assertTrue(image.startswith("data:image/png;base64,"))
            payload = base64.b64decode(image.split(",", 1)[1])
            self.assertTrue(payload.startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertNotIn(b"AAAAAA", payload)

    def test_captcha_breaks_the_legacy_fixed_grid_template(self) -> None:
        glyph = ("01110", "10001", "10001", "11111", "10001", "10001", "10001")
        for sample in range(12):
            image = str(self._challenge(ip=f"192.0.2.{sample + 40}")["image"])
            png = base64.b64decode(image.split(",", 1)[1])
            offset = 8
            compressed = bytearray()
            width = height = 0
            while offset < len(png):
                length = int.from_bytes(png[offset : offset + 4], "big")
                kind = png[offset + 4 : offset + 8]
                data = png[offset + 8 : offset + 8 + length]
                if kind == b"IHDR":
                    width = int.from_bytes(data[:4], "big")
                    height = int.from_bytes(data[4:8], "big")
                elif kind == b"IDAT":
                    compressed.extend(data)
                offset += 12 + length
            raw = zlib.decompress(bytes(compressed))
            stride = 1 + width * 3
            self.assertEqual((width, height), (240, 82))
            best = 0.0
            for index in range(6):
                for y0 in range(13, 21):
                    matches = 0
                    total = 0
                    for row, bits in enumerate(glyph):
                        for column, bit in enumerate(bits):
                            x = 18 + index * 31 + column * 4 + 2
                            y = y0 + row * 4 + 2
                            pixel = raw[y * stride + 1 + x * 3 : y * stride + 1 + x * 3 + 3]
                            dark = sum(pixel) / 3 < 120
                            matches += dark == (bit == "1")
                            total += 1
                    best = max(best, matches / total)
            self.assertLess(best, 0.95)

    def test_captcha_is_one_time_and_failed_attempts_are_durable(self) -> None:
        challenge = self._challenge(max_attempts=2)
        challenge_id = str(challenge["challenge_id"])
        with self.assertRaises(PublicPostCaptchaError):
            self.store.verify_captcha(
                self.subject_id,
                client_ip="192.0.2.1",
                challenge_id=challenge_id,
                answer="WRONG1",
            )
        with self.database.connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT attempts FROM public_post_captcha_challenges WHERE challenge_id = ?",
                    (challenge_id,),
                ).fetchone()["attempts"],
                1,
            )
        self.store.verify_captcha(
            self.subject_id,
            client_ip="192.0.2.1",
            challenge_id=challenge_id,
            answer="AAAAAA",
        )
        with self.assertRaises(PublicPostCaptchaError):
            self.store.verify_captcha(
                self.subject_id,
                client_ip="192.0.2.1",
                challenge_id=challenge_id,
                answer="AAAAAA",
            )

    def test_captcha_binds_ip_and_expiry(self) -> None:
        challenge = self._challenge(ip="192.0.2.2")
        with self.assertRaises(PublicPostCaptchaError):
            self.store.verify_captcha(
                self.subject_id,
                client_ip="192.0.2.3",
                challenge_id=str(challenge["challenge_id"]),
                answer="AAAAAA",
            )
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE public_post_captcha_challenges SET expires_at = 0 WHERE challenge_id = ?",
                (str(challenge["challenge_id"]),),
            )
        with self.assertRaises(PublicPostCaptchaError):
            self.store.verify_captcha(
                self.subject_id,
                client_ip="192.0.2.2",
                challenge_id=str(challenge["challenge_id"]),
                answer="AAAAAA",
            )

    def test_multiple_challenges_do_not_invalidate_an_existing_tab(self) -> None:
        first = self._challenge()
        second = self._challenge()
        self.store.verify_captcha(
            self.subject_id,
            client_ip="192.0.2.1",
            challenge_id=str(first["challenge_id"]),
            answer="AAAAAA",
        )
        self.store.verify_captcha(
            self.subject_id,
            client_ip="192.0.2.1",
            challenge_id=str(second["challenge_id"]),
            answer="AAAAAA",
        )

    def test_client_network_buckets_are_private_and_scoped_ipv6_is_rejected(self) -> None:
        challenge = self._challenge(ip="2001:db8:abcd:42::1")
        with self.database.connection() as connection:
            durable = connection.execute(
                "SELECT client_ip FROM public_post_captcha_challenges WHERE challenge_id = ?",
                (str(challenge["challenge_id"]),),
            ).fetchone()["client_ip"]
        self.assertNotIn("2001:db8", durable)
        self.assertEqual(len(durable), 64)
        # Public IPv6 clients in one /64 share the abuse bucket, preventing
        # trivial address rotation while the unguessable challenge ID remains
        # the possession factor.
        self.store.verify_captcha(
            self.subject_id,
            client_ip="2001:db8:abcd:42::ffff",
            challenge_id=str(challenge["challenge_id"]),
            answer="AAAAAA",
        )
        with self.assertRaises(ValueError):
            self.store.issue_captcha(self.subject_id, "fe80::1%attacker-controlled-scope")

    def test_rate_limit_is_per_ip_and_durable(self) -> None:
        store = PublicPostStore(self.database, rate_limit_per_hour=1)
        first = self._challenge()
        store.create(
            self.subject_id,
            PublicPostInput(kind="post", title="first", content="content"),
            client_ip="192.0.2.1",
            captcha_id=str(first["challenge_id"]),
            captcha_answer="AAAAAA",
        )
        second = self._challenge()
        with self.assertRaises(PublicPostRateLimitError):
            store.create(
                self.subject_id,
                PublicPostInput(kind="post", title="second", content="content"),
                client_ip="192.0.2.1",
                captcha_id=str(second["challenge_id"]),
                captcha_answer="AAAAAA",
            )

    def test_idempotent_retry_does_not_require_or_consume_captcha(self) -> None:
        challenge = self._challenge()
        first = self.store.create(
            self.subject_id,
            PublicPostInput(kind="post", title="same", content="content"),
            idempotency_key="stable-key",
            client_ip="192.0.2.1",
            captcha_id=str(challenge["challenge_id"]),
            captcha_answer="AAAAAA",
        )
        duplicate = self.store.create(
            self.subject_id,
            PublicPostInput(kind="post", title="same", content="content"),
            idempotency_key="stable-key",
            client_ip="192.0.2.1",
        )
        self.assertEqual(duplicate.post_id, first.post_id)
        with self.assertRaises(ValueError):
            self.store.create(
                self.subject_id,
                PublicPostInput(kind="post", title="same", content="content"),
                idempotency_key="stable-key",
            )

    def test_legacy_implicit_idempotency_key_is_still_retryable(self) -> None:
        legacy_key = content_hash({"kind": "post", "title": "legacy", "content": "content"})
        challenge = self._challenge()
        first = self.store.create(
            self.subject_id,
            PublicPostInput(kind="post", title="legacy", content="content"),
            idempotency_key=legacy_key,
            client_ip="192.0.2.1",
            captcha_id=str(challenge["challenge_id"]),
            captcha_answer="AAAAAA",
        )
        retry = self.store.create(
            self.subject_id,
            PublicPostInput(kind="post", title="legacy", content="content"),
            client_ip="192.0.2.1",
        )
        self.assertEqual(retry.post_id, first.post_id)

    def test_expired_challenges_are_garbage_collected_across_ips(self) -> None:
        first = self._challenge(ip="192.0.2.10")
        second = self._challenge(ip="192.0.2.11")
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE public_post_captcha_challenges SET expires_at = 0 WHERE challenge_id = ?",
                (str(first["challenge_id"]),),
            )
        self._challenge(ip="192.0.2.12")
        with self.database.connection() as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM public_post_captcha_challenges WHERE challenge_id = ?",
                    (str(first["challenge_id"]),),
                ).fetchone()
            )
            self.assertIsNotNone(
                connection.execute(
                    "SELECT 1 FROM public_post_captcha_challenges WHERE challenge_id = ?",
                    (str(second["challenge_id"]),),
                ).fetchone()
            )

    def test_stale_rate_evidence_is_pruned_before_capacity_check(self) -> None:
        store = PublicPostStore(self.database, rate_event_cap=1)
        first = self._challenge(ip="192.0.2.20")
        store.create(
            self.subject_id,
            PublicPostInput(kind="post", title="old", content="content"),
            client_ip="192.0.2.20",
            captcha_id=str(first["challenge_id"]),
            captcha_answer="AAAAAA",
        )
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE public_post_rate_events SET occurred_at = 0 WHERE subject_id = ?",
                (self.subject_id,),
            )
        second = self._challenge(ip="192.0.2.21")
        store.create(
            self.subject_id,
            PublicPostInput(kind="post", title="new", content="content"),
            client_ip="192.0.2.21",
            captcha_id=str(second["challenge_id"]),
            captcha_answer="AAAAAA",
        )

    def test_rate_evidence_capacity_fails_closed(self) -> None:
        store = PublicPostStore(self.database, rate_event_cap=1)
        first = self._challenge(ip="192.0.2.30")
        store.create(
            self.subject_id,
            PublicPostInput(kind="post", title="one", content="content"),
            client_ip="192.0.2.30",
            captcha_id=str(first["challenge_id"]),
            captcha_answer="AAAAAA",
        )
        second = self._challenge(ip="192.0.2.31")
        with self.assertRaises(PublicPostCapacityError):
            store.create(
                self.subject_id,
                PublicPostInput(kind="post", title="two", content="content"),
                client_ip="192.0.2.31",
                captcha_id=str(second["challenge_id"]),
                captcha_answer="AAAAAA",
            )

    def test_pending_review_queue_has_backpressure_without_invalidating_other_challenges(
        self,
    ) -> None:
        store = PublicPostStore(self.database, queue_cap=1)
        first = self._challenge()
        store.create(
            self.subject_id,
            PublicPostInput(kind="post", title="first", content="bounded public content"),
            client_ip="192.0.2.1",
            captcha_id=str(first["challenge_id"]),
            captcha_answer="AAAAAA",
        )
        second = self._challenge()
        with self.assertRaises(PublicPostQueueFullError):
            store.create(
                self.subject_id,
                PublicPostInput(kind="post", title="second", content="bounded public content"),
                client_ip="192.0.2.1",
                captcha_id=str(second["challenge_id"]),
                captcha_answer="AAAAAA",
            )
        old = self._challenge()
        newer = self._challenge()
        with self.database.connection() as connection:
            self.assertIsNotNone(
                connection.execute(
                    "SELECT 1 FROM public_post_captcha_challenges WHERE challenge_id = ?",
                    (str(old["challenge_id"]),),
                ).fetchone()
            )
            self.assertIsNotNone(
                connection.execute(
                    "SELECT 1 FROM public_post_captcha_challenges WHERE challenge_id = ?",
                    (str(newer["challenge_id"]),),
                ).fetchone()
            )

    def test_control_overrides_fail_closed_when_state_hash_is_tampered(self) -> None:
        self.store.configure_controls(
            self.subject_id,
            rate_limit_per_hour=10,
            queue_cap=100,
            captcha_ttl_seconds=300,
            captcha_max_attempts=5,
            captcha_mode="alphanumeric",
            actor="operator",
        )
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE public_post_controls SET queue_cap = 1 WHERE subject_id = ?",
                (self.subject_id,),
            )
        with self.assertRaises(IntegrityError):
            self.store.controls(
                self.subject_id,
                defaults={
                    "rate_limit_per_hour": 10,
                    "queue_cap": 100,
                    "captcha_ttl_seconds": 300,
                    "captcha_max_attempts": 5,
                    "captcha_mode": "alphanumeric",
                },
            )

    def test_content_hash_mismatch_is_an_integrity_failure(self) -> None:
        challenge = self._challenge()
        record = self._submit("tamper target", challenge)
        with self.database.transaction() as connection:
            connection.execute("DROP TRIGGER prevent_public_post_identity_update")
            connection.execute(
                "UPDATE public_posts SET content = ? WHERE post_id = ?",
                ("tampered", record.post_id),
            )
        with self.assertRaises(IntegrityError):
            self.store.get(record.post_id, subject_id=self.subject_id)

    def test_reopen_does_not_launder_missing_moderation_history(self) -> None:
        record = self._submit("missing history", self._challenge())
        self.store.moderate(
            record.post_id,
            subject_id=self.subject_id,
            status="published",
            actor="operator",
            reason="approved",
        )
        with self.database.transaction() as connection:
            connection.execute("DROP TRIGGER prevent_public_post_moderation_delete")
            connection.execute(
                "DELETE FROM public_post_moderation_events WHERE post_id = ?",
                (record.post_id,),
            )
        reopened = Database(self.database.path)
        with reopened.connection() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM public_post_moderation_events WHERE post_id = ?",
                (record.post_id,),
            ).fetchone()[0]
        self.assertEqual(count, 0)
        with self.assertRaises(IntegrityError):
            InteractionIntegrity(reopened).verify(self.subject_id)

    def test_schema_47_backfills_moderation_once_and_distinguishes_rejection(self) -> None:
        now = "2026-08-22T00:00:00.000+00:00"
        with self.database.transaction() as connection:
            for trigger in (
                "validate_public_post_moderation_transition",
                "validate_public_post_identity_insert",
                "prevent_public_post_identity_update",
                "prevent_public_post_delete",
                "validate_public_post_status_update",
                "validate_public_post_published_at",
                "prevent_public_post_moderation_update",
                "prevent_public_post_moderation_delete",
                "validate_public_post_moderation_subject",
            ):
                connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            for index in (
                "uq_public_post_moderation_revision",
                "uq_public_post_moderation_idempotency",
                "idx_public_post_rate_events_subject_time",
            ):
                connection.execute(f"DROP INDEX IF EXISTS {index}")
            for column in ("idempotency_key", "previous_event_id", "revision"):
                connection.execute(
                    f"ALTER TABLE public_post_moderation_events DROP COLUMN {column}"
                )
            connection.execute("DROP TABLE public_post_moderation_events")
            for column in (
                "captcha_global_rate_per_minute",
                "captcha_issue_limit_per_hour",
                "storage_cap_bytes",
            ):
                connection.execute(f"ALTER TABLE public_post_controls DROP COLUMN {column}")
            connection.execute("DROP TABLE public_post_controls")
            connection.execute("DROP TABLE public_post_captcha_issue_events")
            for column in ("author_provenance", "identity_hash"):
                connection.execute(f"ALTER TABLE public_posts DROP COLUMN {column}")

            connection.execute("DROP TRIGGER prevent_interaction_inbound_identity_update")
            connection.execute("DROP INDEX idx_interaction_inbound_interaction")
            for column in (
                "reply_context_version",
                "reply_selector",
                "external_thread_id",
            ):
                connection.execute(f"ALTER TABLE interaction_inbound_events DROP COLUMN {column}")
            connection.execute("ALTER TABLE interaction_transports DROP COLUMN endpoint_digest")
            connection.execute("ALTER TABLE interaction_transports DROP COLUMN endpoint_contract")

            for post_id, status, published_at in (
                ("legacy-published", "published", now),
                ("legacy-rejected-archive", "archived", None),
            ):
                connection.execute(
                    """INSERT INTO public_posts(
                        post_id, subject_id, kind, title, content, content_hash,
                        author_label, status, idempotency_key, created_at, updated_at, published_at
                    ) VALUES (?, ?, 'post', ?, 'legacy body', ?, 'legacy visitor',
                              ?, ?, ?, ?, ?)""",
                    (
                        post_id,
                        self.subject_id,
                        post_id,
                        content_hash("legacy body"),
                        status,
                        post_id,
                        now,
                        now,
                        published_at,
                    ),
                )
            connection.execute("UPDATE schema_meta SET value = '47' WHERE key = 'schema_version'")

        upgraded = Database(self.database.path)
        with upgraded.connection() as connection:
            history = connection.execute(
                "SELECT post_id, revision, from_status, to_status "
                "FROM public_post_moderation_events ORDER BY post_id, revision"
            ).fetchall()
        self.assertEqual(
            [tuple(row) for row in history],
            [
                ("legacy-published", 1, "pending_review", "published"),
                ("legacy-rejected-archive", 1, "pending_review", "rejected"),
                ("legacy-rejected-archive", 2, "rejected", "archived"),
            ],
        )
        self.assertEqual(InteractionIntegrity(upgraded).verify(self.subject_id)["public_posts"], 2)
        reopened = Database(self.database.path)
        with reopened.connection() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM public_post_moderation_events").fetchone()[
                    0
                ],
                3,
            )

    def test_moderation_revision_orders_same_millisecond_actions(self) -> None:
        record = self._submit("same millisecond", self._challenge())
        with patch("noyra.interaction.posts.utc_now", return_value="2026-08-22T00:00:00.000+00:00"):
            self.store.moderate(
                record.post_id,
                subject_id=self.subject_id,
                status="published",
                actor="operator",
                reason="approved",
            )
            self.store.moderate(
                record.post_id,
                subject_id=self.subject_id,
                status="archived",
                actor="operator",
                reason="retired",
            )
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT revision, previous_event_id, event_id "
                "FROM public_post_moderation_events WHERE post_id = ? ORDER BY revision",
                (record.post_id,),
            ).fetchall()
        self.assertEqual([row["revision"] for row in rows], [1, 2])
        self.assertIsNone(rows[0]["previous_event_id"])
        self.assertEqual(rows[1]["previous_event_id"], rows[0]["event_id"])
        self.assertEqual(
            InteractionIntegrity(self.database).verify(self.subject_id)["public_posts"], 1
        )

    def test_schema_51_migrates_existing_moderation_chain_and_identity(self) -> None:
        record = self._submit("schema migration", self._challenge())
        self.store.moderate(
            record.post_id,
            subject_id=self.subject_id,
            status="rejected",
            actor="operator",
            reason="not suitable",
        )
        self.store.moderate(
            record.post_id,
            subject_id=self.subject_id,
            status="archived",
            actor="operator",
            reason="retention complete",
        )
        self.store.configure_controls(
            self.subject_id,
            rate_limit_per_hour=10,
            queue_cap=100,
            captcha_ttl_seconds=300,
            captcha_max_attempts=5,
            captcha_mode="alphanumeric",
            storage_cap_bytes=20_000_000,
            captcha_issue_limit_per_hour=30,
            captcha_global_rate_per_minute=300,
            actor="operator",
        )
        self._downgrade_to_schema_51()

        upgraded = Database(self.database.path)
        with upgraded.connection() as connection:
            post = connection.execute(
                "SELECT * FROM public_posts WHERE post_id = ?", (record.post_id,)
            ).fetchone()
            history = connection.execute(
                "SELECT revision, from_status, to_status, previous_event_id, event_id "
                "FROM public_post_moderation_events WHERE post_id = ? ORDER BY revision",
                (record.post_id,),
            ).fetchall()
        self.assertEqual(post["author_provenance"], "visitor")
        self.assertEqual(public_post_identity_hash(post), post["identity_hash"])
        self.assertEqual(
            [(row["revision"], row["from_status"], row["to_status"]) for row in history],
            [(1, "pending_review", "rejected"), (2, "rejected", "archived")],
        )
        self.assertEqual(history[1]["previous_event_id"], history[0]["event_id"])
        self.assertEqual(InteractionIntegrity(upgraded).verify(self.subject_id)["public_posts"], 1)

    def test_schema_51_migration_rejects_tampered_moderation_hash(self) -> None:
        record = self._submit("tampered legacy moderation", self._challenge())
        self.store.moderate(
            record.post_id,
            subject_id=self.subject_id,
            status="rejected",
            actor="operator",
            reason="legacy decision",
        )
        self._downgrade_to_schema_51()
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE public_post_moderation_events SET state_hash = 'tampered' "
                "WHERE post_id = ?",
                (record.post_id,),
            )
        with self.assertRaisesRegex(RuntimeError, "moderation integrity mismatch"):
            Database(self.database.path)

    def test_schema_51_migration_rejects_tampered_controls_hash(self) -> None:
        self.store.configure_controls(
            self.subject_id,
            rate_limit_per_hour=10,
            queue_cap=100,
            captcha_ttl_seconds=300,
            captcha_max_attempts=5,
            captcha_mode="alphanumeric",
            actor="operator",
        )
        self._downgrade_to_schema_51()
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE public_post_controls SET state_hash = 'tampered' WHERE subject_id = ?",
                (self.subject_id,),
            )
        with self.assertRaisesRegex(RuntimeError, "controls integrity mismatch"):
            Database(self.database.path)

    def test_moderation_is_idempotent_and_checks_expected_status(self) -> None:
        record = self._submit("idempotent moderation", self._challenge())
        first = self.store.moderate(
            record.post_id,
            subject_id=self.subject_id,
            status="published",
            actor="operator",
            reason="approved",
            expected_status="pending_review",
            idempotency_key="moderation-operation-1",
        )
        replay = self.store.moderate(
            record.post_id,
            subject_id=self.subject_id,
            status="published",
            actor="operator",
            reason="approved",
            expected_status="pending_review",
            idempotency_key="moderation-operation-1",
        )
        self.assertEqual((first.post_id, replay.status), (record.post_id, "published"))
        with self.assertRaises(PublicPostConflictError):
            self.store.moderate(
                record.post_id,
                subject_id=self.subject_id,
                status="published",
                actor="operator",
                reason="approved",
                expected_status="published",
                idempotency_key="moderation-operation-1",
            )
        with self.assertRaises(PublicPostConflictError):
            self.store.moderate(
                record.post_id,
                subject_id=self.subject_id,
                status="archived",
                actor="operator",
                reason="different operation",
                expected_status="published",
                idempotency_key="moderation-operation-1",
            )
        with self.assertRaises(PublicPostConflictError):
            self.store.moderate(
                record.post_id,
                subject_id=self.subject_id,
                status="archived",
                actor="operator",
                reason="archive",
                expected_status="rejected",
                idempotency_key="moderation-operation-2",
            )

    def test_admin_status_filter_and_cursor_reach_the_next_page(self) -> None:
        records = []
        for index in range(3):
            client_ip = f"192.0.2.{index + 80}"
            records.append(
                self._submit(f"page {index}", self._challenge(ip=client_ip), ip=client_ip)
            )
        first_page = self.store.admin(self.subject_id, limit=2, status="pending_review")
        self.assertEqual(len(first_page), 2)
        cursor = f"{first_page[-1].created_at}|{first_page[-1].post_id}"
        second_page = self.store.admin(
            self.subject_id,
            limit=2,
            status="pending_review",
            cursor=cursor,
        )
        self.assertEqual(len(second_page), 1)
        self.assertEqual(
            {record.post_id for record in first_page + second_page},
            {record.post_id for record in records},
        )

    def test_moderation_history_trigger_requires_the_previous_transition(self) -> None:
        challenge = self._challenge()
        record = self._submit("moderation chain", challenge)
        published = self.store.moderate(
            record.post_id,
            subject_id=self.subject_id,
            status="published",
            actor="operator",
            reason="approved",
        )
        self.assertEqual(published.status, "published")
        public_row = PublicProjection(self.database).public_posts_view(self.subject_id)[0]
        self.assertEqual(public_row["author_provenance"], "visitor")
        with self.database.transaction() as connection:
            event = connection.execute(
                "SELECT * FROM public_post_moderation_events WHERE post_id = ?",
                (record.post_id,),
            ).fetchone()
            self.assertIsNotNone(event)
            assert event is not None
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO public_post_moderation_events(
                        event_id, subject_id, post_id, from_status, to_status,
                        actor, reason, state_hash, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        "post-moderation-duplicate",
                        self.subject_id,
                        record.post_id,
                        "pending_review",
                        "published",
                        "operator",
                        "forged duplicate",
                        content_hash({"forged": True}),
                        event["created_at"],
                    ),
                )


def test_public_post_service_queue_cap_matches_durable_post_cap(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        ServiceSettings(
            data_dir=tmp_path / "data",
            subject_id="Noyra-public-queue-cap-test",
            genesis_hash=content_hash({"public": "queue-cap"}),
            public_post_queue_cap=100_001,
        )


def test_public_post_captcha_http_endpoint_returns_a_challenge(tmp_path: Path) -> None:
    service = NoyraService(
        ServiceSettings(
            data_dir=tmp_path / "data",
            subject_id="Noyra-public-http-test",
            genesis_hash=content_hash({"public": "http"}),
            host="127.0.0.1",
            port=0,
            integrity_mode="off",
        )
    )
    service.boot()
    service.http.start()
    try:
        _, port = service.http.address
        request = Request(
            f"http://127.0.0.1:{port}/api/public-posts/captcha",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=5) as response:
            payload = json.loads(response.read())
        assert response.status == 200
        assert payload["challenge_id"]
        assert payload["image"].startswith("data:image/png;base64,")
        cross_site = Request(
            f"http://127.0.0.1:{port}/api/public-posts/captcha",
            data=b"{}",
            headers={
                "Content-Type": "application/json",
                "Origin": "https://attacker.example",
                "Sec-Fetch-Site": "cross-site",
            },
            method="POST",
        )
        with pytest.raises(HTTPError) as rejected:
            urlopen(cross_site, timeout=5)
        assert rejected.value.code == 403
    finally:
        service.http.close()
        service.kernel.close()


if __name__ == "__main__":
    unittest.main()
