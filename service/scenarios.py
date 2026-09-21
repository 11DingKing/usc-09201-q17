"""情景（what-if）服务：季度调度调整的试算与固化。

典型一次调度同时包含：下调某项目产能、提升另一项目生态风险、
补录农户收益。情景只在试算视图中生效，基线事件链不变；
批准后按 append-only 方式固化为新的真实事件，产生新的 seq 区间，
已发布的旧快照不受影响。
"""

from __future__ import annotations

from datetime import date, datetime

from .domain import MetricEvent, MetricType, Scenario, ValidationError
from .projection import PortfolioViewBuilder
from .store import EventStore, _parse_date  # noqa: PLC2701 - 复用统一日期解析


class ScenarioService:
    """创建、调整、比较、固化季度调度情景。"""

    def __init__(self, store: EventStore) -> None:
        self.store = store
        self.builder = PortfolioViewBuilder(store)
        # 恢复后编号接续已存在情景，避免 SCN 编号冲突
        self._scenario_seq = max(
            (int(s.id.split("-", 1)[1]) for s in store.scenarios.values()
             if s.id.startswith("SCN-") and s.id.split("-", 1)[1].isdigit()),
            default=0,
        )

    def create(
        self,
        *,
        name: str,
        as_of: str | date,
        created_by: str,
        base_seq: int | None = None,
    ) -> Scenario:
        if not name:
            raise ValidationError("情景名称不能为空")
        as_of_date = _parse_date(as_of)
        with self.store.lock:
            self._scenario_seq += 1
            scenario = Scenario(
                id=f"SCN-{self._scenario_seq:03d}",
                name=name,
                base_seq=self.store.seq if base_seq is None else min(base_seq, self.store.seq),
                as_of=as_of_date,
                created_at=datetime.now(),
                created_by=created_by,
            )
            self.store.save_scenario(scenario)
            return scenario

    # ---- 调整项 ---------------------------------------------------------

    def adjust_metric(
        self,
        scenario_id: str,
        *,
        project_id: str,
        metric: MetricType | str,
        delta: float,
    ) -> Scenario:
        """在基线上对某指标累计值施加增减（如产能下调，delta 为负）。"""

        metric = MetricType(metric)
        if isinstance(delta, bool) or not isinstance(delta, (int, float)):
            raise ValidationError("调整量必须是数值")
        scenario = self._require_open(scenario_id)
        if project_id not in self.store.projects:
            raise ValidationError(f"项目不存在：{project_id}")
        key = (project_id, metric)
        scenario.metric_deltas[key] = round(
            scenario.metric_deltas.get(key, 0.0) + float(delta), 4
        )
        return scenario

    def override_risk(
        self,
        scenario_id: str,
        *,
        project_id: str,
        risk_level: int,
        constraint: bool,
    ) -> Scenario:
        """在情景中提升（或调整）某项目生态风险等级/约束状态。"""

        if isinstance(risk_level, bool) or not isinstance(risk_level, int) \
                or not 1 <= risk_level <= 5:
            raise ValidationError("风险等级必须是 1-5 的整数")
        scenario = self._require_open(scenario_id)
        if project_id not in self.store.projects:
            raise ValidationError(f"项目不存在：{project_id}")
        scenario.risk_overrides[project_id] = (risk_level, bool(constraint))
        return scenario

    def backfill_income(
        self,
        scenario_id: str,
        *,
        project_id: str,
        metric: MetricType | str,
        value: float,
        effective_date: str | date,
        source: str,
        note: str = "",
    ) -> Scenario:
        """在情景中补录某项目较早发生的收益（或其他指标）累计值。"""

        metric = MetricType(metric)
        effective = _parse_date(effective_date)
        scenario = self._require_open(scenario_id)
        if project_id not in self.store.projects:
            raise ValidationError(f"项目不存在：{project_id}")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ValidationError("补录值必须是非负数")
        if effective > scenario.as_of:
            raise ValidationError("补录事件发生日期不能晚于情景观察日")
        backfill = MetricEvent(
            seq=-(len(scenario.backfills) + 1),  # 试算期占位 seq，固化时重新分配
            project_id=project_id,
            metric=metric,
            value=float(value),
            effective_date=effective,
            recorded_at=datetime.now(),
            source=source,
            caliber_version=self.builder.caliber_in_force(scenario.as_of),
            note=note or f"情景 {scenario_id} 试算补录",
            scenario_backfill=True,
        )
        scenario.backfills.append(backfill)
        return scenario

    # ---- 试算与比较 -----------------------------------------------------

    def evaluate(self, scenario_id: str, *, caliber_version: int | None = None) -> dict:
        scenario = self._require_open(scenario_id)
        return self.builder.build(
            as_of=scenario.as_of,
            at_seq=scenario.base_seq,
            caliber_version=caliber_version,
            scenario_id=scenario.id,
        )

    def baseline(self, scenario_id: str, *, caliber_version: int | None = None) -> dict:
        scenario = self.store.scenarios[scenario_id]
        return self.builder.build(
            as_of=scenario.as_of,
            at_seq=scenario.base_seq,
            caliber_version=caliber_version,
        )

    def compare(self, scenario_id: str, *, caliber_version: int | None = None) -> dict:
        return self.builder.compare(
            self.baseline(scenario_id, caliber_version=caliber_version),
            self.evaluate(scenario_id, caliber_version=caliber_version),
        )

    # ---- 固化 -----------------------------------------------------------

    def commit(self, scenario_id: str, *, committed_by: str) -> dict:
        """批准情景：把调整与补录追加为真实事件，基线与快照均不被改写。"""

        with self.store.lock:
            scenario = self._require_open(scenario_id)
            if committed_by != scenario.created_by and not committed_by:
                raise ValidationError("固化操作需注明批准人")
            caliber_v = self.builder.caliber_in_force(scenario.as_of)

            # 1) 指标调整（产能下调等）：取当年最新累计值 + 增量，形成新报送
            for (project_id, metric), delta in scenario.metric_deltas.items():
                if delta == 0:
                    continue
                latest = self._latest_year_value(
                    project_id, metric, scenario.as_of.year,
                    scenario.base_seq, scenario.as_of,
                )
                new_value = max(0.0, (latest or 0.0) + delta)
                self.store.report_metric(
                    project_id=project_id,
                    metric=metric,
                    value=new_value,
                    effective_date=scenario.as_of,
                    source=f"情景固化:{scenario.id}",
                    note=f"{metric.value} 调度调整 {delta:+.4f}（批准人 {committed_by}）",
                    caliber_version=caliber_v,
                )

            # 2) 风险提升：追加新风险事件
            for project_id, (level, constraint) in scenario.risk_overrides.items():
                self.store.report_risk(
                    project_id=project_id,
                    risk_level=level,
                    constraint=constraint,
                    effective_date=scenario.as_of,
                    source=f"情景固化:{scenario.id}",
                    note=f"风险调度调整（批准人 {committed_by}）",
                )

            # 3) 收益补录：晚入库、早发生，正常追加（不覆盖任何旧事件）
            for backfill in scenario.backfills:
                self.store.report_metric(
                    project_id=backfill.project_id,
                    metric=backfill.metric,
                    value=backfill.value,
                    effective_date=backfill.effective_date,
                    source=backfill.source,
                    note=f"{backfill.note}（批准人 {committed_by}）",
                    caliber_version=backfill.caliber_version,
                    scenario_backfill=True,
                )

            scenario.committed = True
            scenario.committed_seq = self.store.seq
            return {
                "scenario_id": scenario.id,
                "base_seq": scenario.base_seq,
                "committed_seq": scenario.committed_seq,
                "new_events": scenario.committed_seq - scenario.base_seq,
            }

    def _latest_year_value(
        self, project_id: str, metric: MetricType, year: int, at_seq: int,
        as_of: date,
    ) -> float | None:
        candidates = [
            e for e in self.store.metric_events
            if e.seq <= at_seq
            and e.project_id == project_id
            and e.metric == metric
            and e.effective_date.year == year
        ]
        if not candidates:
            return None
        event = max(candidates, key=lambda e: (e.effective_date, e.seq))
        target_caliber = self.builder.caliber_in_force(as_of)
        return self.builder._convert(  # noqa: SLF001
            event.value, event.caliber_version, metric, target_caliber
        )

    def _require_open(self, scenario_id: str) -> Scenario:
        scenario = self.store.scenarios.get(scenario_id)
        if scenario is None:
            raise ValidationError(f"情景不存在：{scenario_id}")
        if scenario.committed:
            raise ValidationError(f"情景已固化，不可再调整：{scenario_id}")
        return scenario
