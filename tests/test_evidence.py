import tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES
def future(hours=24): return (datetime.now(timezone.utc)+timedelta(hours=hours)).isoformat()
def evidence_payload(**kw):
    base={"evidence_no":"E-1","name":"现场照片","collector":"小张","seal_no":"SEAL-1","medium":"electronic","location":"柜子A-1","digest":"sha256:abc"}
    base.update(kw); return base
class EvidenceCustodyTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.repo=Repository(str(Path(self.tmp.name)/"test.db")); self.service=Service(self.repo)
        self.item=self.service.create_item({"title":"事故A","description":"证据保管链测试","severity":"serious","quantity":5,"threshold":10,"external_ref":"EV-A"},"creator","reporter")
    def tearDown(self): self.repo.close(); self.tmp.cleanup()
    def _close(self,item):
        current=item
        for target in STATES[1:]: current=self.service.transition(current["id"],target,current["version"],"reviewer",TRANSITION_ROLES[target][0])
        return current
    def test_register_duplicate_and_seal_occupancy(self):
        ev=self.service.register_evidence(self.item["id"],evidence_payload(),"reg","investigator")
        self.assertEqual(ev["status"],"stored")
        with self.assertRaises(ConflictError): self.service.register_evidence(self.item["id"],evidence_payload(seal_no="SEAL-9"),"reg","investigator")
        with self.assertRaises(ConflictError): self.service.register_evidence(self.item["id"],evidence_payload(evidence_no="E-2"),"reg","investigator")
        other=self.service.create_item({"title":"事故B","description":"另一起","severity":"minor","quantity":1,"threshold":10,"external_ref":"EV-B"},"creator","reporter")
        with self.assertRaises(ConflictError): self.service.register_evidence(other["id"],evidence_payload(),"reg","investigator")
        with self.assertRaises(PermissionDenied): self.service.register_evidence(self.item["id"],evidence_payload(evidence_no="E-9",seal_no="SEAL-9"),"reg","viewer")
    def test_seal_reuse_after_close_and_no_register_on_closed(self):
        self.service.register_evidence(self.item["id"],evidence_payload(),"reg","investigator")
        self._close(self.service.get_item(self.item["id"],"viewer"))
        other=self.service.create_item({"title":"事故B","description":"另一起","severity":"minor","quantity":1,"threshold":10,"external_ref":"EV-B"},"creator","reporter")
        ev=self.service.register_evidence(other["id"],evidence_payload(),"reg","investigator")
        self.assertEqual(ev["seal_no"],"SEAL-1")
        with self.assertRaises(ConflictError): self.service.register_evidence(self.item["id"],evidence_payload(evidence_no="E-9",seal_no="SEAL-9"),"reg","investigator")
    def test_loan_and_normal_return(self):
        ev=self.service.register_evidence(self.item["id"],evidence_payload(),"reg","investigator")
        with self.assertRaises(ValidationError): self.service.borrow_evidence(ev["id"],{"purpose":"","due_at":future()},"borrower","investigator")
        with self.assertRaises(ValidationError): self.service.borrow_evidence(ev["id"],{"purpose":"查看","due_at":"2020-01-01T00:00:00+00:00"},"borrower","investigator")
        with self.assertRaises(PermissionDenied): self.service.borrow_evidence(ev["id"],{"purpose":"查看","due_at":future()},"borrower","viewer")
        loan=self.service.borrow_evidence(ev["id"],{"purpose":"调查取证","due_at":future()},"borrower","investigator")
        self.assertEqual(loan["status"],"open"); self.assertEqual(loan["borrower"],"borrower")
        self.assertEqual(self.service.list_evidence(self.item["id"],"viewer")[0]["status"],"on_loan")
        with self.assertRaises(ConflictError): self.service.borrow_evidence(ev["id"],{"purpose":"重复借","due_at":future()},"other","investigator")
        with self.assertRaises(ConflictError): self.service.return_loan(loan["id"],{"seal_intact":True,"digest_match":True},"borrower","investigator")
        with self.assertRaises(ValidationError): self.service.return_loan(loan["id"],{"seal_intact":"yes","digest_match":True},"checker","investigator")
        done=self.service.return_loan(loan["id"],{"seal_intact":True,"digest_match":True},"checker","safety_manager")
        self.assertEqual(done["status"],"returned"); self.assertEqual(done["return_result"],"normal"); self.assertEqual(done["return_verifier"],"checker")
        self.assertEqual(self.service.list_evidence(self.item["id"],"viewer")[0]["status"],"stored")
        with self.assertRaises(ConflictError): self.service.return_loan(loan["id"],{"seal_intact":True,"digest_match":True},"checker","safety_manager")
    def test_abnormal_return_review_and_correction(self):
        ev=self.service.register_evidence(self.item["id"],evidence_payload(),"reg","investigator")
        loan=self.service.borrow_evidence(ev["id"],{"purpose":"比对","due_at":future()},"borrower","investigator")
        self.service.return_loan(loan["id"],{"seal_intact":False,"digest_match":True},"checker","investigator")
        self.assertEqual(self.service.list_evidence(self.item["id"],"viewer")[0]["status"],"pending_review")
        with self.assertRaises(ConflictError): self.service.borrow_evidence(ev["id"],{"purpose":"待核不可借","due_at":future()},"x","investigator")
        with self.assertRaises(PermissionDenied): self.service.review_evidence(ev["id"],{"conclusion":"ok"},"mgr","investigator")
        review=self.service.review_evidence(ev["id"],{"conclusion":"封条破损为运输造成，内容未变"},"mgr","safety_manager")
        self.assertEqual(review["status"],"active")
        self.assertEqual(self.service.list_evidence(self.item["id"],"viewer")[0]["status"],"stored")
        with self.assertRaises(ConflictError): self.service.review_evidence(ev["id"],{"conclusion":"重复复核"},"mgr","safety_manager")
        new=self.service.correct_review(review["id"],{"conclusion":"更正：封条破损系人为，已补充封存"},"mgr2","safety_manager")
        self.assertEqual(new["status"],"active"); self.assertEqual(new["supersedes"],review["id"])
        reviews=self.service.list_reviews(ev["id"],"viewer")
        self.assertEqual(len(reviews),2)
        old=[r for r in reviews if r["id"]==review["id"]][0]
        self.assertEqual(old["status"],"invalidated")
        with self.assertRaises(ConflictError): self.service.correct_review(review["id"],{"conclusion":"再次更正"},"mgr","safety_manager")
    def test_close_blocked_until_returned_and_board(self):
        ev=self.service.register_evidence(self.item["id"],evidence_payload(),"reg","investigator")
        ev2=self.service.register_evidence(self.item["id"],evidence_payload(evidence_no="E-2",seal_no="SEAL-2"),"reg","investigator")
        loan=self.service.borrow_evidence(ev["id"],{"purpose":"外借一","due_at":future()},"borrower","investigator")
        loan2=self.service.borrow_evidence(ev2["id"],{"purpose":"外借二","due_at":future()},"borrower2","investigator")
        board=self.service.custody_board("viewer")
        self.assertEqual(len(board),1); self.assertEqual(board[0]["item_id"],self.item["id"])
        self.assertEqual(len(board[0]["pending_returns"]),2); self.assertEqual(board[0]["pending_reviews"],[])
        self.assertFalse(board[0]["pending_returns"][0]["overdue"])
        current=self.service.get_item(self.item["id"],"viewer")
        for target in STATES[1:-1]: current=self.service.transition(current["id"],target,current["version"],"reviewer",TRANSITION_ROLES[target][0])
        with self.assertRaises(ConflictError): self.service.transition(current["id"],STATES[-1],current["version"],"reviewer",TRANSITION_ROLES[STATES[-1]][0])
        self.service.return_loan(loan["id"],{"seal_intact":True,"digest_match":True},"checker","investigator")
        self.service.return_loan(loan2["id"],{"seal_intact":True,"digest_match":False},"checker","investigator")
        board=self.service.custody_board("viewer")
        self.assertEqual(len(board),1); self.assertEqual(board[0]["pending_returns"],[])
        self.assertEqual(len(board[0]["pending_reviews"]),1); self.assertEqual(board[0]["pending_reviews"][0]["evidence_no"],"E-2")
        self.service.review_evidence(ev2["id"],{"conclusion":"摘要不符系誊抄错误，已重新封存"},"mgr","safety_manager")
        self.assertEqual(self.service.custody_board("viewer"),[])
        current=self.service.get_item(self.item["id"],"viewer")
        closed=self.service.transition(current["id"],STATES[-1],current["version"],"reviewer",TRANSITION_ROLES[STATES[-1]][0])
        self.assertEqual(closed["status"],STATES[-1])
        self.assertTrue(self.repo.verify_audit_chain())
if __name__=="__main__": unittest.main()
