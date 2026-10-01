import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, NotFoundError, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine())
        self.admin = Actor("admin", "admin")
        self.field = Actor("field-1", "field")

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, kind, data, actor=None):
        return self.service.create(actor or self.admin, kind, data)

    def act(self, entity, action, data=None, version=None, actor=None):
        return self.service.transition(actor or self.admin, entity["id"], action, data or {}, version)

    def record(self, record_id, recorded_at, payload, source="field-a"):
        return {"source_id": source, "record_id": record_id, "recorded_at": recorded_at, "payload": payload}

    def merge(self, records, batch_id=None):
        return self.service.merge_offline(self.field, records, batch_id=batch_id)

    # --- 修订账 -------------------------------------------------------------

    def test_newer_revision_replaces_older_draft(self):
        old = self.record("7", "2026-09-27T09:00:00Z", {"gas": 10})
        new = self.record("7", "2026-09-27T10:00:00Z", {"gas": 25})
        # 先到旧稿、再到新修订：旧稿不能覆盖新修订，新修订必须成为当前值
        self.merge([old])
        outcome = self.merge([new])
        entity = outcome["items"][0]
        self.assertEqual(entity["data"]["payload"], {"gas": 25})
        self.assertEqual(entity["data"]["current_revision"], entity["data"]["revisions"][1]["rev_id"])
        self.assertEqual([r["state"] for r in entity["data"]["revisions"]], ["superseded", "applied"])

    def test_older_draft_after_new_revision_is_kept_stale_not_applied(self):
        old = self.record("7", "2026-09-27T09:00:00Z", {"gas": 10})
        new = self.record("7", "2026-09-27T10:00:00Z", {"gas": 25})
        # 回网乱序：新稿先到、旧稿后到
        self.merge([new])
        outcome = self.merge([old])
        entity = outcome["items"][0]
        self.assertEqual(entity["data"]["payload"], {"gas": 25})
        self.assertEqual(outcome["results"][0]["state"], "stale")
        self.assertEqual(entity["data"]["revisions"][1]["state"], "stale")

    def test_same_timestamp_divergent_payloads_become_pending_conflict(self):
        a = self.record("7", "2026-09-27T10:00:00Z", {"gas": 10})
        b = self.record("7", "2026-09-27T10:00:00Z", {"gas": 99})
        outcome = self.merge([a, b])
        entity = outcome["items"][0]
        self.assertEqual(entity["status"], "conflict")
        self.assertEqual(len(entity["data"]["pending_conflicts"]), 1)
        self.assertEqual(entity["data"]["pending_conflicts"][0]["reason"], "same_timestamp_divergent")

    def test_confirmed_content_cannot_be_overwritten(self):
        a = self.record("7", "2026-09-27T10:00:00Z", {"gas": 10})
        b = self.record("7", "2026-09-27T11:00:00Z", {"gas": 99})
        first = self.merge([a])
        rev_id = first["items"][0]["data"]["current_revision"]
        confirmed = self.act(first["items"][0], "confirm", {"rev_id": rev_id})
        self.assertTrue(confirmed["data"]["confirmed"])
        # 更新的修订也不能覆盖已确认内容，只能挂冲突
        outcome = self.merge([b])
        entity = outcome["items"][0]
        self.assertEqual(entity["data"]["payload"], {"gas": 10})
        self.assertEqual(entity["status"], "conflict")
        self.assertEqual(entity["data"]["pending_conflicts"][0]["reason"], "differs_from_confirmed")
        # 显式确认后才采纳新修订
        new_rev = entity["data"]["revisions"][1]["rev_id"]
        resolved = self.act(entity, "confirm", {"rev_id": new_rev})
        self.assertEqual(resolved["data"]["payload"], {"gas": 99})
        self.assertEqual(resolved["status"], "merged")
        self.assertEqual(resolved["data"]["pending_conflicts"], [])
        self.assertEqual(resolved["data"]["revisions"][0]["state"], "superseded")

    def test_confirm_unknown_revision_rejected(self):
        first = self.merge([self.record("7", "2026-09-27T10:00:00Z", {"gas": 10})])
        with self.assertRaises(ValidationError):
            self.act(first["items"][0], "confirm", {"rev_id": "nope"})

    # --- 整批续传 -----------------------------------------------------------

    def test_batch_retry_only_processes_unfinished_items(self):
        good = self.record("1", "2026-09-27T10:00:00Z", {"ok": True})
        bad = self.record("2", "not-a-timestamp", {"ok": False})
        first = self.merge([good, bad], batch_id="batch-1")
        self.assertEqual(first["counts"]["created"], 1)
        self.assertEqual(first["counts"]["failed"], 1)

        status = self.service.batch_status("batch-1")
        self.assertEqual(len(status["unfinished"]), 1)

        # 原样重试：成功项跳过不重复处理，失败项重新尝试（仍失败）
        retry = self.merge([good, bad], batch_id="batch-1")
        self.assertEqual(retry["counts"]["skipped"], 1)
        self.assertEqual(retry["counts"]["failed"], 1)
        self.assertEqual(len(self.service.list("offline_record")), 1)

        # 修正同一未完成项后重试：失败键消失，记录落库
        fixed = self.record("2", "2026-09-27T10:05:00Z", {"ok": True})
        final = self.merge([good, fixed], batch_id="batch-1")
        self.assertEqual(final["counts"]["skipped"], 1)
        self.assertEqual(final["counts"]["created"], 1)
        status = self.service.batch_status("batch-1")
        self.assertEqual(status["unfinished"], [])

    def test_unknown_batch_is_404(self):
        with self.assertRaises(NotFoundError):
            self.service.batch_status("missing")

    # --- 通风恢复门禁 -------------------------------------------------------

    def _incident_ready_to_close(self):
        incident = self.create("incident", {"area_code": "M-01", "severity": "high", "summary": "gas"})
        for action in ("begin_evacuation", "search", "stabilize", "recover"):
            incident = self.act(incident, action)
        return incident

    def test_restore_requires_passing_retest(self):
        vent = self.create("ventilation", {"name": "fan", "area_code": "M-02", "capacity": 10})
        vent = self.act(vent, "stop", {"reason": "power outage"})
        with self.assertRaises(ValidationError):
            self.act(vent, "restore", {"tested_at": "2026-09-27T11:00:00Z", "test_result": "fail"})

    def test_restore_blocked_until_area_evacuated(self):
        worker = self.create("worker", {"name": "Li Wei", "location_code": "M-03", "team": "A"})
        self.act(worker, "mark_missing")
        vent = self.create("ventilation", {"name": "fan", "area_code": "M-03", "capacity": 10})
        vent = self.act(vent, "stop", {"reason": "power outage"})
        with self.assertRaises(ConflictError):
            self.act(vent, "restore", {"tested_at": "2026-09-27T11:00:00Z", "test_result": "pass"})
        # 人员撤离（救出）后复测通过，方可恢复通风
        located = self.act(worker, "locate", {"located_at": "2026-09-27T10:30:00Z"})
        incident = self._incident_ready_to_close()
        rescued = self.act(located, "rescue", {"incident_id": incident["id"]})
        self.assertEqual(rescued["status"], "rescued")
        vent = self.act(vent, "restore", {"tested_at": "2026-09-27T11:00:00Z", "test_result": "pass"})
        self.assertEqual(vent["status"], "running")

    # --- 避险硐室容量门禁 ---------------------------------------------------

    def test_refuge_cannot_be_occupied_over_capacity(self):
        refuge = self.create("refuge", {"location_code": "R-1", "capacity": 2})
        refuge = self.act(refuge, "occupy", {"count": 2})
        self.assertEqual(refuge["data"]["occupants"], 2)
        with self.assertRaises(ConflictError):
            self.act(refuge, "occupy", {"count": 1})

    def test_refuge_release_returns_available_when_empty(self):
        refuge = self.create("refuge", {"location_code": "R-1", "capacity": 5})
        refuge = self.act(refuge, "occupy", {"count": 3})
        refuge = self.act(refuge, "release", {"count": 2})
        self.assertEqual(refuge["status"], "occupied")
        self.assertEqual(refuge["data"]["occupants"], 1)
        refuge = self.act(refuge, "release", {})
        self.assertEqual(refuge["status"], "available")
        self.assertEqual(refuge["data"]["occupants"], 0)
        with self.assertRaises(ValidationError):
            self.act(refuge, "release", {"count": 1})

    # --- 关闭事件拦截 -------------------------------------------------------

    def test_close_blocked_by_unresolved_offline_conflict(self):
        incident = self._incident_ready_to_close()
        self.merge([
            self.record("9", "2026-09-27T10:00:00Z", {"gas": 1}),
            self.record("9", "2026-09-27T10:00:00Z", {"gas": 2}),
        ])
        with self.assertRaises(ConflictError):
            self.act(incident, "close", {"summary": "all clear"})

    def test_close_blocked_by_unrestored_ventilation(self):
        incident = self._incident_ready_to_close()
        vent = self.create("ventilation", {"name": "fan", "area_code": "M-99", "capacity": 10})
        self.act(vent, "stop", {"reason": "power outage"})
        with self.assertRaises(ConflictError):
            self.act(incident, "close", {"summary": "all clear"})


if __name__ == "__main__":
    unittest.main()
