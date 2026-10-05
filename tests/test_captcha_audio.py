from __future__ import annotations

import base64
import io
import json
import time
import wave
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from noyra.core import Database, IdentityStore
from noyra.core.types import content_hash
from noyra.interaction import PublicPostCaptchaError, PublicPostRateLimitError, PublicPostStore
from noyra.interaction.captcha_audio import audio_challenge
from noyra.service import NoyraService, ServiceSettings


def test_audio_is_bounded_playable_wave_without_answer_metadata() -> None:
    first = audio_challenge("234567")
    second = audio_challenge("234567")
    assert first != second
    raw = base64.b64decode(first.split(",", 1)[1])
    assert len(raw) < 500_000
    with wave.open(io.BytesIO(raw)) as stream:
        assert (stream.getnchannels(), stream.getsampwidth(), stream.getframerate()) == (
            1,
            2,
            16000,
        )
        assert 6 < stream.getnframes() / 16000 < 16
    assert b"234567" not in raw


def test_audio_uses_same_ip_attempt_expiry_and_one_time_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(database).ensure("Noyra-audio", "a" * 64)
    store = PublicPostStore(database)
    monkeypatch.setattr("noyra.interaction.posts.secrets.choice", lambda _: "2")
    challenge = store.issue_captcha(
        "Noyra-audio", "192.0.2.1", presentation="audio", max_attempts=2
    )
    assert "image" not in challenge and "answer" not in challenge
    identifier = challenge["challenge_id"]
    with pytest.raises(PublicPostCaptchaError):
        store.verify_captcha(
            "Noyra-audio", client_ip="192.0.2.2", challenge_id=identifier, answer="222222"
        )
    with pytest.raises(PublicPostCaptchaError):
        store.verify_captcha(
            "Noyra-audio", client_ip="192.0.2.1", challenge_id=identifier, answer="wrong"
        )
    store.verify_captcha(
        "Noyra-audio", client_ip="192.0.2.1", challenge_id=identifier, answer="222222"
    )
    with pytest.raises(PublicPostCaptchaError):
        store.verify_captcha(
            "Noyra-audio", client_ip="192.0.2.1", challenge_id=identifier, answer="222222"
        )
    second = store.issue_captcha("Noyra-audio", "192.0.2.1", presentation="audio", max_attempts=1)
    with pytest.raises(PublicPostCaptchaError):
        store.verify_captcha(
            "Noyra-audio",
            client_ip="192.0.2.1",
            challenge_id=second["challenge_id"],
            answer="wrong",
        )
    with pytest.raises(PublicPostCaptchaError):
        store.verify_captcha(
            "Noyra-audio",
            client_ip="192.0.2.1",
            challenge_id=second["challenge_id"],
            answer="222222",
        )
    third = store.issue_captcha("Noyra-audio", "192.0.2.1", presentation="audio", ttl_seconds=30)
    expired_now = time.time() + 60
    monkeypatch.setattr("noyra.interaction.posts.time.time", lambda: expired_now)
    with pytest.raises(PublicPostCaptchaError):
        store.verify_captcha(
            "Noyra-audio",
            client_ip="192.0.2.1",
            challenge_id=third["challenge_id"],
            answer="222222",
        )


def test_audio_and_image_share_issue_rate_and_failed_audio_is_not_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(database).ensure("Noyra-audio", "a" * 64)
    store = PublicPostStore(database, captcha_issue_limit_per_hour=1)
    store.issue_captcha("Noyra-audio", "192.0.2.1", presentation="image")
    with pytest.raises(PublicPostRateLimitError):
        store.issue_captcha("Noyra-audio", "192.0.2.1", presentation="audio")

    failed_store = PublicPostStore(database, captcha_issue_limit_per_hour=30)

    def failed_asset(_: str) -> str:
        raise ValueError("asset unavailable")

    monkeypatch.setattr("noyra.interaction.posts.audio_challenge", failed_asset)
    with pytest.raises(ValueError):
        failed_store.issue_captcha("Noyra-audio", "192.0.2.2", presentation="audio")
    with database.connection() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM public_post_captcha_challenges").fetchone()[0]
            == 1
        )


def test_public_audio_http_and_security_headers(tmp_path: Path) -> None:
    service = NoyraService(
        ServiceSettings(
            data_dir=tmp_path,
            subject_id="Noyra-audio",
            genesis_hash=content_hash({"audio": "test"}),
            port=0,
            integrity_mode="off",
        )
    )
    service.boot()
    service.http.start()
    try:
        url = f"http://127.0.0.1:{service.http.address[1]}/api/public-posts/captcha"

        def post(payload: dict[str, Any]) -> Any:
            return urlopen(
                Request(
                    url,
                    data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json"},
                ),
                timeout=10,
            )

        with post({"presentation": "audio"}) as response:
            payload = json.load(response)
            assert payload["audio"].startswith("data:audio/wav;base64,")
            assert "media-src 'self' data:" in response.headers["Content-Security-Policy"]
            assert response.headers["Cache-Control"] == "no-store"
        with pytest.raises(HTTPError) as rejected:
            post({"presentation": []})
        assert rejected.value.code == 400
    finally:
        service.http.close()
        service.kernel.close()
