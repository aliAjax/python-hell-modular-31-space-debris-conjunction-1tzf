import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, DomainError


class MergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _create(self, tca, observed_at, distance=120, covariance=100, budget=5,
                primary="SAT-1", secondary="DEB-9", orgs=("Org-A", "Org-B")):
        return self.service.create_item({
            "primary_object_id": primary,
            "secondary_object_id": secondary,
            "tca": tca,
            "miss_distance_m": distance,
            "covariance_m": covariance,
            "fuel_budget_m_s": budget,
            "track_age_hours": 1,
            "observed_at": observed_at,
            "operating_organizations": list(orgs),
        }, "analyst-1", "analyst")

    def _assess(self, item):
        return self.service.act(item["id"], "assess", {"hours_to_tca": 18}, "analyst-1", "analyst", item["version"])

    def _approve(self, item, fuel):
        return self.service.act(item["id"], "approve", {
            "fuel_cost_m_s": fuel,
            "maneuver_window": "2026-09-28T08:00:00Z/2026-09-28T09:00:00Z",
        }, "coordinator-1", "coordinator", item["version"])

    def _merge(self, survivor, merged, actor="coordinator-1"):
        return self.service.act(survivor["id"], "merge", {
            "other_item_id": merged["id"],
            "other_expected_version": merged["version"],
        }, actor, "coordinator", survivor["version"])

    def test_merge_dedups_same_conjunction_and_keeps_sources(self):
        station = self._create("2026-09-28T12:00:00+00:00", "2026-09-27T10:00:00+00:00", distance=150)
        catalog = self._create("2026-09-28T12:01:00+00:00", "2026-09-27T11:00:00+00:00", distance=90)
        self.service.add_source(station["id"], {
            "source_type": "station", "external_id": "ST-1",
            "observed_at": "2026-09-27T10:00:00+00:00",
            "miss_distance_m": 150, "covariance_m": 100,
        }, "analyst-1", "analyst")
        self.service.add_source(catalog["id"], {
            "source_type": "external_catalog", "external_id": "EXT-1",
            "observed_at": "2026-09-27T11:00:00+00:00",
            "miss_distance_m": 90, "covariance_m": 100,
        }, "analyst-1", "analyst")
        survivor = self._merge(station, catalog)
        # 当前依据取观测时刻较新的外部目录记录
        self.assertEqual(survivor["payload"]["miss_distance_m"], 90)
        self.assertEqual(survivor["payload"]["observed_at"], "2026-09-27T11:00:00+00:00")
        self.assertEqual(survivor["payload"]["basis_origin"], catalog["id"])
        self.assertIn(catalog["id"], survivor["payload"]["merged_items"])
        # 旧依据只进历史
        history = survivor["payload"]["basis_history"]
        self.assertEqual(history[-1]["reason"], "merge_replaced_by_newer")
        self.assertEqual(history[-1]["basis"]["miss_distance_m"], 150)
        merged_item = survivor["merged_item"]
        self.assertEqual(merged_item["status"], "merged")
        self.assertEqual(merged_item["payload"]["merged_into"], station["id"])
        # 来源记录留证：协调案可查到双方来源
        full = self.service.get_item(station["id"])
        self.assertEqual(len(full["sources"]), 1)
        self.assertEqual(len(full["merged_sources"]), 1)
        self.assertEqual(full["merged_sources"][0]["external_id"], "EXT-1")

    def test_merge_with_older_observation_only_goes_to_history(self):
        station = self._create("2026-09-28T12:00:00+00:00", "2026-09-27T12:00:00+00:00", distance=150)
        catalog = self._create("2026-09-28T12:01:00+00:00", "2026-09-27T09:00:00+00:00", distance=90)
        survivor = self._merge(station, catalog)
        self.assertEqual(survivor["payload"]["miss_distance_m"], 150)
        history = survivor["payload"]["basis_history"]
        self.assertEqual(history[-1]["reason"], "merge_older_observation")
        self.assertEqual(history[-1]["basis"]["miss_distance_m"], 90)

    def test_merge_validation_rejects_mismatched_records(self):
        item_a = self._create("2026-09-28T12:00:00+00:00", "2026-09-27T10:00:00+00:00")
        other_pair = self._create("2026-09-28T12:00:30+00:00", "2026-09-27T10:00:00+00:00", secondary="DEB-10")
        with self.assertRaises(DomainError) as context:
            self._merge(item_a, other_pair)
        self.assertEqual(context.exception.code, "object_pair_mismatch")
        far_tca = self._create("2026-09-28T13:00:00+00:00", "2026-09-27T10:00:00+00:00")
        with self.assertRaises(DomainError) as context:
            self._merge(item_a, far_tca)
        self.assertEqual(context.exception.code, "tca_window_exceeded")
        with self.assertRaises(DomainError) as context:
            self._merge(item_a, item_a)
        self.assertEqual(context.exception.code, "invalid_merge")

    def test_merge_resettles_duplicate_fuel_and_over_budget_lists_blocker(self):
        # 同一物理交会被建成两条记录，同一颗卫星的燃料被重复占用
        station = self._assess(self._create("2026-09-28T12:00:00+00:00", "2026-09-27T12:00:00+00:00"))
        catalog = self._assess(self._create("2026-09-28T12:01:00+00:00", "2026-09-27T11:00:00+00:00"))
        station = self._approve(station, 2.5)
        catalog = self._approve(catalog, 2.5)
        # 归并后燃料占用重新结算：被归并方的占用释放，只算一次
        survivor = self._merge(station, catalog)
        self.assertEqual(survivor["status"], "coordinating")
        budget, reservations = self.service._fuel_ledger("SAT-1")
        self.assertEqual(sum(r["fuel_cost_m_s"] for r in reservations), 2.5)
        # 新方案超预算被退回，并列出哪条先占
        third = self._assess(self._create(
            "2026-09-29T08:00:00+00:00", "2026-09-28T08:00:00+00:00", secondary="DEB-20"))
        with self.assertRaises(DomainError) as context:
            self._approve(third, 2.6)
        self.assertEqual(context.exception.code, "fuel_budget_exceeded")
        details = context.exception.details
        self.assertEqual(details["reservations"], [{"item_id": station["id"], "fuel_cost_m_s": 2.5}])
        self.assertEqual(details["budget_m_s"], 5)
        # 不超预算的方案仍可批准
        third = self._approve(third, 2.5)
        self.assertEqual(third["status"], "coordinating")

    def test_basis_update_invalidates_approval(self):
        item = self._approve(self._assess(
            self._create("2026-09-28T12:00:00+00:00", "2026-09-27T10:00:00+00:00")), 2.5)
        self.assertEqual(item["status"], "coordinating")
        item = self.service.act(item["id"], "report_revision", {
            "observed_at": "2026-09-27T13:00:00+00:00",
            "miss_distance_m": 60,
            "covariance_m": 80,
            "source": "station",
        }, "analyst-1", "analyst", item["version"])
        # 轨道依据更新，依赖它的批准立即失效重算
        self.assertEqual(item["status"], "assessed")
        self.assertEqual(item["payload"]["miss_distance_m"], 60)
        invalidated = item["payload"]["invalidated_approvals"]
        self.assertEqual(invalidated[-1]["invalidated_reason"], "basis_updated")
        self.assertNotIn("approved_maneuver", item["payload"])
        # 燃料占用已释放，可以按新依据重新批准
        item = self._approve(item, 2.5)
        self.assertEqual(item["status"], "coordinating")

    def test_stale_revision_only_goes_to_history(self):
        item = self._create("2026-09-28T12:00:00+00:00", "2026-09-27T12:00:00+00:00", distance=150)
        item = self.service.act(item["id"], "report_revision", {
            "observed_at": "2026-09-27T08:00:00+00:00",
            "miss_distance_m": 60,
            "covariance_m": 80,
            "source": "external_catalog",
        }, "analyst-1", "analyst", item["version"])
        # 晚到的旧观测只进历史，不改变当前依据
        self.assertEqual(item["payload"]["miss_distance_m"], 150)
        self.assertEqual(item["payload"]["basis_history"][-1]["reason"], "stale_observation")
        self.assertEqual(len(item["payload"]["revisions"]), 1)

    def test_issued_command_retains_original_basis(self):
        item = self._approve(self._assess(
            self._create("2026-09-28T12:00:00+00:00", "2026-09-27T10:00:00+00:00")), 2.5)
        item = self.service.act(item["id"], "execute", {"command_ref": "CMD-7"}, "operator-1", "operator", item["version"])
        item = self.service.act(item["id"], "report_revision", {
            "observed_at": "2026-09-27T13:00:00+00:00",
            "miss_distance_m": 60,
            "covariance_m": 80,
            "source": "station",
        }, "analyst-1", "analyst", item["version"])
        # 已下发的规避指令保留原依据
        self.assertEqual(item["status"], "executing")
        self.assertEqual(item["payload"]["command_ref"], "CMD-7")
        self.assertEqual(item["payload"]["miss_distance_m"], 120)
        self.assertEqual(item["payload"]["basis_history"][-1]["reason"], "command_issued_retains_basis")

    def test_concurrent_merge_only_one_wins(self):
        station = self._create("2026-09-28T12:00:00+00:00", "2026-09-27T10:00:00+00:00")
        catalog = self._create("2026-09-28T12:01:00+00:00", "2026-09-27T11:00:00+00:00")
        results = {}
        barrier = threading.Barrier(2)

        def attempt(name):
            barrier.wait()
            try:
                self._merge(station, catalog, actor=name)
                results[name] = "ok"
            except DomainError as exc:
                results[name] = exc

        threads = [threading.Thread(target=attempt, args=(name,))
                   for name in ("coordinator-1", "coordinator-2")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        outcomes = sorted("ok" if value == "ok" else "conflict" for value in results.values())
        # 两个协调员同时提交归并时只放行一次
        self.assertEqual(outcomes, ["conflict", "ok"])
        loser = next(value for value in results.values() if value != "ok")
        self.assertEqual(loser.status, 409)
        self.assertIn(loser.code, ("version_conflict", "already_merged"))
        # 另一边返回冲突项和最新版次
        self.assertIn(loser.details["conflict_item_id"], (station["id"], catalog["id"]))
        self.assertEqual(loser.details["current_version"], 2)
        survivor = self.service.get_item(station["id"])
        self.assertIn(catalog["id"], survivor["payload"]["merged_items"])

    def test_split_restores_records_and_resettles_fuel(self):
        station = self._approve(self._assess(self._create(
            "2026-09-28T12:00:00+00:00", "2026-09-27T12:00:00+00:00", budget=4)), 2.5)
        catalog = self._approve(self._assess(self._create(
            "2026-09-28T12:01:00+00:00", "2026-09-27T11:00:00+00:00", budget=4)), 1.5)
        survivor = self._merge(station, catalog)
        self.assertEqual(survivor["status"], "coordinating")
        # 归并期间另一事件占用了剩余额度
        third = self._approve(self._assess(self._create(
            "2026-09-29T08:00:00+00:00", "2026-09-28T08:00:00+00:00", secondary="DEB-20", budget=4)), 1.5)
        self.assertEqual(third["status"], "coordinating")
        # 拆回：燃料重新结算，恢复的方案超预算被退回并列出先占记录
        survivor = self.service.get_item(station["id"])
        survivor = self.service.act(survivor["id"], "split", {
            "merged_item_id": catalog["id"],
        }, "coordinator-1", "coordinator", survivor["version"])
        restored = survivor["restored_item"]
        self.assertEqual(restored["status"], "assessed")
        bounced = restored["payload"]["invalidated_approvals"][-1]
        self.assertEqual(bounced["invalidated_reason"], "fuel_budget_exceeded")
        self.assertEqual(
            [entry["item_id"] for entry in bounced["blocking_reservations"]],
            [station["id"], third["id"]],
        )
        self.assertEqual(survivor["payload"]["merged_items"], [])
        # 协调案自身未被归并方采用依据，拆回后仍是协调中
        self.assertEqual(survivor["status"], "coordinating")

    def test_split_reverts_adopted_basis_and_invalidates_approval(self):
        station = self._approve(self._assess(self._create(
            "2026-09-28T12:00:00+00:00", "2026-09-27T10:00:00+00:00", distance=150)), 2.5)
        catalog = self._create("2026-09-28T12:01:00+00:00", "2026-09-27T11:00:00+00:00", distance=90)
        survivor = self._merge(station, catalog)
        # 归并采用了较新的外部依据，原批准失效
        self.assertEqual(survivor["status"], "assessed")
        self.assertEqual(survivor["payload"]["miss_distance_m"], 90)
        survivor = self._assess(survivor)
        survivor = self._approve(survivor, 2.0)
        survivor = self.service.act(survivor["id"], "split", {
            "merged_item_id": catalog["id"],
        }, "coordinator-1", "coordinator", survivor["version"])
        # 拆回后依据回退到本站原值，依赖新依据的批准失效重算
        self.assertEqual(survivor["payload"]["miss_distance_m"], 150)
        self.assertEqual(survivor["status"], "assessed")
        self.assertEqual(
            survivor["payload"]["invalidated_approvals"][-1]["invalidated_reason"],
            "basis_updated",
        )
        restored = survivor["restored_item"]
        self.assertEqual(restored["status"], "pending")
        self.assertNotIn("merged_into", restored["payload"])

    def test_operator_scope_violation_rejected_and_audited(self):
        item = self._assess(self._create(
            "2026-09-28T12:00:00+00:00", "2026-09-27T10:00:00+00:00", orgs=("Org-A",)))
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "record_opinion", {
                "operator": "Org-B", "opinion": "approve",
            }, "operator-1", "operator", item["version"])
        self.assertEqual(context.exception.status, 403)
        self.assertEqual(context.exception.code, "operator_scope_violation")
        audit = self.service.get_item(item["id"])["audit"]
        rejections = [event for event in audit if event["event_type"] == "opinion_rejected"]
        self.assertEqual(len(rejections), 1)
        self.assertEqual(rejections[0]["payload"]["operator"], "Org-B")
        # 本机构运营方表态不受影响
        item = self.service.act(item["id"], "record_opinion", {
            "operator": "Org-A", "opinion": "approve",
        }, "operator-1", "operator", item["version"])
        self.assertEqual(item["payload"]["opinions"][-1]["operator"], "Org-A")


if __name__ == "__main__":
    unittest.main()
