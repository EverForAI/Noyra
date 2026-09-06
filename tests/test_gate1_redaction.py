from __future__ import annotations

import json
import zipfile
from io import BytesIO
from pathlib import Path

from noyra.core import Database, IdentityStore
from noyra.core.events import EventStore
from noyra.core.redaction import redact_payload, redact_secret_text, redact_secrets
from noyra.core.runtime_export import RuntimeLogExporter
from noyra.core.types import content_hash, utc_now
from noyra.interaction.projection import PublicProjection


def test_shared_redaction_covers_nested_credentials_and_preserves_metrics() -> None:
    private_key = (
        "-----BEGIN PRIVATE KEY-----\n"
        "private-key-material-that-must-not-leak\n"
        "-----END PRIVATE KEY-----"
    )
    payload = {
        "token": "generic-token-that-must-not-leak",
        "sessionToken": "camel-session-token-that-must-not-leak",
        "privateKey": "camel-private-key-that-must-not-leak",
        "provider_api_key": "provider-key-that-must-not-leak",
        "nested": [
            {
                "session_token": "session-token-that-must-not-leak",
                "refresh-token": "refresh-token-that-must-not-leak",
                "http_set-cookie": "sid=cookie-that-must-not-leak",
                "signing_private_key": private_key,
            }
        ],
        "key_fingerprint": "sha256:key-fingerprint-is-metadata",
        "encryption_key_fingerprint": "sha256:encryption-fingerprint-is-metadata",
        "token_count": 17,
        "max_output_tokens": 128,
        "session_count": 3,
    }

    cleaned = redact_secrets(payload)

    assert cleaned["token"] == "[REDACTED]"
    assert cleaned["sessionToken"] == "[REDACTED]"
    assert cleaned["privateKey"] == "[REDACTED]"
    assert cleaned["provider_api_key"] == "[REDACTED]"
    assert cleaned["nested"] == [
        {
            "session_token": "[REDACTED]",
            "refresh-token": "[REDACTED]",
            "http_set-cookie": "[REDACTED]",
            "signing_private_key": "[REDACTED]",
        }
    ]
    assert cleaned["key_fingerprint"] == payload["key_fingerprint"]
    assert cleaned["encryption_key_fingerprint"] == payload["encryption_key_fingerprint"]
    assert cleaned["token_count"] == 17
    assert cleaned["max_output_tokens"] == 128
    assert cleaned["session_count"] == 3

    training_cleaned = redact_payload(payload)
    assert training_cleaned["token"] == "[REDACTED]"
    assert training_cleaned["key_fingerprint"] == payload["key_fingerprint"]
    assert training_cleaned["max_output_tokens"] == 128


def test_shared_redaction_covers_json_headers_assignments_and_private_keys() -> None:
    secrets = {
        "authorization": "bearer-header-secret-123456",
        "cookie": "cookie-header-secret-123456",
        "set_cookie": "set-cookie-header-secret-123456",
        "api_key": "header-api-secret-123456",
        "query": "query-token-secret-123456",
        "private_key": "private-key-secret-123456",
    }
    text = "\n".join(
        (
            f"Authorization: Bearer {secrets['authorization']}",
            f"Cookie: sid={secrets['cookie']}; theme=dark",
            f"Set-Cookie: refresh={secrets['set_cookie']}; Secure; HttpOnly",
            f"X-API-Key: {secrets['api_key']}",
            f"https://example.test/callback?token={secrets['query']}&state=visible-state",
            "-----BEGIN RSA PRIVATE KEY-----",
            secrets["private_key"],
            "-----END RSA PRIVATE KEY-----",
        )
    )

    cleaned = redact_secret_text(text)

    assert all(secret not in cleaned for secret in secrets.values())
    assert "state=visible-state" in cleaned
    assert cleaned.count("[REDACTED]") >= 6

    json_text = json.dumps(
        {
            "session_token": "json-session-secret-123456",
            "nested": [{"cookie": "json-cookie-secret-123456"}],
            "key_fingerprint": "sha256:visible-fingerprint",
            "token_count": 9,
        }
    )
    cleaned_json = json.loads(redact_secret_text(json_text))
    assert cleaned_json == {
        "key_fingerprint": "sha256:visible-fingerprint",
        "nested": [{"cookie": "[REDACTED]"}],
        "session_token": "[REDACTED]",
        "token_count": 9,
    }


def test_runtime_export_and_runtime_log_projection_share_secret_redaction(tmp_path: Path) -> None:
    database = Database(tmp_path / "runtime-redaction.sqlite3")
    subject_id = "Noyra-gate1-redaction"
    IdentityStore(database).ensure(subject_id, content_hash({"subject": subject_id}))
    secrets = {
        "token": "runtime-generic-token-secret-123456",
        "session_token": "runtime-session-token-secret-123456",
        "cookie": "runtime-cookie-secret-123456",
        "private_key": "runtime-private-key-secret-123456",
        "secret_reference": "runtime-secret-reference-that-must-not-leak",
        "json_token": "runtime-json-token-secret-123456",
        "log_token": "runtime-log-token-secret-123456",
    }
    event_id = (
        EventStore(database)
        .append(
            subject_id,
            "gate1-redaction",
            "test",
            {
                "token": secrets["token"],
                "nested": {
                    "session_token": secrets["session_token"],
                    "cookie": secrets["cookie"],
                    "private_key": secrets["private_key"],
                    "secret_reference": secrets["secret_reference"],
                },
                "metadata_json": json.dumps(
                    {
                        "refresh_token": secrets["json_token"],
                        "key_fingerprint": "sha256:visible-runtime-fingerprint",
                    }
                ),
                "key_fingerprint": "sha256:visible-runtime-fingerprint",
                "token_count": 11,
            },
        )
        .event_id
    )
    with database.transaction() as connection:
        connection.execute(
            """INSERT INTO audit_records(
                audit_id, subject_id, action, actor, payload_json, occurred_at
            ) VALUES ('audit-gate1-redaction', ?, 'redaction_test', ?, '{}', ?)""",
            (
                subject_id,
                f"Cookie: sid={secrets['log_token']}; Secure",
                utc_now(),
            ),
        )

    artifact = RuntimeLogExporter(database).export(subject_id, actor="test")
    with zipfile.ZipFile(BytesIO(artifact.content)) as archive:
        decompressed_export = b"\n".join(archive.read(name) for name in archive.namelist())
        event_rows = [
            json.loads(line)
            for line in archive.read("tables/events.jsonl").decode("utf-8").splitlines()
        ]
    event_row = next(row for row in event_rows if row["event_id"] == event_id)
    exported_payload = json.loads(event_row["payload_json"])
    assert exported_payload["token"] == "[REDACTED]"
    assert exported_payload["nested"] == {
        "cookie": "[REDACTED]",
        "private_key": "[REDACTED]",
        "secret_reference": "[REDACTED]",
        "session_token": "[REDACTED]",
    }
    assert json.loads(exported_payload["metadata_json"]) == {
        "key_fingerprint": "sha256:visible-runtime-fingerprint",
        "refresh_token": "[REDACTED]",
    }
    assert exported_payload["key_fingerprint"] == "sha256:visible-runtime-fingerprint"
    assert exported_payload["token_count"] == 11
    assert all(secret.encode() not in decompressed_export for secret in secrets.values())

    page = PublicProjection(database).runtime_logs(subject_id, limit=1_000)
    audit_row = next(item for item in page["items"] if item["record_id"] == "audit-gate1-redaction")
    assert audit_row["summary"] == "Cookie: [REDACTED]"
    assert secrets["log_token"] not in json.dumps(page)
