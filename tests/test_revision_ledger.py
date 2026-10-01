import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class RevisionLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine())
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, kind, data):
        return self.service.create(self.actor, kind, data)

    def act(self, entity, action, data=None, version=None):
        return self.service.transition(self.actor, entity["id"], action, data or {}, version)

    def record(self, source, record_id, payload, recorded_at="2026-09-27T10:00:00Z"):
        return {
            "source_id": source,
            "record_id": record_id,
            "recorded_at": recorded_at,
            "payload": payload,
        }

    def test_draft_record_is_revised_in_place(self):
        first = self.service.merge_offline(self.actor, [self.record("f", "1", {"v": 1})])
        entity = first["items"][0]["entity"]
        self.assertEqual(first["items"][0]["change"], "created")

        second = self.service.merge_offline(self.actor, [self.record("f", "1", {"v": 2})])
        self.assertEqual(second["items"][0]["change"], "revised")
        self.assertEqual(second["items"][0]["entity"]["data"]["payload"]["v"], 2)

        revisions = self.service.record_revisions(entity["id"])
        self.assertEqual([r["revision"] for r in revisions], [1, 2])
        self.assertEqual(revisions[0]["status"], "superseded")
        self.assertEqual(revisions[1]["status"], "active")

    def test_confirmed_record_change_becomes_pending_conflict(self):
        self.service.merge_offline(self.actor, [self.record("f", "1", {"v": 1})])
        entity = self.service.list("offline_record")[0]
        self.act(entity, "confirm")

        changed = self.service.merge_offline(self.actor, [self.record("f", "1", {"v": 2})])
        self.assertEqual(changed["items"][0]["change"], "conflict")
        self.assertEqual(changed["items"][0]["entity"]["status"], "conflict")
        # confirmed content is not overwritten
        self.assertEqual(changed["items"][0]["entity"]["data"]["payload"]["v"], 1)

        conflicts = [r for r in self.service.list("offline_record") if r["status"] == "conflict"]
        self.assertEqual(len(conflicts), 1)

    def test_resolve_conflict_accept_applies_new_revision(self):
        self.service.merge_offline(self.actor, [self.record("f", "1", {"v": 1})])
        entity = self.service.list("offline_record")[0]
        self.act(entity, "confirm")
        self.service.merge_offline(self.actor, [self.record("f", "1", {"v": 2})])

        entity = self.service.list("offline_record")[0]
        resolved = self.service.resolve_record_conflict(self.actor, entity["id"], "accept")
        self.assertEqual(resolved["status"], "confirmed")
        self.assertEqual(resolved["data"]["payload"]["v"], 2)

        revisions = self.service.record_revisions(entity["id"])
        self.assertEqual(revisions[-1]["status"], "active")
        self.assertEqual(revisions[-1]["content"]["payload"]["v"], 2)

    def test_resolve_conflict_reject_keeps_confirmed_content(self):
        self.service.merge_offline(self.actor, [self.record("f", "1", {"v": 1})])
        entity = self.service.list("offline_record")[0]
        self.act(entity, "confirm")
        self.service.merge_offline(self.actor, [self.record("f", "1", {"v": 2})])

        entity = self.service.list("offline_record")[0]
        resolved = self.service.resolve_record_conflict(self.actor, entity["id"], "reject")
        self.assertEqual(resolved["status"], "confirmed")
        self.assertEqual(resolved["data"]["payload"]["v"], 1)

        revisions = self.service.record_revisions(entity["id"])
        self.assertEqual(revisions[-1]["status"], "rejected")

    def test_batch_partial_failure_and_retry(self):
        good = self.record("f", "1", {"v": 1})
        bad = {"source_id": "f", "record_id": "2", "recorded_at": "2026-09-27T10:00:00Z"}
        result = self.service.merge_offline(self.actor, [good, bad])
        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["items"][0]["change"], "created")
        self.assertEqual(result["items"][1]["change"], "failed")

        # retry: good record is not repeated, bad record is fixed and processed
        fixed = self.record("f", "2", {"v": 9})
        retry = self.service.merge_offline(self.actor, [good, fixed])
        self.assertEqual(retry["processed"], 2)
        self.assertEqual(retry["failed"], 0)
        self.assertEqual(retry["items"][0]["change"], "unchanged")
        self.assertEqual(retry["items"][1]["change"], "created")
        self.assertEqual(len(self.service.list("offline_record")), 2)

    def test_ventilation_restore_requires_passing_retest(self):
        vent = self.create("ventilation", {"name": "fan", "area_code": "M", "capacity": 10})
        self.act(vent, "stop")
        with self.assertRaises(ValidationError):
            self.act(vent, "restore", {"tested_at": "2026-09-27T11:00:00Z", "test_result": "fail"})
        with self.assertRaises(ValidationError):
            self.act(vent, "restore", {"tested_at": "2026-09-27T11:00:00Z"})

    def test_ventilation_restore_blocked_until_area_evacuated(self):
        incident = self.create("incident", {"area_code": "M", "severity": "high", "summary": "x"})
        for action in ("begin_evacuation", "search", "stabilize", "recover"):
            incident = self.act(incident, action)
        worker = self.create("worker", {"name": "Li Wei", "location_code": "M", "team": "A"})
        self.act(worker, "mark_missing")
        vent = self.create("ventilation", {"name": "fan", "area_code": "M", "capacity": 10})
        self.act(vent, "stop")

        with self.assertRaises(ConflictError):
            self.act(vent, "restore", {"tested_at": "2026-09-27T11:00:00Z", "test_result": "pass"})

        self.act(worker, "evacuate")
        restored = self.act(vent, "restore", {"tested_at": "2026-09-27T11:00:00Z", "test_result": "pass"})
        self.assertEqual(restored["status"], "running")

    def test_refuge_cannot_be_occupied_over_capacity(self):
        refuge = self.create("refuge", {"location_code": "R-1", "capacity": 2})
        refuge = self.act(refuge, "occupy", {"occupant": "a"})
        self.assertEqual(refuge["data"]["occupancy"], 1)
        refuge = self.act(refuge, "occupy", {"occupant": "b"})
        self.assertEqual(refuge["data"]["occupancy"], 2)
        with self.assertRaises(ConflictError):
            self.act(refuge, "occupy", {"occupant": "c"})
        refuge = self.act(refuge, "release")
        self.assertEqual(refuge["data"]["occupancy"], 1)
        self.assertEqual(refuge["status"], "occupied")
        refuge = self.act(refuge, "release")
        self.assertEqual(refuge["data"]["occupancy"], 0)
        self.assertEqual(refuge["status"], "available")

    def test_close_blocked_by_pending_conflict(self):
        incident = self.create("incident", {"area_code": "M", "severity": "high", "summary": "x"})
        for action in ("begin_evacuation", "search", "stabilize", "recover"):
            incident = self.act(incident, action)
        worker = self.create("worker", {"name": "Li Wei", "location_code": "M", "team": "A"})
        self.act(worker, "mark_missing")
        self.act(worker, "evacuate")
        vent = self.create("ventilation", {"name": "fan", "area_code": "M", "capacity": 10})
        self.act(vent, "stop")
        self.act(vent, "restore", {"tested_at": "2026-09-27T11:00:00Z", "test_result": "pass"})

        self.service.merge_offline(self.actor, [self.record("f", "1", {"v": 1})])
        record = self.service.list("offline_record")[0]
        self.act(record, "confirm")
        self.service.merge_offline(self.actor, [self.record("f", "1", {"v": 2})])

        with self.assertRaises(ConflictError):
            self.act(incident, "close", {"summary": "done"})

        record = self.service.list("offline_record")[0]
        self.service.resolve_record_conflict(self.actor, record["id"], "reject")
        closed = self.act(incident, "close", {"summary": "all clear"})
        self.assertEqual(closed["status"], "closed")


if __name__ == "__main__":
    unittest.main()
