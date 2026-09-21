"""驾驶舱端到端领域测试。

用例对应县级季度调度叙事：四个项目按统一里程碑上报投资、产能、就业、增收与
生态风险；季度调度同时下调一个项目产能、提升另一个生态风险并补录农户收益，
验证排序、预警、决策依据同步变化；另覆盖项目合并、口径生效日期、跨年度目标、
批注权限与已发布视图的不可变性。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from service.analytics import build_view, compare_views
from service.engine import Cockpit
from service.models import (
    METRIC_FARMERS,
    METRIC_INCOME,
    METRIC_INVESTMENT,
    METRIC_RISK,
    DomainError,
)
from service.storage import EventStore


def _new_cockpit() -> Cockpit:
    store = EventStore()  # 内存日志
    tmp = tempfile.mkdtemp()
    return Cockpit(store, Path(tmp) / "published")


def _seed_portfolio(cockpit: Cockpit) -> None:
    """构造四个项目、口径、跨年度目标、里程碑与 Q2 前的指标事实。"""

    intake = cockpit.intake

    # 口径：v2 自 2026-04-01 生效，旧口径投资金额折算系数 1.05
    intake({
        "type": "caliber_defined",
        "occurred_on": "2026-04-01",
        "source": "县统计局",
        "source_batch": "caliber-2026Q2",
        "version": "v2-统稿",
        "label": "2026年林业产值统稿口径",
        "coefficients": {"investment": 1.05, "income": 1.0},
    })

    projects = [
        ("P-YC", "黄精药材种植基地", "药材"),
        ("P-KY", "森林康养中心", "康养"),
        ("P-TS", "珍稀树种特色林", "特色林"),
        ("P-SL", "竹海森林人家集群", "森林人家"),
    ]
    for pid, name, category in projects:
        intake({
            "type": "project_registered",
            "occurred_on": "2025-11-15",
            "source": "县发改局",
            "source_batch": "立项-2025Q4",
            "project_id": pid,
            "name": name,
            "category": category,
        })

    # 跨年度目标（2026 年度）
    targets = {
        "P-YC": {"investment": 1000, "income": 500},
        "P-KY": {"investment": 1500, "income": 400},
        "P-TS": {"investment": 600, "income": 300},
        "P-SL": {"investment": 400, "income": 200},
    }
    for pid, metrics in targets.items():
        for metric, value in metrics.items():
            intake({
                "type": "target_set",
                "occurred_on": "2026-01-10",
                "source": "县政府",
                "source_batch": "年度目标-2026",
                "project_id": pid,
                "metric": metric,
                "year": 2026,
                "value": value,
            })

    # 统一里程碑：康养中心“用地落实”已逾期
    milestones = [
        ("P-YC", "立项批复", "2025-12-01", "2025-11-28"),
        ("P-YC", "主体开工", "2026-03-01", "2026-03-05"),
        ("P-KY", "立项批复", "2025-12-01", "2025-12-10"),
        ("P-KY", "用地落实", "2026-03-31", None),
        ("P-TS", "立项批复", "2025-12-15", "2025-12-12"),
        ("P-TS", "主体开工", "2026-04-01", "2026-04-02"),
        ("P-SL", "立项批复", "2025-12-20", "2025-12-18"),
    ]
    for pid, name, due, reached in milestones:
        intake({
            "type": "milestone_scheduled",
            "occurred_on": "2025-12-01",
            "source": "县发改局",
            "source_batch": "里程碑计划",
            "project_id": pid,
            "milestone": name,
            "due_on": due,
        })
        if reached:
            intake({
                "type": "milestone_reached",
                "occurred_on": reached,
                "source": "县发改局",
                "source_batch": "里程碑核验",
                "project_id": pid,
                "milestone": name,
            })

    # 指标事实（不同单位、不同批次）
    facts = [
        # 药材：投资 800（其中一笔按 v2 口径上报），收益正常 220
        ("P-YC", "investment_recorded", "2026-02-20", 600, "县财政局", "v1", None),
        ("P-YC", "investment_recorded", "2026-05-20", 200, "县财政局", "v2-统稿", None),
        ("P-YC", "income_recorded", "2026-05-30", 220, "乡镇A", None, None),
        ("P-YC", "capacity_recorded", "2026-04-01", None, "县林业局", None, 900),
        ("P-YC", "employment_recorded", "2026-04-01", None, "乡镇A", None, 120),
        ("P-YC", "farmer_count_recorded", "2026-01-15", None, "乡镇A", None, 250),
        ("P-YC", "farmer_count_recorded", "2026-04-15", None, "乡镇A", None, 268),
        ("P-YC", "risk_recorded", "2026-04-15", None, "县林业局", None, "低"),
        # 康养：投资 1200，收益 200，产能 60，用地逾期
        ("P-KY", "investment_recorded", "2026-03-10", 1200, "县财政局", "v1", None),
        ("P-KY", "income_recorded", "2026-05-30", 200, "乡镇B", None, None),
        ("P-KY", "capacity_recorded", "2026-04-10", None, "县文旅局", None, 60),
        ("P-KY", "employment_recorded", "2026-04-10", None, "乡镇B", None, 80),
        ("P-KY", "farmer_count_recorded", "2026-04-10", None, "乡镇B", None, 90),
        ("P-KY", "risk_recorded", "2026-04-10", None, "县林业局", None, "中"),
        # 特色林：平稳
        ("P-TS", "investment_recorded", "2026-03-15", 500, "县财政局", "v1", None),
        ("P-TS", "income_recorded", "2026-05-25", 150, "乡镇A", None, None),
        ("P-TS", "capacity_recorded", "2026-04-01", None, "县林业局", None, 300),
        ("P-TS", "employment_recorded", "2026-04-01", None, "乡镇A", None, 60),
        ("P-TS", "farmer_count_recorded", "2026-04-01", None, "乡镇A", None, 150),
        ("P-TS", "risk_recorded", "2026-04-01", None, "县林业局", None, "低"),
        # 森林人家：风险中
        ("P-SL", "investment_recorded", "2026-03-20", 300, "县财政局", "v1", None),
        ("P-SL", "income_recorded", "2026-05-20", 90, "乡镇C", None, None),
        ("P-SL", "capacity_recorded", "2026-04-01", None, "县文旅局", None, 45),
        ("P-SL", "employment_recorded", "2026-04-01", None, "乡镇C", None, 40),
        ("P-SL", "farmer_count_recorded", "2026-04-01", None, "乡镇C", None, 60),
        ("P-SL", "risk_recorded", "2026-04-01", None, "县林业局", None, "中"),
    ]
    level_keys = {
        "capacity_recorded": "capacity",
        "employment_recorded": "jobs",
        "farmer_count_recorded": "farmers",
        "risk_recorded": "level",
    }
    for pid, etype, on, amount, source, version, level_value in facts:
        payload: dict = {"project_id": pid}
        batch = f"{source}-{on[:7]}"
        if amount is not None:
            payload["amount"] = amount
            if version:
                payload["reported_version"] = version
        else:
            payload[level_keys[etype]] = level_value
        intake({
            "type": etype,
            "occurred_on": on,
            "recorded_at": f"{on}T10:00:00+00:00",
            "source": source,
            "source_batch": batch,
            **payload,
        })


class DashboardTest(unittest.TestCase):
    """季度调度主叙事。"""

    def setUp(self) -> None:
        self.cockpit = _new_cockpit()
        _seed_portfolio(self.cockpit)

    def test_unified_milestones_and_caliber_normalization(self) -> None:
        view = build_view(self.cockpit.store, as_of="2026-06-30")
        rows = {r["project_id"]: r for r in view["projects"]}

        # v2 口径当日生效
        self.assertEqual(view["caliber"]["version"], "v2-统稿")
        # 药材投资：600（v1）+ 200*1.05（v2 折算）= 810
        self.assertAlmostEqual(
            rows["P-YC"]["metrics"][METRIC_INVESTMENT]["ytd"], 810.0, places=2
        )
        # 四个在统项目（未合并时）
        self.assertEqual(view["portfolio"]["active_projects"], 4)
        # 带农户数取最新观测
        self.assertEqual(rows["P-YC"]["metrics"][METRIC_FARMERS]["value"], 268)
        self.assertEqual(
            rows["P-YC"]["metrics"][METRIC_FARMERS]["change_from_previous"], 18
        )

    def test_overdue_milestone_raises_red_alert_and_funding_decision(self) -> None:
        view = build_view(self.cockpit.store, as_of="2026-06-30")
        ky = next(r for r in view["projects"] if r["project_id"] == "P-KY")
        codes = {a["code"] for a in ky["alerts"]}
        self.assertIn("schedule_overdue", codes)
        # 不能只报完成率：决策建议明确资金转向逾期里程碑
        decision = next(d for d in view["decisions"] if d["project_id"] == "P-KY")
        self.assertIn("里程碑", decision["action"] + decision["reallocate_to"])

    def test_late_income_supplement_counts_in_occurred_year(self) -> None:
        # Q1 收益在 6 月补录；另有一笔 2025 年度收益跨年补录
        self.cockpit.intake({
            "type": "income_recorded",
            "occurred_on": "2026-03-31",
            "recorded_at": "2026-06-18T09:00:00+00:00",
            "source": "乡镇A",
            "source_batch": "补录-2026Q2",
            "project_id": "P-YC",
            "amount": 80,
        })
        self.cockpit.intake({
            "type": "income_recorded",
            "occurred_on": "2025-12-20",
            "recorded_at": "2026-06-19T09:00:00+00:00",
            "source": "乡镇A",
            "source_batch": "跨年补录",
            "project_id": "P-YC",
            "amount": 50,
        })
        view = build_view(self.cockpit.store, as_of="2026-06-30")
        yc = next(r for r in view["projects"] if r["project_id"] == "P-YC")
        # 年内补录 80 进入 YTD：220 + 80 = 300
        self.assertAlmostEqual(yc["metrics"][METRIC_INCOME]["ytd"], 300.0)
        # 跨年补录 50 只进累计：300 + 50 = 350
        self.assertAlmostEqual(yc["metrics"][METRIC_INCOME]["cumulative"], 350.0)
        # 补录可追溯
        supplemented = yc["metrics"][METRIC_INCOME]["supplemented"]
        self.assertEqual({s["amount"] for s in supplemented}, {80, 50})
        self.assertTrue(all(s["recorded_at"] > s["occurred_on"] for s in supplemented))

    def test_quarterly_scenario_changes_rank_alerts_and_decisions(self) -> None:
        # 先做一笔年内收益补录（基线事实）
        self.cockpit.intake({
            "type": "income_recorded",
            "occurred_on": "2026-03-31",
            "recorded_at": "2026-06-18T09:00:00+00:00",
            "source": "乡镇A",
            "source_batch": "补录-2026Q2",
            "project_id": "P-YC",
            "amount": 80,
        })
        # 登记季度调度情景
        self.cockpit.intake({
            "type": "scenario_registered",
            "occurred_on": "2026-06-25",
            "source": "县发改局",
            "source_batch": "季度调度",
            "name": "Q2调度",
            "title": "2026年第二季度调度情景",
            "description": "康养产能下调、森林人家生态风险升级",
        })
        # 下调康养产能 60 -> 40
        self.cockpit.intake({
            "type": "capacity_recorded",
            "occurred_on": "2026-06-26",
            "source": "县文旅局",
            "source_batch": "季度调度",
            "project_id": "P-KY",
            "capacity": 40,
        }, scenario="Q2调度")
        # 提升森林人家生态风险并触发约束
        self.cockpit.intake({
            "type": "risk_recorded",
            "occurred_on": "2026-06-27",
            "source": "县林业局",
            "source_batch": "季度调度",
            "project_id": "P-SL",
            "level": "高",
        }, scenario="Q2调度")
        self.cockpit.intake({
            "type": "constraint_triggered",
            "occurred_on": "2026-06-27",
            "source": "县林业局",
            "source_batch": "季度调度",
            "project_id": "P-SL",
            "constraint": "天然林保护红线",
        }, scenario="Q2调度")

        baseline = build_view(self.cockpit.store, as_of="2026-06-30")
        scenario = build_view(
            self.cockpit.store, as_of="2026-06-30", scenario="Q2调度"
        )
        diff = compare_views(baseline, scenario)

        ky = next(c for c in diff["project_changes"] if c["project_id"] == "P-KY")
        sl = next(c for c in diff["project_changes"] if c["project_id"] == "P-SL")

        # 产能下调 -20，组合口径同步
        self.assertEqual(ky["capacity_delta"], -20)
        self.assertEqual(diff["portfolio_delta"]["capacity"], -20)
        # 康养评分下降
        self.assertLess(ky["score_scenario"], ky["score_base"])
        # 排序同步变化：基线康养垫底、森林人家在其前一位；
        # 森林人家风险升级+约束触发后名次下滑，与康养发生对调
        self.assertGreater(ky["rank_base"], sl["rank_base"])
        self.assertLess(ky["rank_scenario"], sl["rank_scenario"])
        self.assertLess(sl["rank_delta"], 0)
        # 森林人家风险中->高、新增红色生态约束预警
        self.assertEqual(sl["risk_change"], {"from": "中", "to": "高"})
        self.assertIn("eco_constraint_active", sl["new_alerts"])
        self.assertEqual(diff["portfolio_delta"]["constrained_projects"], 1)
        # 决策依据同步：情景视图要求冻结森林人家新增投资
        sl_decision = next(
            d for d in scenario["decisions"] if d["project_id"] == "P-SL"
        )
        self.assertEqual(sl_decision["priority"], 1)
        self.assertIn("冻结", sl_decision["action"])
        # 基线视图不受情景叠加影响
        base_sl = next(r for r in baseline["projects"] if r["project_id"] == "P-SL")
        self.assertEqual(base_sl["metrics"][METRIC_RISK]["level"], "中")
        self.assertEqual(base_sl["constraints"], [])

    def test_scenario_cannot_change_structure(self) -> None:
        self.cockpit.intake({
            "type": "scenario_registered",
            "occurred_on": "2026-06-25",
            "source": "县发改局",
            "name": "非法情景",
        })
        with self.assertRaises(DomainError):
            self.cockpit.intake({
                "type": "project_merged",
                "occurred_on": "2026-06-26",
                "source": "县发改局",
                "merged_id": "P-SL",
                "survivor_id": "P-KY",
            }, scenario="非法情景")

    def test_caliber_effective_date_is_enforced(self) -> None:
        # v2 口径 4 月生效，不能用于 3 月业务
        with self.assertRaises(DomainError):
            self.cockpit.intake({
                "type": "investment_recorded",
                "occurred_on": "2026-03-01",
                "source": "县财政局",
                "project_id": "P-YC",
                "amount": 100,
                "reported_version": "v2-统稿",
            })

    def test_as_of_replay_ignores_future_events(self) -> None:
        early = build_view(self.cockpit.store, as_of="2026-02-28")
        # 2 月底前只有一笔药材投资 600
        self.assertAlmostEqual(
            early["portfolio"]["investment_ytd"], 600.0, places=2
        )


class AnnotationPermissionTest(unittest.TestCase):
    """权限不同的批注不得互相覆盖。"""

    def setUp(self) -> None:
        self.cockpit = _new_cockpit()
        _seed_portfolio(self.cockpit)

    def test_annotations_append_only_and_audience_scoped(self) -> None:
        self.cockpit.intake({
            "type": "annotation_added",
            "occurred_on": "2026-06-28",
            "source": "县林业局-张某",
            "role": "林业部门",
            "target_type": "project",
            "target_id": "P-KY",
            "text": "康养中心二期涉及天然林比例复核中，建议暂缓授信",
            "audience": ["林业部门", "领导"],
        })
        self.cockpit.intake({
            "type": "annotation_added",
            "occurred_on": "2026-06-29",
            "source": "乡镇B-李某",
            "role": "乡镇",
            "target_type": "project",
            "target_id": "P-KY",
            "text": "用地手续预计 7 月补齐",
            "audience": ["乡镇", "发改部门", "领导"],
        })

        # 林业部门只看到自己那条
        forestry = self.cockpit.annotations("林业部门", "project", "P-KY")
        self.assertEqual(len(forestry), 1)
        self.assertIn("天然林", forestry[0]["text"])
        # 乡镇只看到自己那条：不同权限的批注互不可见、更不能覆盖
        township = self.cockpit.annotations("乡镇", "project", "P-KY")
        self.assertEqual(len(township), 1)
        self.assertIn("用地手续", township[0]["text"])
        # 领导两条都能看到，且按时间保留
        leader = self.cockpit.annotations("领导", "project", "P-KY")
        self.assertEqual(len(leader), 2)
        self.assertEqual([a["author"] for a in leader], ["林业部门", "乡镇"])
        # 无关角色看不到
        self.assertEqual(self.cockpit.annotations("项目单位", "project", "P-KY"), [])

    def test_empty_annotation_rejected(self) -> None:
        with self.assertRaises(DomainError):
            self.cockpit.intake({
                "type": "annotation_added",
                "source": "县林业局",
                "role": "林业部门",
                "target_type": "portfolio",
                "text": "   ",
            })


class MergeTest(unittest.TestCase):
    """项目合并：指标归集、历史保留、不重复计数。"""

    def setUp(self) -> None:
        self.cockpit = _new_cockpit()

    def test_merge_aggregates_and_keeps_history(self) -> None:
        for pid, name in (("P-A", "森林人家一号"), ("P-B", "森林人家二号")):
            self.cockpit.intake({
                "type": "project_registered",
                "occurred_on": "2025-10-01",
                "source": "县发改局",
                "project_id": pid,
                "name": name,
                "category": "森林人家",
            })
        self.cockpit.intake({
            "type": "investment_recorded",
            "occurred_on": "2026-02-01",
            "source": "县财政局",
            "project_id": "P-A",
            "amount": 100,
        })
        self.cockpit.intake({
            "type": "investment_recorded",
            "occurred_on": "2026-03-01",
            "source": "县财政局",
            "project_id": "P-B",
            "amount": 40,
        })
        self.cockpit.intake({
            "type": "project_merged",
            "occurred_on": "2026-05-01",
            "source": "县发改局",
            "source_batch": "机构整合",
            "merged_id": "P-B",
            "survivor_id": "P-A",
        })
        # 合并后 P-B 仍可接收补录（票据滞后到达），自动归集到存续方
        self.cockpit.intake({
            "type": "investment_recorded",
            "occurred_on": "2026-06-01",
            "source": "县财政局",
            "project_id": "P-B",
            "amount": 10,
        })

        view = build_view(self.cockpit.store, as_of="2026-06-30")
        self.assertEqual(view["portfolio"]["active_projects"], 1)
        self.assertEqual(view["portfolio"]["merged_projects"], 1)
        row = view["projects"][0]
        self.assertEqual(row["project_id"], "P-A")
        self.assertEqual([m["project_id"] for m in row["merged_from"]], ["P-B"])
        self.assertAlmostEqual(row["metrics"][METRIC_INVESTMENT]["ytd"], 150.0)

        # 合并日前的历史视图仍是两个项目
        before = build_view(self.cockpit.store, as_of="2026-04-30")
        self.assertEqual(before["portfolio"]["active_projects"], 2)


class PublishAndRecoveryTest(unittest.TestCase):
    """已发布视图不可变，系统恢复后保持原版本。"""

    def test_published_view_survives_recovery_and_new_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            cockpit = Cockpit(EventStore(data_dir / "events.jsonl"),
                              data_dir / "published")
            _seed_portfolio(cockpit)

            view = build_view(cockpit.store, as_of="2026-06-30")
            view["version"] = cockpit.version_fingerprint(None, "2026-06-30")
            cockpit.publish(view, name="二季度调度会视图", published_by="领导")
            published = cockpit.load_published("view-0001")
            fingerprint_before = published["version"]["fingerprint"]
            investment_before = published["portfolio"]["investment_ytd"]

            # 发布后又发生 7 月新事件
            cockpit.intake({
                "type": "investment_recorded",
                "occurred_on": "2026-07-15",
                "source": "县财政局",
                "project_id": "P-YC",
                "amount": 300,
            })

            # 模拟系统恢复：从同一数据目录重新构建，重放事件日志
            recovered = Cockpit(EventStore(data_dir / "events.jsonl"),
                                data_dir / "published")
            republished = recovered.load_published("view-0001")
            self.assertEqual(
                republished["version"]["fingerprint"], fingerprint_before
            )
            self.assertEqual(
                republished["portfolio"]["investment_ytd"], investment_before
            )
            self.assertTrue(republished["immutable"])
            # 当前视图则已包含新事件
            current = build_view(recovered.store, as_of="2026-07-31")
            self.assertGreater(
                current["portfolio"]["investment_ytd"],
                republished["portfolio"]["investment_ytd"],
            )
            # 事件来源批次完整保留
            batches = {
                e.source_batch for e in recovered.store.all_events(None)
            }
            self.assertIn("caliber-2026Q2", batches)
            self.assertIn("立项-2025Q4", batches)


class StorageRecoveryTest(unittest.TestCase):
    """崩溃后半行日志被截断，已落盘事件全部恢复。"""

    def test_torn_tail_line_is_truncated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            store = EventStore(path)
            store.append(
                "scenario_registered",
                {"name": "S1"},
                occurred_on="2026-01-01",
                source="test",
                source_batch="b1",
            )
            store.append(
                "scenario_registered",
                {"name": "S2"},
                occurred_on="2026-01-02",
                source="test",
                source_batch="b1",
            )
            # 模拟第三条事件写了一半时崩溃
            with path.open("a", encoding="utf-8") as fh:
                fh.write('{"seq": 3, "event_id": "evt-bad", "event_type": "sc')
            recovered = EventStore(path)
            names = [
                e.payload["name"] for e in recovered.all_events(None)
            ]
            self.assertEqual(names, ["S1", "S2"])
            # 恢复后可以继续正常追加，序号连续
            event = recovered.append(
                "scenario_registered",
                {"name": "S3"},
                occurred_on="2026-01-03",
                source="test",
                source_batch="b1",
            )
            self.assertEqual(event.seq, 3)


class ValidationTest(unittest.TestCase):
    """命令侧生命周期与口径校验。"""

    def setUp(self) -> None:
        self.cockpit = _new_cockpit()
        self.cockpit.intake({
            "type": "project_registered", "occurred_on": "2025-10-01",
            "source": "县发改局", "project_id": "P-X", "name": "试验项目",
            "category": "药材",
        })

    def test_milestone_must_be_scheduled_from_catalog(self) -> None:
        with self.assertRaises(DomainError):
            self.cockpit.intake({
                "type": "milestone_reached", "occurred_on": "2026-01-01",
                "source": "县发改局", "project_id": "P-X", "milestone": "中期验收",
            })
        with self.assertRaises(DomainError):
            self.cockpit.intake({
                "type": "milestone_scheduled", "occurred_on": "2025-12-01",
                "source": "县发改局", "project_id": "P-X",
                "milestone": "自造里程碑", "due_on": "2026-06-01",
            })

    def test_constraint_lifecycle_must_pair(self) -> None:
        with self.assertRaises(DomainError):
            self.cockpit.intake({
                "type": "constraint_lifted", "occurred_on": "2026-02-01",
                "source": "县林业局", "project_id": "P-X",
                "constraint": "天然林保护红线",
            })
        self.cockpit.intake({
            "type": "constraint_triggered", "occurred_on": "2026-02-01",
            "source": "县林业局", "project_id": "P-X",
            "constraint": "天然林保护红线",
        })
        with self.assertRaises(DomainError):
            self.cockpit.intake({
                "type": "constraint_triggered", "occurred_on": "2026-02-05",
                "source": "县林业局", "project_id": "P-X",
                "constraint": "天然林保护红线",
            })
        self.cockpit.intake({
            "type": "constraint_lifted", "occurred_on": "2026-03-01",
            "source": "县林业局", "project_id": "P-X",
            "constraint": "天然林保护红线",
        })


if __name__ == "__main__":
    unittest.main()
