"""事件投影：把事件流重放为某一日期、某一情景下的组合状态。

投影是纯函数式的——给定事件列表与 ``as_of`` 日期，结果唯一确定，不保留
可变状态，因此任意历史日期、基准/情景都可以重复计算并相互比较。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .models import (
    CALIBER_METRICS,
    FLOW_METRICS,
    METRIC_CAPACITY,
    METRIC_FARMERS,
    METRIC_INCOME,
    METRIC_INVESTMENT,
    METRIC_JOBS,
    METRIC_RISK,
    Event,
)

# 里程碑达成事件允许进入情景叠加，结构性事件不允许（见 engine 校验）
METRIC_EVENT_TYPES = {
    "investment_recorded": METRIC_INVESTMENT,
    "income_recorded": METRIC_INCOME,
    "employment_recorded": METRIC_JOBS,
    "capacity_recorded": METRIC_CAPACITY,
    "farmer_count_recorded": METRIC_FARMERS,
}

LEVEL_PAYLOAD_KEYS = {
    METRIC_JOBS: "jobs",
    METRIC_CAPACITY: "capacity",
    METRIC_FARMERS: "farmers",
}

DEFAULT_WEIGHTS: dict[str, float] = {
    METRIC_INVESTMENT: 0.18,
    METRIC_INCOME: 0.18,
    METRIC_CAPACITY: 0.12,
    METRIC_JOBS: 0.08,
    METRIC_FARMERS: 0.12,
    "schedule": 0.17,
    METRIC_RISK: 0.15,
}


@dataclass
class MilestoneState:
    milestone: str
    due_on: str
    weight: float
    reached_on: str | None = None


@dataclass
class ConstraintState:
    name: str
    on: str
    lifted_on: str | None = None


@dataclass
class ProjectState:
    project_id: str
    name: str
    category: str
    registered_on: str
    # 流量类：年度内与累计的基准口径金额
    flow: dict[str, float] = field(default_factory=dict)
    flow_cumulative: dict[str, float] = field(default_factory=dict)
    # 存量类：观测历史 [(发生日期, 值)]
    levels: dict[str, list[tuple[str, float]]] = field(default_factory=dict)
    risk_history: list[tuple[str, str]] = field(default_factory=list)
    constraints: list[ConstraintState] = field(default_factory=list)
    milestones: dict[str, MilestoneState] = field(default_factory=dict)
    targets: dict[tuple[str, int], float] = field(default_factory=dict)
    merged_into: str | None = None
    merge_on: str | None = None
    sources: set[str] = field(default_factory=set)

    def latest(self, metric: str) -> tuple[str, float] | None:
        history = self.levels.get(metric) or []
        return history[-1] if history else None


@dataclass
class Caliber:
    version: str
    label: str
    effective_on: str
    coefficients: dict[str, float]


@dataclass
class Projection:
    as_of: str
    projects: dict[str, ProjectState]
    calibers: list[Caliber]
    portfolio_targets: dict[tuple[str, int], float]
    roots: dict[str, str]
    year: int

    # ---------- 口径 ----------

    def active_caliber(self) -> Caliber:
        """``as_of`` 当日生效的口径版本（取生效日期最近者）。

        未定义任何口径时，返回系统内置的 v1 基准口径（名义万元，系数为 1）。
        """

        effective = [c for c in self.calibers if c.effective_on <= self.as_of]
        if effective:
            return effective[-1]
        return Caliber("v1", "基准口径（名义万元）", "1900-01-01", {})

    def normalize(self, metric: str, value: float, reported_version: str | None) -> float:
        """把按某口径上报的金额折算为统一基准口径。

        基准口径 v1 系数恒为 1；未注明口径的事件按其发生日生效版本解释。
        """

        if metric not in CALIBER_METRICS:
            return value
        if reported_version is None:
            return value
        for caliber in self.calibers:
            if caliber.version == reported_version:
                return value * caliber.coefficients.get(metric, 1.0)
        return value

    # ---------- 合并族 ----------

    def family(self, root_id: str) -> list[str]:
        return [pid for pid, root in self.roots.items() if root == root_id]

    def active_projects(self) -> list[ProjectState]:
        seen: set[str] = set()
        result: list[ProjectState] = []
        for pid, root in self.roots.items():
            if root in seen:
                continue
            seen.add(root)
            result.append(self.projects[root])
        return result

    def merged_members(self, root_id: str) -> list[ProjectState]:
        return [self.projects[pid] for pid in self.family(root_id) if pid != root_id]

    # ---------- 指标聚合（按合并族） ----------

    def flow_total(self, root_id: str, metric: str, *, cumulative: bool) -> float:
        total = 0.0
        for pid in self.family(root_id):
            state = self.projects[pid]
            total += state.flow_cumulative.get(metric, 0.0) if cumulative else state.flow.get(metric, 0.0)
        return round(total, 4)

    def level_total(self, root_id: str, metric: str) -> tuple[str | None, float | None]:
        """合并族存量取最新观测（比较发生日期）。"""

        latest: tuple[str, float] | None = None
        for pid in self.family(root_id):
            candidate = self.projects[pid].latest(metric)
            if candidate and (latest is None or candidate[0] > latest[0]):
                latest = candidate
        if latest is None:
            return None, None
        return latest[0], latest[1]

    def risk_state(self, root_id: str) -> tuple[str | None, str | None]:
        latest: tuple[str, str] | None = None
        for pid in self.family(root_id):
            history = self.projects[pid].risk_history
            if history and (latest is None or history[-1][0] > latest[0]):
                latest = history[-1]
        if latest is None:
            return None, None
        return latest[1], latest[0]

    def active_constraints(self, root_id: str) -> list[ConstraintState]:
        result = []
        for pid in self.family(root_id):
            result.extend(
                c for c in self.projects[pid].constraints if c.lifted_on is None
            )
        return result

    def target(self, root_id: str, metric: str, year: int) -> float | None:
        for pid in self.family(root_id):
            if (metric, year) in self.projects[pid].targets:
                return self.projects[pid].targets[(metric, year)]
        return self.portfolio_targets.get((metric, year))

    def schedule(self, root_id: str) -> dict[str, Any]:
        total_weight = reached_weight = 0.0
        reached = overdue = delayed = 0
        total = 0
        overdue_milestones: list[str] = []
        for pid in self.family(root_id):
            for key, ms in self.projects[pid].milestones.items():
                total += 1
                total_weight += ms.weight
                if ms.reached_on is not None:
                    reached += 1
                    reached_weight += ms.weight
                    if ms.reached_on > ms.due_on:
                        delayed += 1
                elif ms.due_on < self.as_of:
                    overdue += 1
                    overdue_milestones.append(f"{key}（应于 {ms.due_on} 完成）")
        rate = (reached_weight / total_weight) if total_weight else 0.0
        return {
            "total": total,
            "reached": reached,
            "overdue": overdue,
            "delayed": delayed,
            "rate": round(rate, 4),
            "overdue_milestones": overdue_milestones,
        }


def project_events(events: list[Event], as_of: str) -> Projection:
    """把事件流投影到 ``as_of``（含当日）。"""

    projects: dict[str, ProjectState] = {}
    calibers: list[Caliber] = []
    portfolio_targets: dict[tuple[str, int], float] = {}
    merge_map: dict[str, tuple[str, str]] = {}  # 被合并方 -> (存续方, 合并日)

    for event in events:
        if event.occurred_on > as_of:
            continue
        payload = event.payload
        on = event.occurred_on

        if event.event_type == "project_registered":
            pid = payload["project_id"]
            projects[pid] = ProjectState(
                project_id=pid,
                name=payload["name"],
                category=payload["category"],
                registered_on=on,
            )
            projects[pid].sources.add(event.source)

        elif event.event_type == "project_merged":
            merged = payload["merged_id"]
            survivor = payload["survivor_id"]
            merge_map[merged] = (survivor, on)
            if merged in projects:
                projects[merged].merged_into = survivor
                projects[merged].merge_on = on

        elif event.event_type == "caliber_defined":
            calibers.append(
                Caliber(
                    version=payload["version"],
                    label=payload.get("label", payload["version"]),
                    effective_on=on,
                    coefficients=dict(payload.get("coefficients", {})),
                )
            )

        elif event.event_type == "target_set":
            key = (payload["metric"], int(payload["year"]))
            value = float(payload["value"])
            pid = payload.get("project_id")
            if pid:
                if pid not in projects:
                    # 目标先于立项或引用了未知项目：忽略，避免污染投影
                    continue
                projects[pid].targets[key] = value
            else:
                portfolio_targets[key] = value

        elif event.event_type in METRIC_EVENT_TYPES:
            metric = METRIC_EVENT_TYPES[event.event_type]
            pid = payload["project_id"]
            state = projects.get(pid)
            if state is None:
                continue
            state.sources.add(event.source)
            if metric in FLOW_METRICS:
                amount = proj_normalize(calibers, metric, float(payload["amount"]),
                                        payload.get("reported_version"), on)
                state.flow_cumulative[metric] = state.flow_cumulative.get(metric, 0.0) + amount
                if date.fromisoformat(on).year == date.fromisoformat(as_of).year:
                    state.flow[metric] = state.flow.get(metric, 0.0) + amount
            else:
                value_key = LEVEL_PAYLOAD_KEYS[metric]
                state.levels.setdefault(metric, []).append((on, float(payload[value_key])))

        elif event.event_type == "risk_recorded":
            pid = payload["project_id"]
            if pid in projects:
                projects[pid].risk_history.append((on, payload["level"]))

        elif event.event_type == "constraint_triggered":
            pid = payload["project_id"]
            if pid in projects:
                projects[pid].constraints.append(
                    ConstraintState(name=payload["constraint"], on=on)
                )

        elif event.event_type == "constraint_lifted":
            pid = payload["project_id"]
            if pid in projects:
                for con in reversed(projects[pid].constraints):
                    if con.name == payload["constraint"] and con.lifted_on is None:
                        con.lifted_on = on
                        break

        elif event.event_type == "milestone_scheduled":
            pid = payload["project_id"]
            if pid in projects:
                key = payload["milestone"]
                projects[pid].milestones[key] = MilestoneState(
                    milestone=key,
                    due_on=payload["due_on"],
                    weight=float(payload.get("weight", 1.0)),
                )

        elif event.event_type == "milestone_reached":
            pid = payload["project_id"]
            if pid in projects:
                key = payload["milestone"]
                ms = projects[pid].milestones.get(key)
                if ms is not None and ms.reached_on is None:
                    ms.reached_on = on

    calibers.sort(key=lambda c: c.effective_on)

    # 观测历史按发生日期排序；同日时情景叠加（后写入）优先于基线
    for state in projects.values():
        for metric, history in state.levels.items():
            history.sort(key=lambda item: item[0])
        state.risk_history.sort(key=lambda item: item[0])

    # 截至 as_of 的归并根（传递闭包），合并日之后才生效
    roots: dict[str, str] = {}
    for pid in projects:
        root = pid
        seen = set()
        while root in merge_map and root not in seen:
            survivor, merge_on = merge_map[root]
            if merge_on > as_of:
                break
            seen.add(root)
            root = survivor
        roots[pid] = root

    return Projection(
        as_of=as_of,
        projects=projects,
        calibers=calibers,
        portfolio_targets=portfolio_targets,
        roots=roots,
        year=date.fromisoformat(as_of).year,
    )


def proj_normalize(
    calibers: list[Caliber],
    metric: str,
    value: float,
    reported_version: str | None,
    occurred_on: str,
) -> float:
    """投影过程中使用的口径折算（投影对象尚未构造完成时）。"""

    if metric not in CALIBER_METRICS:
        return value
    version = reported_version
    if version is None:
        return value
    for caliber in calibers:
        if caliber.version == version:
            return value * caliber.coefficients.get(metric, 1.0)
    return value
