import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES


def _payload(no="EV-1", seal="SEAL-1", digest="digest-1"):
    return {"evidence_no": no, "title": "现场照片", "collector": "小李",
            "seal_no": seal, "medium": "电子", "location": "证物柜A",
            "digest": digest}


class CustodyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self._item("CUST-1")

    def tearDown(self):
        self.repo.close(); self.tmp.cleanup()

    def _item(self, ref):
        return self.service.create_item(
            {"title": "custody item", "description": "evidence custody",
             "severity": "serious", "quantity": 5, "threshold": 10,
             "external_ref": ref}, "creator", "reporter")

    def _register(self, item=None, **kw):
        item = item or self.item
        return self.service.register_evidence(
            item["id"], _payload(**kw), "registrar", "investigator")

    def _close(self, item):
        current = self.service.get_item(item["id"], "viewer")
        for target in STATES[1:]:
            current = self.service.transition(
                current["id"], target, current["version"], "reviewer",
                TRANSITION_ROLES[target][0])
        return current

    def test_register_validates_and_scopes_numbers(self):
        ev = self._register()
        self.assertEqual(ev["status"], "in_custody")
        with self.assertRaises(ConflictError):
            self._register()  # 同一事故下自编号重复
        other = self._item("CUST-2")
        ev2 = self._register(item=other, seal="SEAL-2")  # 不同事故可用同一自编号
        self.assertEqual(ev2["evidence_no"], "EV-1")
        with self.assertRaises(ValidationError):
            self.service.register_evidence(
                self.item["id"], {"evidence_no": "EV-9"}, "registrar", "investigator")
        with self.assertRaises(PermissionDenied):
            self.service.register_evidence(
                self.item["id"], _payload(no="EV-8", seal="SEAL-8"),
                "registrar", "viewer")

    def test_seal_no_reuse_only_after_closure(self):
        self._register(seal="SEAL-X")
        other = self._item("CUST-3")
        with self.assertRaises(ConflictError):
            self._register(item=other, no="EV-9", seal="SEAL-X")  # 未结事故占用封存号
        closed = self._close(self.item)
        self.assertEqual(closed["status"], "closed")
        ev = self._register(item=other, no="EV-9", seal="SEAL-X")  # 关闭后放行
        self.assertEqual(ev["seal_no"], "SEAL-X")

    def test_checkout_return_and_verifier(self):
        ev = self._register()
        with self.assertRaises(ValidationError):
            self.service.checkout_evidence(
                ev["id"], {"borrower": "李四", "purpose": "无时限"},
                "clerk", "investigator")
        loan = self.service.checkout_evidence(
            ev["id"], {"borrower": "张三", "purpose": "庭审举证", "due_in_hours": 24},
            "clerk", "investigator")
        self.assertEqual(loan["status"], "open")
        self.assertEqual(
            self.service.list_evidence(self.item["id"], "viewer")[0]["status"], "on_loan")
        with self.assertRaises(ConflictError):
            self.service.checkout_evidence(
                ev["id"], {"borrower": "李四", "purpose": "再次借阅", "due_in_hours": 1},
                "clerk", "investigator")  # 借出中不可再借
        with self.assertRaises(PermissionDenied):
            self.service.return_loan(
                loan["id"], {"seal_intact": True, "digest": "digest-1"},
                "张三", "investigator")  # 借阅人不能自核
        done = self.service.return_loan(
            loan["id"], {"seal_intact": True, "digest": "digest-1"},
            "王五", "safety_manager")
        self.assertEqual(done["status"], "returned")
        self.assertEqual(done["result"], "normal")
        self.assertEqual(
            self.service.list_evidence(self.item["id"], "viewer")[0]["status"],
            "in_custody")
        with self.assertRaises(ConflictError):
            self.service.return_loan(
                loan["id"], {"seal_intact": True, "digest": "digest-1"},
                "王五", "investigator")  # 重复归还

    def test_abnormal_return_review_and_correction(self):
        ev = self._register()
        loan = self.service.checkout_evidence(
            ev["id"], {"borrower": "张三", "purpose": "比对", "due_in_hours": 12},
            "clerk", "investigator")
        done = self.service.return_loan(
            loan["id"], {"seal_intact": False, "digest": "tampered"},
            "王五", "investigator")
        self.assertEqual(done["result"], "abnormal")
        self.assertEqual(
            self.service.list_evidence(self.item["id"], "viewer")[0]["status"],
            "pending_review")
        with self.assertRaises(ConflictError):
            self.service.checkout_evidence(
                ev["id"], {"borrower": "李四", "purpose": "x", "due_in_hours": 1},
                "clerk", "investigator")  # 待核不可外借
        with self.assertRaises(PermissionDenied):
            self.service.review_evidence(
                ev["id"], {"conclusion": "封条破损但内容一致"}, "mgr", "investigator")
        review = self.service.review_evidence(
            ev["id"], {"conclusion": "封条破损但内容一致"}, "mgr", "safety_manager")
        self.assertEqual(review["status"], "active")
        self.assertEqual(
            self.service.list_evidence(self.item["id"], "viewer")[0]["status"],
            "in_custody")
        with self.assertRaises(ConflictError):
            self.service.review_evidence(
                ev["id"], {"conclusion": "重复复核"}, "mgr", "safety_manager")  # 非待核
        new = self.service.correct_review(
            review["id"], {"conclusion": "更正：摘要不一致，已重新封存"},
            "mgr2", "safety_manager")
        self.assertEqual(new["status"], "active")
        reviews = self.service.list_reviews(ev["id"], "viewer")
        self.assertEqual(len(reviews), 2)  # 原复核留档
        self.assertEqual(reviews[0]["status"], "invalidated")
        self.assertEqual(reviews[0]["superseded_by"], new["id"])
        with self.assertRaises(ConflictError):
            self.service.correct_review(
                review["id"], {"conclusion": "再次更正"}, "mgr", "safety_manager")
        self.assertTrue(self.repo.verify_audit_chain())

    def test_closure_requires_all_loans_returned(self):
        ev = self._register()
        loan = self.service.checkout_evidence(
            ev["id"], {"borrower": "张三", "purpose": "保险理赔", "due_in_hours": 48},
            "clerk", "investigator")
        current = self.service.get_item(self.item["id"], "viewer")
        for target in STATES[1:-1]:
            current = self.service.transition(
                current["id"], target, current["version"], "reviewer",
                TRANSITION_ROLES[target][0])
        with self.assertRaises(ConflictError):
            self.service.transition(
                current["id"], STATES[-1], current["version"], "reviewer",
                TRANSITION_ROLES[STATES[-1]][0])  # 外借未归还不得关闭
        self.service.return_loan(
            loan["id"], {"seal_intact": True, "digest": "digest-1"},
            "王五", "investigator")
        current = self.service.get_item(self.item["id"], "viewer")
        closed = self.service.transition(
            current["id"], STATES[-1], current["version"], "reviewer",
            TRANSITION_ROLES[STATES[-1]][0])
        self.assertEqual(closed["status"], "closed")

    def test_custody_board_groups_by_item(self):
        ev1 = self._register()
        ev2 = self._register(no="EV-2", seal="SEAL-2")
        self.service.checkout_evidence(
            ev1["id"], {"borrower": "张三", "purpose": "鉴定",
                        "due_at": "2000-01-01T00:00:00+00:00"}, "clerk", "investigator")
        loan2 = self.service.checkout_evidence(
            ev2["id"], {"borrower": "李四", "purpose": "复印", "due_in_hours": 6},
            "clerk", "investigator")
        self.service.return_loan(
            loan2["id"], {"seal_intact": True, "digest": "wrong"},
            "王五", "investigator")  # 摘要不符转待核
        board = self.service.custody_board("viewer")
        self.assertEqual(len(board), 1)
        entry = board[0]
        self.assertEqual(entry["item_id"], self.item["id"])
        self.assertEqual(len(entry["pending_returns"]), 1)
        self.assertEqual(entry["pending_returns"][0]["borrower"], "张三")
        self.assertTrue(entry["pending_returns"][0]["overdue"])  # 已过归还时限
        self.assertEqual(len(entry["pending_reviews"]), 1)
        self.assertEqual(entry["pending_reviews"][0]["evidence_no"], "EV-2")


if __name__ == "__main__":
    unittest.main()
