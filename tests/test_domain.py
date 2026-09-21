"""领域规则测试：事件链、版本切片、口径、合并、情景、批注与快照。"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from service.domain import (
    MetricType as M,
    PermissionError_,
    ValidationError,
)
from service.projection import PortfolioViewBuilder
from service.scenarios import ScenarioService
from service.store import EventStore
from tests.fixtures import AS_OF, build_store


class EventChainTest(unittest.TestCase):
    """统一里程碑、来源版本、补报与更正。"""

    def setUp(self) -> None:
        self.store = build_store()
        self.builder = PortfolioViewBuilder(self.store)

    def test_seq_monotonic_and_sources_kept(self) -> None:
        before = self.store.seq
        event = self.store.report_metric(
            project_id="P-YC", metric=M.INCOME, value=999,
            effective_date="2026-07-31", source="乡村振兴局",
        )
        self.assertEqual(event.seq, before + 1)
        view = self.builder.build(as_of=AS_OF)
        row = view["projects"]["P-YC"]
        self.assertIn("乡村振兴局", row["metrics"]["income"]["2026"]["sources"])

    def test_idempotent_submission_rejected(self) -> None:
        kwargs = dict(
            project_id="P-YC", metric=M.INCOME, value=100,
            effective_date="2026-07-31", source="乡村振兴局",
            idempotency_key="INC-20260731-PYC",
        )
        self.store.report_metric(**kwargs)
        with self.assertRaises(ValidationError):
            self.store.report_metric(**kwargs)

    def test_late_backfill_enters_new_view_but_not_old_snapshot(self) -> None:
        # 先发布 Q2 视图（截至 6 月 30 日）
        q2 = self.builder.build(as_of=date(2026, 6, 30))
        q2_income = q2["projects"]["P-KY"]["metrics"]["income"]["2026"]["value"]
        snapshot = self.store.publish_snapshot(
            name="Q2调度视图", payload=q2, created_by="李主任",
            at_seq=q2["spec"]["at_seq"], as_of=date(2026, 6, 30),
            caliber_version=1, scenario_id=None,
        )
        frozen_income = snapshot.payload["projects"]["P-KY"]["metrics"]["income"]["2026"]["value"]

        # 9 月补录一笔 6 月就已发生的增收（晚入库、早发生）
        self.store.report_metric(
            project_id="P-KY", metric=M.INCOME, value=9_000_000,
            effective_date="2026-06-30", source="乡村振兴局(补报)",
            note="凭证延迟归档",
        )
        new_q2 = self.builder.build(as_of=date(2026, 6, 30))
        # 新视图反映补报
        self.assertGreater(
            new_q2["projects"]["P-KY"]["metrics"]["income"]["2026"]["value"],
            q2_income,
        )
        # 已发布视图保持原版本
        self.assertEqual(
            self.store.snapshots["Q2调度视图"].payload["projects"]["P-KY"]
            ["metrics"]["income"]["2026"]["value"],
            frozen_income,
        )

    def test_correction_via_supersede_keeps_old_event_on_chain(self) -> None:
        # 取 6 月 30 日那笔最新累计报送作为被更正对象
        target_seq = max(
            e.seq for e in self.store.metric_events
            if e.project_id == "P-RJ" and e.metric == M.INCOME
            and e.effective_date == date(2026, 6, 30)
        )
        before = self.builder.build(as_of=AS_OF)
        before_value = before["projects"]["P-RJ"]["metrics"]["income"]["2026"]["value"]
        self.assertEqual(before_value, 800_000)
        self.store.report_metric(
            project_id="P-RJ", metric=M.INCOME, value=1_200_000,
            effective_date="2026-06-30", source="乡村振兴局",
            supersedes=target_seq, note="原报送少计 40 万",
        )
        # 旧事件仍在链上
        self.assertTrue(any(e.seq == target_seq for e in self.store.metric_events))
        after = self.builder.build(as_of=AS_OF)
        self.assertEqual(
            after["projects"]["P-RJ"]["metrics"]["income"]["2026"]["value"],
            1_200_000,
        )
        # 但在更正事件之前的历史切片里，仍是旧值
        old_view = self.builder.build(as_of=AS_OF, at_seq=target_seq)
        self.assertEqual(
            old_view["projects"]["P-RJ"]["metrics"]["income"]["2026"]["value"],
            before_value,
        )

    def test_view_at_past_seq_reproduces_history(self) -> None:
        mid = self.store.seq
        self.store.report_risk(
            project_id="P-RJ", risk_level=5, constraint=True,
            effective_date="2026-09-01", source="生态环境局",
        )
        current = self.builder.build(as_of=AS_OF)
        past = self.builder.build(as_of=AS_OF, at_seq=mid)
        self.assertTrue(current["projects"]["P-RJ"]["risk"]["constraint"])
        self.assertFalse(past["projects"]["P-RJ"]["risk"]["constraint"])
        self.assertEqual(past["spec"]["at_seq"], mid)


class CaliberTest(unittest.TestCase):
    """统计口径生效日期与重述。"""

    def test_new_caliber_restates_new_view_but_frozen_snapshot_keeps_old(self) -> None:
        store = build_store(with_caliber_v2=True)
        builder = PortfolioViewBuilder(store)
        # 7 月后有一笔按 v2 口径报送的产能
        store.report_metric(
            project_id="P-TS", metric=M.CAPACITY, value=1500,
            effective_date="2026-09-20", source="农业农村局",
        )
        view_v2 = builder.build(as_of=AS_OF)
        v2_value = view_v2["projects"]["P-TS"]["metrics"]["capacity"]["2026"]["value"]
        view_v1 = builder.build(as_of=AS_OF, caliber_version=1)
        v1_value = view_v1["projects"]["P-TS"]["metrics"]["capacity"]["2026"]["value"]
        # v2 视图：9 月新值 1500（v2 口径），6 月旧值 900 按 v1 记录、重述 ×1.25
        self.assertEqual(v2_value, 1500 + 900 * 0)  # 取最新时点报送，不累加
        self.assertEqual(v2_value, 1500)
        # v1 视图：9 月值折算回 v1
        self.assertAlmostEqual(v1_value, 1500 / 1.25, places=2)
        self.assertEqual(view_v2["spec"]["caliber_version"], 2)
        self.assertEqual(view_v1["spec"]["caliber_version"], 1)

    def test_caliber_in_force_respects_effective_date(self) -> None:
        store = build_store(with_caliber_v2=True)
        builder = PortfolioViewBuilder(store)
        self.assertEqual(builder.caliber_in_force(date(2026, 6, 30)), 1)
        self.assertEqual(builder.caliber_in_force(date(2026, 7, 1)), 2)
        view = builder.build(as_of=date(2026, 6, 30))
        self.assertEqual(view["spec"]["caliber_version"], 1)


class MergeTest(unittest.TestCase):
    """项目合并：生效日归集、历史独立、延期与风险不洗白。"""

    def setUp(self) -> None:
        self.store = build_store()
        self.builder = PortfolioViewBuilder(self.store)

    def test_merge_aggregates_after_effective_date_only(self) -> None:
        before = self.builder.build(as_of=date(2026, 7, 31))
        self.assertIn("P-RJ", before["ranking"])
        self.store.merge_projects(
            source_id="P-RJ", target_id="P-KY",
            effective_date="2026-08-01", source="县政府",
            note="森林人家纳入康养小镇统一运营",
        )
        after = self.builder.build(as_of=AS_OF)
        # 生效后 P-RJ 不再独立出现，指标归集到 P-KY
        self.assertNotIn("P-RJ", after["projects"])
        ky = after["projects"]["P-KY"]
        self.assertIn("P-RJ", ky["merged_from"])
        self.assertEqual(ky["members"], ["P-KY", "P-RJ"])
        self.assertGreater(
            ky["metrics"]["investment"]["2026"]["value"], 2000
        )
        # 合并前的历史视图仍独立
        hist = self.builder.build(as_of=date(2026, 7, 31))
        self.assertIn("P-RJ", hist["projects"])
        self.assertNotIn("P-RJ", hist["projects"]["P-KY"]["members"])

    def test_merge_inherits_delay_and_takes_worst_risk(self) -> None:
        # P-RJ 有已闭环延期 37 天；P-KY 有在建逾期
        self.store.merge_projects(
            source_id="P-RJ", target_id="P-KY",
            effective_date="2026-08-01", source="县政府",
        )
        view = self.builder.build(as_of=AS_OF)
        ky = view["projects"]["P-KY"]
        # 两条延期历史都保留，取最严重
        self.assertGreaterEqual(ky["schedule"]["worst_delay_days"], 143)
        names = {m["name"] for m in ky["schedule"]["delayed_milestones"]}
        self.assertIn("示范户改造", names)

    def test_cannot_merge_twice_or_self(self) -> None:
        self.store.merge_projects(
            source_id="P-RJ", target_id="P-KY",
            effective_date="2026-08-01", source="县政府",
        )
        with self.assertRaises(ValidationError):
            self.store.merge_projects(
                source_id="P-RJ", target_id="P-TS",
                effective_date="2026-09-01", source="县政府",
            )
        with self.assertRaises(ValidationError):
            self.store.merge_projects(
                source_id="P-KY", target_id="P-KY",
                effective_date="2026-09-01", source="县政府",
            )


class TargetTest(unittest.TestCase):
    """年度与跨年度目标。"""

    def setUp(self) -> None:
        self.store = build_store()
        self.builder = PortfolioViewBuilder(self.store)

    def test_annual_completion_rate_and_target_versioning(self) -> None:
        view = self.builder.build(as_of=AS_OF)
        ky = view["projects"]["P-KY"]["annual_completion"]["investment"]
        self.assertEqual(ky["value"], 2000)
        self.assertEqual(ky["target"], 4000)
        self.assertEqual(ky["rate"], 0.5)
        # 调整目标：新目标事件追加，完成率同步变化，旧 seq 仍可追溯
        self.store.set_annual_target(
            project_id="P-KY", year=2026, metric=M.INVESTMENT,
            target=5000, source="发改局(调整)",
        )
        view2 = self.builder.build(as_of=AS_OF)
        self.assertEqual(
            view2["projects"]["P-KY"]["annual_completion"]["investment"]["target"],
            5000,
        )

    def test_multiyear_target_spans_years(self) -> None:
        view = self.builder.build(as_of=AS_OF)
        key = "2026-2027:capacity"
        self.assertIn(key, view["projects"]["P-TS"]["multiyear_completion"])
        info = view["projects"]["P-TS"]["multiyear_completion"][key]
        self.assertEqual(info["target"], 5000)
        self.assertEqual(info["value"], 900)
        self.assertEqual(info["rate"], 0.18)


class AnnotationPermissionTest(unittest.TestCase):
    """不同角色/单位的批注不得互相覆盖。"""

    def setUp(self) -> None:
        self.store = build_store()

    def test_only_author_or_admin_can_edit_or_delete(self) -> None:
        ann = self.store.add_annotation(
            author="张财政", role="财政局", content="投资到位口径需复核",
            project_id="P-KY",
        )
        # 其他单位的人不能改、不能删
        with self.assertRaises(PermissionError_):
            self.store.edit_annotation(
                annotation_id=ann.id, editor="李农业", role="农业农村局",
                content="被覆盖内容",
            )
        with self.assertRaises(PermissionError_):
            self.store.delete_annotation(
                annotation_id=ann.id, editor="李农业", role="农业农村局",
            )
        # 原内容未被覆盖
        self.assertEqual(self.store.annotations[ann.id].content, "投资到位口径需复核")
        # 作者本人可改，留痕
        self.store.edit_annotation(
            annotation_id=ann.id, editor="张财政", role="财政局",
            content="投资到位口径已复核（附凭证）",
        )
        self.assertEqual(len(self.store.annotations[ann.id].edits), 1)
        self.assertEqual(self.store.annotations[ann.id].edits[0].old_content,
                         "投资到位口径需复核")
        # admin 可删除（软删除，保留记录）
        self.store.delete_annotation(
            annotation_id=ann.id, editor="系统管理员", role="admin",
        )
        self.assertTrue(self.store.annotations[ann.id].deleted)
        self.assertEqual(self.store.annotations[ann.id].deleted_by, "系统管理员")


class QuarterlyDispatchTest(unittest.TestCase):
    """题目核心情景：下调产能、提升风险、补录收益，观察同步变化。"""

    def setUp(self) -> None:
        self.store = build_store()
        self.builder = PortfolioViewBuilder(self.store)
        self.svc = ScenarioService(self.store)
        self.base = self.builder.build(as_of=AS_OF)

    def test_scenario_changes_ranking_warnings_and_decisions_without_baseline_mutation(self) -> None:
        base_income = self.base["projects"]["P-KY"]["metrics"]["income"]["2026"]["value"]
        base_ts_rank = self.base["ranking"].index("P-TS")

        scenario = self.svc.create(
            name="Q3季度调度", as_of=AS_OF, created_by="王县长",
        )
        # 1) 下调药材产能 200
        self.svc.adjust_metric(
            scenario.id, project_id="P-YC", metric=M.CAPACITY, delta=-200,
        )
        # 2) 提升特色林生态风险至 5 级并触发生态约束
        self.svc.override_risk(
            scenario.id, project_id="P-TS", risk_level=5, constraint=True,
        )
        # 3) 补录康养农户收益 300 万（6 月已发生）
        self.svc.backfill_income(
            scenario.id, project_id="P-KY", metric=M.INCOME,
            value=8_000_000, effective_date="2026-06-30",
            source="乡村振兴局(补录)",
        )

        scen_view = self.svc.evaluate(scenario.id)
        comparison = self.svc.compare(scenario.id)

        # 排序同步变化：特色林约束触发后跃居第一
        self.assertEqual(scen_view["ranking"][0], "P-TS")
        self.assertGreater(base_ts_rank, scen_view["ranking"].index("P-TS"))
        # 新预警：特色林生态约束
        new_warns = {(w["project_id"], w["type"]) for w in comparison["warnings_new"]}
        self.assertIn(("P-TS", "ecological_constraint"), new_warns)
        # 决策依据同步变化：新增暂停注资建议，评分上升
        ts = scen_view["projects"]["P-TS"]
        self.assertTrue(any("暂停新增资金注入" in r for r in ts["decision"]["recommendations"]))
        ts_move = next(c for c in comparison["score_changes"] if c["project_id"] == "P-TS")
        self.assertGreater(ts_move["score_move"], 0)
        self.assertIn("暂停新增资金注入，先完成生态整改与约束解除审查",
                      ts_move["recommendations_added"])
        # 补录收益只影响康养收益口径，不洗白其延期
        self.assertEqual(
            scen_view["projects"]["P-KY"]["metrics"]["income"]["2026"]["value"],
            base_income + 3_000_000,
        )
        self.assertGreaterEqual(
            scen_view["projects"]["P-KY"]["schedule"]["worst_delay_days"], 143
        )
        # 药材产能下调，缺口扩大
        yc = scen_view["projects"]["P-YC"]
        self.assertEqual(
            yc["metrics"]["capacity"]["2026"]["value"], 500 - 200
        )
        # 基线未被修改
        self.assertEqual(
            self.builder.build(as_of=AS_OF)["projects"]["P-TS"]["risk"]["constraint"],
            False,
        )
        self.assertEqual(len(self.store.metric_events), len(
            [e for e in self.store.metric_events if not e.scenario_backfill]))

    def test_commit_appends_events_and_old_snapshot_stays_frozen(self) -> None:
        # 固化前发布基线视图
        snapshot = self.store.publish_snapshot(
            name="Q3调度前基线", payload=self.base, created_by="王县长",
            at_seq=self.base["spec"]["at_seq"], as_of=AS_OF,
            caliber_version=1, scenario_id=None,
        )
        base_seq = self.store.seq
        frozen_ts = json.dumps(snapshot.payload["projects"]["P-TS"],
                               sort_keys=True, ensure_ascii=False)

        scenario = self.svc.create(name="Q3调度-待批", as_of=AS_OF, created_by="王县长")
        self.svc.adjust_metric(scenario.id, project_id="P-YC",
                               metric=M.CAPACITY, delta=-200)
        self.svc.override_risk(scenario.id, project_id="P-TS",
                               risk_level=5, constraint=True)
        self.svc.backfill_income(
            scenario.id, project_id="P-KY", metric=M.INCOME,
            value=8_000_000, effective_date="2026-06-30",
            source="乡村振兴局(补录)",
        )
        result = self.svc.commit(scenario.id, committed_by="常务副县长")
        self.assertGreater(result["committed_seq"], base_seq)
        self.assertTrue(scenario.committed)

        # 固化后新视图反映真实变化
        new_view = self.builder.build(as_of=AS_OF)
        self.assertTrue(new_view["projects"]["P-TS"]["risk"]["constraint"])
        # 已发布旧视图内容字节级不变
        frozen_now = json.dumps(
            self.store.snapshots["Q3调度前基线"].payload["projects"]["P-TS"],
            sort_keys=True, ensure_ascii=False,
        )
        self.assertEqual(frozen_ts, frozen_now)
        # 固化事件追加在链上，可审计来源
        self.assertTrue(any(
            "情景固化" in e.source for e in self.store.risk_events
            if e.project_id == "P-TS"
        ))

    def test_committed_scenario_cannot_be_reopened(self) -> None:
        scenario = self.svc.create(name="一次性", as_of=AS_OF, created_by="王县长")
        self.svc.commit(scenario.id, committed_by="王县长")
        with self.assertRaises(ValidationError):
            self.svc.adjust_metric(scenario.id, project_id="P-YC",
                                   metric=M.CAPACITY, delta=-1)


class WarningsTest(unittest.TestCase):
    """延期、生态约束、带农户数变化的预警触发。"""

    def setUp(self) -> None:
        self.store = build_store()
        self.builder = PortfolioViewBuilder(self.store)

    def test_three_families_of_warnings_present(self) -> None:
        view = self.builder.build(as_of=AS_OF)
        types = {(w["project_id"], w["type"]) for w in view["warnings"]}
        self.assertIn(("P-KY", "schedule_delay"), types)
        self.assertIn(("P-YC", "farmers_drop"), types)  # 220 -> 200
        # P-KY 在建逾期为高严重度
        delay = next(w for w in view["warnings"]
                     if w["project_id"] == "P-KY" and w["type"] == "schedule_delay")
        self.assertEqual(delay["severity"], "high")

    def test_farmers_warning_clears_when_number_recovers(self) -> None:
        before = self.builder.build(as_of=AS_OF)
        self.assertTrue(any(
            w["project_id"] == "P-YC" and w["type"] == "farmers_drop"
            for w in before["warnings"]
        ))
        # Q3 报送带农户数恢复到 230
        self.store.report_metric(
            project_id="P-YC", metric=M.FARMERS, value=230,
            effective_date="2026-09-20", source="林业局",
        )
        after = self.builder.build(as_of=AS_OF)
        self.assertFalse(any(
            w["project_id"] == "P-YC" and w["type"] == "farmers_drop"
            for w in after["warnings"]
        ))


class PersistenceTest(unittest.TestCase):
    """落盘恢复：已发布视图保持原版本。"""

    def test_save_and_load_roundtrip(self) -> None:
        store = build_store()
        builder = PortfolioViewBuilder(store)
        view = builder.build(as_of=AS_OF)
        store.publish_snapshot(
            name="Q2视图", payload=view, created_by="李主任",
            at_seq=view["spec"]["at_seq"], as_of=AS_OF,
            caliber_version=1, scenario_id=None,
        )
        store.add_annotation(author="张财政", role="财政局", content="测试批注")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            store.save_file(path)
            restored = EventStore.load_file(path)

        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual(restored.seq, store.seq)
        self.assertEqual(
            restored.snapshots["Q2视图"].payload["ranking"],
            view["ranking"],
        )
        rebuilt = PortfolioViewBuilder(restored).build(as_of=AS_OF)
        self.assertEqual(rebuilt["ranking"], view["ranking"])
        self.assertEqual(restored.annotations["ANN-0001"].content, "测试批注")

    def test_survives_new_events_after_restore(self) -> None:
        store = build_store()
        builder = PortfolioViewBuilder(store)
        view = builder.build(as_of=date(2026, 6, 30))
        payload_before = json.dumps(view, sort_keys=True, ensure_ascii=False)
        store.publish_snapshot(
            name="半年视图", payload=view, created_by="李主任",
            at_seq=view["spec"]["at_seq"], as_of=date(2026, 6, 30),
            caliber_version=1, scenario_id=None,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            store.save_file(path)
            restored = EventStore.load_file(path)
            assert restored is not None
            # 恢复后继续接收新事件
            restored.report_risk(
                project_id="P-YC", risk_level=5, constraint=True,
                effective_date="2026-09-15", source="生态环境局",
            )
            payload_after = json.dumps(
                restored.snapshots["半年视图"].payload,
                sort_keys=True, ensure_ascii=False,
            )
        self.assertEqual(payload_before, payload_after)


    def test_scenario_roundtrip(self) -> None:
        store = build_store()
        svc = ScenarioService(store)
        scenario = svc.create(name="Q3试算", as_of=AS_OF, created_by="王县长")
        svc.adjust_metric(scenario.id, project_id="P-YC", metric=M.CAPACITY, delta=-200)
        svc.override_risk(scenario.id, project_id="P-TS", risk_level=5, constraint=True)
        svc.backfill_income(
            scenario.id, project_id="P-KY", metric=M.INCOME,
            value=8_000_000, effective_date="2026-06-30",
            source="乡村振兴局(补录)",
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            store.save_file(path)
            restored = EventStore.load_file(path)
        assert restored is not None
        scn = restored.scenarios[scenario.id]
        self.assertEqual(scn.base_seq, scenario.base_seq)
        self.assertEqual(scn.as_of, AS_OF)
        self.assertEqual(scn.metric_deltas[("P-YC", M.CAPACITY)], -200)
        self.assertEqual(scn.risk_overrides["P-TS"], (5, True))
        self.assertEqual(len(scn.backfills), 1)
        # 恢复后情景仍可试算，结论一致
        restored_view = PortfolioViewBuilder(restored).build(
            as_of=AS_OF, scenario_id=scn.id,
        )
        self.assertEqual(restored_view["ranking"][0], "P-TS")
        # 恢复后新建情景编号不冲突
        next_scn = ScenarioService(restored).create(
            name="Q4试算", as_of=AS_OF, created_by="王县长",
        )
        self.assertNotEqual(next_scn.id, scenario.id)


if __name__ == "__main__":
    unittest.main()
