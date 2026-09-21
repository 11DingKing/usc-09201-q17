"""驾驶舱领域模型、常量与时间工具。

所有业务事实都以“事件”为唯一入口：事件只追加、不覆盖，区分业务发生日期
（``occurred_on``）与入库时间（``recorded_at``），并保留上报单位与来源批次，
使指标补录、口径调整、项目合并等变化都可以被追溯。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

# 四类县级林业产业项目
CATEGORIES = ("药材", "康养", "特色林", "森林人家")

RISK_LOW = "低"
RISK_MEDIUM = "中"
RISK_HIGH = "高"
RISK_LEVELS = (RISK_LOW, RISK_MEDIUM, RISK_HIGH)
RISK_SCORES = {RISK_LOW: 1, RISK_MEDIUM: 2, RISK_HIGH: 3}

# 规范指标键值与中文名称
METRIC_INVESTMENT = "investment"
METRIC_INCOME = "income"
METRIC_JOBS = "jobs"
METRIC_CAPACITY = "capacity"
METRIC_FARMERS = "farmers"
METRIC_RISK = "risk"
METRIC_SCHEDULE = "schedule"

METRIC_LABELS = {
    METRIC_INVESTMENT: "投资到位（万元）",
    METRIC_INCOME: "农户增收（万元）",
    METRIC_JOBS: "就业岗位（个）",
    METRIC_CAPACITY: "产能",
    METRIC_FARMERS: "带农户数（户）",
    METRIC_RISK: "生态风险",
    METRIC_SCHEDULE: "里程碑进度",
}

# 流量类指标按期累加；存量类指标取截至某日的最新观测
FLOW_METRICS = (METRIC_INVESTMENT, METRIC_INCOME)
LEVEL_METRICS = (METRIC_JOBS, METRIC_CAPACITY, METRIC_FARMERS)

# 受口径系数影响的资金类指标
CALIBER_METRICS = (METRIC_INVESTMENT, METRIC_INCOME)

# 事件类型清单：新增事实只能追加，既有事实不得修改
EVENT_TYPES = (
    "project_registered",        # 项目立项
    "investment_recorded",       # 投资事件（计划/到位）
    "capacity_recorded",         # 产能观测
    "employment_recorded",       # 就业岗位观测
    "farmer_count_recorded",     # 带农户数观测
    "income_recorded",           # 农户增收事件（可补录）
    "milestone_scheduled",       # 统一里程碑计划
    "milestone_reached",         # 里程碑达成
    "risk_recorded",             # 生态风险等级观测
    "constraint_triggered",      # 生态约束触发
    "constraint_lifted",         # 生态约束解除
    "project_merged",            # 项目合并
    "target_set",                # （跨年度）目标下达/修订
    "caliber_defined",           # 统计口径版本与生效日期
    "annotation_added",          # 权限批注
    "scenario_registered",       # 情景登记
)

# 不参与业务投影、仅用于追溯与协作的事件
PROJECTION_IGNORED = ("annotation_added", "scenario_registered")


class DomainError(ValueError):
    """请求事件违反领域约定。"""


class NotFoundError(LookupError):
    """引用的资源不存在。"""


def iso_date(value: str | date) -> date:
    """把 ISO 日期字符串解析为 ``date``，``date`` 原样返回。"""

    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise DomainError(f"日期格式应为 YYYY-MM-DD：{value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise DomainError(f"日期格式应为 YYYY-MM-DD：{value!r}") from exc


def today_iso() -> str:
    return date.today().isoformat()


@dataclass(frozen=True, slots=True)
class Event:
    """不可变事件信封。

    ``scenario`` 为 ``None`` 表示基线事实；否则属于某情景的叠加事件，
    情景计算 = 基线事件 + 该情景叠加事件，基线永不被修改。
    """

    seq: int
    event_id: str
    event_type: str
    occurred_on: str
    recorded_at: str
    source: str
    source_batch: str
    payload: dict[str, Any]
    scenario: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "occurred_on": self.occurred_on,
            "recorded_at": self.recorded_at,
            "source": self.source,
            "source_batch": self.source_batch,
            "scenario": self.scenario,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Event":
        return cls(
            seq=int(data["seq"]),
            event_id=data["event_id"],
            event_type=data["event_type"],
            occurred_on=data["occurred_on"],
            recorded_at=data["recorded_at"],
            source=data.get("source", "未注明来源"),
            source_batch=data.get("source_batch", "default"),
            scenario=data.get("scenario"),
            payload=dict(data.get("payload", {})),
        )
