import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from src.domain import Actor, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def future_date(days=30):
    return (date.today() + timedelta(days=days)).isoformat()


def past_date(days=1):
    return (date.today() - timedelta(days=days)).isoformat()


class CommissionSafetyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.safety = Actor("S-1", "safety")
        self.engineer = Actor("E-1", "engineer")

    def tearDown(self):
        self.tmp.cleanup()

    def _implemented_change(self, risk_level="medium"):
        self.service.create(
            self.admin, "unit", {"name": "Reactor-1", "location": "Plant-A"}
        )
        change = self.service.create(
            self.admin,
            "change",
            {"unit_id": _last_id(self.service, "unit"), "description": "Change threshold"},
        )
        required = {"low": 1, "medium": 2, "high": 3, "critical": 4}[risk_level]
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
            {"approvals": ["A-%d" % i for i in range(required)], "permit_id": "MOC-1"},
        )
        implemented = self.service.transition(
            self.engineer,
            change["id"],
            "implement",
            {"procedure_version": "v2"},
        )
        return implemented

    def _verified_item(self, change_id, due_date=None, completed_by="O-1", verifier="V-1"):
        item = self.service.create(
            self.safety,
            "action_item",
            {
                "change_id": change_id,
                "description": "Guard interlock check",
                "owner": "O-1",
                "due_date": due_date or future_date(),
            },
        )
        self.service.transition(
            self.admin,
            item["id"],
            "complete",
            {"completed_by": completed_by, "evidence": "log-1"},
        )
        return self.service.transition(
            self.admin, item["id"], "verify", {"verifier": verifier}
        )

    def _commission(self, change_id):
        return self.service.transition(
            self.engineer, change_id, "commission", {"tests_passed": True}
        )

    def test_expired_control_blocks_commission(self):
        change = self._implemented_change()
        self._verified_item(change["id"], due_date=past_date())
        with self.assertRaises(ValidationError) as caught:
            self._commission(change["id"])
        codes = {blocker["code"] for blocker in caught.exception.blockers}
        self.assertIn("control_expired", codes)

    def test_self_verified_item_blocks_commission(self):
        change = self._implemented_change()
        self._verified_item(change["id"], completed_by="O-1", verifier="O-1")
        with self.assertRaises(ValidationError) as caught:
            self._commission(change["id"])
        codes = {blocker["code"] for blocker in caught.exception.blockers}
        self.assertIn("independent_check_required", codes)

    def test_high_risk_change_requires_safety_review(self):
        change = self._implemented_change(risk_level="high")
        self._verified_item(change["id"])
        with self.assertRaises(ValidationError) as caught:
            self._commission(change["id"])
        codes = {blocker["code"] for blocker in caught.exception.blockers}
        self.assertIn("safety_review_required", codes)

    def test_extension_then_commission_succeeds_and_keeps_original_due(self):
        change = self._implemented_change()
        item = self._verified_item(change["id"], due_date=past_date())
        extended = self.service.transition(
            self.safety,
            item["id"],
            "extend",
            {"new_due_date": future_date(10), "reason": "备件到货延期"},
        )
        self.assertEqual(extended["data"]["due_date"], past_date())
        self.assertEqual(len(extended["data"]["extensions"]), 1)
        self.assertEqual(extended["status"], "verified")
        commissioned = self._commission(change["id"])
        self.assertEqual(commissioned["status"], "commissioned")
        frozen = commissioned["data"]["frozen_controls"]
        self.assertEqual(len(frozen), 1)
        self.assertEqual(frozen[0]["action_item_id"], item["id"])

    def test_high_risk_safety_review_then_commission_succeeds(self):
        change = self._implemented_change(risk_level="critical")
        self._verified_item(change["id"])
        reviewed = self.service.transition(
            self.safety, change["id"], "safety_review", {"note": "现场措施确认"}
        )
        self.assertEqual(reviewed["status"], "implemented")
        self.assertEqual(reviewed["data"]["safety_reviewed_by"], "S-1")
        self.assertEqual(self._commission(change["id"])["status"], "commissioned")

    def test_extension_requires_safety_role(self):
        change = self._implemented_change()
        item = self._verified_item(change["id"], due_date=past_date())
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.engineer,
                item["id"],
                "extend",
                {"new_due_date": future_date(10), "reason": "x"},
            )

    def test_extension_requires_reason_and_later_date(self):
        change = self._implemented_change()
        item = self._verified_item(change["id"])
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.safety,
                item["id"],
                "extend",
                {"new_due_date": future_date(10), "reason": ""},
            )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.safety,
                item["id"],
                "extend",
                {"new_due_date": past_date(), "reason": "无效改期"},
            )

    def test_extension_after_commission_creates_new_open_todo(self):
        change = self._implemented_change()
        item = self._verified_item(change["id"])
        self._commission(change["id"])
        follow_up = self.service.transition(
            self.safety,
            item["id"],
            "extend",
            {"new_due_date": future_date(14), "reason": "投产后巡检改期"},
        )
        self.assertNotEqual(follow_up["id"], item["id"])
        self.assertEqual(follow_up["status"], "open")
        self.assertEqual(follow_up["data"]["rescheduled_from"], item["id"])
        self.assertEqual(follow_up["data"]["due_date"], future_date(14))
        # 原行动项与冻结清单均未被修改
        frozen_item = self.service.get(item["id"])
        self.assertEqual(frozen_item["data"].get("extensions"), None)
        commissioned_change = self.service.get(change["id"])
        frozen_ids = [
            entry["action_item_id"]
            for entry in commissioned_change["data"]["frozen_controls"]
        ]
        self.assertEqual(frozen_ids, [item["id"]])

    def test_create_action_item_requires_due_date(self):
        change = self._implemented_change()
        with self.assertRaises(ValidationError):
            self.service.create(
                self.safety,
                "action_item",
                {"change_id": change["id"], "description": "no date", "owner": "O-1"},
            )


def _last_id(service, kind):
    return service.list(kind)[-1]["id"]


if __name__ == "__main__":
    unittest.main()
