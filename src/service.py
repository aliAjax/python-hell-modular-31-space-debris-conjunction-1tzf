from . import domain, rules
from .domain import DomainError


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
        return result

    def act(self, item_id, action, payload, actor, role, expected_version=None, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        allowed = rules.ACTION_ROLES.get(action, set())
        if role not in allowed:
            raise DomainError("forbidden", "当前角色不能执行该操作", 403)
        if action in rules.ACTION_REQUIRES_VERSION and expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)
        if action == "merge":
            return self._merge(item_id, payload, actor, role, expected_version)
        if action == "split":
            return self._split(item_id, payload, actor, role, expected_version)
        item = self.repository.get_item(item_id)
        if rules.ENFORCE_REGION and action in rules.REGION_SENSITIVE_ACTIONS and region and role != "regulator":
            if item["payload"].get("region") != region:
                raise DomainError("region_mismatch", "不能处理其他区域的记录", 403)
        if action == "record_opinion":
            self._check_operator_scope(item, payload, actor, role)
        new_status, new_payload, event_payload = rules.apply_action(item, action, payload, actor, role)
        if action == "approve":
            self._check_fuel_budget(new_payload)
        self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload, expected_version
        )
        return self.get_item(item_id)

    def _check_operator_scope(self, item, payload, actor, role):
        operator = payload.get("operator")
        organizations = item["payload"].get("operating_organizations", [])
        if operator not in organizations:
            # 越权表态被拒绝并留审计
            self.repository.record_rejection(
                item["id"],
                "opinion_rejected",
                actor,
                role,
                {"operator": operator, "reason": "operator_scope_violation"},
            )
            raise DomainError("operator_scope_violation", "运营方只能对本机构物体表态", 403)

    def _fuel_ledger(self, satellite, overrides=None):
        """同一颗卫星的燃料占用台账：仅协调中（已批准未执行）的方案计入占用。"""
        overrides = overrides or {}
        budget = 0.0
        reservations = []
        for item in self.repository.list_items():
            status = item["status"]
            payload = item["payload"]
            if item["id"] in overrides:
                status, payload = overrides[item["id"]]
            if payload.get("primary_object_id") != satellite:
                continue
            if status in ("merged", "cancelled"):
                continue
            budget = max(budget, float(payload.get("fuel_budget_m_s", 0)))
            if status == "coordinating" and payload.get("approved_maneuver"):
                reservations.append({
                    "item_id": item["id"],
                    "fuel_cost_m_s": float(payload["approved_maneuver"]["fuel_cost_m_s"]),
                })
        reservations.sort(key=lambda entry: entry["item_id"])
        return budget, reservations

    def _check_fuel_budget(self, candidate_payload, overrides=None):
        satellite = candidate_payload.get("primary_object_id")
        cost = float(candidate_payload["approved_maneuver"]["fuel_cost_m_s"])
        budget, reservations = self._fuel_ledger(satellite, overrides)
        budget = max(budget, float(candidate_payload.get("fuel_budget_m_s", 0)))
        reserved = sum(entry["fuel_cost_m_s"] for entry in reservations)
        if reserved + cost > budget + 1e-9:
            # 超预算的方案退回，并列出哪条先占
            raise DomainError(
                "fuel_budget_exceeded",
                "规避燃料超过预算",
                409,
                {
                    "satellite": satellite,
                    "budget_m_s": budget,
                    "reserved_m_s": reserved,
                    "requested_m_s": cost,
                    "reservations": reservations,
                },
            )

    def _merge(self, item_id, payload, actor, role, expected_version):
        other_id = payload.get("other_item_id")
        try:
            other_id = int(other_id)
        except (TypeError, ValueError):
            raise DomainError("field_required", "other_item_id 不能为空")
        other_expected = payload.get("other_expected_version")
        if other_expected is None:
            raise DomainError("expected_version_required", "归并需要 other_expected_version", 400)
        survivor = self.repository.get_item(item_id)
        merged = self.repository.get_item(other_id)
        window = payload.get("tca_window_seconds", rules.MERGE_TCA_WINDOW_SECONDS)
        rules.validate_merge(survivor, merged, window)
        survivor_status, survivor_payload, merged_status, merged_payload, event = rules.merge_items_rule(
            survivor, merged
        )
        survivor_item, merged_item = self.repository.merge_items(
            item_id,
            other_id,
            actor,
            role,
            survivor_status,
            survivor_payload,
            merged_status,
            merged_payload,
            event,
            expected_version,
            other_expected,
        )
        survivor_item["merged_item"] = merged_item
        return survivor_item

    def _split(self, item_id, payload, actor, role, expected_version):
        merged_id = payload.get("merged_item_id")
        try:
            merged_id = int(merged_id)
        except (TypeError, ValueError):
            raise DomainError("field_required", "merged_item_id 不能为空")
        survivor = self.repository.get_item(item_id)
        merged = self.repository.get_item(merged_id)
        survivor_status, survivor_payload, restored_status, restored_payload, event = rules.split_items_rule(
            survivor, merged
        )
        overrides = {
            item_id: (survivor_status, survivor_payload),
            merged_id: (restored_status, restored_payload),
        }
        if restored_status == "coordinating" and restored_payload.get("approved_maneuver"):
            # 拆回后同一颗卫星的燃料占用重新结算，超预算的方案退回
            satellite = restored_payload.get("primary_object_id")
            cost = float(restored_payload["approved_maneuver"]["fuel_cost_m_s"])
            budget, reservations = self._fuel_ledger(satellite, overrides)
            budget = max(budget, float(restored_payload.get("fuel_budget_m_s", 0)))
            blocking = [entry for entry in reservations if entry["item_id"] != merged_id]
            reserved = sum(entry["fuel_cost_m_s"] for entry in blocking)
            if reserved + cost > budget + 1e-9:
                bounced = restored_payload.pop("approved_maneuver")
                bounced["invalidated_reason"] = "fuel_budget_exceeded"
                bounced["blocking_reservations"] = blocking
                restored_payload.setdefault("invalidated_approvals", []).append(bounced)
                restored_status = "assessed"
                event["approval_invalidated"] = True
                event["blocking_reservations"] = blocking
        survivor_item, restored_item = self.repository.split_items(
            item_id,
            merged_id,
            actor,
            role,
            survivor_status,
            survivor_payload,
            restored_status,
            restored_payload,
            event,
            expected_version,
        )
        survivor_item["restored_item"] = restored_item
        return survivor_item

    def get_item(self, item_id):
        item = self.repository.get_item(item_id)
        item["sources"] = self.repository.list_sources(item_id)
        merged_sources = []
        for merged_id in item["payload"].get("merged_items", []):
            merged_sources.extend(self.repository.list_sources(merged_id))
        item["merged_sources"] = merged_sources
        item["audit"] = self.repository.audit_trail(item_id)
        item["assessment"] = rules.assess(item["payload"])
        return item

    def list_items(self, status=None):
        return self.repository.list_items(status)

    def state(self):
        return self.repository.state_summary()
