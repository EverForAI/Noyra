# ruff: noqa: E501
from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import nullcontext
from datetime import UTC, datetime
from typing import Any

from noyra.core.database import (
    Database,
    wallet_payment_policy_state_hash,
    wallet_submission_state_hash,
)
from noyra.core.errors import IntegrityError, InvalidTransitionError, NotFoundError
from noyra.core.identity import validate_subject_id
from noyra.core.types import (
    canonical_json,
    content_hash,
    new_id,
    strict_int,
    strict_json_loads,
    utc_now,
)
from noyra.core.wallet_schema import wallet_entry_hash, wallet_journal_hash

from .economy_types import (
    BountyInput,
    BountyRecord,
    LedgerBalance,
    PaymentOrderRecord,
    PaymentPolicyInput,
    PaymentPolicyRecord,
    SubmissionInput,
    SubmissionRecord,
    canonical_amount,
)
from .store import WalletStore

_BOUNTY_TRANSITIONS = {
    "draft": {"published", "cancelled", "expired"},
    "published": {"closed", "cancelled", "expired"},
    "closed": {"expired"},
    "cancelled": set(),
    "expired": set(),
}
_SUBMISSION_TRANSITIONS = {
    "submitted": {"accepted", "rejected", "withdrawn", "expired"},
    "accepted": set(),
    "rejected": set(),
    "withdrawn": set(),
    "expired": set(),
}
_ORDER_TRANSITIONS = {
    "pending_policy": {"awaiting_confirmation", "reserved", "rejected", "cancelled", "expired"},
    "awaiting_confirmation": {"reserved", "rejected", "cancelled", "expired"},
    "reserved": {"cancelled", "expired", "signing"},
    "cancelled": set(),
    "expired": set(),
    "rejected": set(),
    "signing": {"broadcast", "unknown", "failed"},
    "broadcast": {"unknown", "confirmed", "failed"},
    "unknown": {"signing", "broadcast", "confirmed", "failed", "refunded"},
    "confirmed": set(),
    "failed": {"refunded"},
    "refunded": set(),
}


def _now() -> str:
    return utc_now()


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(UTC)


def _amount_int(value: str) -> int:
    return int(canonical_amount(value))


def _row_has_column(row: Any, name: str) -> bool:
    """Check sqlite.Row columns without iterating row values."""
    keys = getattr(row, "keys", None)
    return callable(keys) and name in keys()


class WalletEconomyStore:
    """Pre-signer bounty, policy, payment-order and append-only ledger domain."""

    def __init__(self, database: Database, *, clock: Callable[[], str] = _now):
        self.database = database
        self.clock = clock
        self.wallets = WalletStore(database)

    @staticmethod
    def _operator(actor: object) -> str:
        if not isinstance(actor, str) or not actor.strip() or actor.strip() == "subject":
            raise PermissionError("wallet economic operation requires an operator")
        return actor.strip()

    def create_bounty(
        self,
        subject_id: str,
        proposal: BountyInput,
        *,
        actor: str,
        _connection: Any | None = None,
    ) -> BountyRecord:
        actor = self._operator(actor)
        validate_subject_id(subject_id)
        if not isinstance(proposal, BountyInput):
            raise TypeError("bounty proposal is invalid")
        now = self.clock()
        transaction = (
            self.database.transaction() if _connection is None else nullcontext(_connection)
        )
        with transaction as c:
            network = self.wallets._network_row(c, proposal.network_id, subject_id=subject_id)
            asset = self.wallets._asset_row(c, proposal.asset_id, subject_id=subject_id)
            if (
                network["status"] != "active"
                or asset["status"] != "active"
                or asset["network_id"] != proposal.network_id
            ):
                raise ValueError("wallet network or asset is not active")
            if proposal.project_id is not None:
                row = c.execute(
                    "SELECT subject_id FROM autonomous_projects WHERE project_id=?",
                    (proposal.project_id,),
                ).fetchone()
                if row is None or row["subject_id"] != subject_id:
                    raise ValueError("bounty project provenance is invalid")
            if proposal.goal_id is not None:
                row = c.execute(
                    "SELECT subject_id FROM goals WHERE goal_id=?", (proposal.goal_id,)
                ).fetchone()
                if row is None or row["subject_id"] != subject_id:
                    raise ValueError("bounty goal provenance is invalid")
            existing = c.execute(
                "SELECT * FROM wallet_bounties WHERE subject_id=? AND idempotency_key=?",
                (subject_id, proposal.idempotency_key),
            ).fetchone()
            if existing is not None:
                equivalent = (
                    existing["title"] == proposal.title
                    and existing["description"] == proposal.description
                    and existing["acceptance_criteria_json"]
                    == canonical_json(proposal.acceptance_criteria)
                    and existing["network_id"] == proposal.network_id
                    and existing["asset_id"] == proposal.asset_id
                    and existing["reward_amount"] == proposal.reward_amount
                    and existing["opens_at"] == proposal.opens_at
                    and existing["expires_at"] == proposal.expires_at
                    and int(existing["max_submissions"]) == proposal.max_submissions
                    and int(existing["reward_slots"]) == proposal.reward_slots
                    and existing["project_id"] == proposal.project_id
                    and existing["goal_id"] == proposal.goal_id
                )
                if not equivalent:
                    raise ValueError("bounty idempotency key conflicts with different input")
                return self._bounty_from_row(existing)
            bounty_id = new_id("bounty")
            payload = self._bounty_payload(bounty_id, subject_id, proposal, now)
            audit_id = self._audit(c, subject_id, "wallet_bounty_created", actor, payload)
            state_hash = self._bounty_state_hash_values(bounty_id, subject_id, proposal, "draft")
            c.execute(
                """INSERT INTO wallet_bounties(
                bounty_id,subject_id,project_id,goal_id,idempotency_key,title,description,acceptance_criteria_json,
                network_id,asset_id,reward_amount,opens_at,expires_at,max_submissions,reward_slots,status,state_hash,
                created_audit_id,last_audit_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'draft',?,?,?,?,?)""",
                (
                    bounty_id,
                    subject_id,
                    proposal.project_id,
                    proposal.goal_id,
                    proposal.idempotency_key,
                    proposal.title,
                    proposal.description,
                    canonical_json(proposal.acceptance_criteria),
                    proposal.network_id,
                    proposal.asset_id,
                    proposal.reward_amount,
                    proposal.opens_at,
                    proposal.expires_at,
                    proposal.max_submissions,
                    proposal.reward_slots,
                    state_hash,
                    audit_id,
                    audit_id,
                    now,
                    now,
                ),
            )
            return self._bounty_from_row(
                c.execute(
                    "SELECT * FROM wallet_bounties WHERE bounty_id=?", (bounty_id,)
                ).fetchone()
            )

    def publish_bounty(self, bounty_id: str, subject_id: str, *, actor: str) -> BountyRecord:
        return self._transition_bounty(bounty_id, subject_id, "published", actor)

    def close_bounty(self, bounty_id: str, subject_id: str, *, actor: str) -> BountyRecord:
        return self._transition_bounty(bounty_id, subject_id, "closed", actor)

    def cancel_bounty(self, bounty_id: str, subject_id: str, *, actor: str) -> BountyRecord:
        return self._transition_bounty(bounty_id, subject_id, "cancelled", actor)

    def _transition_bounty(
        self, bounty_id: str, subject_id: str, target: str, actor: str
    ) -> BountyRecord:
        actor = self._operator(actor)
        validate_subject_id(subject_id)
        with self.database.transaction() as c:
            row = c.execute(
                "SELECT * FROM wallet_bounties WHERE bounty_id=? AND subject_id=?",
                (bounty_id, subject_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("wallet bounty not found")
            status = self._effective_bounty_status(row)
            if status != row["status"]:
                self._set_bounty_status(c, row, status, "expired", actor)
                row = c.execute(
                    "SELECT * FROM wallet_bounties WHERE bounty_id=?", (bounty_id,)
                ).fetchone()
            if target not in _BOUNTY_TRANSITIONS.get(row["status"], set()):
                raise InvalidTransitionError("wallet bounty transition is invalid")
            self._set_bounty_status(c, row, target, target, actor)
            return self._bounty_from_row(
                c.execute(
                    "SELECT * FROM wallet_bounties WHERE bounty_id=?", (bounty_id,)
                ).fetchone()
            )

    def _set_bounty_status(self, c: Any, row: Any, target: str, reason: str, actor: str) -> None:
        now = self.clock()
        payload = {
            "bounty_id": row["bounty_id"],
            "from": row["status"],
            "to": target,
            "reason": reason,
        }
        audit_id = self._audit(c, row["subject_id"], f"wallet_bounty_{target}", actor, payload)
        state_hash = self._bounty_state_hash_row(row, target)
        c.execute(
            "UPDATE wallet_bounties SET status=?,state_hash=?,last_audit_id=?,updated_at=? WHERE bounty_id=?",
            (target, state_hash, audit_id, now, row["bounty_id"]),
        )

    def get_bounty(self, bounty_id: str, subject_id: str) -> BountyRecord:
        validate_subject_id(subject_id)
        with self.database.read_transaction() as c:
            row = c.execute(
                "SELECT * FROM wallet_bounties WHERE bounty_id=? AND subject_id=?",
                (bounty_id, subject_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("wallet bounty not found")
            status = self._effective_bounty_status(row)
            return self._bounty_from_row(row, status=status)

    def list_bounties(
        self, subject_id: str, *, status: str | None = None, limit: int = 100
    ) -> list[BountyRecord]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("invalid bounty limit")
        validate_subject_id(subject_id)
        query = "SELECT * FROM wallet_bounties WHERE subject_id=?"
        args: list[Any] = [subject_id]
        now = self.clock()
        if status is not None:
            if status not in _BOUNTY_TRANSITIONS:
                raise ValueError("invalid bounty status")
            if status in {"draft", "published", "closed"}:
                # Effective public status must be selected before LIMIT so a
                # page cannot be filled by rows that are already expired.
                query += " AND status=? AND expires_at>?"
                args.extend((status, now))
            elif status == "expired":
                query += " AND (status='expired' OR (status IN ('draft','published','closed') AND expires_at<=?))"
                args.append(now)
            else:
                query += " AND status=?"
                args.append(status)
        query += " ORDER BY created_at DESC,bounty_id DESC LIMIT ?"
        args.append(limit)
        with self.database.connection() as c:
            rows = c.execute(query, args).fetchall()
        records = [
            self._bounty_from_row(row, status=self._effective_bounty_status(row)) for row in rows
        ]
        # The SQL predicate above applies effective expiry before LIMIT. Keep
        # this defensive filter for clock implementations with unusual string
        # formatting, without triggering any write or unbounded maintenance.
        if status is not None:
            records = [record for record in records if record.status == status]
        return records

    def maintain_expired_bounties(self, subject_id: str, *, limit: int = 100) -> int:
        """Advance a bounded number of expired bounties under an operator tick."""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("invalid bounty expiry maintenance limit")
        now = _parse_time(self.clock())
        changed = 0
        with self.database.transaction() as c:
            rows = c.execute(
                "SELECT * FROM wallet_bounties WHERE subject_id=? AND status IN ('draft','published','closed') "
                "AND expires_at<=? ORDER BY expires_at,bounty_id LIMIT ?",
                (subject_id, now.isoformat(), limit),
            ).fetchall()
            for row in rows:
                if _parse_time(row["expires_at"]) <= now:
                    self._set_bounty_status(c, row, "expired", "expiry", "system")
                    changed += 1
        return changed

    def _expire_due_bounties(self, subject_id: str) -> None:
        # Compatibility shim for older internal callers.
        self.maintain_expired_bounties(subject_id)

    def submit(
        self,
        bounty_id: str,
        subject_id: str,
        proposal: SubmissionInput,
        *,
        client_ip: str | None = None,
    ) -> SubmissionRecord:
        return self.submit_to_bounty(bounty_id, subject_id, proposal, client_ip=client_ip)

    def submit_to_bounty(
        self,
        bounty_id: str,
        subject_id: str,
        proposal: SubmissionInput,
        *,
        client_ip: str | None = None,
        _connection: Any | None = None,
    ) -> SubmissionRecord:
        validate_subject_id(subject_id)
        if not isinstance(proposal, SubmissionInput):
            raise TypeError("submission is invalid")
        now = self.clock()
        transaction = (
            self.database.transaction() if _connection is None else nullcontext(_connection)
        )
        with transaction as c:
            bounty = c.execute(
                "SELECT * FROM wallet_bounties WHERE bounty_id=? AND subject_id=?",
                (bounty_id, subject_id),
            ).fetchone()
            if bounty is None:
                raise NotFoundError("wallet bounty not found")
            status = self._effective_bounty_status(bounty)
            if status != bounty["status"]:
                self._set_bounty_status(c, bounty, "expired", "expiry", "system")
                status = "expired"
            if status != "published" or _parse_time(now) < _parse_time(bounty["opens_at"]):
                raise ValueError("bounty is not accepting submissions")
            existing = c.execute(
                "SELECT * FROM wallet_bounty_submissions WHERE subject_id=? AND bounty_id=? AND idempotency_key=?",
                (subject_id, bounty_id, proposal.idempotency_key),
            ).fetchone()
            if existing is not None:
                if not self._submission_input_matches(existing, proposal):
                    raise ValueError("submission idempotency key conflicts with different input")
                return self._submission_from_row(existing)
            count = c.execute(
                "SELECT count(*) AS n FROM wallet_bounty_submissions WHERE bounty_id=? AND status IN ('submitted','accepted')",
                (bounty_id,),
            ).fetchone()["n"]
            if count >= bounty["max_submissions"]:
                raise ValueError("bounty submission limit reached")
            sid = new_id("submission")
            payload = {
                "submission_id": sid,
                "bounty_id": bounty_id,
                "subject_id": subject_id,
                "counterparty": proposal.counterparty,
                "content": proposal.content,
                "evidence": proposal.evidence,
                "recipient_address": proposal.recipient_address,
                "idempotency_key": proposal.idempotency_key,
                "consent_version": proposal.consent_version,
            }
            audit = self._audit(
                c, subject_id, "wallet_bounty_submission_created", "visitor", payload
            )
            state_hash = self._submission_state_hash_values(
                sid,
                bounty_id,
                subject_id,
                proposal.counterparty,
                proposal.content,
                proposal.evidence,
                proposal.recipient_address,
                proposal.idempotency_key,
                proposal.consent_version,
                "submitted",
                None,
            )
            c.execute(
                """INSERT INTO wallet_bounty_submissions(submission_id,bounty_id,subject_id,counterparty,content,evidence_json,recipient_address,idempotency_key,consent_version,status,decision_reason,created_at,decided_at,state_hash,created_audit_id) VALUES(?,?,?,?,?,?,?,?,?,'submitted',NULL,?,?,?,?)""",
                (
                    sid,
                    bounty_id,
                    subject_id,
                    proposal.counterparty,
                    proposal.content,
                    canonical_json(proposal.evidence),
                    proposal.recipient_address,
                    proposal.idempotency_key,
                    proposal.consent_version,
                    now,
                    None,
                    state_hash,
                    audit,
                ),
            )
            return self._submission_from_row(
                c.execute(
                    "SELECT * FROM wallet_bounty_submissions WHERE submission_id=?", (sid,)
                ).fetchone()
            )

    def decide_submission(
        self, submission_id: str, subject_id: str, *, accepted: bool, reason: str, actor: str
    ) -> SubmissionRecord:
        actor = self._operator(actor)
        validate_subject_id(subject_id)
        reason = reason.strip()
        if not reason or len(reason) > 2000:
            raise ValueError("submission decision reason is invalid")
        with self.database.transaction() as c:
            return self._decide_submission_connection(
                c,
                submission_id,
                subject_id,
                accepted=accepted,
                reason=reason,
                actor=actor,
            )

    @staticmethod
    def _submission_input_matches(row: Any, proposal: SubmissionInput) -> bool:
        return (
            row["counterparty"] == proposal.counterparty
            and row["content"] == proposal.content
            and strict_json_loads(row["evidence_json"]) == list(proposal.evidence)
            and row["recipient_address"] == proposal.recipient_address
            and int(row["consent_version"]) == int(proposal.consent_version)
        )

    def _decide_submission_connection(
        self,
        c: Any,
        submission_id: str,
        subject_id: str,
        *,
        accepted: bool,
        reason: str,
        actor: str,
    ) -> SubmissionRecord:
        """Decide inside a caller-owned transaction after provenance checks."""
        row = c.execute(
            "SELECT * FROM wallet_bounty_submissions WHERE submission_id=? AND subject_id=?",
            (submission_id, subject_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("wallet submission not found")
        if row["status"] != "submitted":
            raise InvalidTransitionError("submission already decided")
        bounty = c.execute(
            "SELECT * FROM wallet_bounties WHERE bounty_id=? AND subject_id=?",
            (row["bounty_id"], subject_id),
        ).fetchone()
        if bounty is None:
            raise IntegrityError("submission bounty reference is invalid")
        if accepted:
            winners = c.execute(
                "SELECT count(*) AS n FROM wallet_bounty_submissions "
                "WHERE bounty_id=? AND status='accepted'",
                (row["bounty_id"],),
            ).fetchone()["n"]
            if int(winners) >= int(bounty["reward_slots"]):
                raise ValueError("bounty reward slots are exhausted")
        target = "accepted" if accepted else "rejected"
        now = self.clock()
        payload = {
            "submission_id": submission_id,
            "from": "submitted",
            "to": target,
            "reason": reason,
        }
        decision_audit_id = self._audit(
            c, subject_id, f"wallet_submission_{target}", actor, payload
        )
        c.execute(
            "UPDATE wallet_bounty_submissions SET status=?,decision_reason=?,decided_at=?,"
            "decision_audit_id=?,state_hash=? WHERE submission_id=?",
            (
                target,
                reason,
                now,
                decision_audit_id,
                self._submission_state_hash_row(row, target, reason),
                submission_id,
            ),
        )
        result = self._submission_from_row(
            c.execute(
                "SELECT * FROM wallet_bounty_submissions WHERE submission_id=?",
                (submission_id,),
            ).fetchone()
        )
        if accepted:
            self._ensure_order(c, result)
        return result

    def withdraw_submission(
        self, submission_id: str, subject_id: str, *, actor: str = "visitor"
    ) -> SubmissionRecord:
        validate_subject_id(subject_id)
        with self.database.transaction() as c:
            row = c.execute(
                "SELECT * FROM wallet_bounty_submissions WHERE submission_id=? AND subject_id=?",
                (submission_id, subject_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("wallet submission not found")
            if row["status"] != "submitted":
                raise InvalidTransitionError("submission cannot be withdrawn")
            now = self.clock()
            decision_audit_id = self._audit(
                c,
                subject_id,
                "wallet_submission_withdrawn",
                actor,
                {"submission_id": submission_id, "from": "submitted", "to": "withdrawn"},
            )
            c.execute(
                "UPDATE wallet_bounty_submissions SET status='withdrawn',decided_at=?,decision_reason='withdrawn',decision_audit_id=?,state_hash=? WHERE submission_id=?",
                (
                    now,
                    decision_audit_id,
                    self._submission_state_hash_row(row, "withdrawn", "withdrawn"),
                    submission_id,
                ),
            )
            return self._submission_from_row(
                c.execute(
                    "SELECT * FROM wallet_bounty_submissions WHERE submission_id=?",
                    (submission_id,),
                ).fetchone()
            )

    def get_policy(self, subject_id: str) -> PaymentPolicyRecord:
        validate_subject_id(subject_id)
        with self.database.read_transaction() as c:
            row = c.execute(
                "SELECT * FROM wallet_payment_policies WHERE subject_id=?", (subject_id,)
            ).fetchone()
            if row is None:
                raise IntegrityError(f"wallet payment policy is missing: {subject_id}")
            return self._policy_from_row(row)

    def update_policy(
        self, subject_id: str, proposal: PaymentPolicyInput, *, expected_version: int, actor: str
    ) -> PaymentPolicyRecord:
        actor = self._operator(actor)
        validate_subject_id(subject_id)
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("invalid policy version")
        with self.database.transaction() as c:
            row = c.execute(
                "SELECT * FROM wallet_payment_policies WHERE subject_id=?", (subject_id,)
            ).fetchone()
            current = 1 if row is None else int(row["policy_version"])
            if expected_version != current:
                raise ValueError("wallet payment policy version conflict")
            now = self.clock()
            self._insert_policy(c, subject_id, proposal, current + 1, now, replace=True)
            self._audit(
                c,
                subject_id,
                "wallet_payment_policy_updated",
                actor,
                {"policy_version": current + 1, "mode": proposal.mode},
            )
            return self._policy_from_row(
                c.execute(
                    "SELECT * FROM wallet_payment_policies WHERE subject_id=?", (subject_id,)
                ).fetchone()
            )

    def list_orders(self, subject_id: str, *, limit: int = 100) -> list[PaymentOrderRecord]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("invalid order limit")
        with self.database.connection() as c:
            rows = c.execute(
                "SELECT * FROM wallet_payment_orders WHERE subject_id=? ORDER BY created_at DESC,order_id DESC LIMIT ?",
                (subject_id, limit),
            ).fetchall()
        return [self._order_from_row(r) for r in rows]

    def order_for_submission(
        self, submission_id: str, subject_id: str
    ) -> PaymentOrderRecord | None:
        validate_subject_id(subject_id)
        with self.database.connection() as c:
            row = c.execute(
                "SELECT * FROM wallet_payment_orders WHERE submission_id=? AND subject_id=?",
                (submission_id, subject_id),
            ).fetchone()
        return None if row is None else self._order_from_row(row)

    def confirm_order(self, order_id: str, subject_id: str, *, actor: str) -> PaymentOrderRecord:
        actor = self._operator(actor)
        with self.database.transaction() as c:
            row = self._order_row(c, order_id, subject_id)
            if row["status"] != "awaiting_confirmation":
                raise InvalidTransitionError("order is not awaiting confirmation")
            self._reserve_order(c, row, actor)
            return self._order_from_row(self._order_row(c, order_id, subject_id))

    def reject_order(
        self, order_id: str, subject_id: str, *, actor: str, reason: str = "operator rejected"
    ) -> PaymentOrderRecord:
        actor = self._operator(actor)
        with self.database.transaction() as c:
            row = self._order_row(c, order_id, subject_id)
            if row["status"] not in {"pending_policy", "awaiting_confirmation"}:
                raise InvalidTransitionError("order cannot be rejected")
            self._transition_order(c, row, "rejected", actor, reason)
            return self._order_from_row(self._order_row(c, order_id, subject_id))

    def cancel_order(
        self, order_id: str, subject_id: str, *, actor: str, reason: str = "operator cancelled"
    ) -> PaymentOrderRecord:
        actor = self._operator(actor)
        with self.database.transaction() as c:
            row = self._order_row(c, order_id, subject_id)
            if row["status"] == "reserved":
                self._release_order(c, row, actor, reason)
            elif row["status"] in {"pending_policy", "awaiting_confirmation"}:
                self._transition_order(c, row, "cancelled", actor, reason)
            else:
                raise InvalidTransitionError("order cannot be cancelled")
            return self._order_from_row(self._order_row(c, order_id, subject_id))

    def ledger_balances(
        self, subject_id: str, *, network_id: str | None = None, asset_id: str | None = None
    ) -> list[LedgerBalance]:
        validate_subject_id(subject_id)
        query = (
            "SELECT e.account,e.direction,e.amount,j.network_id,j.asset_id "
            "FROM wallet_ledger_entries e JOIN wallet_ledger_journals j "
            "ON j.journal_id=e.journal_id WHERE e.subject_id=?"
        )
        args: list[Any] = [subject_id]
        if network_id:
            query += " AND j.network_id=?"
            args.append(network_id)
        if asset_id:
            query += " AND j.asset_id=?"
            args.append(asset_id)
        with self.database.connection() as c:
            totals: dict[tuple[str, str, str], dict[str, int]] = {}
            for row in c.execute(query, args):
                key = (str(row["network_id"]), str(row["asset_id"]), str(row["account"]))
                totals.setdefault(key, {"debit": 0, "credit": 0})[row["direction"]] += int(
                    row["amount"]
                )
        return [
            LedgerBalance(
                account,
                str(v["debit"]),
                str(v["credit"]),
                str(v["debit"] - v["credit"]),
                network_id=network,
                asset_id=asset,
            )
            for (network, asset, account), v in sorted(totals.items())
        ]

    def verify_integrity(
        self, subject_id: str, *, _connection: Any | None = None
    ) -> dict[str, int]:
        validate_subject_id(subject_id)
        with (
            self.database.read_transaction() if _connection is None else nullcontext(_connection)
        ) as c:
            counts: dict[str, int] = {}
            for table in (
                "wallet_bounties",
                "wallet_bounty_submissions",
                "wallet_payment_orders",
                "wallet_ledger_journals",
                "wallet_ledger_entries",
            ):
                try:
                    row = c.execute(
                        f"SELECT count(*) AS n FROM {table} WHERE subject_id=?", (subject_id,)
                    ).fetchone()
                except Exception as error:
                    raise IntegrityError("wallet economy table unavailable") from error
                if row is None:
                    raise IntegrityError("wallet economy table unavailable")
                counts[table] = int(row["n"])

            policy = c.execute(
                "SELECT * FROM wallet_payment_policies WHERE subject_id=?", (subject_id,)
            ).fetchone()
            if policy is None:
                raise IntegrityError(f"wallet payment policy is missing: {subject_id}")
            try:
                if strict_int(policy["policy_version"]) < 1 or any(
                    strict_int(policy[field]) not in (0, 1)
                    for field in ("anomaly_block", "emergency_paused")
                ):
                    raise ValueError("invalid wallet policy version or flags")
                PaymentPolicyInput.model_validate(
                    {
                        "mode": policy["mode"],
                        "allowed_network_ids": strict_json_loads(
                            policy["allowed_network_ids_json"]
                        ),
                        "allowed_asset_ids": strict_json_loads(policy["allowed_asset_ids_json"]),
                        "per_order_limit": policy["per_order_limit"],
                        "daily_limit": policy["daily_limit"],
                        "monthly_limit": policy["monthly_limit"],
                        "daily_order_limit": policy["daily_order_limit"],
                        "monthly_order_limit": policy["monthly_order_limit"],
                        "min_balance": policy["min_balance"],
                        "max_observation_age_seconds": policy["max_observation_age_seconds"],
                        "automatic_max_amount": policy["automatic_max_amount"],
                        "anomaly_block": bool(policy["anomaly_block"]),
                        "emergency_paused": bool(policy["emergency_paused"]),
                    }
                )
                policy_hash = self._policy_state_hash_row(policy)
            except (KeyError, TypeError, ValueError) as error:
                raise IntegrityError(f"wallet payment policy is malformed: {subject_id}") from error
            if policy["state_hash"] != policy_hash:
                raise IntegrityError(f"wallet payment policy hash mismatch: {subject_id}")

            for bounty in c.execute(
                "SELECT * FROM wallet_bounties WHERE subject_id=?", (subject_id,)
            ):
                if bounty["state_hash"] != self._bounty_state_hash_row(bounty, bounty["status"]):
                    raise IntegrityError(f"wallet bounty hash mismatch: {bounty['bounty_id']}")
            for submission in c.execute(
                "SELECT * FROM wallet_bounty_submissions WHERE subject_id=?", (subject_id,)
            ):
                if submission["state_hash"] != self._submission_state_hash_row(
                    submission, submission["status"], submission["decision_reason"]
                ):
                    raise IntegrityError(
                        f"wallet submission hash mismatch: {submission['submission_id']}"
                    )
            for order in c.execute(
                "SELECT * FROM wallet_payment_orders WHERE subject_id=?", (subject_id,)
            ):
                if order["state_hash"] != self._order_state_hash_row(order, order["status"]):
                    raise IntegrityError(f"wallet payment order hash mismatch: {order['order_id']}")

            expected_accounts = {
                "reservation": {("available", "debit"), ("reserved", "credit")},
                "release": {("reserved", "debit"), ("released", "credit")},
                "settlement": {("reserved", "debit"), ("paid", "credit")},
                "refund": {("reserved", "debit"), ("released", "credit")},
            }
            journal_count = 0
            entry_count = 0
            current_journal_id: str | None = None
            current_entry_count = 0
            current_debit = 0
            current_credit = 0
            current_accounts: set[tuple[str, str]] = set()
            current_amount = 0
            current_type = ""

            def finish_journal() -> None:
                if current_journal_id is None:
                    return
                if current_entry_count != 2 or current_debit != current_credit:
                    raise IntegrityError(
                        f"wallet ledger journal is unbalanced: {current_journal_id}"
                    )
                if current_accounts != expected_accounts.get(current_type):
                    raise IntegrityError(
                        f"wallet ledger journal accounts are invalid: {current_journal_id}"
                    )
                if any(
                    amount != current_amount
                    for amount in (
                        current_debit,
                        current_credit,
                    )
                ):
                    raise IntegrityError(
                        f"wallet ledger journal amount is inconsistent: {current_journal_id}"
                    )

            history_query = """
                SELECT
                    j.journal_id,
                    j.subject_id AS journal_subject_id,
                    j.order_id,
                    j.network_id,
                    j.asset_id,
                    j.journal_type,
                    j.amount AS journal_amount,
                    j.created_at AS journal_created_at,
                    j.state_hash AS journal_state_hash,
                    o.order_id AS order_order_id,
                    o.subject_id AS order_subject_id,
                    o.network_id AS order_network_id,
                    o.asset_id AS order_asset_id,
                    o.amount AS order_amount,
                    e.entry_id,
                    e.journal_id AS entry_journal_id,
                    e.subject_id AS entry_subject_id,
                    e.order_id AS entry_order_id,
                    e.account,
                    e.direction,
                    e.amount AS entry_amount,
                    e.created_at AS entry_created_at,
                    e.state_hash AS entry_state_hash
                FROM wallet_ledger_journals AS j
                LEFT JOIN wallet_payment_orders AS o ON o.order_id = j.order_id
                LEFT JOIN wallet_ledger_entries AS e ON e.journal_id = j.journal_id
                WHERE j.subject_id = ?
                -- Ordering by journal columns keeps the outer cursor ordered
                -- without a connection-sized temporary sort. Entries remain
                -- contiguous for each journal, which is the only ordering
                -- required by the streaming validator.
                ORDER BY j.created_at, j.journal_id
            """
            for row in c.execute(history_query, (subject_id,)):
                journal_id = str(row["journal_id"])
                if journal_id != current_journal_id:
                    finish_journal()
                    current_journal_id = journal_id
                    journal_count += 1
                    current_entry_count = 0
                    current_debit = 0
                    current_credit = 0
                    current_accounts = set()
                    current_type = str(row["journal_type"])
                    try:
                        current_amount = _amount_int(str(row["journal_amount"]))
                    except (TypeError, ValueError) as error:
                        raise IntegrityError(
                            f"wallet ledger amount is invalid: {journal_id}"
                        ) from error
                    if current_amount <= 0:
                        raise IntegrityError(f"wallet ledger amount is invalid: {journal_id}")
                    if current_type not in expected_accounts:
                        raise IntegrityError(f"wallet ledger journal type is invalid: {journal_id}")
                    if row["journal_subject_id"] != subject_id:
                        raise IntegrityError(
                            f"wallet ledger journal subject mismatch: {journal_id}"
                        )
                    if row["order_order_id"] != row["order_id"]:
                        raise IntegrityError(
                            f"wallet ledger order reference is missing: {journal_id}"
                        )
                    if (
                        row["order_subject_id"] != subject_id
                        or row["order_network_id"] != row["network_id"]
                        or row["order_asset_id"] != row["asset_id"]
                        or row["order_amount"] != row["journal_amount"]
                    ):
                        raise IntegrityError(
                            f"wallet ledger order reference mismatch: {journal_id}"
                        )
                    try:
                        expected_journal_hash = self._journal_state_hash_row(row)
                    except (TypeError, ValueError) as error:
                        raise IntegrityError(
                            f"wallet ledger journal is malformed: {journal_id}"
                        ) from error
                    if row["journal_state_hash"] != expected_journal_hash:
                        raise IntegrityError(f"wallet ledger journal hash mismatch: {journal_id}")

                if row["entry_id"] is None:
                    continue
                entry_count += 1
                current_entry_count += 1
                if (
                    row["entry_journal_id"] != journal_id
                    or row["entry_subject_id"] != subject_id
                    or row["entry_order_id"] != row["order_id"]
                    or row["entry_amount"] != row["journal_amount"]
                    or row["entry_created_at"] != row["journal_created_at"]
                ):
                    raise IntegrityError(f"wallet ledger entry reference mismatch: {journal_id}")
                try:
                    expected_entry_hash = self._entry_state_hash_row(row)
                except (TypeError, ValueError) as error:
                    raise IntegrityError(
                        f"wallet ledger entry is malformed: {row['entry_id']}"
                    ) from error
                if row["entry_state_hash"] != expected_entry_hash:
                    raise IntegrityError(f"wallet ledger entry hash mismatch: {row['entry_id']}")
                if row["direction"] not in {"debit", "credit"}:
                    raise IntegrityError(f"wallet ledger entry direction is invalid: {journal_id}")
                current_accounts.add((str(row["account"]), str(row["direction"])))
                try:
                    amount = _amount_int(str(row["entry_amount"]))
                except (TypeError, ValueError) as error:
                    raise IntegrityError(
                        f"wallet ledger entry amount is invalid: {row['entry_id']}"
                    ) from error
                if row["direction"] == "debit":
                    current_debit += amount
                else:
                    current_credit += amount
            finish_journal()

            orphan = c.execute(
                """
                SELECT e.entry_id, e.journal_id
                FROM wallet_ledger_entries AS e
                LEFT JOIN wallet_ledger_journals AS j ON j.journal_id = e.journal_id
                WHERE e.subject_id = ?
                  AND (j.journal_id IS NULL OR j.subject_id <> ? OR j.order_id <> e.order_id)
                LIMIT 1
                """,
                (subject_id, subject_id),
            ).fetchone()
            if orphan is not None:
                raise IntegrityError(
                    f"wallet ledger entry has invalid journal: {orphan['entry_id']}"
                )
            if entry_count != counts["wallet_ledger_entries"]:
                raise IntegrityError("wallet ledger entry count is inconsistent")
            return {
                "wallet_bounties": counts["wallet_bounties"],
                "wallet_bounty_submissions": counts["wallet_bounty_submissions"],
                "wallet_payment_orders": counts["wallet_payment_orders"],
                "wallet_ledger_journals": journal_count,
                "wallet_ledger_entries": entry_count,
            }

    def _ensure_order(self, c: Any, submission: SubmissionRecord) -> None:
        existing = c.execute(
            "SELECT * FROM wallet_payment_orders WHERE submission_id=?", (submission.submission_id,)
        ).fetchone()
        if existing is not None:
            return
        bounty = c.execute(
            "SELECT * FROM wallet_bounties WHERE bounty_id=?", (submission.bounty_id,)
        ).fetchone()
        policy = self._policy_row(c, submission.subject_id)
        order_id = new_id("payorder")
        now = self.clock()
        mode = policy["mode"] if policy else "disabled"
        key = f"{submission.bounty_id}:{submission.submission_id}"
        audit_id = self._audit(
            c,
            submission.subject_id,
            "wallet_payment_order_created",
            "system",
            {"submission_id": submission.submission_id, "amount": bounty["reward_amount"]},
        )
        has_authorized_at = "authorized_at" in {
            str(item[1]) for item in c.execute("PRAGMA table_info(wallet_payment_orders)")
        }
        initial_hash = (
            self._order_temporal_hash(
                order_id,
                submission.subject_id,
                submission.bounty_id,
                submission.submission_id,
                bounty["network_id"],
                bounty["asset_id"],
                submission.recipient_address,
                bounty["reward_amount"],
                mode,
                int(policy["policy_version"]) if policy else 1,
                key,
                "pending_policy",
                now,
                None,
                now,
            )
            if has_authorized_at
            else self._order_state_hash_values(
                order_id,
                submission.subject_id,
                submission.bounty_id,
                submission.submission_id,
                bounty["network_id"],
                bounty["asset_id"],
                submission.recipient_address,
                bounty["reward_amount"],
                mode,
                int(policy["policy_version"]) if policy else 1,
                key,
                "pending_policy",
            )
        )
        c.execute(
            "INSERT INTO wallet_payment_orders(order_id,subject_id,bounty_id,submission_id,network_id,asset_id,recipient_address,amount,payment_mode,policy_version,idempotency_key,created_audit_id,last_audit_id,status,state_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?, ?, ?, 'pending_policy',?,?,?)",
            (
                order_id,
                submission.subject_id,
                submission.bounty_id,
                submission.submission_id,
                bounty["network_id"],
                bounty["asset_id"],
                submission.recipient_address,
                bounty["reward_amount"],
                mode,
                int(policy["policy_version"]) if policy else 1,
                key,
                audit_id,
                audit_id,
                initial_hash,
                now,
                now,
            ),
        )
        row = c.execute(
            "SELECT * FROM wallet_payment_orders WHERE order_id=?", (order_id,)
        ).fetchone()
        if mode == "disabled":
            self._transition_order(c, row, "rejected", "system", "payment policy disabled")
        elif mode == "conditional_confirmation":
            self._transition_order(
                c, row, "awaiting_confirmation", "system", "confirmation required"
            )
        elif not self._policy_allows(c, row, policy):
            self._transition_order(c, row, "rejected", "system", "payment policy denied")
        else:
            self._reserve_order(c, row, "system")

    def _spending_address(self, c: Any, subject_id: str, network_id: str) -> Any | None:
        rows = c.execute(
            "SELECT * FROM wallet_addresses WHERE subject_id=? AND network_id=? "
            "AND status='active' AND purpose='spending' ORDER BY created_at,address_id LIMIT 2",
            (subject_id, network_id),
        ).fetchall()
        return rows[0] if len(rows) == 1 else None

    def _policy_allows(
        self, c: Any, row: Any, policy: Any, *, exclude_order_id: str | None = None
    ) -> bool:
        if policy is None or int(policy["emergency_paused"]):
            return False
        if str(policy["mode"]) == "disabled":
            return False
        if row["payment_mode"] == "automatic" and str(policy["mode"]) != "automatic":
            return False
        if row["network_id"] not in strict_json_loads(policy["allowed_network_ids_json"]):
            return False
        if row["asset_id"] not in strict_json_loads(policy["allowed_asset_ids_json"]):
            return False
        amount = int(row["amount"])
        if amount <= 0 or (
            int(policy["per_order_limit"]) > 0 and amount > int(policy["per_order_limit"])
        ):
            return False
        if (
            row["payment_mode"] == "automatic"
            and int(policy["automatic_max_amount"]) > 0
            and amount > int(policy["automatic_max_amount"])
        ):
            return False
        now = _parse_time(self.clock())
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
        active_status = ("reserved", "signing", "broadcast", "unknown", "confirmed")
        placeholders = ",".join("?" for _ in active_status)
        # Aggregate in Python so valid per-order int64 values cannot overflow
        # SQLite's signed integer SUM.  Authorization periods are anchored at
        # reservation time when the schema provides it, never at order creation.
        has_authorized_at = "authorized_at" in {
            str(item[1]) for item in c.execute("PRAGMA table_info(wallet_payment_orders)")
        }
        query = (
            "SELECT order_id,amount,created_at,updated_at"
            + (",authorized_at " if has_authorized_at else " ")
            + ""
            f"FROM wallet_payment_orders WHERE subject_id=? AND network_id=? AND asset_id=? "
            f"AND status IN ({placeholders})"
        )
        query_args: list[Any] = [
            row["subject_id"],
            row["network_id"],
            row["asset_id"],
            *active_status,
        ]
        if exclude_order_id is not None:
            query += " AND order_id<>?"
            query_args.append(exclude_order_id)
        day_count = month_count = 0
        day_total = month_total = 0
        for existing in c.execute(query, query_args):
            period_value = existing["authorized_at"] if has_authorized_at else None
            period_value = period_value or existing["updated_at"] or existing["created_at"]
            try:
                value = int(existing["amount"])
                period = _parse_time(str(period_value))
            except (TypeError, ValueError) as error:
                raise IntegrityError(
                    "wallet payment order amount or timestamp is invalid"
                ) from error
            if period >= _parse_time(day_start):
                day_count += 1
                day_total += value
            if period >= _parse_time(month_start):
                month_count += 1
                month_total += value
        if int(policy["daily_order_limit"]) and day_count >= int(policy["daily_order_limit"]):
            return False
        if int(policy["monthly_order_limit"]) and month_count >= int(policy["monthly_order_limit"]):
            return False
        if int(policy["daily_limit"]) and day_total + amount > int(policy["daily_limit"]):
            return False
        if int(policy["monthly_limit"]) and month_total + amount > int(policy["monthly_limit"]):
            return False
        source = self._spending_address(c, row["subject_id"], row["network_id"])
        # Every reservable order must already have exactly one active spending
        # address.  Otherwise it could be accepted into a durable reservation
        # that can never be executed by the signer.
        if source is None:
            return False
        latest_balance = None
        if source is not None:
            latest_balance = c.execute(
                "SELECT balance,observed_at FROM wallet_balance_snapshots "
                "WHERE subject_id=? AND network_id=? AND asset_id=? AND address_id=? "
                "ORDER BY observed_at DESC,snapshot_id DESC LIMIT 1",
                (row["subject_id"], row["network_id"], row["asset_id"], source["address_id"]),
            ).fetchone()
        if int(policy["anomaly_block"]):
            if source is None:
                return False
            previous = c.execute(
                "SELECT balance FROM wallet_balance_snapshots "
                "WHERE subject_id=? AND network_id=? AND asset_id=? AND address_id=? "
                "ORDER BY observed_at DESC,snapshot_id DESC LIMIT 2",
                (row["subject_id"], row["network_id"], row["asset_id"], source["address_id"]),
            ).fetchall()
            if len(previous) >= 2:
                newer, older = int(previous[0]["balance"]), int(previous[1]["balance"])
                if (newer == 0) != (older == 0) or (older and abs(newer - older) > older):
                    return False
        if latest_balance is not None and _parse_time(latest_balance["observed_at"]) > now:
            # Future-dated observations cannot establish current funds.  The
            # public observation health projection classifies them as
            # attention; payment authorization must fail closed as well.
            return False
        if policy["max_observation_age_seconds"] > 0 and (
            latest_balance is None
            or (now - _parse_time(latest_balance["observed_at"])).total_seconds()
            > int(policy["max_observation_age_seconds"])
        ):
            return False
        # A fresh balance observation is authoritative for this spending
        # address.  Every reservable amount, not only policies with a
        # non-zero minimum balance, must fit after existing reservations.  A
        # zero minimum means "do not retain a reserve", never "ignore the
        # observed balance".  The candidate is excluded when revalidating an
        # already-reserved order during execution.
        if latest_balance is not None:
            available = int(latest_balance["balance"])
            available -= sum(
                int(item["amount"])
                for item in c.execute(
                    "SELECT amount FROM wallet_payment_orders WHERE subject_id=? AND network_id=? "
                    "AND asset_id=? AND status IN ('reserved','signing','broadcast','unknown')"
                    + (" AND order_id<>?" if exclude_order_id else ""),
                    (
                        [row["subject_id"], row["network_id"], row["asset_id"]]
                        + ([exclude_order_id] if exclude_order_id else [])
                    ),
                )
            )
            if available < amount + int(policy["min_balance"]):
                return False
        elif int(policy["min_balance"]) > 0:
            # Without an observation, an explicit reserve requirement cannot
            # be proven.  Preserve the legacy read-only behavior only when no
            # minimum balance was requested.
            return False
        return True

    def _reserve_order(self, c: Any, row: Any, actor: str) -> None:
        if not self._policy_allows(
            c,
            row,
            c.execute(
                "SELECT * FROM wallet_payment_policies WHERE subject_id=?", (row["subject_id"],)
            ).fetchone(),
        ):
            raise ValueError("payment policy denied reservation")
        self._transition_order(c, row, "reserved", actor, "funds reserved")
        self._post_order_journal(
            c, row, "reservation", ("available", "debit"), ("reserved", "credit")
        )

    def _release_order(self, c: Any, row: Any, actor: str, reason: str) -> None:
        self._transition_order(c, row, "cancelled", actor, reason)
        self._post_order_journal(c, row, "release", ("reserved", "debit"), ("released", "credit"))

    def _post_order_journal(
        self,
        c: Any,
        row: Any,
        journal_type: str,
        first: tuple[str, str],
        second: tuple[str, str],
    ) -> None:
        """Post one balanced immutable journal for an order."""
        existing = c.execute(
            "SELECT 1 FROM wallet_ledger_journals WHERE order_id=? AND journal_type=?",
            (row["order_id"], journal_type),
        ).fetchone()
        if existing is not None:
            raise InvalidTransitionError(f"order {journal_type} journal already exists")
        now = self.clock()
        amount = row["amount"]
        jid = new_id("journal")
        jhash = wallet_journal_hash(
            {
                "journal_id": jid,
                "subject_id": row["subject_id"],
                "order_id": row["order_id"],
                "network_id": row["network_id"],
                "asset_id": row["asset_id"],
                "journal_type": journal_type,
                "amount": amount,
                "created_at": now,
            }
        )
        c.execute(
            "INSERT INTO wallet_ledger_journals(journal_id,subject_id,order_id,network_id,asset_id,journal_type,amount,created_at,state_hash) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                jid,
                row["subject_id"],
                row["order_id"],
                row["network_id"],
                row["asset_id"],
                journal_type,
                amount,
                now,
                jhash,
            ),
        )
        for account, direction in (first, second):
            eid = new_id("entry")
            c.execute(
                "INSERT INTO wallet_ledger_entries(entry_id,journal_id,subject_id,order_id,account,direction,amount,created_at,state_hash) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    eid,
                    jid,
                    row["subject_id"],
                    row["order_id"],
                    account,
                    direction,
                    amount,
                    now,
                    wallet_entry_hash(
                        {
                            "entry_id": eid,
                            "journal_id": jid,
                            "subject_id": row["subject_id"],
                            "order_id": row["order_id"],
                            "account": account,
                            "direction": direction,
                            "amount": amount,
                            "created_at": now,
                        }
                    ),
                ),
            )

    def _settle_order(
        self, c: Any, row: Any, actor: str, reason: str = "chain receipt confirmed"
    ) -> None:
        if row["status"] not in {"broadcast", "unknown"}:
            raise InvalidTransitionError("order is not awaiting settlement")
        self._transition_order(c, row, "confirmed", actor, reason)
        self._post_order_journal(c, row, "settlement", ("reserved", "debit"), ("paid", "credit"))

    @staticmethod
    def _require_resolved_legacy_payments(c: Any, subject_id: str, network_id: str) -> None:
        """Old rejected retries/refunds are not evidence of chain cancellation."""
        unresolved = c.execute(
            "SELECT e.execution_id FROM wallet_payment_executions e "
            "JOIN wallet_payment_orders o ON o.order_id=e.order_id AND o.subject_id=e.subject_id "
            "WHERE e.subject_id=? AND e.network_id=? AND ("
            "(e.status='failed' AND e.receipt_status IS NULL AND EXISTS ("
            "SELECT 1 FROM wallet_payment_execution_attempts a "
            "WHERE a.execution_id=e.execution_id AND a.subject_id=e.subject_id "
            "AND a.attempt_number<e.attempt_count AND a.status IN ('unknown','broadcast'))) "
            "OR (o.status='refunded' AND e.status IN ('signing','broadcast','unknown'))) LIMIT 1",
            (subject_id, network_id),
        ).fetchone()
        if unresolved is not None:
            raise IntegrityError(
                "legacy wallet payment has unresolved broadcasts; recovery required"
            )

    def refund_order(
        self, order_id: str, subject_id: str, *, actor: str, reason: str = "operator refunded"
    ) -> PaymentOrderRecord:
        """Release a failed reservation; unknown broadcasts still own their funds."""
        actor = self._operator(actor)
        reason = reason.strip()
        if not reason or len(reason) > 2000:
            raise ValueError("refund reason is invalid")
        with self.database.transaction() as c:
            row = self._order_row(c, order_id, subject_id)
            self._require_resolved_legacy_payments(c, subject_id, row["network_id"])
            if row["status"] != "failed":
                raise InvalidTransitionError("order cannot be refunded")
            self._transition_order(c, row, "refunded", actor, reason)
            self._post_order_journal(
                c, row, "refund", ("reserved", "debit"), ("released", "credit")
            )
            return self._order_from_row(self._order_row(c, order_id, subject_id))

    def _transition_order(self, c: Any, row: Any, target: str, actor: str, reason: str) -> None:
        if target not in _ORDER_TRANSITIONS.get(row["status"], set()):
            raise InvalidTransitionError("payment order transition is invalid")
        now = self.clock()
        audit_id = self._audit(
            c,
            row["subject_id"],
            f"wallet_payment_order_{target}",
            actor,
            {"order_id": row["order_id"], "from": row["status"], "to": target, "reason": reason},
        )
        has_authorized_at = _row_has_column(row, "authorized_at")
        authorized_at = row["authorized_at"] if has_authorized_at else None
        if target == "reserved" and has_authorized_at:
            authorized_at = now
        state_hash = self._order_state_hash_row(
            row, target, updated_at=now, authorized_at=authorized_at
        )
        if has_authorized_at:
            c.execute(
                "UPDATE wallet_payment_orders SET status=?,authorized_at=?,updated_at=?,last_audit_id=?,state_hash=? WHERE order_id=?",
                (target, authorized_at, now, audit_id, state_hash, row["order_id"]),
            )
        else:
            c.execute(
                "UPDATE wallet_payment_orders SET status=?,updated_at=?,last_audit_id=?,state_hash=? WHERE order_id=?",
                (target, now, audit_id, state_hash, row["order_id"]),
            )

    @staticmethod
    def _order_row(c: Any, order_id: str, subject_id: str) -> Any:
        row = c.execute(
            "SELECT * FROM wallet_payment_orders WHERE order_id=? AND subject_id=?",
            (order_id, subject_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("wallet payment order not found")
        return row

    @staticmethod
    def _bounty_payload(bid: str, sid: str, p: BountyInput, now: str) -> dict[str, Any]:
        return {
            "bounty_id": bid,
            "subject_id": sid,
            "project_id": p.project_id,
            "goal_id": p.goal_id,
            "idempotency_key": p.idempotency_key,
            "title": p.title,
            "description": p.description,
            "acceptance_criteria": p.acceptance_criteria,
            "network_id": p.network_id,
            "asset_id": p.asset_id,
            "reward_amount": p.reward_amount,
            "opens_at": p.opens_at,
            "expires_at": p.expires_at,
            "max_submissions": p.max_submissions,
            "reward_slots": p.reward_slots,
            "status": "draft",
            "created_at": now,
        }

    @staticmethod
    def _bounty_input_hash(p: BountyInput) -> str:
        return content_hash(
            {
                "title": p.title,
                "description": p.description,
                "criteria": p.acceptance_criteria,
                "network_id": p.network_id,
                "asset_id": p.asset_id,
                "reward_amount": p.reward_amount,
                "opens_at": p.opens_at,
                "expires_at": p.expires_at,
                "max_submissions": p.max_submissions,
                "reward_slots": p.reward_slots,
                "project_id": p.project_id,
                "goal_id": p.goal_id,
            }
        )

    @staticmethod
    def _bounty_state_hash_values(bid: str, sid: str, proposal: BountyInput, status: str) -> str:
        return content_hash(
            {
                "bounty_id": bid,
                "subject_id": sid,
                "project_id": proposal.project_id,
                "goal_id": proposal.goal_id,
                "idempotency_key": proposal.idempotency_key,
                "title": proposal.title,
                "description": proposal.description,
                "acceptance_criteria_json": canonical_json(proposal.acceptance_criteria),
                "network_id": proposal.network_id,
                "asset_id": proposal.asset_id,
                "reward_amount": proposal.reward_amount,
                "opens_at": proposal.opens_at,
                "expires_at": proposal.expires_at,
                "max_submissions": proposal.max_submissions,
                "reward_slots": proposal.reward_slots,
                "status": status,
            }
        )

    @classmethod
    def _bounty_state_hash_row(cls, row: Any, status: str) -> str:
        keys = (
            "bounty_id",
            "subject_id",
            "project_id",
            "goal_id",
            "idempotency_key",
            "title",
            "description",
            "acceptance_criteria_json",
            "network_id",
            "asset_id",
            "reward_amount",
            "opens_at",
            "expires_at",
            "max_submissions",
            "reward_slots",
        )
        return content_hash({key: row[key] for key in keys} | {"status": status})

    @staticmethod
    def _submission_state_hash_values(
        sid: str,
        bid: str,
        subject: str,
        counterparty: str,
        content: str,
        evidence: list[str] | tuple[str, ...],
        recipient: str,
        key: str,
        consent_version: int,
        status: str,
        reason: str | None,
    ) -> str:
        return wallet_submission_state_hash(
            submission_id=sid,
            bounty_id=bid,
            subject_id=subject,
            counterparty=counterparty,
            content=content,
            evidence_json=canonical_json(list(evidence)),
            recipient_address=recipient,
            idempotency_key=key,
            consent_version=consent_version,
            status=status,
            decision_reason=reason,
        )

    @classmethod
    def _submission_state_hash_row(cls, row: Any, status: str, reason: str | None) -> str:
        return cls._submission_state_hash_values(
            row["submission_id"],
            row["bounty_id"],
            row["subject_id"],
            row["counterparty"],
            row["content"],
            strict_json_loads(row["evidence_json"]),
            row["recipient_address"],
            row["idempotency_key"],
            int(row["consent_version"]),
            status,
            reason,
        )

    @staticmethod
    def _order_state_hash_values(
        oid: str,
        sid: str,
        bid: str,
        subid: str,
        network: str,
        asset: str,
        recipient: str,
        amount: str,
        mode: str,
        version: int,
        key: str,
        status: str,
    ) -> str:
        return content_hash(
            {
                "order_id": oid,
                "subject_id": sid,
                "bounty_id": bid,
                "submission_id": subid,
                "network_id": network,
                "asset_id": asset,
                "recipient_address": recipient,
                "amount": amount,
                "payment_mode": mode,
                "policy_version": version,
                "idempotency_key": key,
                "status": status,
            }
        )

    @staticmethod
    def _order_temporal_hash(
        oid: str,
        sid: str,
        bid: str,
        subid: str,
        network: str,
        asset: str,
        recipient: str,
        amount: str,
        mode: str,
        version: int,
        key: str,
        status: str,
        created_at: str,
        authorized_at: str | None,
        updated_at: str,
    ) -> str:
        return content_hash(
            {
                "order_id": oid,
                "subject_id": sid,
                "bounty_id": bid,
                "submission_id": subid,
                "network_id": network,
                "asset_id": asset,
                "recipient_address": recipient,
                "amount": amount,
                "payment_mode": mode,
                "policy_version": version,
                "idempotency_key": key,
                "status": status,
                "created_at": created_at,
                "authorized_at": authorized_at,
                "updated_at": updated_at,
            }
        )

    @classmethod
    def _order_state_hash_row(
        cls,
        row: Any,
        status: str,
        *,
        authorized_at: str | None = None,
        updated_at: str | None = None,
    ) -> str:
        if _row_has_column(row, "authorized_at"):
            return cls._order_temporal_hash(
                row["order_id"],
                row["subject_id"],
                row["bounty_id"],
                row["submission_id"],
                row["network_id"],
                row["asset_id"],
                row["recipient_address"],
                row["amount"],
                row["payment_mode"],
                int(row["policy_version"]),
                row["idempotency_key"],
                status,
                row["created_at"],
                row["authorized_at"] if authorized_at is None else authorized_at,
                row["updated_at"] if updated_at is None else updated_at,
            )
        return cls._order_state_hash_values(
            row["order_id"],
            row["subject_id"],
            row["bounty_id"],
            row["submission_id"],
            row["network_id"],
            row["asset_id"],
            row["recipient_address"],
            row["amount"],
            row["payment_mode"],
            int(row["policy_version"]),
            row["idempotency_key"],
            status,
        )

    @staticmethod
    def _policy_state_hash_row(row: Any) -> str:
        return wallet_payment_policy_state_hash(
            subject_id=str(row["subject_id"]),
            mode=str(row["mode"]),
            allowed_network_ids_json=str(row["allowed_network_ids_json"]),
            allowed_asset_ids_json=str(row["allowed_asset_ids_json"]),
            per_order_limit=str(row["per_order_limit"]),
            daily_limit=str(row["daily_limit"]),
            monthly_limit=str(row["monthly_limit"]),
            daily_order_limit=strict_int(row["daily_order_limit"]),
            monthly_order_limit=strict_int(row["monthly_order_limit"]),
            min_balance=str(row["min_balance"]),
            max_observation_age_seconds=strict_int(row["max_observation_age_seconds"]),
            automatic_max_amount=str(row["automatic_max_amount"]),
            anomaly_block=int(row["anomaly_block"]),
            emergency_paused=int(row["emergency_paused"]),
            policy_version=strict_int(row["policy_version"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _legacy_policy_state_hash(row: Any) -> str:
        """Return the pre-4-B-4 policy hash used by schema versions <= 59."""
        return content_hash(
            {
                "subject_id": str(row["subject_id"]),
                "version": int(row["policy_version"]),
                "mode": str(row["mode"]),
                "updated_at": str(row["updated_at"]),
            }
        )

    @staticmethod
    def _journal_state_hash_row(row: Any) -> str:
        return wallet_journal_hash(
            {
                "journal_id": str(row["journal_id"]),
                "subject_id": row["journal_subject_id"],
                "order_id": str(row["order_id"]),
                "network_id": row["network_id"],
                "asset_id": row["asset_id"],
                "journal_type": str(row["journal_type"]),
                "amount": str(row["journal_amount"]),
                "created_at": row["journal_created_at"],
            }
        )

    @staticmethod
    def _entry_state_hash_row(row: Any) -> str:
        return wallet_entry_hash(
            {
                "entry_id": str(row["entry_id"]),
                "journal_id": str(row["journal_id"]),
                "subject_id": row["entry_subject_id"],
                "order_id": row["entry_order_id"],
                "account": str(row["account"]),
                "direction": str(row["direction"]),
                "amount": str(row["entry_amount"]),
                "created_at": row["entry_created_at"],
            }
        )

    def _effective_bounty_status(self, row: Any) -> str:
        return (
            "expired"
            if row["status"] in {"draft", "published", "closed"}
            and _parse_time(row["expires_at"]) <= _parse_time(self.clock())
            else row["status"]
        )

    @staticmethod
    def _audit(c: Any, sid: str, action: str, actor: str, payload: Mapping[str, object]) -> str:
        aid = new_id("audit")
        c.execute(
            "INSERT INTO audit_records(audit_id,subject_id,action,actor,payload_json,occurred_at) VALUES(?,?,?,?,?,?)",
            (aid, sid, action, actor, canonical_json(dict(payload)), utc_now()),
        )
        return aid

    @staticmethod
    def _bounty_from_row(r: Any, *, status: str | None = None) -> BountyRecord:
        effective_status = r["status"] if status is None else status
        state_hash = r["state_hash"]
        if effective_status != r["status"]:
            state_hash = WalletEconomyStore._bounty_state_hash_row(r, effective_status)
        return BountyRecord(
            r["bounty_id"],
            r["subject_id"],
            r["project_id"],
            r["goal_id"],
            r["idempotency_key"],
            r["title"],
            r["description"],
            tuple(strict_json_loads(r["acceptance_criteria_json"])),
            r["network_id"],
            r["asset_id"],
            r["reward_amount"],
            r["opens_at"],
            r["expires_at"],
            int(r["max_submissions"]),
            int(r["reward_slots"]),
            effective_status,
            state_hash,
            r["created_at"],
            r["updated_at"],
        )

    @staticmethod
    def _submission_from_row(r: Any) -> SubmissionRecord:
        return SubmissionRecord(
            r["submission_id"],
            r["bounty_id"],
            r["subject_id"],
            r["counterparty"],
            r["content"],
            tuple(strict_json_loads(r["evidence_json"])),
            r["recipient_address"],
            r["idempotency_key"],
            int(r["consent_version"]),
            r["status"],
            r["decision_reason"],
            r["created_at"],
            r["decided_at"],
        )

    @staticmethod
    def _policy_from_row(r: Any) -> PaymentPolicyRecord:
        return PaymentPolicyRecord(
            r["subject_id"],
            r["mode"],
            tuple(strict_json_loads(r["allowed_network_ids_json"])),
            tuple(strict_json_loads(r["allowed_asset_ids_json"])),
            r["per_order_limit"],
            r["daily_limit"],
            r["monthly_limit"],
            int(r["daily_order_limit"]),
            int(r["monthly_order_limit"]),
            r["min_balance"],
            int(r["max_observation_age_seconds"]),
            r["automatic_max_amount"],
            bool(r["anomaly_block"]),
            bool(r["emergency_paused"]),
            int(r["policy_version"]),
            r["updated_at"],
        )

    @staticmethod
    def _order_from_row(r: Any) -> PaymentOrderRecord:
        return PaymentOrderRecord(
            r["order_id"],
            r["subject_id"],
            r["bounty_id"],
            r["submission_id"],
            r["network_id"],
            r["asset_id"],
            r["recipient_address"],
            r["amount"],
            r["payment_mode"],
            int(r["policy_version"]),
            r["idempotency_key"],
            r["status"],
            r["created_at"],
            r["updated_at"],
        )

    @staticmethod
    def _policy_row(c: Any, sid: str) -> Any:
        return c.execute(
            "SELECT * FROM wallet_payment_policies WHERE subject_id=?", (sid,)
        ).fetchone()

    def _insert_policy(
        self, c: Any, sid: str, p: PaymentPolicyInput, version: int, now: str, replace: bool = False
    ) -> None:
        allowed_network_ids_json = canonical_json(p.allowed_network_ids)
        allowed_asset_ids_json = canonical_json(p.allowed_asset_ids)
        vals = (
            sid,
            p.mode,
            allowed_network_ids_json,
            allowed_asset_ids_json,
            p.per_order_limit,
            p.daily_limit,
            p.monthly_limit,
            p.daily_order_limit,
            p.monthly_order_limit,
            p.min_balance,
            p.max_observation_age_seconds,
            p.automatic_max_amount,
            int(p.anomaly_block),
            int(p.emergency_paused),
            version,
            now,
            wallet_payment_policy_state_hash(
                subject_id=sid,
                mode=p.mode,
                allowed_network_ids_json=allowed_network_ids_json,
                allowed_asset_ids_json=allowed_asset_ids_json,
                per_order_limit=p.per_order_limit,
                daily_limit=p.daily_limit,
                monthly_limit=p.monthly_limit,
                daily_order_limit=p.daily_order_limit,
                monthly_order_limit=p.monthly_order_limit,
                min_balance=p.min_balance,
                max_observation_age_seconds=p.max_observation_age_seconds,
                automatic_max_amount=p.automatic_max_amount,
                anomaly_block=p.anomaly_block,
                emergency_paused=p.emergency_paused,
                policy_version=version,
                updated_at=now,
            ),
        )
        sql = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
        c.execute(
            f"{sql} INTO wallet_payment_policies(subject_id,mode,allowed_network_ids_json,allowed_asset_ids_json,per_order_limit,daily_limit,monthly_limit,daily_order_limit,monthly_order_limit,min_balance,max_observation_age_seconds,automatic_max_amount,anomaly_block,emergency_paused,policy_version,updated_at,state_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            vals,
        )
