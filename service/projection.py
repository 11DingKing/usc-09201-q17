"""版本化读取模型：在任意数据版本/口径/日期切片上构建组合驾驶舱视图。

同一份事件链可以切出：
- 任意 as_of 日期、at_seq 数据版本、caliber_version 目标口径的视图；
- 叠加情景（产能下调、风险提升、农户收益补录）的 what-if 视图；
- 两版视图的排序、预警、决策依据差异比较。

所有派生数值都携带触发事件 seq 与口径版本，保证可追溯。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .domain import CUMULATIVE_METRICS, MetricType
from .store import EventStore

EXPECTED_PROGRESS_TOLERANCE = 0.9  # 完成率低于时间进度的 90% 视为滞后
RISK_LEVEL_WARN = 4  # 风险等级达到 4 级触发预警


@dataclass(frozen=True)
class ViewSpec:
    """视图切片说明。"""

    as_of: date
    at_seq: int
    caliber_version: int
    scenario_id: str | None = None


class PortfolioViewBuilder:
    """从只追加事件链构建不可变组合视图。"""

    def __init__(self, store: EventStore) -> None:
        self.store = store

    # ---- 口径与合并 -----------------------------------------------------

    def caliber_in_force(self, as_of: date) -> int:
        """as_of 当日生效的最新口径版本（新视图默认采用）。"""

        return max(
            c.version
            for c in self.store.calibers.values()
            if c.effective_date <= as_of
        )

    def _convert(self, value: float, recorded_caliber: int, metric: MetricType,
                 target_caliber: int) -> float:
        factors = self.store.calibers
        return (
            value
            * factors[target_caliber].factors[metric]
            / factors[recorded_caliber].factors[metric]
        )

    def _survivors(self, as_of: date, at_seq: int) -> dict[str, str]:
        """project_id -> 切片日存活实体 id。

        生效日晚于 as_of 或登记 seq 晚于 at_seq 的合并不生效，
        因此历史视图中后来才合并的项目仍独立呈现。
        """

        mapping = {pid: pid for pid in self.store.projects}
        for merge in sorted(self.store.merge_events, key=lambda e: (e.effective_date, e.seq)):
            if merge.seq <= at_seq and merge.effective_date <= as_of:
                for pid, current in list(mapping.items()):
                    if current == merge.source_id:
                        mapping[pid] = merge.target_id
        return mapping

    # ---- 事件选取 -------------------------------------------------------

    def _metric_events_in_slice(self, as_of: date, at_seq: int) -> list:
        superseded_seqs: set[int] = set()
        for event in self.store.metric_events:
            if event.seq <= at_seq and event.supersedes is not None:
                superseded_seqs.add(event.supersedes)
        events = []
        for event in self.store.metric_events:
            if event.seq > at_seq or event.effective_date > as_of:
                continue
            if event.seq in superseded_seqs:
                continue  # 已被同一切片内的更正事件替代；旧值留链不入视图
            events.append(event)
        return events

    def _risk_in_slice(self, as_of: date, at_seq: int, scenario) -> dict:
        result: dict[str, dict] = {}
        for event in sorted(self.store.risk_events, key=lambda e: (e.effective_date, e.seq)):
            if event.seq <= at_seq and event.effective_date <= as_of:
                result[event.project_id] = {
                    "project_id": event.project_id,
                    "risk_level": event.risk_level,
                    "constraint": event.constraint,
                    "effective_date": event.effective_date,
                    "seq": event.seq,
                }
        if scenario is not None and not scenario.committed:
            for pid, (level, constraint) in scenario.risk_overrides.items():
                prev = result.get(pid, {})
                result[pid] = {
                    "project_id": pid,
                    "risk_level": level,
                    "constraint": constraint,
                    "effective_date": as_of,
                    "seq": prev.get("seq"),
                    "scenario_override": True,
                }
        return result

    # ---- 主构建 ---------------------------------------------------------

    def build(
        self,
        *,
        as_of: date | str,
        at_seq: int | None = None,
        caliber_version: int | None = None,
        scenario_id: str | None = None,
    ) -> dict:
        as_of = date.fromisoformat(as_of) if isinstance(as_of, str) else as_of
        with self.store.lock:
            at_seq = self.store.seq if at_seq is None else min(at_seq, self.store.seq)
            caliber_v = caliber_version or self.caliber_in_force(as_of)
            if caliber_v not in self.store.calibers:
                raise ValueError(f"口径版本不存在：v{caliber_v}")
            scenario = self.store.scenarios.get(scenario_id) if scenario_id else None
            if scenario_id and scenario is None:
                raise ValueError(f"情景不存在：{scenario_id}")
            if scenario is not None and not scenario.committed and at_seq > scenario.base_seq:
                # 未固化情景只允许在其基线 seq 上试算，避免把基线后续事件悄悄混入
                at_seq = scenario.base_seq

            survivors = self._survivors(as_of, at_seq)
            metric_events = self._metric_events_in_slice(as_of, at_seq)
            risk_latest = self._risk_in_slice(as_of, at_seq, scenario)
            deltas = scenario.metric_deltas if scenario and not scenario.committed else {}
            backfills = scenario.backfills if scenario and not scenario.committed else []

            members: dict[str, list[str]] = {}
            for pid, survivor in survivors.items():
                if self.store.projects[pid].registered_seq <= at_seq:
                    members.setdefault(survivor, []).append(pid)

            rows = {}
            for survivor_id, member_ids in members.items():
                rows[survivor_id] = self._build_row(
                    survivor_id=survivor_id,
                    member_ids=sorted(member_ids),
                    as_of=as_of,
                    at_seq=at_seq,
                    caliber_v=caliber_v,
                    metric_events=metric_events,
                    risk_latest=risk_latest,
                    deltas=deltas,
                    backfills=backfills,
                )

            ranking = self._rank(rows, as_of)
            for rank, pid in enumerate(ranking, start=1):
                rows[pid]["rank"] = rank
            warnings = self._warnings(rows, as_of)

            return {
                "spec": {
                    "as_of": as_of.isoformat(),
                    "at_seq": at_seq,
                    "caliber_version": caliber_v,
                    "caliber_description": self.store.calibers[caliber_v].description,
                    "scenario_id": scenario_id,
                    "latest_seq": self.store.seq,
                },
                "ranking": ranking,
                "warnings": warnings,
                "totals": self._totals(rows),
                "projects": {pid: rows[pid] for pid in ranking},
            }

    def _build_row(
        self,
        *,
        survivor_id: str,
        member_ids: list[str],
        as_of: date,
        at_seq: int,
        caliber_v: int,
        metric_events: list,
        risk_latest: dict,
        deltas: dict,
        backfills: list,
    ) -> dict:
        member_set = set(member_ids)
        project = self.store.projects[survivor_id]

        history: dict[MetricType, list] = {}
        for event in metric_events:
            if event.project_id in member_set:
                history.setdefault(event.metric, []).append(event)

        # 情景补录：(member, metric, year) -> 事件，补录值代表年末最新累计
        backfill_map = {
            (e.project_id, e.metric, e.effective_date.year): e for e in backfills
        }

        metric_values: dict[MetricType, dict[int, dict]] = {}
        for metric in MetricType:
            per_year: dict[int, list] = {}
            for event in history.get(metric, []):
                per_year.setdefault(event.effective_date.year, []).append(event)
            yearly_values: dict[int, dict] = {}
            for year, evs in per_year.items():
                value = 0.0
                picked_seq = None
                picked_date = None
                sources: set[str] = set()
                backfilled = False
                for member_id in member_ids:
                    bf = backfill_map.get((member_id, metric, year))
                    member_evs = [e for e in evs if e.project_id == member_id]
                    if bf is not None:
                        value += self._convert(bf.value, bf.caliber_version,
                                               metric, caliber_v)
                        sources.add(f"scenario_backfill:{bf.source}")
                        backfilled = True
                        picked_date = bf.effective_date
                        continue
                    if not member_evs:
                        continue
                    event = max(member_evs, key=lambda e: (e.effective_date, e.seq))
                    value += self._convert(event.value, event.caliber_version,
                                           metric, caliber_v)
                    sources.add(event.source)
                    picked_seq = max(picked_seq or event.seq, event.seq)
                    picked_date = event.effective_date
                delta = deltas.get((survivor_id, metric), 0.0)
                if delta:
                    value = max(0.0, value + delta)
                if value == 0.0 and not evs and not backfilled:
                    continue
                yearly_values[year] = {
                    "value": round(value, 4),
                    "seq": picked_seq,
                    "sources": sorted(sources),
                    "effective_date": picked_date.isoformat() if picked_date else None,
                    "scenario_delta": round(delta, 4) if delta else None,
                    "scenario_backfill": backfilled or None,
                }
            # 补录年份可能在基线中尚无事件
            for (pid, metric_key, year), bf in backfill_map.items():
                if metric_key == metric and pid in member_set and year not in yearly_values:
                    value = self._convert(bf.value, bf.caliber_version, metric, caliber_v)
                    delta = deltas.get((survivor_id, metric), 0.0)
                    if delta:
                        value = max(0.0, value + delta)
                    yearly_values[year] = {
                        "value": round(value, 4),
                        "seq": None,
                        "sources": [f"scenario_backfill:{bf.source}"],
                        "effective_date": bf.effective_date.isoformat(),
                        "scenario_delta": round(delta, 4) if delta else None,
                        "scenario_backfill": True,
                    }
            metric_values[metric] = dict(sorted(yearly_values.items()))

        targets_annual = self._annual_targets(member_set, at_seq, caliber_v)
        targets_multi = self._multiyear_targets(member_set, at_seq, caliber_v)
        schedule = self._schedule(member_set, as_of, at_seq)
        risk = self._risk(member_set, risk_latest)
        farmers_trend = self._farmers_trend(
            history.get(MetricType.FARMERS, []), member_ids, caliber_v,
            survivor_id, deltas, backfill_map,
        )

        completion = {}
        current_year = as_of.year
        for metric, years in metric_values.items():
            current = years.get(current_year)
            target = targets_annual.get((current_year, metric))
            if current is not None and target is not None and target["target"] > 0:
                completion[metric.value] = {
                    "rate": round(current["value"] / target["target"], 4),
                    "value": current["value"],
                    "target": target["target"],
                    "seq": current["seq"],
                    "target_seq": target["seq"],
                }

        multiyear_completion = {}
        for (start, end, metric), target in targets_multi.items():
            years = metric_values.get(metric, {})
            if metric in CUMULATIVE_METRICS:
                value = sum(years[y]["value"] for y in range(start, end + 1) if y in years)
            else:  # 时点数（带农户数）取期末
                value = years[end]["value"] if end in years else 0.0
            if target["target"] > 0:
                multiyear_completion[f"{start}-{end}:{metric.value}"] = {
                    "rate": round(value / target["target"], 4),
                    "value": round(value, 4),
                    "target": target["target"],
                    "start_year": start,
                    "end_year": end,
                    "metric": metric.value,
                    "seq": target["seq"],
                }

        return {
            "project_id": survivor_id,
            "name": project.name,
            "category": project.category,
            "merged_from": sorted(m for m in member_ids if m != survivor_id),
            "members": member_ids,
            "metrics": {
                metric.value: {str(year): payload for year, payload in years.items()}
                for metric, years in metric_values.items()
            },
            "annual_completion": completion,
            "multiyear_completion": multiyear_completion,
            "schedule": schedule,
            "risk": risk,
            "farmers_trend": farmers_trend,
        }

    # ---- 目标 / 延期 / 风险 / 趋势 ------------------------------------

    def _annual_targets(self, member_set: set[str], at_seq: int, caliber_v: int) -> dict:
        latest: dict[tuple[int, MetricType], object] = {}
        for event in self.store.annual_targets:
            if event.seq > at_seq or event.project_id not in member_set:
                continue
            key = (event.year, event.metric)
            if key not in latest or event.seq > latest[key].seq:
                latest[key] = event
        # 合并后目标按实体归集：同年同指标各成员最新目标之和
        grouped: dict[tuple[int, MetricType], list] = {}
        for event in latest.values():
            grouped.setdefault((event.year, event.metric), []).append(event)
        result = {}
        for (year, metric), events in grouped.items():
            result[(year, metric)] = {
                "target": round(sum(
                    self._convert(e.target, e.caliber_version, metric, caliber_v)
                    for e in events
                ), 4),
                "seq": max(e.seq for e in events),
            }
        return result

    def _multiyear_targets(self, member_set: set[str], at_seq: int, caliber_v: int) -> dict:
        latest: dict[tuple, object] = {}
        for event in self.store.multiyear_targets:
            if event.seq > at_seq or event.project_id not in member_set:
                continue
            key = (event.start_year, event.end_year, event.metric)
            if key not in latest or event.seq > latest[key].seq:
                latest[key] = event
        result = {}
        for event in latest.values():
            result[(event.start_year, event.end_year, event.metric)] = {
                "target": round(
                    self._convert(event.target, event.caliber_version,
                                  event.metric, caliber_v),
                    4,
                ),
                "seq": event.seq,
            }
        return result

    def _schedule(self, member_set: set[str], as_of: date, at_seq: int) -> dict:
        """延期判定。合并实体继承被合并方的延期历史，延期不会被合并洗白。"""

        latest: dict[tuple, object] = {}
        for event in self.store.milestone_events:
            if event.seq > at_seq or event.project_id not in member_set:
                continue
            key = (event.project_id, event.name)
            if key not in latest or event.seq > latest[key].seq:
                latest[key] = event

        worst_delay = 0
        delayed: list[dict] = []
        overdue: list[dict] = []
        for event in latest.values():
            if event.completed and event.actual_date is not None:
                delay = (event.actual_date - event.plan_date).days
                if delay > 0:
                    delayed.append({
                        "name": event.name,
                        "delay_days": delay,
                        "plan_date": event.plan_date.isoformat(),
                        "actual_date": event.actual_date.isoformat(),
                        "seq": event.seq,
                    })
                    worst_delay = max(worst_delay, delay)
            elif as_of > event.plan_date:
                delay = (as_of - event.plan_date).days
                overdue.append({
                    "name": event.name,
                    "overdue_days": delay,
                    "plan_date": event.plan_date.isoformat(),
                    "seq": event.seq,
                })
                worst_delay = max(worst_delay, delay)
        status = "on_time"
        if overdue:
            status = "overdue_in_progress"
        elif delayed:
            status = "delayed_closed"
        return {
            "status": status,
            "worst_delay_days": worst_delay,
            "delayed_milestones": sorted(delayed, key=lambda x: -x["delay_days"]),
            "overdue_milestones": sorted(overdue, key=lambda x: -x["overdue_days"]),
        }

    def _risk(self, member_set: set[str], risk_latest: dict) -> dict:
        events = [risk_latest[pid] for pid in member_set if pid in risk_latest]
        if not events:
            return {"risk_level": None, "constraint": False, "seq": None}
        worst = max(events, key=lambda e: e["risk_level"])
        constraint = any(e["constraint"] for e in events)
        return {
            "risk_level": worst["risk_level"],
            "constraint": constraint,
            "effective_date": worst["effective_date"].isoformat(),
            "seq": worst["seq"],
            "scenario_override": worst.get("scenario_override", False),
        }

    def _farmers_trend(self, events: list, member_ids: list[str], caliber_v: int,
                       survivor_id: str, deltas: dict, backfill_map: dict) -> dict:
        """带农户数最近两个时点数对比，识别联农带农规模下滑。"""

        per_day: dict[date, dict[str, object]] = {}
        for event in sorted(events, key=lambda e: (e.effective_date, e.seq)):
            bucket = per_day.setdefault(
                event.effective_date, {"events": [], "seq": 0}
            )
            bucket["events"].append(event)
            bucket["seq"] = max(bucket["seq"], event.seq)

        def day_value(day: date, bucket: dict) -> float:
            total = 0.0
            for member_id in member_ids:
                year = day.year
                bf = backfill_map.get((member_id, MetricType.FARMERS, year))
                member_evs = [e for e in bucket["events"] if e.project_id == member_id]
                if member_evs:
                    event = max(member_evs, key=lambda e: e.seq)
                    total += self._convert(event.value, event.caliber_version,
                                           MetricType.FARMERS, caliber_v)
                elif bf is not None and bf.effective_date == day:
                    total += self._convert(bf.value, bf.caliber_version,
                                           MetricType.FARMERS, caliber_v)
            return max(0.0, total + deltas.get((survivor_id, MetricType.FARMERS), 0.0))

        series = [(day, day_value(day, bucket), bucket["seq"])
                  for day, bucket in sorted(per_day.items())]
        if not series:
            return {"latest": None, "previous": None, "change": None,
                    "change_rate": None}
        latest_day, latest_value, latest_seq = series[-1]
        previous = series[-2] if len(series) >= 2 else None
        change = round(latest_value - previous[1], 4) if previous else None
        rate = round(change / previous[1], 4) if previous and previous[1] else None
        return {
            "latest": {"date": latest_day.isoformat(), "value": round(latest_value, 4),
                       "seq": latest_seq},
            "previous": (
                {"date": previous[0].isoformat(), "value": round(previous[1], 4)}
                if previous else None
            ),
            "change": change,
            "change_rate": rate,
        }

    # ---- 预警 / 评分排序 / 汇总 ----------------------------------------

    def _warnings(self, rows: dict, as_of: date) -> list[dict]:
        warnings: list[dict] = []
        year_start = date(as_of.year, 1, 1)
        year_progress = min(1.0, max(0.0, (as_of - year_start).days / 365.0))
        expected_floor = year_progress * EXPECTED_PROGRESS_TOLERANCE
        for pid, row in rows.items():
            schedule = row["schedule"]
            if schedule["worst_delay_days"] > 0:
                overdue = schedule["overdue_milestones"]
                closed = schedule["delayed_milestones"]
                warnings.append({
                    "project_id": pid,
                    "type": "schedule_delay",
                    "severity": "high" if schedule["worst_delay_days"] >= 30 else "medium",
                    "message": (
                        f"{row['name']} 有 {len(overdue)} 个里程碑到期未完成，"
                        f"最大逾期 {schedule['worst_delay_days']} 天"
                        if overdue else
                        f"{row['name']} 存在已闭环延期，最大延期 "
                        f"{schedule['worst_delay_days']} 天"
                    ),
                    "trigger_seqs": [m["seq"] for m in (overdue + closed)],
                })
            risk = row["risk"]
            if risk.get("constraint") or (risk.get("risk_level") or 0) >= RISK_LEVEL_WARN:
                warnings.append({
                    "project_id": pid,
                    "type": "ecological_constraint",
                    "severity": "high" if risk.get("constraint") else "medium",
                    "message": (
                        f"{row['name']} 触发生态约束（风险等级 {risk['risk_level']}），"
                        "新增投资与资源调入应先经生态审查"
                        if risk.get("constraint") else
                        f"{row['name']} 生态风险等级升至 {risk['risk_level']} 级"
                    ),
                    "trigger_seqs": [risk["seq"]] if risk.get("seq") else [],
                    "scenario_override": risk.get("scenario_override", False),
                })
            trend = row["farmers_trend"]
            if trend["change"] is not None and trend["change"] < 0:
                warnings.append({
                    "project_id": pid,
                    "type": "farmers_drop",
                    "severity": "high" if (trend["change_rate"] or 0) <= -0.1 else "medium",
                    "message": (
                        f"{row['name']} 带农户数由 {trend['previous']['value']} 户"
                        f"降至 {trend['latest']['value']} 户"
                        f"（{trend['change_rate']:+.1%}），联农带农能力弱化需核实"
                    ),
                    "trigger_seqs": [trend["latest"]["seq"]],
                })
            for metric_key, info in row["annual_completion"].items():
                if year_progress > 0 and info["rate"] < expected_floor:
                    warnings.append({
                        "project_id": pid,
                        "type": "target_lag",
                        "severity": "high" if info["rate"] < expected_floor * 0.6 else "medium",
                        "message": (
                            f"{row['name']} {metric_key} 年度完成率 {info['rate']:.1%}，"
                            f"低于时间进度下限 {expected_floor:.0%}，"
                            "资金/产能安排需重新平衡"
                        ),
                        "trigger_seqs": [info["seq"], info["target_seq"]],
                    })
        severity_rank = {"high": 0, "medium": 1, "low": 2}
        warnings.sort(key=lambda w: (severity_rank[w["severity"]], w["project_id"]))
        return warnings

    @staticmethod
    def _risk_score(risk: dict) -> float:
        level = risk.get("risk_level")
        if level is None:
            return 0.0
        score = level / 5 * 30
        if risk.get("constraint"):
            score += 10
        return min(40.0, score)

    def _decision_score(self, row: dict, as_of: date) -> tuple[float, dict, list[str]]:
        """决策评分：分数越高越需要优先调度资金与资源（或先施加生态约束）。

        不止于完成率：生态风险、延期、目标缺口、联农带农下滑各占权重，
        并输出可读的资源转向建议，每条都能回到对应行数据与触发 seq。
        """

        parts: dict[str, float] = {}
        actions: list[str] = []

        parts["risk"] = self._risk_score(row["risk"])
        if row["risk"].get("constraint"):
            actions.append("暂停新增资金注入，先完成生态整改与约束解除审查")
        elif (row["risk"].get("risk_level") or 0) >= RISK_LEVEL_WARN:
            actions.append("提高生态巡护与风险缓释资源配置")

        delay = row["schedule"]["worst_delay_days"]
        parts["schedule"] = round(min(delay / 90, 1) * 20, 2)
        if delay > 0:
            actions.append("按逾期里程碑清单专项调度，资源优先保障卡点工序")

        year_start = date(as_of.year, 1, 1)
        year_progress = max(0.05, min(1.0, (as_of - year_start).days / 365.0))
        gaps = []
        for info in row["annual_completion"].values():
            gaps.append(max(0.0, year_progress - info["rate"]))
        parts["target_gap"] = round(
            (sum(gaps) / len(gaps) / year_progress) * 20, 2
        ) if gaps else 0.0
        if parts["target_gap"] > 8:
            actions.append("完成率显著滞后于时间进度，评估追加投资或将闲置指标调剂他用")

        trend = row["farmers_trend"]
        if trend["change_rate"] is not None and trend["change_rate"] < 0:
            parts["farmers_drop"] = round(min(-trend["change_rate"], 1) * 10, 2)
            actions.append("带农户数下降，核查联农带农协议与收益兑付")
        else:
            parts["farmers_drop"] = 0.0

        # 数据缺口显式提示，避免在缺报时仅凭"完成率=0"误导资金流向
        if not row["metrics"].get(MetricType.INVESTMENT.value):
            actions.append("尚无投资到位报送，需按统一里程碑限期补报")
        if not row["metrics"].get(MetricType.CAPACITY.value):
            actions.append("尚无产能报送，排序仅基于风险与进度，置信度有限")

        if not actions:
            actions.append("运行平稳，维持现有资金与资源配置")
        return round(sum(parts.values()), 2), parts, actions

    def _rank(self, rows: dict, as_of: date) -> list[str]:
        scored = []
        for pid, row in rows.items():
            score, parts, actions = self._decision_score(row, as_of)
            row["decision"] = {
                "score": score,
                "score_parts": {k: round(v, 2) for k, v in parts.items()},
                "recommendations": actions,
            }
            scored.append((score, pid))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [pid for _, pid in scored]

    def _totals(self, rows: dict) -> dict:
        totals: dict[str, float] = {}
        for row in rows.values():
            for metric in MetricType:
                per_year = row["metrics"].get(metric.value, {})
                if not per_year:
                    continue
                latest_year = max(per_year)
                totals[metric.value] = round(
                    totals.get(metric.value, 0.0)
                    + per_year[latest_year]["value"],
                    4,
                )
        return totals

    # ---- 视图比较 -------------------------------------------------------

    def compare(self, base: dict, scenario_view: dict) -> dict:
        """比较基线视图与情景视图：排序、预警、评分、建议如何同步变化。"""

        base_rank = {pid: i for i, pid in enumerate(base["ranking"])}
        scen_rank = {pid: i for i, pid in enumerate(scenario_view["ranking"])}
        rank_changes = []
        for pid in scenario_view["ranking"]:
            rank_changes.append({
                "project_id": pid,
                "rank_base": base_rank[pid] + 1 if pid in base_rank else None,
                "rank_scenario": scen_rank[pid] + 1,
                # 正数表示排序前移（更需关注）
                "rank_move": (
                    base_rank[pid] - scen_rank[pid] if pid in base_rank else None
                ),
            })
        rank_changes.sort(key=lambda x: -(x["rank_move"] or 0))

        def warn_key(w: dict) -> tuple:
            return (w["project_id"], w["type"])

        base_warns = {warn_key(w): w for w in base["warnings"]}
        scen_warns = {warn_key(w): w for w in scenario_view["warnings"]}
        new_warnings = [w for k, w in scen_warns.items() if k not in base_warns]
        cleared_warnings = [w for k, w in base_warns.items() if k not in scen_warns]

        score_changes = []
        for pid, row in scenario_view["projects"].items():
            base_row = base["projects"].get(pid)
            base_score = base_row["decision"]["score"] if base_row else None
            base_actions = set(base_row["decision"]["recommendations"]) if base_row else set()
            scen_actions = set(row["decision"]["recommendations"])
            score_changes.append({
                "project_id": pid,
                "score_base": base_score,
                "score_scenario": row["decision"]["score"],
                "score_move": (
                    round(row["decision"]["score"] - base_score, 2)
                    if base_score is not None else None
                ),
                "recommendations_added": sorted(scen_actions - base_actions),
                "recommendations_removed": sorted(base_actions - scen_actions),
            })
        score_changes.sort(key=lambda x: -(x["score_move"] or 0))

        return {
            "base_spec": base["spec"],
            "scenario_spec": scenario_view["spec"],
            "ranking": base["ranking"],
            "ranking_scenario": scenario_view["ranking"],
            "rank_changes": rank_changes,
            "warnings_new": new_warnings,
            "warnings_cleared": cleared_warnings,
            "score_changes": score_changes,
            "totals_base": base["totals"],
            "totals_scenario": scenario_view["totals"],
        }
