from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, STATES


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    evidence_no TEXT NOT NULL,
                    title TEXT NOT NULL,
                    collector TEXT NOT NULL,
                    seal_no TEXT NOT NULL,
                    medium TEXT NOT NULL,
                    location TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'in_custody'
                        CHECK(status IN ('in_custody','on_loan','pending_review')),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, evidence_no)
                );
                CREATE TABLE IF NOT EXISTS loans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    evidence_id INTEGER NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
                    borrower TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    checked_out_by TEXT NOT NULL,
                    checked_out_at TEXT NOT NULL,
                    returned_at TEXT,
                    return_verified_by TEXT,
                    return_digest TEXT,
                    seal_intact INTEGER,
                    return_note TEXT,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','returned'))
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    evidence_id INTEGER NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
                    loan_id INTEGER REFERENCES loans(id),
                    conclusion TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active','invalidated')),
                    superseded_by INTEGER,
                    reviewed_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    # ---------- 证据保管链 ----------

    def create_evidence(self, item_id: int, evidence_no: str, title: str,
                        collector: str, seal_no: str, medium: str, location: str,
                        digest: str, actor: str) -> Dict[str, Any]:
        self.get_item(item_id)
        now = utc_now()
        with self._lock, self.conn:
            occupied = self.conn.execute(
                """SELECT e.id FROM evidence e JOIN items i ON e.item_id=i.id
                   WHERE e.seal_no=? AND i.status<>'closed' LIMIT 1""",
                (seal_no,),
            ).fetchone()
            if occupied is not None:
                raise ConflictError("封存号已被未结事故占用")
            try:
                cur = self.conn.execute(
                    """INSERT INTO evidence(item_id, evidence_no, title, collector, seal_no,
                       medium, location, digest, status, created_by, created_at)
                       VALUES(?,?,?,?,?,?,?,?,'in_custody',?,?)""",
                    (item_id, evidence_no, title, collector, seal_no, medium, location,
                     digest, actor, now),
                )
                evidence_id = int(cur.lastrowid)
            except sqlite3.IntegrityError as exc:
                raise ConflictError("该事故下自编号已存在") from exc
        return self.get_evidence(evidence_id)

    def get_evidence(self, evidence_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM evidence WHERE id=?", (evidence_id,)).fetchone()
        if row is None:
            raise NotFoundError("材料不存在")
        return dict(row)

    def list_evidence(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM evidence WHERE item_id=? ORDER BY id", (item_id,)).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _loan(row: sqlite3.Row) -> Dict[str, Any]:
        loan = dict(row)
        if loan.get("seal_intact") is not None:
            loan["seal_intact"] = bool(loan["seal_intact"])
        return loan

    def create_loan(self, evidence_id: int, borrower: str, purpose: str,
                    due_at: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT status FROM evidence WHERE id=?", (evidence_id,)).fetchone()
            if row is None:
                raise NotFoundError("材料不存在")
            if row["status"] != "in_custody":
                raise ConflictError("材料当前不可外借")
            cur = self.conn.execute(
                """INSERT INTO loans(evidence_id, borrower, purpose, due_at,
                   checked_out_by, checked_out_at, status) VALUES(?,?,?,?,?,?,'open')""",
                (evidence_id, borrower, purpose, due_at, actor, now),
            )
            loan_id = int(cur.lastrowid)
            self.conn.execute(
                "UPDATE evidence SET status='on_loan' WHERE id=?", (evidence_id,))
        return self.get_loan(loan_id)

    def get_loan(self, loan_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
        if row is None:
            raise NotFoundError("调阅记录不存在")
        return self._loan(row)

    def list_loans(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                """SELECT l.*, e.evidence_no, e.title AS evidence_title
                   FROM loans l JOIN evidence e ON l.evidence_id=e.id
                   WHERE e.item_id=? ORDER BY l.id""",
                (item_id,),
            ).fetchall()
        return [self._loan(row) for row in rows]

    def record_return(self, loan_id: int, verifier: str, digest: str,
                      seal_intact: bool, note: Optional[str],
                      new_status: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
            if row is None:
                raise NotFoundError("调阅记录不存在")
            if row["status"] != "open":
                raise ConflictError("该调阅已归还")
            self.conn.execute(
                """UPDATE loans SET status='returned', returned_at=?, return_verified_by=?,
                   return_digest=?, seal_intact=?, return_note=? WHERE id=?""",
                (now, verifier, digest, 1 if seal_intact else 0, note, loan_id),
            )
            self.conn.execute(
                "UPDATE evidence SET status=? WHERE id=?",
                (new_status, row["evidence_id"]),
            )
        return self.get_loan(loan_id)

    def open_loan_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                """SELECT COUNT(*) AS n FROM loans l JOIN evidence e ON l.evidence_id=e.id
                   WHERE e.item_id=? AND l.status='open'""",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def create_review(self, evidence_id: int, conclusion: str,
                      actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT status FROM evidence WHERE id=?", (evidence_id,)).fetchone()
            if row is None:
                raise NotFoundError("材料不存在")
            if row["status"] != "pending_review":
                raise ConflictError("材料不在待核状态")
            loan = self.conn.execute(
                "SELECT id FROM loans WHERE evidence_id=? ORDER BY id DESC LIMIT 1",
                (evidence_id,),
            ).fetchone()
            loan_id = int(loan["id"]) if loan else None
            cur = self.conn.execute(
                """INSERT INTO reviews(evidence_id, loan_id, conclusion, status,
                   reviewed_by, created_at) VALUES(?,?,?,'active',?,?)""",
                (evidence_id, loan_id, conclusion, actor, now),
            )
            review_id = int(cur.lastrowid)
            self.conn.execute(
                "UPDATE evidence SET status='in_custody' WHERE id=?", (evidence_id,))
        return self.get_review(review_id)

    def get_review(self, review_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
        if row is None:
            raise NotFoundError("复核记录不存在")
        return dict(row)

    def list_reviews(self, evidence_id: int) -> List[Dict[str, Any]]:
        self.get_evidence(evidence_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM reviews WHERE evidence_id=? ORDER BY id",
                (evidence_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def correct_review(self, review_id: int, conclusion: str,
                       actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
            if row is None:
                raise NotFoundError("复核记录不存在")
            if row["status"] != "active":
                raise ConflictError("该复核已失效，不能更正")
            cur = self.conn.execute(
                """INSERT INTO reviews(evidence_id, loan_id, conclusion, status,
                   reviewed_by, created_at) VALUES(?,?,?,'active',?,?)""",
                (row["evidence_id"], row["loan_id"], conclusion, actor, now),
            )
            new_id = int(cur.lastrowid)
            self.conn.execute(
                "UPDATE reviews SET status='invalidated', superseded_by=? WHERE id=?",
                (new_id, review_id),
            )
        return self.get_review(new_id)

    def pending_returns(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT l.id AS loan_id, l.evidence_id, l.borrower, l.purpose, l.due_at,
                          e.evidence_no, e.title AS evidence_title, e.seal_no,
                          i.id AS item_id, i.title AS item_title, i.status AS item_status
                   FROM loans l
                   JOIN evidence e ON l.evidence_id=e.id
                   JOIN items i ON e.item_id=i.id
                   WHERE l.status='open' ORDER BY i.id, l.id"""
            ).fetchall()
        return [dict(row) for row in rows]

    def pending_reviews(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT e.id AS evidence_id, e.evidence_no, e.title AS evidence_title,
                          e.seal_no, i.id AS item_id, i.title AS item_title,
                          i.status AS item_status
                   FROM evidence e JOIN items i ON e.item_id=i.id
                   WHERE e.status='pending_review' ORDER BY i.id, e.id"""
            ).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self.conn.close()
