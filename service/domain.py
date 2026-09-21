"""林业项目组合驾驶舱的领域模型。

所有业务记录都以"只追加事件"的形式存在，区分来源（报送单位）、
发生时间（里程碑日期）、记录时间（入库时间）与记录时生效的口径版本，
保证任何对外结果都可以追溯到采用的数据版本。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum


class MetricType(str, Enum):
    """统一里程碑接收的指标口径。"""

    INVESTMENT = "investment"  # 年度累计投资到位（万元）
    CAPACITY = "capacity"  # 年度累计产能（单位随口径版本说明）
    EMPLOYMENT = "employment"  # 年度累计带动就业（人）
    INCOME = "income"  # 年度累计农户增收（元）
    FARMERS = "farmers"  # 带农户数（户，时点数）


# 可按年度汇总的累计指标
CUMULATIVE_METRICS = frozenset(
    {
        MetricType.INVESTMENT,
        MetricType.CAPACITY,
        MetricType.EMPLOYMENT,
        MetricType.INCOME,
    }
)


class ValidationError(ValueError):
    """报送内容不满足领域约束。"""


class PermissionError_(Exception):  # noqa: N818 - 与内置异常区分，便于 HTTP 层映射 403
    """当前角色无权执行该批注操作。"""\


@dataclass(frozen=True)
class Project:
    """林业项目（药材、康养、特色林、森林人家等）。"""

    id: str
    name: str
    category: str
    effective_date: date
    source: str
    registered_seq: int


@dataclass(frozen=True)
class MetricEvent:
    """一次统一里程碑指标报送。

    value 为截至 effective_date 的年度累计完成值（带农户数为时点数）。
    补报事件允许晚入库、早发生；supersedes 声明更正的更早事件，
    被更正事件保留在链上，但在新版本视图中排除。
    """

    seq: int
    project_id: str
    metric: MetricType
    value: float
    effective_date: date
    recorded_at: datetime
    source: str
    caliber_version: int
    note: str = ""
    idempotency_key: str | None = None
    supersedes: int | None = None
    scenario_backfill: bool = False


@dataclass(frozen=True)
class RiskEvent:
    """生态风险事件：等级 1-5，constraint 表示触发生态约束。"""

    seq: int
    project_id: str
    risk_level: int
    constraint: bool
    effective_date: date
    recorded_at: datetime
    source: str
    note: str = ""
    idempotency_key: str | None = None


@dataclass(frozen=True)
class MilestoneEvent:
    """项目里程碑：计划日期、实际日期与完成状态，用于判定延期。"""

    seq: int
    project_id: str
    name: str
    plan_date: date
    actual_date: date | None
    completed: bool
    effective_date: date
    recorded_at: datetime
    source: str
    note: str = ""


@dataclass(frozen=True)
class MergeEvent:
    """项目合并：source 在生效日起归入 target，历史视图不合并。"""

    seq: int
    source_id: str
    target_id: str
    effective_date: date
    recorded_at: datetime
    source: str
    note: str = ""


@dataclass(frozen=True)
class AnnualTarget:
    """单年度目标（同样只追加，调整保留版本，查询取最新版本）。"""

    seq: int
    project_id: str
    year: int
    metric: MetricType
    target: float
    recorded_at: datetime
    source: str
    caliber_version: int
    note: str = ""


@dataclass(frozen=True)
class MultiYearTarget:
    """跨年度目标：[start_year, end_year] 内某指标的总目标。"""

    seq: int
    project_id: str
    start_year: int
    end_year: int
    metric: MetricType
    target: float
    recorded_at: datetime
    source: str
    caliber_version: int
    note: str = ""


@dataclass(frozen=True)
class Caliber:
    """统计口径版本。

    factors 给出各指标相对 v1 基线的重述因子：在 v_c 口径下展示
    v_r 口径记录的值时，value * factor[c] / factor[r]。
    口径有生效日期：新视图全部统一为新口径，已发布视图保持发布时口径。
    """

    version: int
    effective_date: date
    description: str
    factors: dict[MetricType, float]
    recorded_at: datetime
    source: str


@dataclass(frozen=True)
class AnnotationEdit:
    """批注编辑留痕。"""

    edited_at: datetime
    editor: str
    role: str
    old_content: str
    new_content: str


@dataclass
class Annotation:
    """带权限的批注，可挂在项目、指标、具体事件或整个驾驶舱上。

    只有作者本人或 admin 可以修改/删除；越权操作直接拒绝，
    不得用一个角色的批注覆盖另一个角色的批注。
    """

    id: str
    author: str
    role: str
    content: str
    created_at: datetime
    project_id: str | None = None
    metric: MetricType | None = None
    seq_ref: int | None = None
    edits: list[AnnotationEdit] = field(default_factory=list)
    deleted: bool = False
    deleted_at: datetime | None = None
    deleted_by: str | None = None


@dataclass
class Snapshot:
    """已发布视图：冻结数据版本、口径版本与完整渲染结果。"""

    name: str
    at_seq: int
    as_of: date
    caliber_version: int
    scenario_id: str | None
    payload: dict
    created_at: datetime
    created_by: str


@dataclass
class Scenario:
    """情景（what-if）：在基线上叠加调度调整，不改基线，可固化。"""

    id: str
    name: str
    base_seq: int
    as_of: date
    created_at: datetime
    created_by: str
    metric_deltas: dict[tuple[str, MetricType], float] = field(default_factory=dict)
    risk_overrides: dict[str, tuple[int, bool]] = field(default_factory=dict)
    backfills: list[MetricEvent] = field(default_factory=list)
    committed: bool = False
    committed_seq: int | None = None
