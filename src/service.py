from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import RuleEngine, validate_extend_payload


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
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        payload = dict(data or {})
        if (
            entity["kind"] == "action_item"
            and action == "extend"
            and self._control_list_frozen(entity)
        ):
            return self._reschedule_frozen_item(actor, entity, payload)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, payload, self._lookup
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

    def _control_list_frozen(self, item):
        """投产时已冻结本次控制清单：父变更已投产且快照中包含该行动项。"""
        change = self.repository.get_entity(item["data"].get("change_id"))
        if not change or change["status"] != "commissioned":
            return False
        frozen_ids = {
            entry.get("action_item_id")
            for entry in change["data"].get("frozen_controls", [])
        }
        return item["id"] in frozen_ids

    def _reschedule_frozen_item(self, actor, entity, data):
        """投产后改期：冻结清单不再改动，延期归入一条新的 open 待办。"""
        self.rules._ensure_role(actor, ("admin", "safety"))
        new_due = validate_extend_payload(data)
        change_id = entity["data"]["change_id"]
        follow_up = {
            "change_id": change_id,
            "description": entity["data"].get("description"),
            "owner": entity["data"].get("owner"),
            "due_date": new_due,
            "rescheduled_from": entity["id"],
            "reschedule_reason": str(data["reason"]).strip(),
        }
        new_entity = self.repository.create_entity(
            str(uuid4()), "action_item", "open", follow_up, actor.user_id
        )
        self.audit.record(
            new_entity["id"],
            actor,
            "create",
            None,
            "open",
            {"kind": "action_item", "rescheduled_from": entity["id"]},
        )
        self.audit.record(
            entity["id"],
            actor,
            "reschedule",
            entity["status"],
            entity["status"],
            {"new_action_item_id": new_entity["id"], "reason": follow_up["reschedule_reason"]},
        )
        return new_entity

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
