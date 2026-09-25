import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from src.domain import Actor, PermissionDenied, PreStartupSafetyBlocked, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def _future(days):
    return (date.today() + timedelta(days=days)).isoformat()


def _past(days):
    return (date.today() - timedelta(days=days)).isoformat()


class PreStartupSafetyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.safety = Actor("S-9", "safety")
        self.engineer = Actor("E-9", "engineer")
        self.verifier = Actor("V-9", "verifier")

    def tearDown(self):
        self.tmp.cleanup()

    def _implemented_change(self, risk_level="medium"):
        unit = self.service.create(
            self.admin, "unit", {"name": "U-1", "location": "Plant-A"}
        )
        change = self.service.create(
            self.admin,
            "change",
            {"unit_id": unit["id"], "description": "replace pump seal"},
        )
        self.service.transition(
            self.admin,
            change["id"],
            "assess",
            {"risk_level": risk_level, "analyst": "E-1"},
        )
        self.service.transition(
            self.admin,
            change["id"],
            "approve",
            {"approvals": ["S-1", "S-2", "S-3"], "permit_id": "MOC-2"},
        )
        self.service.transition(
            self.admin,
            change["id"],
            "implement",
            {"procedure_version": "v3"},
        )
        return change["id"]

    def _verified_item(self, change_id, valid_until, completed_by="O-1", verifier="V-1"):
        item = self.service.create(
            self.admin,
            "action_item",
            {
                "change_id": change_id,
                "description": "isolate energy",
                "owner": "O-1",
                "valid_until": valid_until,
            },
        )
        self.service.transition(
            self.admin,
            item["id"],
            "complete",
            {"completed_by": completed_by, "evidence": "photo"},
        )
        self.service.transition(
            self.admin, item["id"], "verify", {"verifier": verifier}
        )
        return item

    def test_create_action_item_requires_valid_until(self):
        change_id = self._implemented_change()
        with self.assertRaises(ValidationError):
            self.service.create(
                self.admin,
                "action_item",
                {"change_id": change_id, "description": "x", "owner": "O-1"},
            )

    def test_commission_blocked_when_control_expired(self):
        change_id = self._implemented_change()
        item = self._verified_item(change_id, _past(1))
        with self.assertRaises(PreStartupSafetyBlocked) as caught:
            self.service.transition(
                self.admin, change_id, "commission", {"tests_passed": True}
            )
        codes = {block["code"] for block in caught.exception.blockers}
        self.assertIn("expired", codes)
        self.assertEqual(
            caught.exception.blockers[0]["action_item"], item["id"]
        )

    def test_safety_officer_extension_keeps_original_deadline(self):
        change_id = self._implemented_change()
        item = self._verified_item(change_id, _past(2))
        original = item["data"]["valid_until"]

        extended = self.service.transition(
            self.safety,
            item["id"],
            "extend",
            {"valid_until": _future(14), "reason": "备件到货延迟"},
        )
        self.assertEqual(extended["status"], "verified")
        self.assertEqual(extended["data"]["original_valid_until"], original)
        self.assertEqual(extended["data"]["valid_until"], _future(14))
        self.assertEqual(len(extended["data"]["extensions"]), 1)
        self.assertEqual(extended["data"]["extensions"][0]["extended_by"], "S-9")

        # 再次延期，原期限继续保留
        again = self.service.transition(
            self.safety,
            item["id"],
            "extend",
            {"valid_until": _future(28), "reason": "窗口检修安排"},
        )
        self.assertEqual(again["data"]["original_valid_until"], original)
        self.assertEqual(len(again["data"]["extensions"]), 2)

        # 延期后可以投产
        commissioned = self.service.transition(
            self.admin, change_id, "commission", {"tests_passed": True}
        )
        self.assertEqual(commissioned["status"], "commissioned")

    def test_extension_requires_reason_and_later_date(self):
        change_id = self._implemented_change()
        item = self._verified_item(change_id, _future(10))
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.safety,
                item["id"],
                "extend",
                {"valid_until": _future(20), "reason": ""},
            )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.safety,
                item["id"],
                "extend",
                {"valid_until": _future(5), "reason": "提前"},
            )
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.verifier,
                item["id"],
                "extend",
                {"valid_until": _future(20), "reason": "无权操作"},
            )

    def test_commission_blocked_when_verifier_is_completer(self):
        change_id = self._implemented_change()
        self._verified_item(
            change_id, _future(10), completed_by="O-1", verifier="O-1"
        )
        with self.assertRaises(PreStartupSafetyBlocked) as caught:
            self.service.transition(
                self.admin, change_id, "commission", {"tests_passed": True}
            )
        codes = {block["code"] for block in caught.exception.blockers}
        self.assertIn("independent_check_missing", codes)

    def test_high_risk_change_requires_safety_review(self):
        change_id = self._implemented_change(risk_level="high")
        self._verified_item(change_id, _future(10))
        with self.assertRaises(PreStartupSafetyBlocked) as caught:
            self.service.transition(
                self.admin, change_id, "commission", {"tests_passed": True}
            )
        codes = {block["code"] for block in caught.exception.blockers}
        self.assertIn("safety_review_missing", codes)

        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.engineer,
                change_id,
                "safety_review",
                {"note": "工程师不能复核"},
            )

        self.service.transition(
            self.safety,
            change_id,
            "safety_review",
            {"note": "现场复核通过"},
        )
        commissioned = self.service.transition(
            self.admin, change_id, "commission", {"tests_passed": True}
        )
        self.assertEqual(commissioned["data"]["safety_review_by"], "S-9")

    def test_commission_freezes_controls_and_later_extension_becomes_todo(self):
        change_id = self._implemented_change()
        item = self._verified_item(change_id, _future(10))
        commissioned = self.service.transition(
            self.admin, change_id, "commission", {"tests_passed": True}
        )
        self.assertEqual(len(commissioned["data"]["frozen_controls"]), 1)

        # 投产后再对冻结项延期：原记录不变，生成新待办
        follow_up = self.service.transition(
            self.safety,
            item["id"],
            "extend",
            {"valid_until": _future(30), "reason": "投产后巡检发现需复测"},
        )
        self.assertNotEqual(follow_up["id"], item["id"])
        self.assertEqual(follow_up["status"], "open")
        self.assertEqual(follow_up["data"]["follow_up_of"], item["id"])
        self.assertEqual(follow_up["data"]["valid_until"], _future(30))
        self.assertEqual(follow_up["data"]["change_id"], change_id)

        frozen_item = self.service.get(item["id"])
        self.assertEqual(frozen_item["data"]["valid_until"], _future(10))
        self.assertNotIn("extensions", frozen_item["data"])

        todos = self.service.list("action_item")
        self.assertEqual(len(todos), 2)


if __name__ == "__main__":
    unittest.main()
