from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from noyra.core import Database, IdentityStore, LongRunResilience


class LongRunResilienceTestCase(unittest.TestCase):
    def test_audit_and_recovery_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "noyra.sqlite3")
            subject_id = "Noyra-resilience-test"
            IdentityStore(database).ensure(subject_id, "a" * 64)
            harness = LongRunResilience(database, subject_id, directory)
            attempts = 0

            async def operation() -> str:
                nonlocal attempts
                attempts += 1
                if attempts < 2:
                    raise RuntimeError("temporary")
                return "ok"

            self.assertEqual(asyncio.run(harness.run_with_recovery(operation)), "ok")
            report = harness.audit()
            self.assertEqual(report.p0, ())
            self.assertEqual(report.checks["sqlite_integrity"], "ok")
            self.assertEqual(report.checks["identity_continuity"], "ok")
            self.assertTrue(harness.write_report(report).exists())

    def test_tampered_event_is_a_p0_finding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "noyra.sqlite3")
            subject_id = "Noyra-resilience-tamper"
            IdentityStore(database).ensure(subject_id, "b" * 64)
            harness = LongRunResilience(database, subject_id, directory)
            event = harness.events.append(subject_id, "experience", "test", {"value": 1})
            with database.transaction() as connection:
                connection.execute("DROP TRIGGER prevent_event_immutable_update")
                connection.execute(
                    "UPDATE events SET payload_json = '{\"value\":2}' WHERE event_id = ?",
                    (event.event_id,),
                )
            report = harness.audit()
            self.assertIn(f"event_hash:{event.event_id}", report.p0)
