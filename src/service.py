import copy
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

    @staticmethod
    def _offline_entity_id(source_id, record_id):
        digest = hashlib.sha256((source_id + "\0" + record_id).encode("utf-8")).hexdigest()[:32]
        return "offline-" + digest

    @staticmethod
    def _revision_id(source_id, record_id, recorded_at, payload):
        basis = json.dumps(
            {"source_id": source_id, "record_id": record_id, "recorded_at": recorded_at, "payload": payload},
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _record_identity_key(source_id, record_id):
        digest = hashlib.sha256((source_id + "\0" + record_id).encode("utf-8")).hexdigest()[:32]
        return "pending-" + digest

    @staticmethod
    def _invalid_item_key(raw):
        basis = json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str)
        return "invalid-" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]

    def _save_offline_entity(self, actor, entity_id, status, record, action, detail):
        existing = self.repository.get_entity(entity_id)
        if existing:
            updated = self.repository.update_entity(entity_id, existing["version"], status, record)
            self.audit.record(entity_id, actor, action, existing["status"], status, detail)
            return updated
        entity = self.repository.create_entity(
            entity_id, "offline_record", status, record, actor.user_id
        )
        self.audit.record(entity_id, actor, "merge_offline", None, status, detail)
        return entity

    def _merge_one(self, actor, raw):
        """Apply one field record to the revision ledger. Returns (entity, outcome, detail)."""
        if not isinstance(raw, dict):
            raise ValidationError("each offline record must be an object")
        source_id = str(raw.get("source_id", "")).strip()
        record_id = str(raw.get("record_id", "")).strip()
        if not source_id or not record_id:
            raise ValidationError("source_id and record_id are required")
        incoming = dict(raw)
        self.rules.validate_create(actor, "offline_record", incoming, self._lookup)
        recorded_at = incoming["recorded_at"]
        payload = incoming["payload"]
        entity_id = self._offline_entity_id(source_id, record_id)
        rev_id = self._revision_id(source_id, record_id, recorded_at, payload)
        entity = self.repository.get_entity(entity_id)

        if entity is None:
            revision = {"rev_id": rev_id, "recorded_at": recorded_at, "payload": copy.deepcopy(payload), "state": "applied"}
            record = {
                "source_id": source_id,
                "record_id": record_id,
                "recorded_at": recorded_at,
                "payload": copy.deepcopy(payload),
                "current_revision": rev_id,
                "pinned_revision": None,
                "confirmed": False,
                "confirmed_by": None,
                "revisions": [revision],
                "pending_conflicts": [],
            }
            entity = self._save_offline_entity(
                actor, entity_id, "merged", record, "merge_offline",
                {"source_id": source_id, "record_id": record_id, "rev_id": rev_id, "outcome": "created"},
            )
            return entity, "created", {"rev_id": rev_id}

        record = dict(entity["data"])
        revisions = list(record.get("revisions", []))
        duplicate = next((r for r in revisions if r.get("rev_id") == rev_id), None)
        if duplicate is not None:
            return entity, "existing", {"rev_id": rev_id, "state": duplicate.get("state")}

        incoming_ts = recorded_at.replace("Z", "+00:00")
        current_ts = str(record.get("recorded_at", "")).replace("Z", "+00:00")
        new_revision = {
            "rev_id": rev_id,
            "recorded_at": recorded_at,
            "payload": copy.deepcopy(payload),
            "state": "applied",
        }
        pending = list(record.get("pending_conflicts", []))

        if record.get("confirmed"):
            # Confirmed content is never overwritten; the revision parks as a conflict.
            new_revision["state"] = "conflict"
            pending.append({
                "rev_id": rev_id,
                "recorded_at": recorded_at,
                "reason": "differs_from_confirmed",
                "confirmed_revision": record.get("pinned_revision"),
            })
            revisions.append(new_revision)
            record["revisions"] = revisions
            record["pending_conflicts"] = pending
            entity = self._save_offline_entity(
                actor, entity_id, "conflict", record, "merge_offline_conflict",
                {"source_id": source_id, "record_id": record_id, "rev_id": rev_id, "outcome": "conflict"},
            )
            return entity, "conflict", {"rev_id": rev_id, "reason": "differs_from_confirmed"}

        try:
            is_newer = incoming_ts > current_ts
            is_same = incoming_ts == current_ts
        except TypeError:
            is_newer, is_same = recorded_at > record.get("recorded_at"), recorded_at == record.get("recorded_at")

        if not is_newer and not is_same:
            # An older draft must never overwrite the current revision.
            new_revision["state"] = "stale"
            revisions.append(new_revision)
            record["revisions"] = revisions
            entity = self._save_offline_entity(
                actor, entity_id, entity["status"], record, "merge_offline_stale",
                {"source_id": source_id, "record_id": record_id, "rev_id": rev_id, "outcome": "stale"},
            )
            return entity, "stale", {"rev_id": rev_id}

        if is_same:
            # Same recorded_at but different content: human must pick one.
            new_revision["state"] = "conflict"
            for revision in revisions:
                if revision.get("state") == "applied":
                    revision["state"] = "conflict"
            pending.append({"rev_id": rev_id, "recorded_at": recorded_at, "reason": "same_timestamp_divergent"})
            pending = [entry for entry in pending if entry.get("reason") != "same_timestamp_divergent" or entry["rev_id"] == rev_id]
            status = "conflict"
            outcome = "conflict"
        else:
            for revision in revisions:
                if revision.get("state") in ("applied", "conflict"):
                    revision["state"] = "superseded"
            pending = []
            record["recorded_at"] = recorded_at
            record["payload"] = copy.deepcopy(payload)
            record["current_revision"] = rev_id
            status = "merged"
            outcome = "updated"

        revisions.append(new_revision)
        record["revisions"] = revisions
        record["pending_conflicts"] = pending
        action = "merge_offline_conflict" if outcome == "conflict" else "merge_offline_revision"
        entity = self._save_offline_entity(
            actor, entity_id, status, record, action,
            {"source_id": source_id, "record_id": record_id, "rev_id": rev_id, "outcome": outcome},
        )
        return entity, outcome, {"rev_id": rev_id}

    def merge_offline(self, actor, records, batch_id=None):
        """Merge field records into the revision ledger.

        A stable (source_id, record_id) identity makes each record idempotent.
        When ``batch_id`` is supplied, per-item outcomes are persisted so that
        retrying a failed batch skips everything already processed.
        """
        if not isinstance(records, list):
            raise ValidationError("records must be a list")
        results = []
        entities = {}
        counts = {key: 0 for key in ("created", "updated", "existing", "stale", "conflict", "skipped", "failed")}
        for raw in records:
            source_id = str(raw.get("source_id", "")).strip() if isinstance(raw, dict) else ""
            record_id = str(raw.get("record_id", "")).strip() if isinstance(raw, dict) else ""
            if source_id and record_id:
                rev_id = self._revision_id(source_id, record_id, raw.get("recorded_at"), raw.get("payload"))
                item_key = rev_id
                pending_key = self._record_identity_key(source_id, record_id)
            else:
                rev_id = None
                item_key = self._invalid_item_key(raw)
                pending_key = None

            if batch_id:
                prior = self.repository.get_batch_item(batch_id, item_key)
                if not prior and pending_key:
                    # 该记录此前校验失败挂过账；仅当挂账指向同一修订时才跳过
                    alias = self.repository.get_batch_item(batch_id, pending_key)
                    if alias and alias["state"] != "failed" and alias.get("detail", {}).get("rev_id") == rev_id:
                        prior = alias
                if prior and prior["state"] != "failed":
                    entity = self.repository.get_entity(prior["entity_id"]) if prior.get("entity_id") else None
                    results.append({
                        "item_key": item_key,
                        "state": "skipped",
                        "outcome": prior["state"],
                        "entity_id": prior.get("entity_id"),
                        "detail": prior.get("detail", {}),
                    })
                    counts["skipped"] += 1
                    if entity:
                        entities[entity["id"]] = entity
                    continue
            try:
                entity, outcome, detail = self._merge_one(actor, raw)
            except ValidationError as exc:
                if batch_id:
                    self.repository.save_batch_item(batch_id, pending_key or item_key, "failed", None, {"error": str(exc)})
                results.append({"item_key": pending_key or item_key, "state": "failed", "error": str(exc)})
                counts["failed"] += 1
                continue
            if batch_id:
                self.repository.save_batch_item(batch_id, item_key, outcome, entity["id"], detail)
                if pending_key:
                    # 修正后的成功修订勾销原失败挂账
                    self.repository.save_batch_item(batch_id, pending_key, outcome, entity["id"], detail)
            results.append({"item_key": item_key, "state": outcome, "entity_id": entity["id"], "detail": detail})
            counts[outcome] += 1
            entities[entity["id"]] = entity
        return {
            "batch_id": batch_id,
            "results": results,
            "items": list(entities.values()),
            "counts": counts,
        }

    def batch_status(self, batch_id):
        items = self.repository.list_batch_items(batch_id)
        if not items:
            raise NotFoundError("batch not found: " + str(batch_id))
        counts = {key: 0 for key in ("created", "updated", "existing", "stale", "conflict", "skipped", "failed")}
        for item in items:
            counts[item["state"]] = counts.get(item["state"], 0) + 1
        return {"batch_id": batch_id, "items": items, "counts": counts,
                "unfinished": [item["item_key"] for item in items if item["state"] == "failed"]}

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
