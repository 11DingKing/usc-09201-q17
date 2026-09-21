"""只追加事件存储与恢复。

存储中的数据事件严格按入库顺序获得全局递增 seq；任何更正都通过
新事件（supersedes）表达，旧事件保留链上。已发布视图保存完整快照，
状态整体可导出/导入，系统恢复后快照内容与发布时逐字节一致。
"""

from __future__ import annotations

import json
import math
import threading
from datetime import date, datetime
from pathlib import Path

from .domain import (
    Annotation,
    AnnotationEdit,
    AnnualTarget,
    Caliber,
    MergeEvent,
    MetricEvent,
    MetricType,
    MilestoneEvent,
    MultiYearTarget,
    PermissionError_,
    Project,
    RiskEvent,
    Snapshot,
    Scenario,
    ValidationError,
)


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"日期格式应为 YYYY-MM-DD：{value!r}") from exc


def _json_default(value: object) -> object:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, MetricType):
        return value.value
    raise TypeError(f"不可序列化的类型：{type(value)!r}")


class EventStore:
    """线程安全的只追加存储。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.seq = 0
        self.projects: dict[str, Project] = {}
        self.metric_events: list[MetricEvent] = []
        self.risk_events: list[RiskEvent] = []
        self.milestone_events: list[MilestoneEvent] = []
        self.merge_events: list[MergeEvent] = []
        self.annual_targets: list[AnnualTarget] = []
        self.multiyear_targets: list[MultiYearTarget] = []
        self.calibers: dict[int, Caliber] = {}
        self.annotations: dict[str, Annotation] = {}
        self.snapshots: dict[str, Snapshot] = {}
        self.scenarios: dict[str, Scenario] = {}
        self._idempotency: set[str] = set()
        self._annotation_seq = 0
        # 初始基线口径 v1
        self.add_caliber(
            effective_date=date(2026, 1, 1),
            description="全县统一基线口径 v1",
            factors={},
            source="系统",
        )

    # ---- 通用工具 -------------------------------------------------------

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def _next_seq(self) -> int:
        self.seq += 1
        return self.seq

    @staticmethod
    def _check_number(value: object, field_name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError(f"{field_name} 必须是数值")
        number = float(value)
        if not math.isfinite(number) or number < 0:
            raise ValidationError(f"{field_name} 必须是非负有限数值")
        return number

    def _check_idempotency(self, key: str | None) -> None:
        if key is None:
            return
        if key in self._idempotency:
            raise ValidationError(f"重复报送（幂等键已存在）：{key}")
        self._idempotency.add(key)

    def _require_project(self, project_id: str, effective: date) -> Project:
        project = self.projects.get(project_id)
        if project is None:
            raise ValidationError(f"项目不存在：{project_id}")
        if project.effective_date > effective:
            raise ValidationError(
                f"事件发生日期 {effective} 早于项目立项日 {project.effective_date}"
            )
        return project

    def _require_caliber(self, version: int | None, recorded_at: datetime) -> int:
        if version is None:
            in_force = [
                c.version
                for c in self.calibers.values()
                if c.effective_date <= recorded_at.date()
            ]
            return max(in_force)
        if version not in self.calibers:
            raise ValidationError(f"口径版本不存在：v{version}")
        return version

    # ---- 写入：项目与事件 ----------------------------------------------

    def register_project(
        self,
        project_id: str,
        name: str,
        category: str,
        effective_date: str | date,
        source: str,
    ) -> Project:
        with self._lock:
            if not project_id or not name or not source:
                raise ValidationError("项目编号、名称、报送来源不能为空")
            if project_id in self.projects:
                raise ValidationError(f"项目已存在：{project_id}")
            project = Project(
                id=project_id,
                name=name,
                category=category,
                effective_date=_parse_date(effective_date),
                source=source,
                registered_seq=self._next_seq(),
            )
            self.projects[project_id] = project
            return project

    def report_metric(
        self,
        *,
        project_id: str,
        metric: MetricType | str,
        value: float,
        effective_date: str | date,
        source: str,
        note: str = "",
        idempotency_key: str | None = None,
        supersedes: int | None = None,
        recorded_at: datetime | None = None,
        caliber_version: int | None = None,
        scenario_backfill: bool = False,
        force_seq: int | None = None,
    ) -> MetricEvent:
        metric = MetricType(metric)
        effective = _parse_date(effective_date)
        number = self._check_number(value, "指标值")
        recorded = recorded_at or datetime.now()
        with self._lock:
            self._require_project(project_id, effective)
            self._check_idempotency(idempotency_key)
            caliber_v = self._require_caliber(caliber_version, recorded)
            if supersedes is not None:
                target = next(
                    (
                        e
                        for e in self.metric_events
                        if e.seq == supersedes and e.project_id == project_id
                        and e.metric == metric
                    ),
                    None,
                )
                if target is None:
                    raise ValidationError(
                        f"被更正事件 seq={supersedes} 不存在或项目/指标不一致"
                    )
                if any(e.supersedes == supersedes for e in self.metric_events):
                    raise ValidationError(f"事件 seq={supersedes} 已被更正过")
            event = MetricEvent(
                seq=force_seq or self._next_seq(),
                project_id=project_id,
                metric=metric,
                value=number,
                effective_date=effective,
                recorded_at=recorded,
                source=source,
                caliber_version=caliber_v,
                note=note,
                idempotency_key=idempotency_key,
                supersedes=supersedes,
                scenario_backfill=scenario_backfill,
            )
            if force_seq is not None:
                self.seq = max(self.seq, force_seq)
            self.metric_events.append(event)
            return event

    def report_risk(
        self,
        *,
        project_id: str,
        risk_level: int,
        constraint: bool,
        effective_date: str | date,
        source: str,
        note: str = "",
        idempotency_key: str | None = None,
        recorded_at: datetime | None = None,
        force_seq: int | None = None,
    ) -> RiskEvent:
        effective = _parse_date(effective_date)
        if isinstance(risk_level, bool) or not isinstance(risk_level, int):
            raise ValidationError("风险等级必须是 1-5 的整数")
        if not 1 <= risk_level <= 5:
            raise ValidationError("生态风险等级必须在 1 到 5 之间")
        recorded = recorded_at or datetime.now()
        with self._lock:
            self._require_project(project_id, effective)
            self._check_idempotency(idempotency_key)
            event = RiskEvent(
                seq=force_seq or self._next_seq(),
                project_id=project_id,
                risk_level=risk_level,
                constraint=bool(constraint),
                effective_date=effective,
                recorded_at=recorded,
                source=source,
                note=note,
                idempotency_key=idempotency_key,
            )
            if force_seq is not None:
                self.seq = max(self.seq, force_seq)
            self.risk_events.append(event)
            return event

    def report_milestone(
        self,
        *,
        project_id: str,
        name: str,
        plan_date: str | date,
        actual_date: str | date | None,
        completed: bool,
        effective_date: str | date,
        source: str,
        note: str = "",
        recorded_at: datetime | None = None,
        force_seq: int | None = None,
    ) -> MilestoneEvent:
        plan = _parse_date(plan_date)
        actual = _parse_date(actual_date) if actual_date is not None else None
        effective = _parse_date(effective_date)
        if actual is not None and not completed:
            raise ValidationError("已有实际完成日期的里程碑必须标记为已完成")
        recorded = recorded_at or datetime.now()
        with self._lock:
            self._require_project(project_id, effective)
            event = MilestoneEvent(
                seq=force_seq or self._next_seq(),
                project_id=project_id,
                name=name,
                plan_date=plan,
                actual_date=actual,
                completed=bool(completed),
                effective_date=effective,
                recorded_at=recorded,
                source=source,
                note=note,
            )
            if force_seq is not None:
                self.seq = max(self.seq, force_seq)
            self.milestone_events.append(event)
            return event

    def merge_projects(
        self,
        *,
        source_id: str,
        target_id: str,
        effective_date: str | date,
        source: str,
        note: str = "",
    ) -> MergeEvent:
        """登记项目合并。禁止重复合并与成环；历史视图不受影响。"""

        effective = _parse_date(effective_date)
        with self._lock:
            src = self._require_project(source_id, effective)
            tgt = self._require_project(target_id, effective)
            if src.id == tgt.id:
                raise ValidationError("不能将项目合并到自身")
            if any(
                e.source_id == source_id and e.effective_date <= effective
                for e in self.merge_events
            ):
                raise ValidationError(f"项目 {source_id} 在该日期前已被合并")

            def follows(chain_from: str, wanted: str) -> bool:
                current = chain_from
                seen: set[str] = set()
                while current not in seen:
                    if current == wanted:
                        return True
                    seen.add(current)
                    nexts = [
                        e.target_id
                        for e in self.merge_events
                        if e.source_id == current
                    ]
                    if not nexts:
                        return False
                    current = nexts[-1]
                return True

            if follows(target_id, source_id):
                raise ValidationError("合并链路不允许成环")
            event = MergeEvent(
                seq=self._next_seq(),
                source_id=source_id,
                target_id=target_id,
                effective_date=effective,
                recorded_at=datetime.now(),
                source=source,
                note=note,
            )
            self.merge_events.append(event)
            return event

    # ---- 写入：目标与口径 ----------------------------------------------

    def set_annual_target(
        self,
        *,
        project_id: str,
        year: int,
        metric: MetricType | str,
        target: float,
        source: str,
        caliber_version: int | None = None,
        note: str = "",
    ) -> AnnualTarget:
        metric = MetricType(metric)
        number = self._check_number(target, "目标值")
        if isinstance(year, bool) or not isinstance(year, int) or year < 2000:
            raise ValidationError("年度必须是合理整数年份")
        with self._lock:
            self._require_project(project_id, date(year, 12, 31))
            caliber_v = self._require_caliber(caliber_version, datetime.now())
            event = AnnualTarget(
                seq=self._next_seq(),
                project_id=project_id,
                year=year,
                metric=metric,
                target=number,
                recorded_at=datetime.now(),
                source=source,
                caliber_version=caliber_v,
                note=note,
            )
            self.annual_targets.append(event)
            return event

    def set_multiyear_target(
        self,
        *,
        project_id: str,
        start_year: int,
        end_year: int,
        metric: MetricType | str,
        target: float,
        source: str,
        caliber_version: int | None = None,
        note: str = "",
    ) -> MultiYearTarget:
        metric = MetricType(metric)
        number = self._check_number(target, "目标值")
        if start_year > end_year:
            raise ValidationError("跨年度目标起始年不能晚于结束年")
        with self._lock:
            self._require_project(project_id, date(end_year, 12, 31))
            caliber_v = self._require_caliber(caliber_version, datetime.now())
            event = MultiYearTarget(
                seq=self._next_seq(),
                project_id=project_id,
                start_year=start_year,
                end_year=end_year,
                metric=metric,
                target=number,
                recorded_at=datetime.now(),
                source=source,
                caliber_version=caliber_v,
                note=note,
            )
            self.multiyear_targets.append(event)
            return event

    def add_caliber(
        self,
        *,
        effective_date: str | date,
        description: str,
        factors: dict[MetricType | str, float] | None,
        source: str,
    ) -> Caliber:
        """登记新生效口径。factors 为各指标相对 v1 的重述因子。"""

        effective = _parse_date(effective_date)
        normalized: dict[MetricType, float] = {}
        for metric in MetricType:
            raw = (factors or {}).get(metric, (factors or {}).get(metric.value, 1.0))
            factor = self._check_number(raw, f"{metric.value} 重述因子")
            if factor == 0:
                raise ValidationError("重述因子不能为 0")
            normalized[metric] = factor
        with self._lock:
            version = (max(self.calibers, default=0)) + 1
            same_day = [c for c in self.calibers.values() if c.effective_date == effective]
            if same_day:
                raise ValidationError(f"日期 {effective} 已存在口径版本")
            caliber = Caliber(
                version=version,
                effective_date=effective,
                description=description or f"口径 v{version}",
                factors=normalized,
                recorded_at=datetime.now(),
                source=source,
            )
            self.calibers[version] = caliber
            return caliber

    # ---- 批注：权限隔离、编辑留痕 --------------------------------------

    def add_annotation(
        self,
        *,
        author: str,
        role: str,
        content: str,
        project_id: str | None = None,
        metric: MetricType | str | None = None,
        seq_ref: int | None = None,
    ) -> Annotation:
        if not author or not role:
            raise ValidationError("批注作者与角色不能为空")
        if not content or not content.strip():
            raise ValidationError("批注内容不能为空")
        metric_enum = MetricType(metric) if metric is not None else None
        with self._lock:
            if project_id is not None and project_id not in self.projects:
                raise ValidationError(f"批注关联项目不存在：{project_id}")
            self._annotation_seq += 1
            annotation = Annotation(
                id=f"ANN-{self._annotation_seq:04d}",
                author=author,
                role=role,
                content=content.strip(),
                created_at=datetime.now(),
                project_id=project_id,
                metric=metric_enum,
                seq_ref=seq_ref,
            )
            self.annotations[annotation.id] = annotation
            return annotation

    def _require_live_annotation(self, annotation_id: str) -> Annotation:
        annotation = self.annotations.get(annotation_id)
        if annotation is None:
            raise ValidationError(f"批注不存在：{annotation_id}")
        if annotation.deleted:
            raise ValidationError(f"批注已删除：{annotation_id}")
        return annotation

    def edit_annotation(
        self, *, annotation_id: str, editor: str, role: str, content: str
    ) -> Annotation:
        if not content or not content.strip():
            raise ValidationError("批注内容不能为空")
        with self._lock:
            annotation = self._require_live_annotation(annotation_id)
            if role != "admin" and annotation.author != editor:
                raise PermissionError_(
                    f"用户 {editor}（{role}）无权修改 {annotation.author} 的批注"
                )
            annotation.edits.append(
                AnnotationEdit(
                    edited_at=datetime.now(),
                    editor=editor,
                    role=role,
                    old_content=annotation.content,
                    new_content=content.strip(),
                )
            )
            annotation.content = content.strip()
            return annotation

    def delete_annotation(
        self, *, annotation_id: str, editor: str, role: str
    ) -> None:
        with self._lock:
            annotation = self._require_live_annotation(annotation_id)
            if role != "admin" and annotation.author != editor:
                raise PermissionError_(
                    f"用户 {editor}（{role}）无权删除 {annotation.author} 的批注"
                )
            annotation.deleted = True
            annotation.deleted_at = datetime.now()
            annotation.deleted_by = editor

    # ---- 快照与情景 -----------------------------------------------------

    def publish_snapshot(
        self,
        *,
        name: str,
        payload: dict,
        created_by: str,
        at_seq: int,
        as_of: date,
        caliber_version: int,
        scenario_id: str | None,
    ) -> Snapshot:
        with self._lock:
            if not name:
                raise ValidationError("快照名称不能为空")
            if name in self.snapshots:
                raise ValidationError(f"已发布视图名称已存在：{name}")
            snapshot = Snapshot(
                name=name,
                at_seq=at_seq,
                as_of=as_of,
                caliber_version=caliber_version,
                scenario_id=scenario_id,
                payload=payload,
                created_at=datetime.now(),
                created_by=created_by,
            )
            self.snapshots[name] = snapshot
            return snapshot

    def save_scenario(self, scenario: Scenario) -> None:
        with self._lock:
            self.scenarios[scenario.id] = scenario

    # ---- 序列化与恢复 ---------------------------------------------------

    def to_dict(self) -> dict:
        """导出完整状态。已发布快照的 payload 原样导出。"""

        with self._lock:
            return json.loads(json.dumps(self._dump(), default=_json_default))

    def _dump(self) -> dict:
        def dump_dataclass(obj: object) -> dict:
            return {k: v for k, v in vars(obj).items()}

        def dump_scenario(s: Scenario) -> dict:
            raw = dump_dataclass(s)
            raw["metric_deltas"] = {
                f"{pid}|{metric.value}": value
                for (pid, metric), value in s.metric_deltas.items()
            }
            raw["backfills"] = [dump_dataclass(e) for e in s.backfills]
            return raw

        return {
            "seq": self.seq,
            "annotation_seq": self._annotation_seq,
            "idempotency": sorted(self._idempotency),
            "projects": {pid: dump_dataclass(p) for pid, p in self.projects.items()},
            "metric_events": [dump_dataclass(e) for e in self.metric_events],
            "risk_events": [dump_dataclass(e) for e in self.risk_events],
            "milestone_events": [dump_dataclass(e) for e in self.milestone_events],
            "merge_events": [dump_dataclass(e) for e in self.merge_events],
            "annual_targets": [dump_dataclass(e) for e in self.annual_targets],
            "multiyear_targets": [dump_dataclass(e) for e in self.multiyear_targets],
            "calibers": {str(v): dump_dataclass(c) for v, c in self.calibers.items()},
            "annotations": {aid: dump_dataclass(a) for aid, a in self.annotations.items()},
            "snapshots": {name: dump_dataclass(s) for name, s in self.snapshots.items()},
            "scenarios": {sid: dump_scenario(s) for sid, s in self.scenarios.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> EventStore:
        store = cls.__new__(cls)
        store._lock = threading.RLock()
        store.seq = int(data.get("seq", 0))
        store._annotation_seq = int(data.get("annotation_seq", 0))
        store._idempotency = set(data.get("idempotency", []))
        store.projects = {
            pid: Project(
                id=p["id"],
                name=p["name"],
                category=p["category"],
                effective_date=date.fromisoformat(p["effective_date"]),
                source=p["source"],
                registered_seq=p["registered_seq"],
            )
            for pid, p in data.get("projects", {}).items()
        }
        store.metric_events = [
            MetricEvent(
                seq=e["seq"],
                project_id=e["project_id"],
                metric=MetricType(e["metric"]),
                value=e["value"],
                effective_date=date.fromisoformat(e["effective_date"]),
                recorded_at=datetime.fromisoformat(e["recorded_at"]),
                source=e["source"],
                caliber_version=e["caliber_version"],
                note=e.get("note", ""),
                idempotency_key=e.get("idempotency_key"),
                supersedes=e.get("supersedes"),
                scenario_backfill=e.get("scenario_backfill", False),
            )
            for e in data.get("metric_events", [])
        ]
        store.risk_events = [
            RiskEvent(
                seq=e["seq"],
                project_id=e["project_id"],
                risk_level=e["risk_level"],
                constraint=e["constraint"],
                effective_date=date.fromisoformat(e["effective_date"]),
                recorded_at=datetime.fromisoformat(e["recorded_at"]),
                source=e["source"],
                note=e.get("note", ""),
                idempotency_key=e.get("idempotency_key"),
            )
            for e in data.get("risk_events", [])
        ]
        store.milestone_events = [
            MilestoneEvent(
                seq=e["seq"],
                project_id=e["project_id"],
                name=e["name"],
                plan_date=date.fromisoformat(e["plan_date"]),
                actual_date=(
                    date.fromisoformat(e["actual_date"])
                    if e.get("actual_date")
                    else None
                ),
                completed=e["completed"],
                effective_date=date.fromisoformat(e["effective_date"]),
                recorded_at=datetime.fromisoformat(e["recorded_at"]),
                source=e["source"],
                note=e.get("note", ""),
            )
            for e in data.get("milestone_events", [])
        ]
        store.merge_events = [
            MergeEvent(
                seq=e["seq"],
                source_id=e["source_id"],
                target_id=e["target_id"],
                effective_date=date.fromisoformat(e["effective_date"]),
                recorded_at=datetime.fromisoformat(e["recorded_at"]),
                source=e["source"],
                note=e.get("note", ""),
            )
            for e in data.get("merge_events", [])
        ]
        store.annual_targets = [
            AnnualTarget(
                seq=e["seq"],
                project_id=e["project_id"],
                year=e["year"],
                metric=MetricType(e["metric"]),
                target=e["target"],
                recorded_at=datetime.fromisoformat(e["recorded_at"]),
                source=e["source"],
                caliber_version=e["caliber_version"],
                note=e.get("note", ""),
            )
            for e in data.get("annual_targets", [])
        ]
        store.multiyear_targets = [
            MultiYearTarget(
                seq=e["seq"],
                project_id=e["project_id"],
                start_year=e["start_year"],
                end_year=e["end_year"],
                metric=MetricType(e["metric"]),
                target=e["target"],
                recorded_at=datetime.fromisoformat(e["recorded_at"]),
                source=e["source"],
                caliber_version=e["caliber_version"],
                note=e.get("note", ""),
            )
            for e in data.get("multiyear_targets", [])
        ]
        store.calibers = {}
        for raw in data.get("calibers", {}).values():
            factors = {MetricType(k): v for k, v in raw["factors"].items()}
            store.calibers[int(raw["version"])] = Caliber(
                version=raw["version"],
                effective_date=date.fromisoformat(raw["effective_date"]),
                description=raw["description"],
                factors=factors,
                recorded_at=datetime.fromisoformat(raw["recorded_at"]),
                source=raw["source"],
            )
        store.annotations = {}
        for aid, raw in data.get("annotations", {}).items():
            store.annotations[aid] = Annotation(
                id=raw["id"],
                author=raw["author"],
                role=raw["role"],
                content=raw["content"],
                created_at=datetime.fromisoformat(raw["created_at"]),
                project_id=raw.get("project_id"),
                metric=MetricType(raw["metric"]) if raw.get("metric") else None,
                seq_ref=raw.get("seq_ref"),
                edits=[
                    AnnotationEdit(
                        edited_at=datetime.fromisoformat(e["edited_at"]),
                        editor=e["editor"],
                        role=e["role"],
                        old_content=e["old_content"],
                        new_content=e["new_content"],
                    )
                    for e in raw.get("edits", [])
                ],
                deleted=raw.get("deleted", False),
                deleted_at=(
                    datetime.fromisoformat(raw["deleted_at"])
                    if raw.get("deleted_at")
                    else None
                ),
                deleted_by=raw.get("deleted_by"),
            )
        store.snapshots = {}
        for name, raw in data.get("snapshots", {}).items():
            store.snapshots[name] = Snapshot(
                name=raw["name"],
                at_seq=raw["at_seq"],
                as_of=date.fromisoformat(raw["as_of"]),
                caliber_version=raw["caliber_version"],
                scenario_id=raw.get("scenario_id"),
                payload=raw["payload"],
                created_at=datetime.fromisoformat(raw["created_at"]),
                created_by=raw["created_by"],
            )
        store.scenarios = {}
        for sid, raw in data.get("scenarios", {}).items():
            store.scenarios[sid] = Scenario(
                id=raw["id"],
                name=raw["name"],
                base_seq=raw["base_seq"],
                as_of=date.fromisoformat(raw["as_of"]),
                created_at=datetime.fromisoformat(raw["created_at"]),
                created_by=raw["created_by"],
                metric_deltas={
                    (pid, MetricType(metric_key)): value
                    for key, value in raw.get("metric_deltas", {}).items()
                    for pid, metric_key in [key.split("|", 1)]
                },
                risk_overrides={
                    pid: tuple(v) for pid, v in raw.get("risk_overrides", {}).items()
                },
                backfills=[
                    MetricEvent(
                        seq=e["seq"],
                        project_id=e["project_id"],
                        metric=MetricType(e["metric"]),
                        value=e["value"],
                        effective_date=date.fromisoformat(e["effective_date"]),
                        recorded_at=datetime.fromisoformat(e["recorded_at"]),
                        source=e["source"],
                        caliber_version=e["caliber_version"],
                        note=e.get("note", ""),
                        scenario_backfill=True,
                    )
                    for e in raw.get("backfills", [])
                ],
                committed=raw.get("committed", False),
                committed_seq=raw.get("committed_seq"),
            )
        return store

    def save_file(self, path: str | Path) -> None:
        """原子落盘，供系统重启后恢复。"""

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._dump(), default=_json_default, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load_file(cls, path: str | Path) -> EventStore | None:
        path = Path(path)
        if not path.exists():
            return None
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
