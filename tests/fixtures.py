"""测试共用：构建接近真实季度调度场景的数据集。"""

from __future__ import annotations

from datetime import date

from service.domain import MetricType as M
from service.store import EventStore

AS_OF = date(2026, 9, 21)

PROJECTS = {
    "P-YC": ("道地药材基地", "药材"),
    "P-KY": ("森林康养小镇", "康养"),
    "P-TS": ("特色经济林", "特色林"),
    "P-RJ": ("森林人家集群", "森林人家"),
}


def build_store(*, with_caliber_v2: bool = False) -> EventStore:
    """构建四个项目 × 五类指标的基线数据。

    - 康养 P-KY：里程碑逾期（5 月计划，至 9 月未完成）
    - 特色林 P-TS：生态风险 3 级
    - 药材 P-YC：带农户数由 220 降至 200
    - 森林人家 P-RJ：有一个已闭环的延期里程碑
    """

    store = EventStore()
    for pid, (name, category) in PROJECTS.items():
        store.register_project(
            project_id=pid, name=name, category=category,
            effective_date="2026-01-01", source="林业局",
        )

    half = {
        # pid: (投资目标/完成, 产能目标/完成, 就业, 增收万元, 农户H1)
        "P-YC": (2000, 800, 1200, 500, 120, 300, 200),
        "P-KY": (4000, 2000, 800, 300, 300, 500, 150),
        "P-TS": (1800, 600, 2000, 900, 80, 100, 300),
        "P-RJ": (1000, 400, 500, 200, 60, 80, 120),
    }
    for pid, (inv_t, inv, cap_t, cap, emp, inc_w, farmers) in half.items():
        store.set_annual_target(project_id=pid, year=2026, metric=M.INVESTMENT,
                                target=inv_t, source="发改局")
        store.set_annual_target(project_id=pid, year=2026, metric=M.CAPACITY,
                                target=cap_t, source="发改局")
        store.report_metric(project_id=pid, metric=M.INVESTMENT, value=inv * 0.4,
                            effective_date="2026-03-31", source="财政局")
        store.report_metric(project_id=pid, metric=M.CAPACITY, value=cap * 0.4,
                            effective_date="2026-03-31", source="农业农村局")
        store.report_metric(project_id=pid, metric=M.EMPLOYMENT, value=emp * 0.4,
                            effective_date="2026-03-31", source="人社局")
        store.report_metric(project_id=pid, metric=M.INCOME, value=inc_w * 0.4 * 10000,
                            effective_date="2026-03-31", source="乡村振兴局")
        store.report_metric(project_id=pid, metric=M.FARMERS,
                            value=farmers + (20 if pid == "P-YC" else 0),
                            effective_date="2026-03-31", source="林业局")
        # 上半年正式报送
        store.report_metric(project_id=pid, metric=M.INVESTMENT, value=inv,
                            effective_date="2026-06-30", source="财政局")
        store.report_metric(project_id=pid, metric=M.CAPACITY, value=cap,
                            effective_date="2026-06-30", source="农业农村局")
        store.report_metric(project_id=pid, metric=M.EMPLOYMENT, value=emp,
                            effective_date="2026-06-30", source="人社局")
        store.report_metric(project_id=pid, metric=M.INCOME, value=inc_w * 10000,
                            effective_date="2026-06-30", source="乡村振兴局")
        store.report_metric(project_id=pid, metric=M.FARMERS, value=farmers,
                            effective_date="2026-06-30", source="林业局")

    # 康养：在建里程碑逾期
    store.report_milestone(
        project_id="P-KY", name="康养中心主体封顶", plan_date="2026-05-01",
        actual_date=None, completed=False, effective_date="2026-05-01",
        source="住建局",
    )
    # 森林人家：已闭环但延期 37 天
    store.report_milestone(
        project_id="P-RJ", name="示范户改造", plan_date="2026-02-01",
        actual_date="2026-03-10", completed=True, effective_date="2026-03-10",
        source="林业局",
    )
    store.report_risk(project_id="P-KY", risk_level=2, constraint=False,
                      effective_date="2026-03-31", source="生态环境局")
    store.report_risk(project_id="P-TS", risk_level=3, constraint=False,
                      effective_date="2026-03-31", source="生态环境局")
    store.report_risk(project_id="P-RJ", risk_level=2, constraint=False,
                      effective_date="2026-03-31", source="生态环境局")

    # 跨年度目标（2026-2027 特色林产能总目标）
    store.set_multiyear_target(
        project_id="P-TS", start_year=2026, end_year=2027,
        metric=M.CAPACITY, target=5000, source="发改局",
    )

    if with_caliber_v2:
        # v2 口径自 7 月 1 日生效：产能按新标准折算 ×1.25
        store.add_caliber(
            effective_date="2026-07-01",
            description="产能折算口径 v2（含初加工折算）",
            factors={M.CAPACITY: 1.25},
            source="统计局",
        )
    return store
