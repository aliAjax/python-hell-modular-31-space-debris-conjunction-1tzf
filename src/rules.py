import copy
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
    "merge": {"coordinator"},
    "split": {"coordinator"},
}
ENFORCE_REGION = False
REGION_SENSITIVE_ACTIONS = set()
ACTION_REQUIRES_VERSION = {"approve", "execute", "resolve", "cancel", "merge", "split"}

# 同一物理交会的判定窗口：物体对相同且交会时刻相差不超过该秒数
MERGE_TCA_WINDOW_SECONDS = 300
MERGEABLE_STATUSES = {"pending", "assessed", "coordinating", "executing"}
BASIS_FIELDS = ("tca", "miss_distance_m", "covariance_m", "track_age_hours", "observed_at")


def parse_ts(value):
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


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


def apply_action(item, action, payload, actor, role):
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
        if parse_ts(revision["observed_at"]) is None:
            raise DomainError("invalid_timestamp", "observed_at 必须是 ISO 时间")
        current.setdefault("revisions", []).append(revision)
        current_observed = parse_ts(current.get("observed_at"))
        revision_observed = parse_ts(revision["observed_at"])
        is_newer = current_observed is None or revision_observed > current_observed
        if not is_newer:
            # 晚到的旧观测只进历史，不改变当前依据
            current.setdefault("basis_history", []).append({
                "basis": _revision_basis(revision),
                "reason": "stale_observation",
                "origin": revision["source"],
            })
            return status, current, {"revision": revision, "applied": False}
        if status == "executing":
            # 已下发的规避指令保留原依据，新观测只进历史
            current.setdefault("basis_history", []).append({
                "basis": _revision_basis(revision),
                "reason": "command_issued_retains_basis",
                "origin": revision["source"],
            })
            return status, current, {"revision": revision, "applied": False}
        current.setdefault("basis_history", []).append({
            "basis": _basis_of(current),
            "reason": "superseded_by_revision",
            "origin": revision["source"],
        })
        current["miss_distance_m"] = revision["miss_distance_m"]
        current["covariance_m"] = revision["covariance_m"]
        current["observed_at"] = revision["observed_at"]
        current["assessment"] = assess(current)
        if status == "coordinating":
            # 轨道依据更新，依赖它的批准立即失效重算
            _invalidate_approval(current, "basis_updated")
            return "assessed", current, {"revision": revision, "applied": True, "approval_invalidated": True}
        return status, current, {"revision": revision, "applied": True}

    if action == "record_opinion":
        _need_status(item, {"assessed", "coordinating"})
        opinion = _require_text(payload, "opinion").lower()
        if opinion not in {"approve", "reject", "request_review"}:
            raise DomainError("invalid_opinion", "意见必须是 approve、reject 或 request_review")
        operator = _require_text(payload, "operator")
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


def _basis_of(payload):
    return {field: payload.get(field) for field in BASIS_FIELDS}


def _revision_basis(revision):
    return {
        "tca": None,
        "miss_distance_m": revision["miss_distance_m"],
        "covariance_m": revision["covariance_m"],
        "track_age_hours": None,
        "observed_at": revision["observed_at"],
    }


def _apply_basis(payload, basis, origin_item_id):
    for field in BASIS_FIELDS:
        if basis.get(field) is not None:
            payload[field] = basis[field]
    payload["basis_origin"] = origin_item_id
    payload["assessment"] = assess(payload)


def _invalidate_approval(payload, reason, blocking=None):
    maneuver = payload.pop("approved_maneuver", None)
    if maneuver is None:
        return None
    maneuver["invalidated_reason"] = reason
    if blocking:
        maneuver["blocking_reservations"] = blocking
    payload.setdefault("invalidated_approvals", []).append(maneuver)
    return maneuver


def _object_pair(payload):
    return sorted([payload.get("primary_object_id"), payload.get("secondary_object_id")])


def validate_merge(survivor, merged, window_seconds=MERGE_TCA_WINDOW_SECONDS):
    if survivor["id"] == merged["id"]:
        raise DomainError("invalid_merge", "不能将记录与自身归并")
    for item, label in ((survivor, "协调案"), (merged, "被归并记录")):
        if item["status"] == "merged":
            raise DomainError(
                "already_merged",
                "%s已被归并，不能重复归并" % label,
                409,
                {"conflict_item_id": item["id"], "current_version": int(item["version"])},
            )
        if item["status"] not in MERGEABLE_STATUSES:
            raise DomainError(
                "invalid_state",
                "%s当前状态 %s 不允许归并" % (label, item["status"]),
                409,
                {"conflict_item_id": item["id"], "current_version": int(item["version"])},
            )
    if _object_pair(survivor["payload"]) != _object_pair(merged["payload"]):
        raise DomainError("object_pair_mismatch", "物体对不一致，不能归并")
    survivor_tca = parse_ts(survivor["payload"].get("tca"))
    merged_tca = parse_ts(merged["payload"].get("tca"))
    if survivor_tca is None or merged_tca is None:
        raise DomainError("invalid_timestamp", "交会时刻缺失，不能归并")
    gap = abs((survivor_tca - merged_tca).total_seconds())
    if gap > float(window_seconds):
        raise DomainError(
            "tca_window_exceeded",
            "交会时刻相差 %.0f 秒，超过归并窗口" % gap,
            400,
            {"tca_gap_seconds": gap, "window_seconds": float(window_seconds)},
        )


def merge_items_rule(survivor, merged):
    """归并两条接近事件为一个协调案；当前依据取观测时刻较新的那条。"""
    survivor_payload = copy.deepcopy(survivor["payload"])
    merged_payload = copy.deepcopy(merged["payload"])
    survivor_basis = _basis_of(survivor_payload)
    merged_basis = _basis_of(merged_payload)
    survivor_observed = parse_ts(survivor_basis.get("observed_at"))
    merged_observed = parse_ts(merged_basis.get("observed_at"))
    adopt_merged = merged_observed is not None and (
        survivor_observed is None or merged_observed > survivor_observed
    )
    history = survivor_payload.setdefault("basis_history", [])
    if adopt_merged:
        history.append({
            "basis": survivor_basis,
            "reason": "merge_replaced_by_newer",
            "origin_item_id": survivor["id"],
        })
        _apply_basis(survivor_payload, merged_basis, merged["id"])
    else:
        # 被归并方的观测更旧，只进历史留证
        history.append({
            "basis": merged_basis,
            "reason": "merge_older_observation",
            "origin_item_id": merged["id"],
        })
    merged_ids = survivor_payload.setdefault("merged_items", [])
    if merged["id"] not in merged_ids:
        merged_ids.append(merged["id"])
    organizations = list(survivor_payload.get("operating_organizations", []))
    for org in merged_payload.get("operating_organizations", []):
        if org not in organizations:
            organizations.append(org)
    survivor_payload["operating_organizations"] = organizations
    survivor_payload.setdefault("merge_log", []).append({
        "merged_item_id": merged["id"],
        "adopted_basis": adopt_merged,
        "survivor_basis_before": survivor_basis,
    })
    survivor_status = survivor["status"]
    invalidated = None
    if adopt_merged and survivor_status == "coordinating":
        # 轨道依据更新，依赖它的批准立即失效重算，燃料占用随之释放
        invalidated = _invalidate_approval(survivor_payload, "basis_updated")
        survivor_status = "assessed"
    merged_payload["merged_into"] = survivor["id"]
    merged_payload["pre_merge_status"] = merged["status"]
    event = {
        "merged_item_id": merged["id"],
        "adopted_basis_from": merged["id"] if adopt_merged else survivor["id"],
        "approval_invalidated": invalidated is not None,
    }
    return survivor_status, survivor_payload, "merged", merged_payload, event


def split_items_rule(survivor, merged):
    """把已归并的记录拆回独立事件；协调案依据回退到归并前，批准失效重算。"""
    survivor_payload = copy.deepcopy(survivor["payload"])
    merged_payload = copy.deepcopy(merged["payload"])
    if merged_payload.get("merged_into") != survivor["id"] or merged["status"] != "merged":
        raise DomainError(
            "not_merged",
            "该记录未归并到此协调案",
            409,
            {"conflict_item_id": merged["id"], "status": merged["status"]},
        )
    merged_ids = survivor_payload.get("merged_items", [])
    if merged["id"] not in merged_ids:
        raise DomainError("not_merged", "该记录未归并到此协调案", 409, {"conflict_item_id": merged["id"]})
    merged_ids.remove(merged["id"])
    log_entry = None
    remaining_log = []
    for entry in survivor_payload.get("merge_log", []):
        if entry.get("merged_item_id") == merged["id"] and log_entry is None:
            log_entry = entry
        else:
            remaining_log.append(entry)
    survivor_payload["merge_log"] = remaining_log
    survivor_status = survivor["status"]
    basis_reverted = False
    if log_entry and log_entry.get("adopted_basis") and survivor_payload.get("basis_origin") == merged["id"]:
        survivor_payload.setdefault("basis_history", []).append({
            "basis": _basis_of(survivor_payload),
            "reason": "split_reverted",
            "origin_item_id": merged["id"],
        })
        _apply_basis(survivor_payload, log_entry.get("survivor_basis_before", {}), survivor["id"])
        basis_reverted = True
    invalidated = None
    if basis_reverted and survivor_status == "coordinating":
        invalidated = _invalidate_approval(survivor_payload, "basis_updated")
        survivor_status = "assessed"
    restored_status = merged_payload.pop("pre_merge_status", "assessed")
    merged_payload.pop("merged_into", None)
    event = {
        "merged_item_id": merged["id"],
        "restored_status": restored_status,
        "basis_reverted": basis_reverted,
        "approval_invalidated": invalidated is not None,
    }
    return survivor_status, survivor_payload, restored_status, merged_payload, event
