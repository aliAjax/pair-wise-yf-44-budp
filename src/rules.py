from datetime import date, datetime

from .domain import (
    CommissionBlocked,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def parse_date(value):
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except (ValueError, TypeError):
        raise ValidationError("invalid date: %s (expected YYYY-MM-DD)" % value)


def required_approval_level(risk_level):
    levels = {"low": 1, "medium": 2, "high": 3, "critical": 4}
    return levels.get(str(risk_level).lower(), 4)


def is_high_risk(risk_level):
    return str(risk_level).lower() in ("high", "critical")


def today():
    return date.today()


def effective_due_date(item):
    """当前生效期限：有延期时取最后一次延期，否则取登记期限。"""
    data = item.get("data", {})
    extensions = data.get("extensions") or []
    if extensions:
        return parse_date(extensions[-1]["new_due_date"])
    return parse_date(data.get("due_date"))


def original_due_date(item):
    return parse_date(item.get("data", {}).get("due_date"))


def _validate_change(actor, data, lookup):
    unit = _find_one(lookup, "unit", "id", data.get("unit_id"))
    if not unit:
        raise ValidationError("unit does not exist")
    if not data.get("description", "").strip():
        raise ValidationError("change description is required")


def _validate_action_item(actor, data, lookup):
    change = _find_one(lookup, "change", "id", data.get("change_id"))
    if not change:
        raise ValidationError("change does not exist")
    parse_date(data.get("due_date"))


def _validate_assess(actor, entity, data, lookup):
    return {"required_approvals": required_approval_level(data.get("risk_level"))}


def _validate_approve(actor, entity, data, lookup):
    required = int(entity["data"].get("required_approvals", 1))
    approvals = data.get("approvals") or []
    if len(set(approvals)) < required:
        raise ValidationError("not enough distinct approvals")
    return {"approved_by": actor.user_id}


def _validate_safety_review(actor, entity, data, lookup):
    return {
        "safety_reviewed_by": actor.user_id,
        "safety_reviewed_at": today().isoformat(),
    }


def validate_extend_payload(data):
    """投产前安全确认：延期必须给出新期限和理由。"""
    new_due = data.get("new_due_date")
    if not new_due:
        raise ValidationError("missing required field: new_due_date")
    reason = str(data.get("reason", "")).strip()
    if not reason:
        raise ValidationError("extension reason is required")
    parse_date(new_due)
    return new_due


def _validate_extend(actor, entity, data, lookup):
    new_due = validate_extend_payload(data)
    current = effective_due_date(entity)
    if parse_date(new_due) <= current:
        raise ValidationError("new due date must be later than current due date")
    extensions = list(entity["data"].get("extensions") or [])
    extensions.append(
        {
            "new_due_date": new_due,
            "reason": str(data["reason"]).strip(),
            "extended_by": actor.user_id,
            "extended_at": today().isoformat(),
        }
    )
    return {"extensions": extensions}


def _freeze_controls(items):
    snapshot = []
    for item in items:
        data = item["data"]
        snapshot.append(
            {
                "action_item_id": item["id"],
                "description": data.get("description"),
                "owner": data.get("owner"),
                "completed_by": data.get("completed_by"),
                "verifier": data.get("verifier"),
                "original_due_date": data.get("due_date"),
                "extensions": list(data.get("extensions") or []),
                "status": item["status"],
            }
        )
    return snapshot


def _commission_blockers(entity, items, as_of=None):
    """投产前安全确认，逐项返回阻断项（一次性反馈全部问题）。"""
    blockers = []
    as_of = as_of or today()
    for item in items:
        data = item["data"]
        item_id = item["id"]
        if not data.get("due_date"):
            blockers.append(
                {
                    "code": "due_date_not_registered",
                    "action_item_id": item_id,
                    "message": "行动项未登记有效期: " + item_id,
                }
            )
        if item["status"] != "verified":
            blockers.append(
                {
                    "code": "action_item_not_verified",
                    "action_item_id": item_id,
                    "message": "行动项尚未完成独立校核: " + item_id,
                }
            )
        if data.get("due_date") and effective_due_date(item) < as_of:
            blockers.append(
                {
                    "code": "control_expired",
                    "action_item_id": item_id,
                    "message": "控制措施已过有效期，需安全员延期: " + item_id,
                }
            )
        verifier = data.get("verifier")
        completed_by = data.get("completed_by")
        if verifier and completed_by and verifier == completed_by:
            blockers.append(
                {
                    "code": "independent_check_required",
                    "action_item_id": item_id,
                    "message": "校核人与完成人相同，必须由第三人独立校核: " + item_id,
                }
            )
    risk_level = entity["data"].get("risk_level")
    if is_high_risk(risk_level) and not entity["data"].get("safety_reviewed_by"):
        blockers.append(
            {
                "code": "safety_review_required",
                "message": "高风险变更缺少安全员复核",
            }
        )
    return blockers


def _validate_commission(actor, entity, data, lookup):
    items = lookup("action_item", "change_id", entity["id"]) if lookup else []
    items = items or []
    blockers = _commission_blockers(entity, items)
    if blockers:
        raise CommissionBlocked(blockers)
    return {
        "commissioned_by": actor.user_id,
        "commissioned_at": today().isoformat(),
        "frozen_controls": _freeze_controls(items),
    }


CUSTOM_CREATE = {'change': _validate_change, 'action_item': _validate_action_item}
CUSTOM_TRANSITIONS = {
    ('change', 'assess'): _validate_assess,
    ('change', 'approve'): _validate_approve,
    ('change', 'safety_review'): _validate_safety_review,
    ('change', 'commission'): _validate_commission,
    ('action_item', 'extend'): _validate_extend,
}

# 状态保持不变的“侧面动作”：状态机里以 "*" 标记
SIDE_ACTIONS = {('change', 'safety_review'), ('action_item', 'extend')}


class RuleEngine:
    ALIASES = {'units': 'unit', 'changes': 'change', 'action_items': 'action_item'}
    INITIAL_STATUS = {'unit': 'operating', 'change': 'draft', 'action_item': 'open'}
    TRANSITIONS = {'unit': {'shutdown': (('operating',), 'shutdown'), 'startup': (('shutdown',), 'operating'), 'freeze': (('operating',), 'frozen'), 'unfreeze': (('frozen',), 'operating')}, 'change': {'assess': (('draft',), 'assessed'), 'approve': (('assessed',), 'approved'), 'implement': (('approved',), 'implemented'), 'safety_review': (('implemented',), '*'), 'commission': (('implemented',), 'commissioned'), 'rollback': (('implemented', 'commissioned'), 'rolled_back'), 'close': (('rolled_back',), 'closed')}, 'action_item': {'complete': (('open',), 'completed'), 'verify': (('completed',), 'verified'), 'extend': (('open', 'completed', 'verified'), '*'), 'reopen': (('verified',), 'open')}}
    CREATE_REQUIRED = {'unit': ('name', 'location'), 'change': ('unit_id', 'description'), 'action_item': ('change_id', 'description', 'owner', 'due_date')}
    ACTION_REQUIRED = {('unit', 'shutdown'): ('reason',), ('unit', 'freeze'): ('reason',), ('change', 'assess'): ('risk_level', 'analyst'), ('change', 'approve'): ('approvals', 'permit_id'), ('change', 'implement'): ('procedure_version',), ('change', 'commission'): ('tests_passed',), ('change', 'rollback'): ('reason',), ('change', 'close'): ('outcome',), ('action_item', 'complete'): ('completed_by', 'evidence'), ('action_item', 'verify'): ('verifier',), ('action_item', 'extend'): ('new_due_date', 'reason'), ('action_item', 'reopen'): ('reason',)}
    CREATE_ROLES = {'unit': ('admin', 'engineer'), 'change': ('admin', 'engineer'), 'action_item': ('admin', 'safety')}
    ROLE_ACTIONS = {'shutdown': ('admin', 'operator'), 'startup': ('admin', 'operator'), 'freeze': ('admin', 'operator'), 'unfreeze': ('admin', 'operator'), 'assess': ('admin', 'engineer'), 'approve': ('admin', 'safety'), 'implement': ('admin', 'engineer'), 'safety_review': ('admin', 'safety'), 'commission': ('admin', 'engineer'), 'rollback': ('admin', 'engineer'), 'close': ('admin', 'safety'), 'complete': ('admin', 'engineer'), 'verify': ('admin', 'verifier'), 'extend': ('admin', 'safety'), 'reopen': ('admin', 'verifier')}

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        if next_status == "*":
            next_status = entity["status"]
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch
