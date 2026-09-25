from datetime import date, datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    PreStartupSafetyBlocked,
    ValidationError,
)

HIGH_RISK_LEVELS = {"high", "critical"}
EXTEND_ROLES = ("admin", "safety")


def today():
    return date.today()


def _parse_date(value, field):
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except (ValueError, TypeError):
        raise ValidationError("invalid date for %s (use YYYY-MM-DD): %s" % (field, value))


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


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
    _parse_date(data.get("valid_until"), "valid_until")


def required_approval_level(risk_level):
    levels = {"low": 1, "medium": 2, "high": 3, "critical": 4}
    return levels.get(str(risk_level).lower(), 4)


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
        "safety_review_by": actor.user_id,
        "safety_review_note": data.get("note", ""),
    }


def _is_item_expired(item, on_date=None):
    raw = item["data"].get("valid_until")
    if not raw:
        return False
    try:
        return _parse_date(raw, "valid_until") < (on_date or today())
    except ValidationError:
        return False


def pre_startup_blockers(change, items, on_date=None):
    """投产前安全确认：返回阻断项列表，空列表表示可以投产。"""
    blockers = []
    on_date = on_date or today()
    risk_level = str(change["data"].get("risk_level", "")).lower()

    if risk_level in HIGH_RISK_LEVELS and not change["data"].get("safety_review_by"):
        blockers.append(
            {
                "code": "safety_review_missing",
                "risk_level": risk_level,
                "message": "高风险变更缺少安全员复核",
            }
        )

    for item in items:
        detail = item["data"]
        prefix = item["id"]

        if item["status"] != "verified":
            blockers.append(
                {
                    "code": "not_verified",
                    "action_item": item["id"],
                    "status": item["status"],
                    "message": "行动项尚未核验完成: " + prefix,
                }
            )
            continue

        raw_deadline = detail.get("valid_until")
        if not raw_deadline:
            blockers.append(
                {
                    "code": "validity_missing",
                    "action_item": item["id"],
                    "message": "行动项未登记有效期: " + prefix,
                }
            )
        else:
            try:
                deadline = _parse_date(raw_deadline, "valid_until")
            except ValidationError:
                blockers.append(
                    {
                        "code": "validity_missing",
                        "action_item": item["id"],
                        "value": raw_deadline,
                        "message": "行动项有效期无法识别: " + prefix,
                    }
                )
            else:
                if deadline < on_date:
                    blockers.append(
                        {
                            "code": "expired",
                            "action_item": item["id"],
                            "valid_until": raw_deadline,
                            "message": "行动项控制措施已过期: %s（有效期至 %s）"
                            % (prefix, raw_deadline),
                        }
                    )

        completed_by = detail.get("completed_by")
        verifier = detail.get("verifier")
        if completed_by and verifier and str(verifier) == str(completed_by):
            blockers.append(
                {
                    "code": "independent_check_missing",
                    "action_item": item["id"],
                    "completed_by": completed_by,
                    "verifier": verifier,
                    "message": "校核人与完成人相同，缺少独立校核: " + prefix,
                }
            )

    return blockers


def _freeze_snapshot(items, on_date):
    snapshot = []
    for item in items:
        snapshot.append(
            {
                "id": item["id"],
                "status": item["status"],
                "description": item["data"].get("description", ""),
                "owner": item["data"].get("owner", ""),
                "completed_by": item["data"].get("completed_by", ""),
                "verifier": item["data"].get("verifier", ""),
                "valid_until": item["data"].get("valid_until", ""),
                "original_valid_until": item["data"].get("original_valid_until", ""),
                "frozen_on": on_date.isoformat(),
            }
        )
    return snapshot


def _validate_commission(actor, entity, data, lookup):
    items = (lookup("action_item", "change_id", entity["id"]) if lookup else None) or []
    on_date = today()
    blockers = pre_startup_blockers(entity, items, on_date=on_date)
    if blockers:
        raise PreStartupSafetyBlocked(blockers)
    return {
        "commissioned_by": actor.user_id,
        "frozen_at": on_date.isoformat(),
        "frozen_controls": _freeze_snapshot(items, on_date),
    }


def _validate_extend(actor, item, data, lookup):
    reason = str(data.get("reason", "")).strip()
    if not reason:
        raise ValidationError("missing required field: reason")
    current_raw = item["data"].get("valid_until")
    if not current_raw:
        raise ValidationError("action item has no valid_until registered")
    current = _parse_date(current_raw, "valid_until")
    new_deadline = _parse_date(data.get("valid_until"), "valid_until")
    if new_deadline <= current:
        raise ValidationError(
            "new valid_until must be later than current deadline %s" % current_raw
        )
    extensions = list(item["data"].get("extensions") or [])
    extensions.append(
        {
            "valid_until": new_deadline.isoformat(),
            "reason": reason,
            "extended_by": actor.user_id,
        }
    )
    return {
        "valid_until": new_deadline.isoformat(),
        "original_valid_until": item["data"].get("original_valid_until", current_raw),
        "extensions": extensions,
        "reason": reason,
    }


CUSTOM_CREATE = {
    "change": _validate_change,
    "action_item": _validate_action_item,
}
CUSTOM_TRANSITIONS = {
    ("change", "assess"): _validate_assess,
    ("change", "approve"): _validate_approve,
    ("change", "safety_review"): _validate_safety_review,
    ("change", "commission"): _validate_commission,
    ("action_item", "extend"): _validate_extend,
}


class RuleEngine:
    ALIASES = {'units': 'unit', 'changes': 'change', 'action_items': 'action_item'}
    INITIAL_STATUS = {'unit': 'operating', 'change': 'draft', 'action_item': 'open'}
    TRANSITIONS = {'unit': {'shutdown': (('operating',), 'shutdown'), 'startup': (('shutdown',), 'operating'), 'freeze': (('operating',), 'frozen'), 'unfreeze': (('frozen',), 'operating')}, 'change': {'assess': (('draft',), 'assessed'), 'approve': (('assessed',), 'approved'), 'implement': (('approved',), 'implemented'), 'safety_review': (('implemented',), 'implemented'), 'commission': (('implemented',), 'commissioned'), 'rollback': (('implemented', 'commissioned'), 'rolled_back'), 'close': (('rolled_back',), 'closed')}, 'action_item': {'complete': (('open',), 'completed'), 'verify': (('completed',), 'verified'), 'extend': (('open', 'completed', 'verified'), None), 'reopen': (('verified',), 'open')}}
    CREATE_REQUIRED = {'unit': ('name', 'location'), 'change': ('unit_id', 'description'), 'action_item': ('change_id', 'description', 'owner', 'valid_until')}
    ACTION_REQUIRED = {('unit', 'shutdown'): ('reason',), ('unit', 'freeze'): ('reason',), ('change', 'assess'): ('risk_level', 'analyst'), ('change', 'approve'): ('approvals', 'permit_id'), ('change', 'implement'): ('procedure_version',), ('change', 'safety_review'): ('note',), ('change', 'commission'): ('tests_passed',), ('change', 'rollback'): ('reason',), ('change', 'close'): ('outcome',), ('action_item', 'complete'): ('completed_by', 'evidence'), ('action_item', 'verify'): ('verifier',), ('action_item', 'extend'): ('valid_until', 'reason'), ('action_item', 'reopen'): ('reason',)}
    CREATE_ROLES = {'unit': ('admin', 'engineer'), 'change': ('admin', 'engineer'), 'action_item': ('admin', 'safety')}
    ROLE_ACTIONS = {'shutdown': ('admin', 'operator'), 'startup': ('admin', 'operator'), 'freeze': ('admin', 'operator'), 'unfreeze': ('admin', 'operator'), 'assess': ('admin', 'engineer'), 'approve': ('admin', 'safety'), 'implement': ('admin', 'engineer'), 'safety_review': ('admin', 'safety'), 'commission': ('admin', 'engineer'), 'rollback': ('admin', 'engineer'), 'close': ('admin', 'safety'), 'complete': ('admin', 'engineer'), 'verify': ('admin', 'verifier'), 'extend': EXTEND_ROLES, 'reopen': ('admin', 'verifier')}

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
        if next_status is None:
            next_status = entity["status"]
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
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

    def controls_frozen(self, change):
        """变更已投产且存在冻结的控制清单时，后续改期归入新待办。"""
        if not change:
            return False
        return bool(change["data"].get("frozen_controls"))

    def build_follow_up_todo(self, actor, item, data, lookup):
        """对冻结控制项做延期时，生成一条新的跟进待办，而不是改动原记录。"""
        reason = str(data.get("reason", "")).strip()
        new_deadline = _parse_date(data.get("valid_until"), "valid_until")
        self._require(data, ("valid_until", "reason"))
        original_deadline = item["data"].get("original_valid_until") or item["data"].get("valid_until", "")
        payload = {
            "change_id": item["data"].get("change_id"),
            "description": item["data"].get("description", ""),
            "owner": actor.user_id,
            "valid_until": new_deadline.isoformat(),
            "follow_up_of": item["id"],
            "follow_up_reason": reason,
            "original_valid_until": original_deadline,
        }
        self.validate_create(actor, "action_item", payload, lookup)
        return payload


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
