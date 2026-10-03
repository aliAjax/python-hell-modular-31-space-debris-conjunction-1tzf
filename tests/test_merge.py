import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, DomainError


def make_payload(primary="SAT-1", secondary="DEB-9", tca="2026-09-28T12:00:00+00:00",
                 distance=120, covariance=100, budget=10, track_age=1, orgs=None):
    return {
        "primary_object_id": primary,
        "secondary_object_id": secondary,
        "tca": tca,
        "miss_distance_m": distance,
        "covariance_m": covariance,
        "fuel_budget_m_s": budget,
        "track_age_hours": track_age,
        "operating_organizations": orgs if orgs is not None else ["Org-A", "Org-B"],
    }


class MergeCaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _create(self, **kw):
        return self.service.create_item(make_payload(**kw), "analyst-1", "analyst")

    def _source(self, item_id, observed_at, distance=100, covariance=100, stype="station", ext=None):
        return self.service.add_source(
            item_id,
            {
                "source_type": stype,
                "external_id": ext or (stype + "-" + observed_at),
                "observed_at": observed_at,
                "miss_distance_m": distance,
                "covariance_m": covariance,
            },
            "analyst-1", "analyst",
        )

    def test_merge_keeps_sources_as_evidence_and_newest_observation_is_basis(self):
        # 同一物理交会：本站一条、外部目录一条
        local = self._create()
        external = self._create(tca="2026-09-28T12:05:00+00:00")  # 时刻相近
        # 外部目录的观测更新
        self._source(external["id"], "2026-09-27T08:00:00+00:00", distance=80, covariance=90,
                     stype="external_catalog", ext="EXT-1")
        # 本站观测较旧
        self._source(local["id"], "2026-09-26T08:00:00+00:00", distance=200, covariance=150,
                     stype="station", ext="LOC-1")

        case = self.service.merge_cases([local["id"], external["id"]], "coordinator-1", "coordinator")
        self.assertEqual(case["status"], "open")
        members = self.service.get_case(case["id"])["members"]
        self.assertEqual(len(members), 2)
        # 来源留证：两条记录的来源都还在
        self.assertEqual(len(case["sources"]), 2)
        # 当前依据 = 观测时刻最新的那条
        self.assertEqual(case["basis"]["source_type"], "external_catalog")
        self.assertEqual(case["basis"]["observed_at"], "2026-09-27T08:00:00+00:00")
        self.assertEqual(case["payload"]["miss_distance_m"], 80)
        # 旧观测进历史
        self.assertGreaterEqual(len(case["basis_history"]), 1)
        # 成员记录被标记为已归并
        for m in members:
            self.assertEqual(m["status"], "merged")

    def test_far_apart_tca_is_not_same_conjunction(self):
        a = self._create()
        b = self._create(tca="2026-09-29T12:00:00+00:00")  # 差 24 小时
        with self.assertRaises(DomainError) as ctx:
            self.service.merge_cases([a["id"], b["id"]], "coordinator-1", "coordinator")
        self.assertEqual(ctx.exception.code, "not_same_conjunction")

    def test_different_objects_not_merged(self):
        a = self._create(primary="SAT-1")
        b = self._create(primary="SAT-9")
        with self.assertRaises(DomainError) as ctx:
            self.service.merge_cases([a["id"], b["id"]], "coordinator-1", "coordinator")
        self.assertEqual(ctx.exception.code, "not_same_conjunction")

    def test_late_old_observation_goes_to_history_only(self):
        local = self._create()
        external = self._create(tca="2026-09-28T12:05:00+00:00")
        self._source(external["id"], "2026-09-27T08:00:00+00:00", distance=80, stype="external_catalog", ext="EXT-1")
        case = self.service.merge_cases([local["id"], external["id"]], "coordinator-1", "coordinator")
        # 归并后才到的更旧观测
        self.service.add_case_source(
            case["id"],
            {"source_type": "station", "external_id": "LOC-LATE",
             "observed_at": "2026-09-25T00:00:00+00:00", "miss_distance_m": 999, "covariance_m": 999},
            "analyst-1", "analyst",
        )
        refreshed = self.service.get_case(case["id"])
        # 依据不变
        self.assertEqual(refreshed["basis"]["observed_at"], "2026-09-27T08:00:00+00:00")
        self.assertEqual(refreshed["payload"]["miss_distance_m"], 80)
        # 旧观测进历史
        self.assertTrue(any(h["observed_at"] == "2026-09-25T00:00:00+00:00" for h in refreshed["basis_history"]))

    def test_new_observation_invalidates_active_approval_but_command_keeps_basis(self):
        local = self._create(budget=10)
        external = self._create(tca="2026-09-28T12:05:00+00:00", budget=10)
        self._source(external["id"], "2026-09-27T08:00:00+00:00", distance=80, stype="external_catalog", ext="EXT-1")
        case = self.service.merge_cases([local["id"], external["id"]], "coordinator-1", "coordinator")
        case = self.service.case_act(case["id"], "assess", {"hours_to_tca": 18}, "analyst-1", "analyst", case["version"])
        case = self.service.case_act(case["id"], "approve", {"fuel_cost_m_s": 2, "maneuver_window": "w1"},
                                      "coordinator-1", "coordinator", case["version"])
        self.assertEqual(case["status"], "coordinating")
        old_basis = case["basis"]["observed_at"]
        # 新观测到达，依据更新
        self.service.add_case_source(
            case["id"],
            {"source_type": "station", "external_id": "LOC-NEW",
             "observed_at": "2026-09-27T12:00:00+00:00", "miss_distance_m": 60, "covariance_m": 80},
            "analyst-1", "analyst",
        )
        refreshed = self.service.get_case(case["id"])
        self.assertEqual(refreshed["basis"]["observed_at"], "2026-09-27T12:00:00+00:00")
        # 未下发的批准立即失效
        active = [a for a in refreshed["payload"]["approvals"] if a["status"] == "active"]
        invalid = [a for a in refreshed["payload"]["approvals"] if a["status"] == "invalidated"]
        self.assertEqual(len(active), 0)
        self.assertEqual(len(invalid), 1)
        self.assertEqual(invalid[0]["basis_observed_at"], old_basis)
        # 需要重新评估/批准
        refreshed = self.service.case_act(refreshed["id"], "assess", {"hours_to_tca": 16}, "analyst-1", "analyst", refreshed["version"])
        refreshed = self.service.case_act(refreshed["id"], "approve", {"fuel_cost_m_s": 2, "maneuver_window": "w2"},
                                           "coordinator-1", "coordinator", refreshed["version"])
        self.assertEqual(refreshed["status"], "coordinating")
        # 下发指令后再来新观测，指令保留原依据
        self.service.case_act(refreshed["id"], "execute", {"command_ref": "CMD-1"}, "operator-1", "operator", refreshed["version"])
        self.service.add_case_source(
            case["id"],
            {"source_type": "external_catalog", "external_id": "EXT-NEW",
             "observed_at": "2026-09-27T18:00:00+00:00", "miss_distance_m": 55, "covariance_m": 70},
            "analyst-1", "analyst",
        )
        final = self.service.get_case(case["id"])
        cmd = final["payload"]["command"]
        self.assertEqual(cmd["command_ref"], "CMD-1")
        # 指令依据 = 下发时的依据（第二次批准时的观测），不随后续更新改变
        self.assertEqual(cmd["basis_observed_at"], "2026-09-27T12:00:00+00:00")
        self.assertEqual(final["basis"]["observed_at"], "2026-09-27T18:00:00+00:00")

    def test_over_budget_plan_returned_with_first_occupier(self):
        a = self._create(budget=5)
        b = self._create(tca="2026-09-28T12:05:00+00:00", budget=5)
        case = self.service.merge_cases([a["id"], b["id"]], "coordinator-1", "coordinator")
        case = self.service.case_act(case["id"], "assess", {"hours_to_tca": 18}, "analyst-1", "analyst", case["version"])
        case = self.service.case_act(case["id"], "approve", {"fuel_cost_m_s": 3, "maneuver_window": "w1"},
                                      "coordinator-1", "coordinator", case["version"])
        # 再来一个规避方案，燃料超预算
        with self.assertRaises(DomainError) as ctx:
            self.service.case_act(case["id"], "approve", {"fuel_cost_m_s": 3, "maneuver_window": "w2"},
                                  "coordinator-1", "coordinator", case["version"])
        self.assertEqual(ctx.exception.code, "fuel_budget_exceeded")
        # 列出先占方
        self.assertIn("first_occupier", ctx.exception.payload if hasattr(ctx.exception, "payload") else {})

    def test_concurrent_merge_only_one_passes(self):
        a = self._create()
        b = self._create(tca="2026-09-28T12:05:00+00:00")
        # 协调员1 先提交
        self.service.merge_cases([a["id"], b["id"]], "coordinator-1", "coordinator")
        # 协调员2 同时提交同一批归并
        with self.assertRaises(ConflictError) as ctx:
            self.service.merge_cases([a["id"], b["id"]], "coordinator-2", "coordinator")
        self.assertEqual(ctx.exception.code, "merge_conflict")
        body = ctx.exception.payload
        self.assertEqual(body["conflict_item_id"], a["id"])
        self.assertIn("latest_version", body)

    def test_operator_can_only_opine_on_own_org_object(self):
        # 单条记录（未归并）上的越权表态
        item = self._create(orgs=["Org-A"], secondary="DEB-7")
        item = self.service.act(item["id"], "assess", {"hours_to_tca": 12}, "analyst-1", "analyst", item["version"])
        # Org-B 的操作员对 Org-A 的物体表态 -> 拒绝
        with self.assertRaises(DomainError) as ctx:
            self.service.act(
                item["id"], "record_opinion",
                {"operator": "Org-B", "opinion": "reject", "reason": "unsafe"},
                "operator-9", "operator", item["version"],
            )
        self.assertEqual(ctx.exception.code, "opinion_denied")
        # 留审计
        audit = self.repo.audit_trail(item["id"])
        self.assertTrue(any(e["event_type"] == "opinion_denied" for e in audit))
        # 本机构表态放行
        item = self.service.act(
            item["id"], "record_opinion",
            {"operator": "Org-A", "opinion": "approve"},
            "operator-1", "operator", item["version"],
        )
        self.assertEqual(len(item["payload"]["opinions"]), 1)

    def test_operator_cannot_impersonate_other_org(self):
        item = self._create(orgs=["Org-A", "Org-B"])
        item = self.service.act(item["id"], "assess", {"hours_to_tca": 12}, "analyst-1", "analyst", item["version"])
        # X-Org 头表明身份是 Org-A，却声称代表 Org-B -> 越权
        with self.assertRaises(DomainError) as ctx:
            self.service.act(
                item["id"], "record_opinion",
                {"operator": "Org-B", "opinion": "reject"},
                "operator-1", "operator", item["version"], org="Org-A",
            )
        self.assertEqual(ctx.exception.code, "opinion_denied")

    def test_issued_command_retained_through_merge(self):
        a = self._create(budget=10)
        b = self._create(tca="2026-09-28T12:05:00+00:00", budget=10)
        # a 记录已下发规避指令
        a = self.service.act(a["id"], "assess", {"hours_to_tca": 18}, "analyst-1", "analyst", a["version"])
        a = self.service.act(a["id"], "approve", {"fuel_cost_m_s": 2, "maneuver_window": "w1"},
                              "coordinator-1", "coordinator", a["version"])
        a = self.service.act(a["id"], "execute", {"command_ref": "CMD-A"}, "operator-1", "operator", a["version"])
        # 归并
        case = self.service.merge_cases([a["id"], b["id"]], "coordinator-1", "coordinator")
        self.assertEqual(case["status"], "executing")
        # 已下发指令带入协调案，保留原依据
        self.assertIsNotNone(case["payload"]["command"])
        self.assertEqual(case["payload"]["command"]["command_ref"], "CMD-A")
        self.assertEqual(case["payload"]["command"]["retained_from_item"], a["id"])
        # 燃料重新结算：指令占用仍在
        primary = case["payload"]["primary_object_id"]
        occupations = case["payload"]["fuel_ledger"][primary]["occupations"]
        self.assertEqual(len(occupations), 1)
        self.assertEqual(occupations[0]["status"], "commanded")

    def test_merge_over_budget_rejected_with_first_occupier(self):
        # 两条记录各自已下发规避指令（各占 6），预算 10，归并后合计超预算
        a = self._create(budget=10)
        b = self._create(tca="2026-09-28T12:05:00+00:00", budget=10)
        for item in (a, b):
            item = self.service.act(item["id"], "assess", {"hours_to_tca": 18}, "analyst-1", "analyst", item["version"])
            item = self.service.act(item["id"], "approve", {"fuel_cost_m_s": 6, "maneuver_window": "w"},
                                      "coordinator-1", "coordinator", item["version"])
            self.service.act(item["id"], "execute", {"command_ref": "CMD-%d" % item["id"]}, "operator-1", "operator", item["version"])
        with self.assertRaises(DomainError) as ctx:
            self.service.merge_cases([a["id"], b["id"]], "coordinator-1", "coordinator")
        self.assertEqual(ctx.exception.code, "fuel_budget_exceeded")
        self.assertEqual(ctx.exception.payload["first_occupier"], "approval:APR-M1")
        self.assertEqual(ctx.exception.payload["occupied"], 12)

    def test_split_restores_members_and_reintegrates_fuel(self):
        a = self._create(budget=10)
        b = self._create(tca="2026-09-28T12:05:00+00:00", budget=10)
        case = self.service.merge_cases([a["id"], b["id"]], "coordinator-1", "coordinator")
        case = self.service.case_act(case["id"], "assess", {"hours_to_tca": 18}, "analyst-1", "analyst", case["version"])
        case = self.service.case_act(case["id"], "approve", {"fuel_cost_m_s": 2, "maneuver_window": "w1"},
                                      "coordinator-1", "coordinator", case["version"])
        # 归并后批准未下发，拆回
        split = self.service.split_case(case["id"], "coordinator-1", "coordinator", case["version"])
        self.assertEqual(split["status"], "split")
        members = self.service.get_case(case["id"])["members"]
        for m in members:
            # 成员恢复为独立记录
            self.assertNotEqual(m["status"], "merged")
        # 拆回后燃料占用重新结算：未下发批准失效
        refreshed_a = self.service.get_item(a["id"])
        self.assertNotIn("approved_maneuver", refreshed_a["payload"])


if __name__ == "__main__":
    unittest.main()
