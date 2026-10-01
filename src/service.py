import hashlib
import json
from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind, payload)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def merge_offline(self, actor, records):
        """Merge field records by a stable (source_id, record_id) identity.

        Each record is committed independently so a batch can partially fail;
        re-submitting the same record is idempotent and never creates a
        duplicate. Changed content is kept as a new revision: a draft record
        is revised in place, while a confirmed record's change is held as a
        pending conflict and cannot overwrite confirmed content.
        """
        if not isinstance(records, list):
            raise ValidationError("records must be a list")
        allowed = self.rules.CREATE_ROLES.get("offline_record", ("admin",))
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)
        batch_id = str(uuid4())
        items = []
        processed = 0
        failed = 0
        for raw in records:
            identity = self._record_identity(raw)
            try:
                result = self._merge_one_offline(actor, raw)
                items.append({
                    "source_id": identity[0],
                    "record_id": identity[1],
                    "change": result["change"],
                    "revision": result["revision"],
                    "entity": result["entity"],
                    "error": None,
                })
                processed += 1
            except (ValidationError, ConflictError, NotFoundError) as exc:
                items.append({
                    "source_id": identity[0],
                    "record_id": identity[1],
                    "change": "failed",
                    "revision": None,
                    "entity": None,
                    "error": str(exc),
                })
                failed += 1
        return {
            "batch_id": batch_id,
            "total": len(records),
            "processed": processed,
            "failed": failed,
            "items": items,
        }

    @staticmethod
    def _record_identity(raw):
        if not isinstance(raw, dict):
            return "", ""
        return str(raw.get("source_id", "")).strip(), str(raw.get("record_id", "")).strip()

    def _merge_one_offline(self, actor, raw):
        source_id, record_id = self._record_identity(raw)
        if not source_id or not record_id:
            raise ValidationError("source_id and record_id are required")
        payload = raw.get("payload")
        if not isinstance(payload, dict):
            raise ValidationError("offline payload must be an object")
        digest = hashlib.sha256((source_id + "\0" + record_id).encode("utf-8")).hexdigest()[:32]
        entity_id = "offline-" + digest
        content_hash = hashlib.sha256(
            json.dumps(raw, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        existing = self.repository.get_entity(entity_id)
        if not existing:
            self.rules.validate_create(actor, "offline_record", dict(raw), self._lookup)
            entity = self.repository.create_entity(
                entity_id, "offline_record", "merged", dict(raw), actor.user_id
            )
            self.repository.add_record_revision(
                entity_id, 1, "active", dict(raw), content_hash, "offline_merge", actor.user_id
            )
            self.audit.record(
                entity_id, actor, "merge_offline", None, "merged",
                {"source_id": source_id, "record_id": record_id, "revision": 1},
            )
            return {"change": "created", "revision": 1, "entity": entity}
        latest = self.repository.latest_record_revision(entity_id)
        latest_hash = latest["content_hash"] if latest else None
        if latest_hash == content_hash:
            return {"change": "unchanged", "revision": latest["revision"] if latest else 1, "entity": existing}
        next_rev = (latest["revision"] + 1) if latest else 1
        if existing["status"] == "confirmed":
            self.repository.add_record_revision(
                entity_id, next_rev, "pending_conflict", dict(raw), content_hash,
                "offline_merge", actor.user_id,
            )
            updated = self.repository.update_entity(
                entity_id, existing["version"], "conflict", dict(existing["data"])
            )
            self.audit.record(
                entity_id, actor, "merge_offline", "confirmed", "conflict",
                {"source_id": source_id, "record_id": record_id, "revision": next_rev, "conflict": True},
            )
            return {"change": "conflict", "revision": next_rev, "entity": updated}
        if existing["status"] == "conflict":
            if latest:
                self.repository.update_record_revision_status(entity_id, latest["revision"], "superseded")
            self.repository.add_record_revision(
                entity_id, next_rev, "pending_conflict", dict(raw), content_hash,
                "offline_merge", actor.user_id,
            )
            updated = self.repository.update_entity(
                entity_id, existing["version"], "conflict", dict(existing["data"])
            )
            self.audit.record(
                entity_id, actor, "merge_offline", "conflict", "conflict",
                {"source_id": source_id, "record_id": record_id, "revision": next_rev, "conflict": True},
            )
            return {"change": "conflict", "revision": next_rev, "entity": updated}
        # merged draft: revise in place, keeping the old revision in history
        if latest:
            self.repository.update_record_revision_status(entity_id, latest["revision"], "superseded")
        self.repository.add_record_revision(
            entity_id, next_rev, "active", dict(raw), content_hash, "offline_merge", actor.user_id
        )
        updated = self.repository.update_entity(
            entity_id, existing["version"], existing["status"], dict(raw)
        )
        self.audit.record(
            entity_id, actor, "merge_offline", existing["status"], existing["status"],
            {"source_id": source_id, "record_id": record_id, "revision": next_rev},
        )
        return {"change": "revised", "revision": next_rev, "entity": updated}

    def resolve_record_conflict(self, actor, entity_id, decision):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        next_status, patch = self.rules.validate_transition(
            actor, entity, "resolve_conflict", {"decision": decision}, self._lookup
        )
        pending = self.repository.latest_record_revision(entity_id)
        if not pending or pending["status"] != "pending_conflict":
            raise ConflictError("no pending conflict to resolve")
        if decision == "accept":
            for rev in self.repository.list_record_revisions(entity_id):
                if rev["status"] == "active":
                    self.repository.update_record_revision_status(entity_id, rev["revision"], "superseded")
            self.repository.update_record_revision_status(entity_id, pending["revision"], "active")
            updated = self.repository.update_entity(
                entity_id, entity["version"], "confirmed", dict(pending["content"])
            )
        else:
            self.repository.update_record_revision_status(entity_id, pending["revision"], "rejected")
            updated = self.repository.update_entity(
                entity_id, entity["version"], "confirmed", dict(entity["data"])
            )
        self.audit.record(
            entity_id, actor, "resolve_conflict", entity["status"], "confirmed",
            {"decision": decision, "revision": pending["revision"]},
        )
        return updated

    def record_revisions(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return self.repository.list_record_revisions(entity_id)

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
