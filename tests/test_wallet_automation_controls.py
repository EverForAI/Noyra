from pathlib import Path

from noyra.core import Database, IdentityStore
from noyra.core.types import content_hash
from noyra.wallet import PaymentPolicyInput, WalletEconomyStore
from noyra.wallet.execution import WALLET_PAYMENT_REASON_CODES, WalletPaymentExecutionEngine


def _db(tmp_path: Path):
    db = Database(tmp_path / "noyra.sqlite3")
    subject = "Noyra-automation-test"
    IdentityStore(db).ensure(subject, content_hash({"subject": subject}))
    return db, subject


def test_new_policy_defaults_automation_disabled(tmp_path: Path):
    db, subject = _db(tmp_path)
    policy = WalletEconomyStore(db).get_policy(subject)
    assert policy.automation_enabled is False


def test_policy_input_carries_automation_toggle():
    assert PaymentPolicyInput(mode="automatic", automation_enabled=True).automation_enabled is True
    assert PaymentPolicyInput(mode="automatic").automation_enabled is None


def test_reason_codes_are_bounded_and_unknown_is_safe():
    assert "insufficient_balance" in WALLET_PAYMENT_REASON_CODES
    assert "chain_reorg" in WALLET_PAYMENT_REASON_CODES
    assert WalletPaymentExecutionEngine.normalize_reason_code("not-a-code") == "unknown"
    assert WalletPaymentExecutionEngine.normalize_reason_code("gas_too_high") == "gas_too_high"
