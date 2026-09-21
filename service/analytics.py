"""驾驶舱读模型：组合视图、排序、预警、决策建议与情景比较。

读模型完全由投影即时计算，不落任何可变状态；发布时由应用层把视图整体固化，
从而保证“系统恢复后已发布视图保持原版本”。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from .models import (
    METRIC_CAPACITY,
    METRIC_FARMERS,
    METRIC_INCOME,
    METRIC_INVESTMENT,
    METRIC_JOBS,
    METRIC_LABELS,
    METRIC_RISK,
    RISK_SCORES,
    Event,
)
from .projection import DEFAULT_WEIGHTS, Projection, project_events
from .storage import EventStore

ALERT_RED = "红"
ALERT_AMBER = "黄"
ALERT_BLUE = "蓝"
ALERT_ORDER = {ALERT_RED: 0, ALERT_AMBER: 1, ALERT_BLUE: 2}


def build_view(store: EventStore, *, as_of: str, scenario: str | None = None) -> dict[str, Any]:
    """构造完整驾驶舱视图。"""

    events = store.events_for_scenario(scenario)
    projection = project_events(events, as_of)
    event_index = _index_events(events, as_of)
    rows = [_project_row(projection, root, event_index) for root in _root_order(projection)]
    _score_and_rank(rows)

    portfolio = _portfolio_totals(projection, rows)
    alerts, decisions = _alerts_and_decisions(projection, rows)

    caliber = projection.active_caliber()
    return {
        "as_of": as_of,
        "quarter": _quarter(as_of),
        "year": projection.year,
        "scenario": scenario,
        "caliber": {
            "version": caliber.version,
            "label": caliber.label,
            "effective_on": caliber.effective_on,
            "coefficients": caliber.coefficients,
        },
        "portfolio": portfolio,
        "projects": rows,
        "rankings": [
            {"rank": r["rank"], "project_id": r["project_id"], "name": r["name"],
             "score": r["score"], "alert_level": r["alert_level"]}
            for r in rows
        ],
        "alerts": alerts,
        "decisions": decisions,
    }


def _root_order(projection: Projection) -> list[str]:
    roots: list[str] = []
    for pid in projection.projects:
        root = projection.roots[pid]
        if root not in roots:
            roots.append(root)
    return roots


def _index_events(events: list[Event], as_of: str) -> dict[tuple[str, str], list[Event]]:
    """建立 (项目, 事件类型) -> 事件列表 的一次性索引，供补录追溯使用。"""

    index: dict[tuple[str, str], list[Event]] = {}
    for event in events:
        if event.occurred_on > as_of:
            continue
        pid = event.payload.get("project_id")
        if pid:
            index.setdefault((pid, event.event_type), []).append(event)
    return index


def _project_row(
    projection: Projection, root: str, event_index: dict[tuple[str, str], list[Event]]
) -> dict[str, Any]:
    state = projection.projects[root]
    members = projection.merged_members(root)

    inv_ytd = projection.flow_total(root, METRIC_INVESTMENT, cumulative=False)
    income_ytd = projection.flow_total(root, METRIC_INCOME, cumulative=False)
    inv_cum = projection.flow_total(root, METRIC_INVESTMENT, cumulative=True)
    income_cum = projection.flow_total(root, METRIC_INCOME, cumulative=True)
    jobs_on, jobs = projection.level_total(root, METRIC_JOBS)
    cap_on, capacity = projection.level_total(root, METRIC_CAPACITY)
    farmers_on, farmers = projection.level_total(root, METRIC_FARMERS)
    risk_level, risk_on = projection.risk_state(root)
    schedule = projection.schedule(root)
    constraints = [
        {"name": c.name, "triggered_on": c.on}
        for c in projection.active_constraints(root)
    ]

    targets = {}
    achievement = {}
    for metric in (METRIC_INVESTMENT, METRIC_INCOME, METRIC_CAPACITY, METRIC_JOBS, METRIC_FARMERS):
        target = projection.target(root, metric, projection.year)
        if target is None:
            continue
        targets[metric] = target
        current = {
            METRIC_INVESTMENT: inv_ytd,
            METRIC_INCOME: income_ytd,
            METRIC_JOBS: jobs or 0.0,
            METRIC_CAPACITY: capacity or 0.0,
            METRIC_FARMERS: farmers or 0.0,
        }[metric]
        achievement[metric] = round(current / target, 4)

    family_pids = projection.family(root)
    supplements = _supplements(event_index, family_pids)
    farmers_delta = _farmers_delta(projection, root)

    return {
        "project_id": root,
        "name": state.name,
        "category": state.category,
        "registered_on": state.registered_on,
        "merged_from": [
            {"project_id": m.project_id, "name": m.name, "merged_on": m.merge_on}
            for m in members
        ],
        "metrics": {
            METRIC_INVESTMENT: {"ytd": inv_ytd, "cumulative": inv_cum},
            METRIC_INCOME: {"ytd": income_ytd, "cumulative": income_cum,
                            "supplemented": supplements},
            METRIC_JOBS: {"value": jobs, "as_of": jobs_on},
            METRIC_CAPACITY: {"value": capacity, "as_of": cap_on},
            METRIC_FARMERS: {"value": farmers, "as_of": farmers_on,
                             "change_from_previous": farmers_delta},
            METRIC_RISK: {"level": risk_level, "as_of": risk_on},
            "schedule": schedule,
        },
        "targets": targets,
        "achievement": achievement,
        "constraints": constraints,
        "sources": sorted(_family_sources(projection, root)),
        "alerts": [],
        "score": None,
        "score_parts": {},
        "rank": None,
        "alert_level": None,
    }


def _family_sources(projection: Projection, root: str) -> set[str]:
    sources: set[str] = set()
    for pid in projection.family(root):
        sources.update(projection.projects[pid].sources)
    return sources


def _supplements(
    event_index: dict[tuple[str, str], list[Event]], family_pids: list[str]
) -> list[dict[str, Any]]:
    """跨期补录：入库时间晚于业务发生日期的农户收益事件（含合并前成员）。"""

    result = []
    for pid in family_pids:
        for event in event_index.get((pid, "income_recorded"), []):
            recorded = event.recorded_at[:10]
            if recorded > event.occurred_on:
                result.append({
                    "event_id": event.event_id,
                    "occurred_on": event.occurred_on,
                    "recorded_at": event.recorded_at,
                    "amount": event.payload.get("amount"),
                    "source": event.source,
                })
    result.sort(key=lambda x: x["occurred_on"])
    return result


def _farmers_delta(projection: Projection, root: str) -> float | None:
    delta = None
    for pid in projection.family(root):
        history = projection.projects[pid].levels.get(METRIC_FARMERS) or []
        if len(history) >= 2:
            d = history[-1][1] - history[-2][1]
            delta = d if delta is None else delta + d
    return delta


def _score_and_rank(rows: list[dict[str, Any]]) -> None:
    """按可解释的分项评分排序：完成率/进度取达成度，横向指标做极差归一。"""

    def achievement_or_scale(row: dict, metric: str) -> float | None:
        if metric in row["achievement"]:
            return min(row["achievement"][metric], 1.5) / 1.5
        return None

    # 极差归一所需的当前值
    def current(row: dict, metric: str) -> float | None:
        if metric in (METRIC_INVESTMENT, METRIC_INCOME):
            return row["metrics"][metric]["ytd"]
        return row["metrics"][metric]["value"]

    scales = {}
    for metric in (METRIC_CAPACITY, METRIC_JOBS, METRIC_FARMERS):
        values = [current(r, metric) for r in rows if current(r, metric) is not None]
        scales[metric] = (min(values), max(values)) if values else (None, None)

    for row in rows:
        parts: dict[str, float] = {}
        for metric in (METRIC_INVESTMENT, METRIC_INCOME):
            rate = achievement_or_scale(row, metric)
            if rate is not None:
                parts[metric] = round(rate * 100, 2)
        for metric in (METRIC_CAPACITY, METRIC_JOBS, METRIC_FARMERS):
            rate = achievement_or_scale(row, metric)
            if rate is None:
                value = current(row, metric)
                lo, hi = scales[metric]
                if value is not None and hi is not None and hi > (lo or 0):
                    rate = (value - (lo or 0)) / (hi - (lo or 0))
            if rate is not None:
                parts[metric] = round(rate * 100, 2)
        parts["schedule"] = round(row["metrics"]["schedule"]["rate"] * 100, 2)
        risk = row["metrics"][METRIC_RISK]["level"]
        if risk is None:
            parts[METRIC_RISK] = 60.0
        else:
            parts[METRIC_RISK] = 100 - (RISK_SCORES[risk] - 1) * 40
        if row["constraints"]:
            parts[METRIC_RISK] *= 0.5  # 存在生效中的生态约束，风险项折半

        weight_sum = sum(DEFAULT_WEIGHTS.get(k, 0.0) for k in parts)
        if weight_sum == 0:
            row["score_parts"] = {}
            row["score"] = None
            continue
        score = sum(parts[k] * DEFAULT_WEIGHTS.get(k, 0.0) for k in parts) / weight_sum
        row["score_parts"] = {k: round(v, 2) for k, v in parts.items()}
        row["score"] = round(score, 2)

    rows.sort(key=lambda r: (r["score"] is None, -(r["score"] or 0)))
    for index, row in enumerate(rows, 1):
        row["rank"] = index


def _alerts_and_decisions(
    projection: Projection, rows: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    alerts: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []

    for row in rows:
        pid = row["project_id"]
        row_alerts: list[dict[str, Any]] = []
        schedule = row["metrics"]["schedule"]
        risk = row["metrics"][METRIC_RISK]["level"]

        # 1. 项目延期
        if schedule["overdue"]:
            row_alerts.append({
                "level": ALERT_RED,
                "code": "schedule_overdue",
                "message": f"里程碑逾期 {schedule['overdue']} 项：{('、'.join(schedule['overdue_milestones']))}",
            })
        elif schedule["delayed"]:
            row_alerts.append({
                "level": ALERT_AMBER,
                "code": "schedule_delayed",
                "message": f"{schedule['delayed']} 个里程碑虽已完成但晚于计划日期",
            })

        # 2. 生态风险与约束
        if row["constraints"]:
            names = "、".join(c["name"] for c in row["constraints"])
            row_alerts.append({
                "level": ALERT_RED,
                "code": "eco_constraint_active",
                "message": f"生态约束触发中：{names}，暂停新增经营性投入",
            })
        elif risk == "高":
            row_alerts.append({
                "level": ALERT_AMBER,
                "code": "risk_high",
                "message": f"最新生态风险等级为高（{row['metrics'][METRIC_RISK]['as_of']}）",
            })

        # 3. 带农户数变化
        delta = row["metrics"][METRIC_FARMERS]["change_from_previous"]
        if delta is not None and delta < 0:
            row_alerts.append({
                "level": ALERT_AMBER,
                "code": "farmers_decreased",
                "message": f"带农户数较上次观测减少 {abs(int(delta))} 户，联农带农能力弱化",
            })
        elif delta is not None and delta > 0:
            row_alerts.append({
                "level": ALERT_BLUE,
                "code": "farmers_increased",
                "message": f"带农户数较上次增加 {int(delta)} 户",
            })

        # 4. 增收/投资达成度
        income_rate = row["achievement"].get(METRIC_INCOME)
        if income_rate is not None and income_rate < 0.5:
            row_alerts.append({
                "level": ALERT_AMBER,
                "code": "income_behind_target",
                "message": f"农户增收仅完成年度目标 {income_rate:.0%}",
            })
        inv_rate = row["achievement"].get(METRIC_INVESTMENT)
        if inv_rate is not None and inv_rate < 0.5 and not schedule["overdue"]:
            row_alerts.append({
                "level": ALERT_BLUE,
                "code": "investment_behind_target",
                "message": f"投资到位率 {inv_rate:.0%}，关注资金拨付进度",
            })

        # 5. 补录提示（口径治理，蓝色提示）
        supplements = row["metrics"][METRIC_INCOME]["supplemented"]
        if supplements:
            row_alerts.append({
                "level": ALERT_BLUE,
                "code": "income_supplemented",
                "message": f"存在 {len(supplements)} 笔跨期补录农户收益，已按发生期归入对应年度",
            })

        row_alerts.sort(key=lambda a: ALERT_ORDER[a["level"]])
        row["alerts"] = row_alerts
        row["alert_level"] = row_alerts[0]["level"] if row_alerts else None
        for alert in row_alerts:
            alerts.append({"project_id": pid, "name": row["name"], **alert})

        decisions.extend(_decide_for_row(row))

    alerts.sort(key=lambda a: (ALERT_ORDER[a["level"]], a["project_id"]))
    decisions.sort(key=lambda d: (d["priority"], d["project_id"]))
    return alerts, decisions


def _decide_for_row(row: dict[str, Any]) -> list[dict[str, Any]]:
    """把预警翻译成资金/资源转向建议——回答“钱该往哪转”。"""

    actions: list[dict[str, Any]] = []
    pid, name = row["project_id"], row["name"]
    codes = {a["code"] for a in row["alerts"]}

    if "eco_constraint_active" in codes:
        actions.append({
            "priority": 1,
            "project_id": pid,
            "name": name,
            "action": "冻结新增经营性投资与产能扩张安排",
            "reason": "生态约束生效，继续投入将形成沉没成本",
            "reallocate_to": "约束解除或风险低、带农户数多的项目",
        })
    if "schedule_overdue" in codes and "eco_constraint_active" not in codes:
        actions.append({
            "priority": 2,
            "project_id": pid,
            "name": name,
            "action": "集中拨付后续资金并提级调度逾期里程碑",
            "reason": "仅报完成率会掩盖卡点，逾期里程碑决定投产时点",
            "reallocate_to": "本项目关键里程碑（用地、开工）",
        })
    if "farmers_decreased" in codes or "income_behind_target" in codes:
        actions.append({
            "priority": 2,
            "project_id": pid,
            "name": name,
            "action": "安排联农带农补助与订单收购倾斜",
            "reason": "带农户数下降、增收滞后，需稳住农户收益基本盘",
            "reallocate_to": "农户劳务岗位与保底收购",
        })
    if "risk_high" in codes and "eco_constraint_active" not in codes:
        actions.append({
            "priority": 3,
            "project_id": pid,
            "name": name,
            "action": "暂缓新增授信，先完成风险整改与复核",
            "reason": "生态风险升至高等级",
            "reallocate_to": "风险低、评分排序靠前的项目",
        })
    if not actions and row["score"] is not None and row["score"] >= 75:
        actions.append({
            "priority": 4,
            "project_id": pid,
            "name": name,
            "action": "可作为增量资金优先承接项目",
            "reason": f"综合评分 {row['score']}，无红/黄预警",
            "reallocate_to": None,
        })
    return actions


def _portfolio_totals(projection: Projection, rows: list[dict[str, Any]]) -> dict[str, Any]:
    def sum_metric(metric: str, key: str) -> float:
        return round(sum(r["metrics"][metric][key] or 0 for r in rows), 4)

    active_projects = len(rows)
    merged_count = sum(len(r["merged_from"]) for r in rows)

    targets = {}
    for metric in (METRIC_INVESTMENT, METRIC_INCOME, METRIC_CAPACITY, METRIC_JOBS, METRIC_FARMERS):
        total_target = 0.0
        found = False
        for root in (r["project_id"] for r in rows):
            target = projection.target(root, metric, projection.year)
            if target is not None:
                total_target += target
                found = True
        if found:
            targets[metric] = round(total_target, 4)

    return {
        "active_projects": active_projects,
        "merged_projects": merged_count,
        "investment_ytd": sum_metric(METRIC_INVESTMENT, "ytd"),
        "investment_cumulative": sum_metric(METRIC_INVESTMENT, "cumulative"),
        "income_ytd": sum_metric(METRIC_INCOME, "ytd"),
        "income_cumulative": sum_metric(METRIC_INCOME, "cumulative"),
        "jobs": sum_metric(METRIC_JOBS, "value"),
        "capacity": sum_metric(METRIC_CAPACITY, "value"),
        "farmers": sum_metric(METRIC_FARMERS, "value"),
        "constrained_projects": sum(1 for r in rows if r["constraints"]),
        "targets": targets,
        "metric_labels": METRIC_LABELS,
    }


def compare_views(baseline: dict[str, Any], scenario: dict[str, Any]) -> dict[str, Any]:
    """情景对比：产能/收益/风险/排序/预警/决策依据的同步变化。"""

    base_rows = {r["project_id"]: r for r in baseline["projects"]}
    scen_rows = {r["project_id"]: r for r in scenario["projects"]}
    changes = []
    for pid, scen in scen_rows.items():
        base = base_rows.get(pid)
        if base is None:
            continue
        cap_b = base["metrics"][METRIC_CAPACITY]["value"] or 0
        cap_s = scen["metrics"][METRIC_CAPACITY]["value"] or 0
        income_b = base["metrics"][METRIC_INCOME]["ytd"]
        income_s = scen["metrics"][METRIC_INCOME]["ytd"]
        farmers_b = base["metrics"][METRIC_FARMERS]["value"] or 0
        farmers_s = scen["metrics"][METRIC_FARMERS]["value"] or 0
        risk_b = base["metrics"][METRIC_RISK]["level"]
        risk_s = scen["metrics"][METRIC_RISK]["level"]
        alerts_b = {a["code"] for a in base["alerts"]}
        alerts_s = {a["code"] for a in scen["alerts"]}
        changes.append({
            "project_id": pid,
            "name": scen["name"],
            "capacity_delta": round(cap_s - cap_b, 4),
            "income_ytd_delta": round(income_s - income_b, 4),
            "farmers_delta": round(farmers_s - farmers_b, 4),
            "risk_change": None if risk_b == risk_s else {"from": risk_b, "to": risk_s},
            "score_base": base["score"],
            "score_scenario": scen["score"],
            "rank_base": base["rank"],
            "rank_scenario": scen["rank"],
            "rank_delta": (base["rank"] or 0) - (scen["rank"] or 0),
            "new_alerts": sorted(alerts_s - alerts_b),
            "resolved_alerts": sorted(alerts_b - alerts_s),
        })

    return {
        "baseline_as_of": baseline["as_of"],
        "scenario": scenario["scenario"],
        "scenario_as_of": scenario["as_of"],
        "portfolio_delta": {
            "capacity": round(
                scenario["portfolio"]["capacity"] - baseline["portfolio"]["capacity"], 4),
            "income_ytd": round(
                scenario["portfolio"]["income_ytd"] - baseline["portfolio"]["income_ytd"], 4),
            "farmers": round(
                scenario["portfolio"]["farmers"] - baseline["portfolio"]["farmers"], 4),
            "constrained_projects": scenario["portfolio"]["constrained_projects"]
            - baseline["portfolio"]["constrained_projects"],
        },
        "project_changes": sorted(changes, key=lambda c: -abs(c["rank_delta"])),
    }


def _quarter(as_of: str) -> str:
    day = date.fromisoformat(as_of)
    return f"{day.year}年Q{(day.month - 1) // 3 + 1}"
