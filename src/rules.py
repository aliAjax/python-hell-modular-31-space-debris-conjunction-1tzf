from datetime import datetime, timezone

from .domain import DomainError

ENTITY_TYPE = "space_conjunction"
INITIAL_STATUS = "pending"
CREATE_ROLES = {"analyst"}
SOURCE_ROLES = {"analyst", "operator"}
ACTION_ROLES = {
    "assess": {"analyst"},
    "record_opinion": {"operator"},
    "approve": {"coordinator"},
    "execute": {"operator"},
    "resolve": {"coordinator"},
    "cancel": {"coordinator"},
    "report_revision": {"analyst"},
}
ENFORCE_REGION = False
REGION_SENSITIVE_ACTIONS = set()
ACTION_REQUIRES_VERSION = {"approve", "execute", "resolve", "cancel"}

# 协调案（多记录归并）相关常量
CASE_ENTITY_TYPE = "coordination_case"
CASE_INITIAL_STATUS = "open"
MERGE_WINDOW_HOURS = 2  # TCA 相差在该窗口内视为同一交会
CASE_ACTION_ROLES = {
    "assess": {"analyst"},
    "record_opinion": {"operator"},
    "approve": {"coordinator"},
    "execute": {"operator"},
    "resolve": {"coordinator"},
}
CASE_ACTIONS_REQUIRES_VERSION = {"approve", "execute", "resolve"}


def _now():
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def assess(payload):
    ratio = float(payload.get("miss_distance_m", 0)) / max(float(payload.get("covariance_m", 1)), 1.0)
    tca_hours = float(payload.get("hours_to_tca", 24))
    severity = max(0.0, 100.0 - min(95.0, ratio * 20.0))
    urgency = max(0.0, min(20.0, (24.0 - tca_hours) * 0.8))
    score = round(min(100.0, severity + urgency), 2)
    if score >= 80:
        level = "high"
    elif score >= 50:
        level = "medium"
    else:
        level = "low"
    return {"score": score, "level": level, "distance_to_covariance_ratio": round(ratio, 3)}


def _need_status(item, allowed):
    if item["status"] not in allowed:
        raise DomainError("invalid_state", "当前状态 %s 不允许执行该操作" % item["status"])


def _require_number(payload, name, minimum=None):
    try:
        value = float(payload[name])
    except (KeyError, TypeError, ValueError):
        raise DomainError("field_required", "%s 不能为空" % name)
    if minimum is not None and value < minimum:
        raise DomainError("invalid_number", "%s 不能小于 %s" % (name, minimum))
    return value


def _require_text(payload, name):
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DomainError("field_required", "%s 不能为空" % name)
    return value.strip()


def _check_operator_org(current, operator, org):
    """运营方只能对本机构物体表态；越权（冒充其他机构）拒绝。"""
    owners = current.get("operating_organizations", [])
    if org is not None:
        if operator != org:
            raise DomainError("opinion_denied", "不能代表本机构以外的运营方表态", 403)
        if org not in owners:
            raise DomainError("opinion_denied", "本机构不在该物体的运营方列表中", 403)
    else:
        if operator not in owners:
            raise DomainError("opinion_denied", "该运营方不在本记录的运营方列表中", 403)


def _basis_from_source(source):
    payload = source.get("payload", {})
    return {
        "source_id": source["id"],
        "source_type": source["source_type"],
        "external_id": source["external_id"],
        "observed_at": source["observed_at"],
        "miss_distance_m": payload.get("miss_distance_m"),
        "covariance_m": payload.get("covariance_m"),
    }


def select_basis(members):
    """从全体成员的来源记录中，按观测时刻选出当前依据；其余进历史。"""
    candidates = []
    for member in members:
        for source in member.get("sources", []):
            candidates.append((source["observed_at"], source))
    if not candidates:
        return None, []
    candidates.sort(key=lambda item: parse_iso(item[0]))
    newest = candidates[-1][1]
    basis = _basis_from_source(newest)
    history = [_basis_from_source(source) for _, source in candidates[:-1]]
    return basis, history


def build_case_payload(members):
    """成员形如 {"item": 记录行, "sources": [来源行]}。返回 (payload, 主成员item_id)。"""
    basis, history = select_basis(members)
    if basis is not None:
        rep = next(m for m in members if any(s["id"] == basis["source_id"] for s in m["sources"]))
    else:
        rep = members[0]
    rep_payload = rep["item"]["payload"]
    if basis is None:
        # 无外部来源时，以记录自报数据作为当前依据
        basis = {
            "source_id": None,
            "source_type": "self_report",
            "external_id": "item:%s" % rep["item"]["id"],
            "observed_at": rep["item"]["created_at"],
            "miss_distance_m": rep_payload.get("miss_distance_m"),
            "covariance_m": rep_payload.get("covariance_m"),
        }
    primary = rep_payload["primary_object_id"]
    payload = {
        "primary_object_id": primary,
        "secondary_object_id": rep_payload["secondary_object_id"],
        "tca": rep_payload["tca"],
        "miss_distance_m": basis["miss_distance_m"] if basis else rep_payload.get("miss_distance_m"),
        "covariance_m": basis["covariance_m"] if basis else rep_payload.get("covariance_m"),
        "track_age_hours": rep_payload.get("track_age_hours", 0),
        "fuel_budget_m_s": rep_payload.get("fuel_budget_m_s", 0),
        "operating_organizations": rep_payload.get("operating_organizations", []),
        "basis": basis,
        "basis_history": history,
        "approvals": [],
        "command": None,
        "opinions": [],
        "conflict": False,
        "assessment": None,
        "fuel_ledger": {},
        "merge_history": [],
    }
    payload["fuel_ledger"][primary] = {
        "budget": float(payload["fuel_budget_m_s"]),
        "occupations": [],
    }
    # 归并时燃料重新结算：已下发指令保留原依据并带入；未下发批准随依据更新释放
    seq = 0
    for member in members:
        member_payload = member["item"]["payload"]
        member_basis = _member_basis_observed_at(member)
        if member_payload.get("command_ref"):
            seq += 1
            fuel = float(member_payload.get("approved_maneuver", {}).get("fuel_cost_m_s", 0))
            window = member_payload.get("approved_maneuver", {}).get("maneuver_window", "")
            approval = {
                "id": "APR-M%d" % seq,
                "fuel_cost_m_s": fuel,
                "maneuver_window": window,
                "basis_observed_at": member_basis,
                "basis_id": None,
                "status": "commanded",
                "actor": member["item"].get("created_by"),
                "created_at": member["item"].get("updated_at"),
            }
            payload["approvals"].append(approval)
            payload["command"] = {
                "command_ref": member_payload["command_ref"],
                "approval_id": approval["id"],
                "fuel_cost_m_s": fuel,
                "basis_observed_at": member_basis,
                "basis_id": None,
                "issued_at": member["item"].get("updated_at"),
                "retained_from_item": member["item"]["id"],
            }
            payload["merge_history"].append({
                "item_id": member["item"]["id"],
                "action": "command_retained",
                "command_ref": member_payload["command_ref"],
                "basis_observed_at": member_basis,
            })
        elif member_payload.get("approved_maneuver"):
            payload["merge_history"].append({
                "item_id": member["item"]["id"],
                "action": "approval_released",
                "fuel_cost_m_s": member_payload["approved_maneuver"].get("fuel_cost_m_s"),
                "reason": "orbit_basis_superseded_on_merge",
            })
    _rebuild_fuel_ledger(payload)
    return payload, rep["item"]["id"]


def _member_basis_observed_at(member):
    sources = member.get("sources", [])
    if sources:
        newest = max(sources, key=lambda s: parse_iso(s["observed_at"]))
        return newest["observed_at"]
    return member["item"].get("created_at")


def same_conjunction(payload_a, payload_b):
    same_pair = sorted([payload_a["primary_object_id"], payload_a["secondary_object_id"]]) == sorted(
        [payload_b["primary_object_id"], payload_b["secondary_object_id"]]
    )
    if not same_pair:
        return False
    delta = abs((parse_iso(payload_a["tca"]) - parse_iso(payload_b["tca"])).total_seconds())
    return delta <= MERGE_WINDOW_HOURS * 3600


def _rebuild_fuel_ledger(current):
    primary = current["primary_object_id"]
    budget = float(current.get("fuel_budget_m_s", 0))
    occupations = []
    for approval in current.get("approvals", []):
        if approval["status"] in ("active", "commanded"):
            occupations.append({
                "ref": "approval:" + approval["id"],
                "fuel": float(approval["fuel_cost_m_s"]),
                "status": approval["status"],
                "basis_observed_at": approval.get("basis_observed_at"),
                "occupied_at": approval.get("created_at"),
            })
    occupations.sort(key=lambda item: item["occupied_at"] or "")
    current["fuel_ledger"] = {primary: {"budget": budget, "occupations": occupations}}


def _check_fuel(current, requested):
    primary = current["primary_object_id"]
    ledger = current.get("fuel_ledger", {}).get(primary, {"budget": current.get("fuel_budget_m_s", 0), "occupations": []})
    occupations = [o for o in ledger.get("occupations", []) if o["status"] in ("active", "commanded")]
    occupied = sum(float(o["fuel"]) for o in occupations)
    budget = float(ledger.get("budget", current.get("fuel_budget_m_s", 0)))
    if occupied + float(requested) > budget:
        first = occupations[0] if occupations else None
        raise DomainError(
            "fuel_budget_exceeded",
            "规避燃料超过预算，方案退回",
            409,
            payload={
                "budget": budget,
                "occupied": occupied,
                "requested": float(requested),
                "first_occupier": first["ref"] if first else None,
                "first_occupier_fuel": first["fuel"] if first else None,
            },
        )


def apply_basis_update(current, new_basis):
    """新观测到达：依据更新则未下发批准失效；更旧的观测只进历史。返回依据是否变化。"""
    old = current.get("basis")
    if old and parse_iso(new_basis["observed_at"]) <= parse_iso(old["observed_at"]):
        current.setdefault("basis_history", []).append(new_basis)
        return False
    if old:
        current.setdefault("basis_history", []).append(old)
    current["basis"] = new_basis
    current["miss_distance_m"] = new_basis["miss_distance_m"]
    current["covariance_m"] = new_basis["covariance_m"]
    for approval in current.get("approvals", []):
        if approval["status"] == "active":
            approval["status"] = "invalidated"
            approval["invalidated_by_basis"] = old["observed_at"] if old else None
    _rebuild_fuel_ledger(current)
    return True


def apply_case_action(case, action, payload, actor, role, org=None):
    status = case["status"]
    current = dict(case["payload"])

    if action == "assess":
        _need_status(case, {"open", "assessed"})
        age = float(current.get("track_age_hours", 0))
        if age > 6:
            raise DomainError("stale_track", "轨道数据已过期，不能用于风险评估")
        current["hours_to_tca"] = float(payload.get("hours_to_tca", current.get("hours_to_tca", 24)))
        current["assessment"] = assess(current)
        return "assessed", current, {"assessment": current["assessment"]}

    if action == "record_opinion":
        _need_status(case, {"assessed", "coordinating"})
        opinion = _require_text(payload, "opinion").lower()
        if opinion not in {"approve", "reject", "request_review"}:
            raise DomainError("invalid_opinion", "意见必须是 approve、reject 或 request_review")
        operator = _require_text(payload, "operator")
        _check_operator_org(current, operator, org)
        entry = {"operator": operator, "opinion": opinion, "reason": payload.get("reason", "")}
        current.setdefault("opinions", []).append(entry)
        if opinion in {"reject", "request_review"}:
            current["conflict"] = True
        return status, current, {"opinion": entry}

    if action == "approve":
        _need_status(case, {"assessed", "coordinating"})
        if current.get("conflict"):
            raise DomainError("unresolved_conflict", "存在未解决的运营方冲突意见", 409)
        fuel = _require_number(payload, "fuel_cost_m_s", 0)
        window = _require_text(payload, "maneuver_window")
        _check_fuel(current, fuel)
        approval = {
            "id": "APR-%d" % (len(current.get("approvals", [])) + 1),
            "fuel_cost_m_s": fuel,
            "maneuver_window": window,
            "basis_observed_at": current["basis"]["observed_at"],
            "basis_id": current["basis"]["source_id"],
            "status": "active",
            "actor": actor,
            "created_at": _now(),
        }
        current.setdefault("approvals", []).append(approval)
        _rebuild_fuel_ledger(current)
        return "coordinating", current, {"approval": approval}

    if action == "execute":
        _need_status(case, {"coordinating"})
        command_ref = _require_text(payload, "command_ref")
        active = [a for a in current.get("approvals", []) if a["status"] == "active"]
        if not active:
            raise DomainError("no_active_approval", "没有已批准的规避方案可下发")
        approval = active[-1]
        approval["status"] = "commanded"
        command = {
            "command_ref": command_ref,
            "approval_id": approval["id"],
            "fuel_cost_m_s": approval["fuel_cost_m_s"],
            "basis_observed_at": current["basis"]["observed_at"],
            "basis_id": current["basis"]["source_id"],
            "issued_at": _now(),
        }
        current["command"] = command
        _rebuild_fuel_ledger(current)
        return "executing", current, {"command": command}

    if action == "resolve":
        _need_status(case, {"executing"})
        report_ref = _require_text(payload, "report_ref")
        current["resolution"] = {"report_ref": report_ref, "resolved_by": actor}
        for approval in current.get("approvals", []):
            if approval["status"] == "commanded":
                approval["status"] = "resolved"
        return "resolved", current, {"report_ref": report_ref}

    raise DomainError("unknown_action", "不支持的操作")


def apply_action(item, action, payload, actor, role, org=None):
    status = item["status"]
    current = dict(item["payload"])

    if action == "assess":
        _need_status(item, {"pending", "assessed"})
        age = float(current.get("track_age_hours", 0))
        if age > 6:
            raise DomainError("stale_track", "轨道数据已过期，不能用于风险评估")
        current["hours_to_tca"] = float(payload.get("hours_to_tca", current.get("hours_to_tca", 24)))
        result = assess(current)
        current["assessment"] = result
        return "assessed", current, {"assessment": result, "actor": actor}

    if action == "report_revision":
        _need_status(item, {"pending", "assessed", "coordinating", "executing"})
        revision = {
            "observed_at": _require_text(payload, "observed_at"),
            "miss_distance_m": _require_number(payload, "miss_distance_m", 0),
            "covariance_m": _require_number(payload, "covariance_m", 0.001),
            "source": _require_text(payload, "source"),
        }
        if revision["covariance_m"] <= 0:
            raise DomainError("invalid_covariance", "协方差必须大于零")
        current.setdefault("revisions", []).append(revision)
        current["miss_distance_m"] = revision["miss_distance_m"]
        current["covariance_m"] = revision["covariance_m"]
        current["assessment"] = assess(current)
        return status, current, {"revision": revision}

    if action == "record_opinion":
        _need_status(item, {"assessed", "coordinating"})
        opinion = _require_text(payload, "opinion").lower()
        if opinion not in {"approve", "reject", "request_review"}:
            raise DomainError("invalid_opinion", "意见必须是 approve、reject 或 request_review")
        operator = _require_text(payload, "operator")
        _check_operator_org(current, operator, org)
        entry = {"operator": operator, "opinion": opinion, "reason": payload.get("reason", "")}
        current.setdefault("opinions", []).append(entry)
        if opinion in {"reject", "request_review"}:
            current["conflict"] = True
        return status, current, {"opinion": entry}

    if action == "approve":
        _need_status(item, {"assessed"})
        if current.get("conflict"):
            raise DomainError("unresolved_conflict", "存在未解决的运营方冲突意见", 409)
        fuel = _require_number(payload, "fuel_cost_m_s", 0)
        budget = float(current.get("fuel_budget_m_s", 0))
        if fuel > budget:
            raise DomainError("fuel_budget_exceeded", "规避燃料超过预算", 409)
        window = _require_text(payload, "maneuver_window")
        current["approved_maneuver"] = {"fuel_cost_m_s": fuel, "maneuver_window": window}
        return "coordinating", current, {"approved_maneuver": current["approved_maneuver"]}

    if action == "execute":
        _need_status(item, {"coordinating"})
        command_ref = _require_text(payload, "command_ref")
        current["command_ref"] = command_ref
        return "executing", current, {"command_ref": command_ref}

    if action == "resolve":
        _need_status(item, {"executing"})
        report_ref = _require_text(payload, "report_ref")
        current["resolution"] = {"report_ref": report_ref, "resolved_by": actor}
        return "resolved", current, {"report_ref": report_ref}

    if action == "cancel":
        _need_status(item, {"pending", "assessed"})
        reason = _require_text(payload, "reason")
        current["cancellation"] = {"reason": reason, "cancelled_by": actor}
        return "cancelled", current, {"reason": reason}

    raise DomainError("unknown_action", "不支持的操作")
