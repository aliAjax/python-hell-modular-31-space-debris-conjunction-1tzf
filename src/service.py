from . import domain, rules
from .domain import DomainError, ConflictError


class Service:
    def __init__(self, repository):
        self.repository = repository

    def create_item(self, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.CREATE_ROLES:
            raise DomainError("forbidden", "当前角色不能创建此类业务记录", 403)
        normalized = domain.normalize_create(payload)
        stable_key = normalized.pop("_stable_key")
        return self.repository.create_item(
            rules.ENTITY_TYPE, stable_key, rules.INITIAL_STATUS, normalized, actor, role
        )

    def add_source(self, item_id, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能提交来源记录", 403)
        item = self.repository.get_item(item_id)
        normalized = domain.normalize_source(payload)
        if region and rules.ENFORCE_REGION and role != "regulator" and normalized.get("region") and normalized["region"] != region:
            raise DomainError("region_mismatch", "来源记录不属于当前管辖区域", 403)
        result = self.repository.add_source(
            item_id,
            normalized.pop("source_type"),
            normalized.pop("external_id"),
            normalized,
            normalized.pop("observed_at"),
            actor,
            role,
        )
        # 记录已归并时，来源同时归入协调案并刷新依据
        if item.get("case_id"):
            self.repository.attach_source_to_case(result["id"], item["case_id"])
            self._refresh_case_basis(item["case_id"])
        return result

    def act(self, item_id, action, payload, actor, role, expected_version=None, region=None, org=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        item = self.repository.get_item(item_id)
        if item.get("case_id"):
            raise DomainError(
                "item_merged",
                "该记录已归并到协调案，请通过协调案接口操作",
                409,
                payload={"case_id": item["case_id"]},
            )
        allowed = rules.ACTION_ROLES.get(action, set())
        if role not in allowed:
            raise DomainError("forbidden", "当前角色不能执行该操作", 403)
        if rules.ENFORCE_REGION and action in rules.REGION_SENSITIVE_ACTIONS and region and role != "regulator":
            if item["payload"].get("region") != region:
                raise DomainError("region_mismatch", "不能处理其他区域的记录", 403)
        if action in rules.ACTION_REQUIRES_VERSION and expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)
        try:
            new_status, new_payload, event_payload = rules.apply_action(item, action, payload, actor, role, org=org)
        except DomainError as exc:
            if exc.code == "opinion_denied":
                self.repository.record_denial(
                    item_id, None, actor, role, exc.code,
                    {"action": action, "message": str(exc)},
                )
            raise
        self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload, expected_version
        )
        return self.get_item(item_id)

    def get_item(self, item_id):
        item = self.repository.get_item(item_id)
        item["sources"] = self.repository.list_sources(item_id)
        item["audit"] = self.repository.audit_trail(item_id)
        item["assessment"] = rules.assess(item["payload"])
        return item

    def list_items(self, status=None):
        return self.repository.list_items(status)

    def state(self):
        return self.repository.state_summary()

    # ---- 协调案 ----

    def _require_coordinator(self, actor, role):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role != "coordinator":
            raise DomainError("forbidden", "只有协调员可以执行该操作", 403)

    def merge_cases(self, item_ids, actor, role):
        self._require_coordinator(actor, role)
        normalized = domain.normalize_merge({"item_ids": item_ids})
        items = [self.repository.get_item(item_id) for item_id in normalized["item_ids"]]
        base_payload = items[0]["payload"]
        for item in items[1:]:
            if not rules.same_conjunction(base_payload, item["payload"]):
                raise DomainError(
                    "not_same_conjunction",
                    "物体对不同或交会时刻相差超过 %s 小时，不能归并为同一协调案" % rules.MERGE_WINDOW_HOURS,
                )
        for item in items:
            if item.get("case_id"):
                raise ConflictError(
                    "merge_conflict",
                    "记录已被其他协调案归并",
                    payload={"conflict_item_id": item["id"], "case_id": item["case_id"], "latest_version": item["version"]},
                )
        members = []
        for item in items:
            sources = self.repository.list_sources(item["id"])
            members.append({
                "item": item,
                "sources": sources,
                "snapshot": {"status": item["status"], "payload": item["payload"]},
            })
        payload, primary_id = rules.build_case_payload(members)
        # 归并燃料重新结算：已带入的已下发指令占用合计不得超过预算，否则退回并列出先占方
        primary = payload["primary_object_id"]
        ledger = payload["fuel_ledger"].get(primary, {})
        commanded = [o for o in ledger.get("occupations", []) if o["status"] == "commanded"]
        occupied = sum(float(o["fuel"]) for o in commanded)
        budget = float(ledger.get("budget", 0))
        if occupied > budget:
            first = commanded[0] if commanded else None
            raise DomainError(
                "fuel_budget_exceeded",
                "归并后同一卫星燃料占用超过预算，方案退回",
                409,
                payload={
                    "budget": budget,
                    "occupied": occupied,
                    "first_occupier": first["ref"] if first else None,
                    "first_occupier_fuel": first["fuel"] if first else None,
                },
            )
        for member in members:
            member["member_role"] = "primary" if member["item"]["id"] == primary_id else "member"
        case_key = "case:%s|%s|%s" % (
            payload["primary_object_id"], payload["secondary_object_id"], payload["tca"],
        )
        initial_status = "executing" if payload.get("command") else "open"
        case = self.repository.create_case(payload, case_key, members, actor, role, initial_status=initial_status)
        return self.get_case(case["id"])

    def split_case(self, case_id, actor, role, expected_version=None):
        self._require_coordinator(actor, role)
        if expected_version is None:
            raise DomainError("expected_version_required", "拆回需要 expected_version", 400)
        return self.repository.split_case(case_id, actor, role, expected_version)

    def get_case(self, case_id):
        case = self.repository.get_case(case_id)
        return case

    def list_cases(self):
        return self.repository.list_cases()

    def case_act(self, case_id, action, payload, actor, role, expected_version=None, org=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        case = self.repository.get_case(case_id)
        allowed = rules.CASE_ACTION_ROLES.get(action, set())
        if role not in allowed:
            raise DomainError("forbidden", "当前角色不能执行该操作", 403)
        if action in rules.CASE_ACTIONS_REQUIRES_VERSION and expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)
        try:
            new_status, new_payload, event_payload = rules.apply_case_action(
                case, action, payload, actor, role, org=org
            )
        except DomainError as exc:
            if exc.code == "opinion_denied":
                primary = next(
                    (m for m in case["members"] if m.get("member_role") == "primary"),
                    case["members"][0] if case["members"] else None,
                )
                self.repository.record_denial(
                    primary["id"] if primary else None, case_id, actor, role, exc.code,
                    {"action": action, "message": str(exc)},
                )
            raise
        return self.repository.apply_case_action(
            case_id, action, actor, role, new_status, new_payload, event_payload, expected_version
        )

    def add_case_source(self, case_id, payload, actor, role):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能提交来源记录", 403)
        case = self.repository.get_case(case_id)
        normalized = domain.normalize_source(payload)
        result = self.repository.add_case_source(
            case_id,
            normalized.pop("source_type"),
            normalized.pop("external_id"),
            normalized,
            normalized.pop("observed_at"),
            actor,
            role,
        )
        self._refresh_case_basis(case_id)
        return result

    def _refresh_case_basis(self, case_id):
        case = self.repository.get_case(case_id)
        members = []
        for item in case["members"]:
            members.append({"item": item, "sources": self.repository.list_sources(item["id"])})
        basis, _history = rules.select_basis(members)
        if basis is None:
            return case
        payload = case["payload"]
        current_basis = payload.get("basis")
        basis_changed = current_basis is None or current_basis.get("source_id") != basis["source_id"]
        invalidated = False
        if basis_changed:
            for approval in payload.get("approvals", []):
                if approval["status"] == "active":
                    approval["status"] = "invalidated"
                    approval["invalidated_by_basis"] = current_basis.get("observed_at") if current_basis else None
                    invalidated = True
            payload["basis"] = basis
            payload["miss_distance_m"] = basis["miss_distance_m"]
            payload["covariance_m"] = basis["covariance_m"]
            rules._rebuild_fuel_ledger(payload)
        # 历史依据 = 除最新观测外的全部旧观测（按时间排序）
        all_descriptors = []
        for member in members:
            for source in member["sources"]:
                all_descriptors.append(rules._basis_from_source(source))
        all_descriptors.sort(key=lambda item: rules.parse_iso(item["observed_at"]))
        payload["basis_history"] = [
            d for d in all_descriptors
            if not (d["source_id"] == basis["source_id"])
        ]
        new_status = "assessed" if invalidated else case["status"]
        return self.repository.update_case_payload(case_id, payload, case["version"], status=new_status)
