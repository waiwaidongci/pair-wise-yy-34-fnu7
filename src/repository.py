from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, STATES, TERMINAL_STATES


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
                    name TEXT NOT NULL,
                    collector TEXT NOT NULL,
                    seal_no TEXT NOT NULL,
                    medium TEXT NOT NULL,
                    location TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'stored'
                        CHECK(status IN ('stored','on_loan','pending_review')),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(item_id, evidence_no)
                );
                CREATE INDEX IF NOT EXISTS ix_evidence_seal ON evidence(seal_no);
                CREATE TABLE IF NOT EXISTS loans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    evidence_id INTEGER NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
                    borrower TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','returned')),
                    returned_at TEXT,
                    return_verifier TEXT,
                    return_result TEXT
                        CHECK(return_result IS NULL OR return_result IN ('normal','abnormal')),
                    return_note TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_loans_evidence ON loans(evidence_id, status);
                CREATE TABLE IF NOT EXISTS reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    evidence_id INTEGER NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
                    conclusion TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active','invalidated')),
                    supersedes INTEGER REFERENCES reviews(id),
                    created_by TEXT NOT NULL,
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

    def create_evidence(self, item_id: int, evidence_no: str, name: str,
                        collector: str, seal_no: str, medium: str, location: str,
                        digest: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        terminal = ",".join("'" + s + "'" for s in TERMINAL_STATES)
        with self._lock, self.conn:
            occupied = self.conn.execute(
                f"""SELECT e.id FROM evidence e JOIN items i ON i.id=e.item_id
                    WHERE e.seal_no=? AND i.status NOT IN ({terminal}) LIMIT 1""",
                (seal_no,),
            ).fetchone()
            if occupied is not None:
                raise ConflictError("封存号已被未结事故占用，登记退回")
            try:
                cur = self.conn.execute(
                    """INSERT INTO evidence(item_id, evidence_no, name, collector, seal_no,
                       medium, location, digest, status, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,'stored',?,?,?)""",
                    (item_id, evidence_no, name, collector, seal_no, medium, location,
                     digest, actor, now, now),
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
            raise NotFoundError("证据不存在")
        return dict(row)

    def list_evidence(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM evidence WHERE item_id=? ORDER BY id", (item_id,)).fetchall()
        return [dict(row) for row in rows]

    def create_loan(self, evidence_id: int, borrower: str, purpose: str,
                    due_at: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT status FROM evidence WHERE id=?", (evidence_id,)).fetchone()
            if row is None:
                raise NotFoundError("证据不存在")
            if row["status"] != "stored":
                raise ConflictError("材料当前不在库，不能调阅")
            cur = self.conn.execute(
                """INSERT INTO loans(evidence_id, borrower, purpose, due_at, status,
                   created_by, created_at) VALUES(?,?,?,?,'open',?,?)""",
                (evidence_id, borrower, purpose, due_at, actor, now),
            )
            loan_id = int(cur.lastrowid)
            self.conn.execute(
                "UPDATE evidence SET status='on_loan', updated_at=? WHERE id=?",
                (now, evidence_id))
        return self.get_loan(loan_id)

    def get_loan(self, loan_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
        if row is None:
            raise NotFoundError("调阅记录不存在")
        return dict(row)

    def return_loan(self, loan_id: int, verifier: str, result: str,
                    new_status: str, note: Optional[str]) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
            if row is None:
                raise NotFoundError("调阅记录不存在")
            if row["status"] != "open":
                raise ConflictError("该调阅已归还")
            self.conn.execute(
                """UPDATE loans SET status='returned', returned_at=?, return_verifier=?,
                   return_result=?, return_note=? WHERE id=?""",
                (now, verifier, result, note, loan_id))
            self.conn.execute(
                "UPDATE evidence SET status=?, updated_at=? WHERE id=?",
                (new_status, now, row["evidence_id"]))
        return self.get_loan(loan_id)

    def open_loan_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                """SELECT COUNT(*) AS n FROM loans l JOIN evidence e ON e.id=l.evidence_id
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
                raise NotFoundError("证据不存在")
            if row["status"] != "pending_review":
                raise ConflictError("材料不在待核状态")
            cur = self.conn.execute(
                """INSERT INTO reviews(evidence_id, conclusion, status, supersedes,
                   created_by, created_at) VALUES(?,?,'active',NULL,?,?)""",
                (evidence_id, conclusion, actor, now),
            )
            review_id = int(cur.lastrowid)
            self.conn.execute(
                "UPDATE evidence SET status='stored', updated_at=? WHERE id=?",
                (now, evidence_id))
        return self.get_review(review_id)

    def get_review(self, review_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
        if row is None:
            raise NotFoundError("复核记录不存在")
        return dict(row)

    def correct_review(self, review_id: int, conclusion: str,
                       actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
            if row is None:
                raise NotFoundError("复核记录不存在")
            if row["status"] != "active":
                raise ConflictError("原复核已失效，不能再次更正")
            self.conn.execute(
                "UPDATE reviews SET status='invalidated' WHERE id=?", (review_id,))
            cur = self.conn.execute(
                """INSERT INTO reviews(evidence_id, conclusion, status, supersedes,
                   created_by, created_at) VALUES(?,?,'active',?,?,?)""",
                (row["evidence_id"], conclusion, review_id, actor, now),
            )
            new_id = int(cur.lastrowid)
        return self.get_review(new_id)

    def list_reviews(self, evidence_id: int) -> List[Dict[str, Any]]:
        self.get_evidence(evidence_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM reviews WHERE evidence_id=? ORDER BY id",
                (evidence_id,)).fetchall()
        return [dict(row) for row in rows]

    def pending_returns(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT l.*, e.item_id, e.evidence_no, e.name AS evidence_name,
                          i.title AS item_title, i.status AS item_status
                   FROM loans l JOIN evidence e ON e.id=l.evidence_id
                   JOIN items i ON i.id=e.item_id
                   WHERE l.status='open' ORDER BY l.due_at"""
            ).fetchall()
        return [dict(row) for row in rows]

    def pending_reviews(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT e.*, i.title AS item_title, i.status AS item_status
                   FROM evidence e JOIN items i ON i.id=e.item_id
                   WHERE e.status='pending_review' ORDER BY e.updated_at"""
            ).fetchall()
        return [dict(row) for row in rows]

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

    def close(self) -> None:
        with self._lock:
            self.conn.close()
