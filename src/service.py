from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, ensure_role, normalize_severity,
                     require_bool, require_future_time, require_number,
                     require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, EVIDENCE_ROLES,
                    LOAN_ROLES, RECORD_ROLES, RETURN_ROLES, REVIEW_ROLES,
                    TERMINAL_STATES, TITLE, VIEW_ROLES, completion_blockers,
                    custody_blockers, escalation_required,
                    evidence_state_after_return, is_overdue, priority_score,
                    response_deadline_hours, return_result, role_for_transition,
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

    def register_evidence(self, item_id: int, payload: Dict[str, Any], actor: str,
                          role: str) -> Dict[str, Any]:
        ensure_role(role, EVIDENCE_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        if item["status"] in TERMINAL_STATES:
            raise ConflictError("事故已关闭，不能归档新材料")
        evidence_no = require_text(payload.get("evidence_no"), "evidence_no", 100)
        name = require_text(payload.get("name"), "name", 200)
        collector = require_text(payload.get("collector"), "collector", 100)
        seal_no = require_text(payload.get("seal_no"), "seal_no", 100)
        medium = require_text(payload.get("medium"), "medium", 100)
        location = require_text(payload.get("location"), "location", 200)
        digest = require_text(payload.get("digest"), "digest", 200)
        evidence = self.repository.create_evidence(
            item_id, evidence_no, name, collector, seal_no, medium, location,
            digest, actor)
        self.repository.append_audit("evidence_register", ENTITY, item_id, actor, {
            "evidence_id": evidence["id"], "evidence_no": evidence_no,
            "collector": collector, "seal_no": seal_no, "medium": medium,
            "location": location,
        })
        return evidence

    def list_evidence(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_evidence(item_id)

    def borrow_evidence(self, evidence_id: int, payload: Dict[str, Any], actor: str,
                        role: str) -> Dict[str, Any]:
        ensure_role(role, LOAN_ROLES)
        actor = require_text(actor, "actor", 100)
        purpose = require_text(payload.get("purpose"), "purpose", 500)
        due_at = require_future_time(payload.get("due_at"), "due_at")
        evidence = self.repository.get_evidence(evidence_id)
        loan = self.repository.create_loan(evidence_id, actor, purpose, due_at, actor)
        self.repository.append_audit("evidence_loan", ENTITY, evidence["item_id"],
                                     actor, {
            "loan_id": loan["id"], "evidence_id": evidence_id,
            "evidence_no": evidence["evidence_no"], "borrower": actor,
            "purpose": purpose, "due_at": due_at,
        })
        return loan

    def return_loan(self, loan_id: int, payload: Dict[str, Any], actor: str,
                    role: str) -> Dict[str, Any]:
        ensure_role(role, RETURN_ROLES)
        actor = require_text(actor, "actor", 100)
        seal_intact = require_bool(payload.get("seal_intact"), "seal_intact")
        digest_match = require_bool(payload.get("digest_match"), "digest_match")
        note = payload.get("note")
        if note is not None:
            note = require_text(note, "note", 500)
        loan = self.repository.get_loan(loan_id)
        if loan["borrower"] == actor:
            raise ConflictError("归还须由借阅人以外的另一人核对")
        result = return_result(seal_intact, digest_match)
        new_status = evidence_state_after_return(result)
        updated = self.repository.return_loan(loan_id, actor, result, new_status, note)
        evidence = self.repository.get_evidence(updated["evidence_id"])
        self.repository.append_audit("evidence_return", ENTITY, evidence["item_id"],
                                     actor, {
            "loan_id": loan_id, "evidence_id": evidence["id"],
            "evidence_no": evidence["evidence_no"], "verifier": actor,
            "result": result, "evidence_status": new_status, "note": note,
        })
        return updated

    def review_evidence(self, evidence_id: int, payload: Dict[str, Any], actor: str,
                        role: str) -> Dict[str, Any]:
        ensure_role(role, REVIEW_ROLES)
        actor = require_text(actor, "actor", 100)
        conclusion = require_text(payload.get("conclusion"), "conclusion", 1000)
        review = self.repository.create_review(evidence_id, conclusion, actor)
        evidence = self.repository.get_evidence(evidence_id)
        self.repository.append_audit("evidence_review", ENTITY, evidence["item_id"],
                                     actor, {
            "review_id": review["id"], "evidence_id": evidence_id,
            "evidence_no": evidence["evidence_no"], "conclusion": conclusion,
        })
        return review

    def correct_review(self, review_id: int, payload: Dict[str, Any], actor: str,
                       role: str) -> Dict[str, Any]:
        ensure_role(role, REVIEW_ROLES)
        actor = require_text(actor, "actor", 100)
        conclusion = require_text(payload.get("conclusion"), "conclusion", 1000)
        old = self.repository.get_review(review_id)
        new = self.repository.correct_review(review_id, conclusion, actor)
        evidence = self.repository.get_evidence(old["evidence_id"])
        self.repository.append_audit("evidence_review_correct", ENTITY,
                                     evidence["item_id"], actor, {
            "review_id": new["id"], "superseded_review_id": review_id,
            "evidence_id": evidence["id"], "evidence_no": evidence["evidence_no"],
            "conclusion": conclusion,
        })
        return new

    def list_reviews(self, evidence_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_reviews(evidence_id)

    def custody_board(self, role: str) -> list:
        self._view(role)
        board: Dict[int, Dict[str, Any]] = {}

        def entry(item_id: int, title: str, status: str) -> Dict[str, Any]:
            return board.setdefault(item_id, {
                "item_id": item_id, "title": title, "status": status,
                "pending_returns": [], "pending_reviews": [],
            })

        for loan in self.repository.pending_returns():
            entry(loan["item_id"], loan["item_title"],
                  loan["item_status"])["pending_returns"].append({
                "loan_id": loan["id"], "evidence_id": loan["evidence_id"],
                "evidence_no": loan["evidence_no"], "name": loan["evidence_name"],
                "borrower": loan["borrower"], "purpose": loan["purpose"],
                "due_at": loan["due_at"], "overdue": is_overdue(loan["due_at"]),
            })
        for evidence in self.repository.pending_reviews():
            entry(evidence["item_id"], evidence["item_title"],
                  evidence["item_status"])["pending_reviews"].append({
                "evidence_id": evidence["id"], "evidence_no": evidence["evidence_no"],
                "name": evidence["name"], "seal_no": evidence["seal_no"],
                "since": evidence["updated_at"],
            })
        return [board[key] for key in sorted(board)]

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
