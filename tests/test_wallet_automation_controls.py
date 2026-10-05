import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from pydantic import SecretStr

from noyra.core import Database, IdentityStore, SubjectKernel
from noyra.core.types import content_hash
from noyra.service import NoyraHTTPServer, ServiceSettings
from noyra.wallet import PaymentPolicyInput, WalletEconomyStore
from noyra.wallet.execution import WALLET_PAYMENT_REASON_CODES, WalletPaymentExecutionEngine


def _db(tmp_path: Path) -> Any:
    db = Database(tmp_path / "noyra.sqlite3")
    subject = "Noyra-automation-test"
    IdentityStore(db).ensure(subject, content_hash({"subject": subject}))
    return db, subject


def test_new_policy_defaults_automation_disabled(tmp_path: Path) -> None:
    db, subject = _db(tmp_path)
    policy = WalletEconomyStore(db).get_policy(subject)
    assert policy.automation_enabled is False


def test_policy_input_carries_automation_toggle() -> None:
    assert PaymentPolicyInput(mode="automatic", automation_enabled=True).automation_enabled is True
    assert PaymentPolicyInput(mode="automatic").automation_enabled is None


def test_reason_codes_are_bounded_and_unknown_is_safe() -> None:
    assert "insufficient_balance" in WALLET_PAYMENT_REASON_CODES
    assert "chain_reorg" in WALLET_PAYMENT_REASON_CODES
    assert WalletPaymentExecutionEngine.normalize_reason_code("not-a-code") == "unknown"
    assert WalletPaymentExecutionEngine.normalize_reason_code("gas_too_high") == "gas_too_high"


@pytest.fixture
def control_server(tmp_path: Path) -> Iterator[NoyraHTTPServer]:
    settings = ServiceSettings(
        data_dir=tmp_path,
        subject_id="Noyra-wallet-controls",
        genesis_hash=content_hash({"seed": "wallet-controls"}),
        host="127.0.0.1",
        port=0,
        admin_token=SecretStr("test-admin-token-with-sufficient-entropy"),
    )
    kernel = SubjectKernel(tmp_path / "noyra.sqlite3", settings.subject_id, settings.genesis_hash)
    kernel.boot()
    kernel.orient()
    kernel.activate()
    server = NoyraHTTPServer(kernel, settings)
    server.start()
    try:
        yield server
    finally:
        server.close()
        kernel.close()


def _post(server: NoyraHTTPServer, suffix: str, payload: dict[str, Any]) -> int:
    request = Request(
        f"http://127.0.0.1:{server.address[1]}/api/v1/admin/wallet-automation{suffix}",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": "Bearer test-admin-token-with-sufficient-entropy",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=10) as response:
            return int(response.status)
    except HTTPError as error:
        return error.code


def test_http_pause_and_resume_preserve_policy(control_server: NoyraHTTPServer) -> None:
    store = control_server.wallet_economy
    subject = control_server.kernel.subject_id
    before = store.update_policy(
        subject,
        PaymentPolicyInput(
            mode="automatic", automation_enabled=True, per_order_limit="50", daily_limit="100"
        ),
        expected_version=1,
        actor="operator",
    )
    for suffix, paused in (("/pause", True), ("/resume", False)):
        current = store.get_policy(subject)
        payload = {
            "expected_version": current.policy_version,
            "reason": "test",
            "idempotency_key": suffix,
        }
        assert _post(control_server, suffix, payload) == 200
        updated = store.get_policy(subject)
        assert updated.emergency_paused is paused
        assert updated.policy_version == current.policy_version + 1
        for field, value in asdict(before).items():
            if field not in {"emergency_paused", "policy_version", "updated_at"}:
                assert asdict(updated)[field] == value
        assert _post(control_server, suffix, payload) == 200
        assert store.get_policy(subject) == updated


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None, [], {}])
@pytest.mark.parametrize("field", ["automation_enabled", "emergency_paused"])
def test_http_rejects_nonboolean_controls(
    control_server: NoyraHTTPServer, field: str, value: Any
) -> None:
    store = control_server.wallet_economy
    subject = control_server.kernel.subject_id
    before = store.get_policy(subject)
    assert (
        _post(control_server, "", {"expected_version": 1, "idempotency_key": "bad", field: value})
        == 400
    )
    assert store.get_policy(subject) == before


def test_http_audit_failure_rolls_back_policy(
    control_server: NoyraHTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = control_server.wallet_economy
    subject = control_server.kernel.subject_id
    before = store.get_policy(subject)
    original = store._audit

    def fail_control_audit(*args: Any, **kwargs: Any) -> str:
        if args[2] == "wallet_automation_updated":
            raise RuntimeError("injected audit write failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "_audit", fail_control_audit)
    payload = {"expected_version": 1, "idempotency_key": "atomic", "automation_enabled": True}
    assert _post(control_server, "", payload) == 400
    assert store.get_policy(subject) == before
    with store.database.connection() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM audit_records WHERE action IN "
                "('wallet_automation_updated', 'wallet_payment_policy_updated')"
            ).fetchone()[0]
            == 0
        )
    monkeypatch.setattr(store, "_audit", original)
    assert _post(control_server, "", payload) == 200
    assert _post(control_server, "", payload) == 200
    assert _post(control_server, "", {**payload, "automation_enabled": False}) == 409
    assert store.get_policy(subject).policy_version == 2


def test_concurrent_http_controls_have_single_winner(control_server: NoyraHTTPServer) -> None:
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda key: _post(
                    control_server, "/pause", {"expected_version": 1, "idempotency_key": key}
                ),
                ("concurrent-1", "concurrent-2"),
            )
        )
    assert sorted(results) == [200, 409]
    assert (
        control_server.wallet_economy.get_policy(control_server.kernel.subject_id).policy_version
        == 2
    )
