from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from .domain import (ConflictError, ValidationError, ensure_role,
                     normalize_severity, require_bool, require_number,
                     require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, EVIDENCE_ENTITY,
                    EVIDENCE_ROLES, LOAN_ROLES, RECORD_ROLES, RETURN_ROLES,
                    REVIEW_ROLES, TITLE, VIEW_ROLES, completion_blockers,
                    custody_blockers, escalation_required,
                    evidence_status_after_return, priority_score,
                    response_deadline_hours, return_check_result,
                    role_for_transition, validate_checkout,
                    validate_return_verifier, validate_reviewable,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        blockers += custody_blockers(target, self.repository.open_loan_count(item_id))
        if blockers:
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    # ---------- 证据保管链 ----------

    def register_evidence(self, item_id: int, payload: Dict[str, Any], actor: str,
                          role: str) -> Dict[str, Any]:
        ensure_role(role, EVIDENCE_ROLES)
        actor = require_text(actor, "actor", 100)
        evidence_no = require_text(payload.get("evidence_no"), "evidence_no", 100)
        title = require_text(payload.get("title"), "title", 200)
        collector = require_text(payload.get("collector"), "collector", 100)
        seal_no = require_text(payload.get("seal_no"), "seal_no", 100)
        medium = require_text(payload.get("medium"), "medium", 100)
        location = require_text(payload.get("location"), "location", 200)
        digest = require_text(payload.get("digest"), "digest", 128)
        evidence = self.repository.create_evidence(
            item_id, evidence_no, title, collector, seal_no, medium, location,
            digest, actor)
        self.repository.append_audit("evidence_register", EVIDENCE_ENTITY,
                                     evidence["id"], actor, {
                                         "item_id": item_id, "evidence_no": evidence_no,
                                         "seal_no": seal_no, "medium": medium,
                                         "collector": collector, "location": location,
                                     })
        return evidence

    def checkout_evidence(self, evidence_id: int, payload: Dict[str, Any], actor: str,
                          role: str) -> Dict[str, Any]:
        ensure_role(role, LOAN_ROLES)
        actor = require_text(actor, "actor", 100)
        borrower = require_text(payload.get("borrower"), "borrower", 100)
        purpose = require_text(payload.get("purpose"), "purpose", 500)
        due_at = self._resolve_due(payload)
        evidence = self.repository.get_evidence(evidence_id)
        validate_checkout(evidence["status"])
        loan = self.repository.create_loan(evidence_id, borrower, purpose, due_at, actor)
        self.repository.append_audit("evidence_checkout", EVIDENCE_ENTITY, evidence_id,
                                     actor, {"loan_id": loan["id"], "borrower": borrower,
                                             "purpose": purpose, "due_at": due_at})
        return loan

    def return_loan(self, loan_id: int, payload: Dict[str, Any], actor: str,
                    role: str) -> Dict[str, Any]:
        ensure_role(role, RETURN_ROLES)
        actor = require_text(actor, "actor", 100)
        loan = self.repository.get_loan(loan_id)
        validate_return_verifier(actor, loan["borrower"])
        seal_intact = require_bool(payload.get("seal_intact"), "seal_intact")
        digest = require_text(payload.get("digest"), "digest", 128)
        note = payload.get("note")
        if note is not None:
            note = require_text(note, "note", 500)
        evidence = self.repository.get_evidence(loan["evidence_id"])
        result = return_check_result(seal_intact, digest, evidence["digest"])
        updated = self.repository.record_return(
            loan_id, actor, digest, seal_intact, note,
            evidence_status_after_return(result))
        self.repository.append_audit("evidence_return", EVIDENCE_ENTITY, evidence["id"],
                                     actor, {"loan_id": loan_id, "result": result,
                                             "seal_intact": seal_intact})
        updated["result"] = result
        return updated

    def review_evidence(self, evidence_id: int, payload: Dict[str, Any], actor: str,
                        role: str) -> Dict[str, Any]:
        ensure_role(role, REVIEW_ROLES)
        actor = require_text(actor, "actor", 100)
        conclusion = require_text(payload.get("conclusion"), "conclusion", 1000)
        evidence = self.repository.get_evidence(evidence_id)
        validate_reviewable(evidence["status"])
        review = self.repository.create_review(evidence_id, conclusion, actor)
        self.repository.append_audit("evidence_review", EVIDENCE_ENTITY, evidence_id,
                                     actor, {"review_id": review["id"],
                                             "conclusion": conclusion})
        return review

    def correct_review(self, review_id: int, payload: Dict[str, Any], actor: str,
                       role: str) -> Dict[str, Any]:
        ensure_role(role, REVIEW_ROLES)
        actor = require_text(actor, "actor", 100)
        conclusion = require_text(payload.get("conclusion"), "conclusion", 1000)
        review = self.repository.correct_review(review_id, conclusion, actor)
        self.repository.append_audit("review_correct", EVIDENCE_ENTITY,
                                     review["evidence_id"], actor,
                                     {"review_id": review["id"], "supersedes": review_id,
                                      "conclusion": conclusion})
        return review

    def list_evidence(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_evidence(item_id)

    def list_loans(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_loans(item_id)

    def list_reviews(self, evidence_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_reviews(evidence_id)

    def custody_board(self, role: str) -> list:
        self._view(role)
        now = datetime.now(timezone.utc)
        board: Dict[int, Dict[str, Any]] = {}

        def entry(row: Dict[str, Any]) -> Dict[str, Any]:
            return board.setdefault(row["item_id"], {
                "item_id": row["item_id"], "title": row["item_title"],
                "status": row["item_status"], "pending_returns": [],
                "pending_reviews": []})

        for row in self.repository.pending_returns():
            entry(row)["pending_returns"].append({
                "loan_id": row["loan_id"], "evidence_id": row["evidence_id"],
                "evidence_no": row["evidence_no"], "title": row["evidence_title"],
                "seal_no": row["seal_no"], "borrower": row["borrower"],
                "purpose": row["purpose"], "due_at": row["due_at"],
                "overdue": self._overdue(row["due_at"], now)})
        for row in self.repository.pending_reviews():
            entry(row)["pending_reviews"].append({
                "evidence_id": row["evidence_id"], "evidence_no": row["evidence_no"],
                "title": row["evidence_title"], "seal_no": row["seal_no"]})
        return list(board.values())

    @staticmethod
    def _resolve_due(payload: Dict[str, Any]) -> str:
        due_at = payload.get("due_at")
        if due_at is not None:
            due_at = require_text(due_at, "due_at", 40)
            try:
                datetime.fromisoformat(due_at)
            except ValueError as exc:
                raise ValidationError("due_at必须是ISO时间") from exc
            return due_at
        hours = payload.get("due_in_hours")
        if hours is None:
            raise ValidationError("归还时限不能为空")
        hours = require_number(hours, "due_in_hours", 0.000001)
        due = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=hours)
        return due.isoformat()

    @staticmethod
    def _overdue(due_at: str, now: datetime) -> bool:
        try:
            due = datetime.fromisoformat(due_at)
        except (TypeError, ValueError):
            return False
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        return due < now

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
