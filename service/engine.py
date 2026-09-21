"""组合驾驶舱应用服务：命令校验、批注权限、情景叠加与发布快照。

- 所有写操作都经过统一校验后落入只追加日志；
- 情景 = 基线事件 + 叠加事件，叠加不允许改变立项/合并/口径等结构事实；
- 批注只追加，按角色可见，任何角色都不能覆盖他人批注；
- 发布视图是写入磁盘的不可变快照，系统恢复后仍按原版本提供。
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

from .models import (
    CATEGORIES,
    RISK_LEVELS,
    DomainError,
    NotFoundError,
    iso_date,
    today_iso,
)
from .storage import EventStore

# 可查看驾驶舱的角色；批注按 audience 控制可见范围
ROLES = ("领导", "发改部门", "林业部门", "项目单位", "乡镇")

# 情景叠加允许的事件类型：只能调指标/风险/约束/进度，不能改结构
SCENARIO_ALLOWED_TYPES = {
    "investment_recorded",
    "income_recorded",
    "employment_recorded",
    "capacity_recorded",
    "farmer_count_recorded",
    "risk_recorded",
    "constraint_triggered",
    "constraint_lifted",
    "milestone_reached",
}

MILESTONE_CATALOG = ("立项批复", "用地落实", "主体开工", "中期验收", "投产达效")

# 内置基准口径版本，无需 caliber_defined 即可使用，折算系数恒为 1
BASELINE_CALIBER = "v1"


class Cockpit:
    """驾驶舱门面。"""

    def __init__(self, store: EventStore, published_dir: str | Path) -> None:
        self.store = store
        self._published_dir = Path(published_dir)
        self._published_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # ================= 写入：统一事件入口 =================

    def intake(self, data: dict[str, Any], scenario: str | None = None) -> dict[str, Any]:
        """接收一个事件。

        请求体：``{"type", "occurred_on", "source", "source_batch",
        "event_id"?, "scenario"?, "payload": {...}}``，指标字段也允许平铺。
        """

        event_type = data.get("type") or data.get("event_type")
        if not event_type:
            raise DomainError("缺少事件类型 type")
        occurred_on = self._date_str(data.get("occurred_on") or today_iso())
        source = str(data.get("source") or "未注明来源")
        source_batch = str(data.get("source_batch") or "default")
        event_id = data.get("event_id")
        # 离线批次/补报应保留来源单位的实际记录时间；缺省由存储生成
        recorded_at = data.get("recorded_at")
        payload = self._extract_payload(data)

        if scenario is None and data.get("scenario"):
            scenario = str(data["scenario"])

        with self._lock:
            if event_id and self._event_id_exists(event_id, scenario):
                raise DomainError(f"事件 {event_id} 已存在，请勿重复提交")
            if scenario is not None:
                self._validate_scenario_overlay(scenario, event_type, payload, occurred_on)
            else:
                self._validate_baseline(event_type, payload, occurred_on)
            event = self.store.append(
                event_type,
                payload,
                occurred_on=occurred_on,
                source=source,
                source_batch=source_batch,
                scenario=scenario,
                event_id=event_id,
                recorded_at=recorded_at,
            )
        return event.to_dict()

    def _extract_payload(self, data: dict[str, Any]) -> dict[str, Any]:
        payload = dict(data.get("payload") or {})
        reserved = {
            "type", "event_type", "occurred_on", "source", "source_batch",
            "event_id", "scenario", "payload", "recorded_at",
        }
        for key, value in data.items():
            if key not in reserved and value is not None:
                payload.setdefault(key, value)
        return payload

    def _event_id_exists(self, event_id: str, scenario: str | None) -> bool:
        target = self.store.events_for_scenario(scenario)
        return any(e.event_id == event_id for e in target)

    # ---------- 基线校验 ----------

    def _baseline_events(self) -> list:
        return self.store.all_events(None)

    def _project_exists(self, project_id: str, as_of: str | None = None) -> bool:
        for e in self._baseline_events():
            if e.event_type == "project_registered" and e.payload.get("project_id") == project_id:
                if as_of is None or e.occurred_on <= as_of:
                    return True
        return False

    def _caliber_versions(self) -> set[str]:
        return {
            e.payload["version"]
            for e in self._baseline_events()
            if e.event_type == "caliber_defined"
        }

    def _caliber_effective_on(self, version: str) -> str | None:
        for e in self._baseline_events():
            if e.event_type == "caliber_defined" and e.payload["version"] == version:
                return e.occurred_on
        return None

    def _milestone_exists(self, project_id: str, milestone: str) -> bool:
        for e in self._baseline_events():
            if (
                e.event_type == "milestone_scheduled"
                and e.payload.get("project_id") == project_id
                and e.payload.get("milestone") == milestone
            ):
                return True
        return False

    def _validate_constraint_lifecycle(
        self, project_id: str, constraint: str, on: str, event_type: str
    ) -> None:
        """约束成对出现：未解除时不能重复触发，未触发时不能解除。"""

        active = False
        for e in self._baseline_events():
            p = e.payload
            if (
                p.get("project_id") == project_id
                and p.get("constraint") == constraint
                and e.occurred_on <= on
            ):
                if e.event_type == "constraint_triggered":
                    active = True
                elif e.event_type == "constraint_lifted":
                    active = False
        if event_type == "constraint_triggered" and active:
            raise DomainError(f"约束 {constraint} 已在触发中，不能重复触发")
        if event_type == "constraint_lifted" and not active:
            raise DomainError(f"约束 {constraint} 未在触发中，不能解除")

    def _scenario_names(self) -> set[str]:
        return {
            e.payload["name"]
            for e in self._baseline_events()
            if e.event_type == "scenario_registered"
        }

    def _validate_baseline(self, event_type: str, p: dict, on: str) -> None:
        if event_type == "project_registered":
            pid = self._require(p, "project_id")
            if self._project_exists(pid):
                raise DomainError(f"项目 {pid} 已立项")
            if p.get("category") not in CATEGORIES:
                raise DomainError(f"category 必须是 {CATEGORIES} 之一")
            self._require(p, "name")
            return

        if event_type == "project_merged":
            merged, survivor = self._require(p, "merged_id"), self._require(p, "survivor_id")
            if merged == survivor:
                raise DomainError("被合并方与存续方不能相同")
            if not self._project_exists(merged, on) or not self._project_exists(survivor, on):
                raise DomainError("合并双方必须均已立项")
            # 截至合并日不能形成环、不能把已注销方作为存续方
            root = survivor
            seen = set()
            merge_pairs = {
                e.payload["merged_id"]: e.payload["survivor_id"]
                for e in self._baseline_events()
                if e.event_type == "project_merged" and e.occurred_on <= on
            }
            while root in merge_pairs and root not in seen:
                seen.add(root)
                root = merge_pairs[root]
            if root == merged:
                raise DomainError("合并关系不能形成环")
            return

        if event_type == "caliber_defined":
            version = self._require(p, "version")
            if version in self._caliber_versions():
                raise DomainError(f"口径版本 {version} 已存在")
            coeffs = p.get("coefficients", {})
            if not isinstance(coeffs, dict) or any(float(v) <= 0 for v in coeffs.values()):
                raise DomainError("口径折算系数必须为正数")
            return

        if event_type == "target_set":
            metric = self._require(p, "metric")
            year = int(self._require(p, "year"))
            if year < 2000 or year > 2100:
                raise DomainError("跨年度目标年份超出合理范围")
            if float(self._require(p, "value")) <= 0:
                raise DomainError("目标值必须为正数")
            pid = p.get("project_id")
            if pid and not self._project_exists(pid, on):
                raise DomainError(f"项目 {pid} 尚未立项，不能下达目标")
            return

        if event_type == "scenario_registered":
            name = self._require(p, "name")
            if name in self._scenario_names():
                raise DomainError(f"情景 {name} 已登记")
            return

        if event_type == "annotation_added":
            self._validate_annotation(p)
            return

        if event_type in ("milestone_scheduled", "milestone_reached"):
            pid = self._require(p, "project_id")
            self._require(p, "milestone")
            if not self._project_exists(pid, on):
                raise DomainError(f"项目 {pid} 尚未立项")
            if event_type == "milestone_scheduled":
                due = self._require(p, "due_on")
                iso_date(due)
                milestone = p["milestone"]
                if milestone not in MILESTONE_CATALOG:
                    raise DomainError(
                        f"统一里程碑必须取自目录：{', '.join(MILESTONE_CATALOG)}"
                    )
                if self._milestone_exists(pid, milestone):
                    raise DomainError(f"项目 {pid} 的里程碑 {milestone} 已排期")
                weight = float(p.get("weight", 1.0))
                if weight <= 0:
                    raise DomainError("里程碑权重必须为正数")
            elif event_type == "milestone_reached":
                if not self._milestone_exists(pid, p["milestone"]):
                    raise DomainError(
                        f"项目 {pid} 的里程碑 {p['milestone']} 尚未排期，不能核验达成"
                    )
            return

        # 指标 / 风险 / 约束类
        metric_project_types = {
            "investment_recorded", "income_recorded", "employment_recorded",
            "capacity_recorded", "farmer_count_recorded", "risk_recorded",
            "constraint_triggered", "constraint_lifted",
        }
        if event_type in metric_project_types:
            pid = self._require(p, "project_id")
            if not self._project_exists(pid, on):
                raise DomainError(f"项目 {pid} 尚未立项，不能接收指标")
            if event_type in ("investment_recorded", "income_recorded"):
                if float(self._require(p, "amount")) < 0:
                    raise DomainError("金额不能为负；冲销请用红字事件并注明原因")
                version = p.get("reported_version")
                if version is not None and version != BASELINE_CALIBER:
                    effective_on = self._caliber_effective_on(version)
                    if effective_on is None:
                        raise DomainError(f"口径版本 {version} 尚未定义")
                    if effective_on > on:
                        raise DomainError(
                            f"口径版本 {version} 自 {effective_on} 才生效，"
                            f"不能用于 {on} 的业务"
                        )
            if event_type == "risk_recorded" and p.get("level") not in RISK_LEVELS:
                raise DomainError(f"风险等级必须是 {RISK_LEVELS} 之一")
            if event_type in ("constraint_triggered", "constraint_lifted"):
                self._require(p, "constraint")
                self._validate_constraint_lifecycle(
                    pid, p["constraint"], on, event_type
                )
            return

        raise DomainError(f"未知或不支持的事件类型：{event_type}")

    def _validate_annotation(self, p: dict) -> None:
        text = str(self._require(p, "text")).strip()
        if not text:
            raise DomainError("批注内容不能为空")
        role = self._require(p, "role")
        if role not in ROLES:
            raise DomainError(f"批注角色必须是 {ROLES} 之一")
        audience = p.get("audience") or [role]
        if not isinstance(audience, list) or any(r not in ROLES for r in audience):
            raise DomainError(f"可见范围 audience 必须是 {ROLES} 的子集")
        target_type = p.get("target_type", "portfolio")
        if target_type not in ("project", "portfolio"):
            raise DomainError("批注对象必须是 project 或 portfolio")
        if target_type == "project" and not p.get("target_id"):
            raise DomainError("项目批注必须提供 target_id")
        # 作者归属写入事件，后续任何人只能新增、不能改写或删除
        p.setdefault("author", role)
        p["audience"] = audience
        p["target_type"] = target_type

    # ---------- 情景叠加校验 ----------

    def _validate_scenario_overlay(
        self, scenario: str, event_type: str, p: dict, on: str
    ) -> None:
        if scenario not in self._scenario_names():
            raise NotFoundError(f"情景 {scenario} 未登记")
        if event_type not in SCENARIO_ALLOWED_TYPES:
            raise DomainError(
                f"情景叠加只允许调整指标/风险/约束/进度，不允许 {event_type}"
            )
        pid = self._require(p, "project_id")
        if not self._project_exists(pid):
            raise DomainError(f"情景引用的基线项目 {pid} 不存在")
        if event_type in ("investment_recorded", "income_recorded"):
            if float(self._require(p, "amount")) < 0:
                raise DomainError("金额不能为负")
        if event_type == "risk_recorded" and p.get("level") not in RISK_LEVELS:
            raise DomainError(f"风险等级必须是 {RISK_LEVELS} 之一")
        if event_type in ("constraint_triggered", "constraint_lifted"):
            self._require(p, "constraint")
            self._validate_overlay_constraint_lifecycle(
                scenario, pid, p["constraint"], on, event_type
            )

    def _validate_overlay_constraint_lifecycle(
        self, scenario: str, project_id: str, constraint: str, on: str, event_type: str
    ) -> None:
        active = False
        for e in self.store.events_for_scenario(scenario):
            p = e.payload
            if (
                p.get("project_id") == project_id
                and p.get("constraint") == constraint
                and e.occurred_on <= on
            ):
                if e.event_type == "constraint_triggered":
                    active = True
                elif e.event_type == "constraint_lifted":
                    active = False
        if event_type == "constraint_triggered" and active:
            raise DomainError(f"约束 {constraint} 已在触发中，不能重复触发")
        if event_type == "constraint_lifted" and not active:
            raise DomainError(f"约束 {constraint} 未在触发中，不能解除")

    # ================= 批注读取（按角色过滤） =================

    def annotations(self, role: str, target_type: str | None = None,
                    target_id: str | None = None) -> list[dict[str, Any]]:
        if role not in ROLES:
            raise DomainError(f"角色必须是 {ROLES} 之一")
        result = []
        for e in self._baseline_events():
            if e.event_type != "annotation_added":
                continue
            p = e.payload
            if role not in p.get("audience", []):
                continue
            if target_type and p.get("target_type") != target_type:
                continue
            if target_id and p.get("target_id") != target_id:
                continue
            result.append({
                "annotation_id": e.event_id,
                "target_type": p.get("target_type"),
                "target_id": p.get("target_id"),
                "author": p.get("author"),
                "role": p.get("role"),
                "text": p.get("text"),
                "audience": p.get("audience", []),
                "occurred_on": e.occurred_on,
                "source": e.source,
            })
        return result

    def scenarios(self) -> list[dict[str, Any]]:
        result = []
        for e in self._baseline_events():
            if e.event_type == "scenario_registered":
                p = e.payload
                overlay_count = sum(
                    1 for ev in self.store.all_events(p["name"])
                )
                result.append({
                    "name": p["name"],
                    "title": p.get("title", p["name"]),
                    "description": p.get("description", ""),
                    "created_by": p.get("created_by"),
                    "registered_on": e.occurred_on,
                    "overlay_events": overlay_count,
                })
        return result

    # ================= 版本指纹与发布快照 =================

    def version_fingerprint(self, scenario: str | None, as_of: str) -> dict[str, Any]:
        """对截至某日参与计算的事件求哈希，作为视图版本依据。"""

        events = [
            e for e in self.store.events_for_scenario(scenario)
            if e.occurred_on <= as_of
        ]
        digest = hashlib.sha256()
        for e in events:
            digest.update(f"{e.seq}|{e.event_id}|{e.scenario or ''}\n".encode("utf-8"))
        return {
            "as_of": as_of,
            "scenario": scenario,
            "event_count": len(events),
            "last_seq": max((e.seq for e in events), default=0),
            "fingerprint": digest.hexdigest(),
        }

    def publish(self, view: dict[str, Any], name: str, published_by: str) -> dict[str, Any]:
        """把已生成的视图固化为不可变快照文件。"""

        with self._lock:
            existing = [p.stem for p in self._published_dir.glob("view-*.json")]
            view_id = f"view-{len(existing) + 1:04d}"
            snapshot = {
                "view_id": view_id,
                "name": name,
                "published_by": published_by,
                "published_at": today_iso(),
                "immutable": True,
                **view,
            }
            path = self._published_dir / f"{view_id}.json"
            path.write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return {"view_id": view_id, "name": name}

    def load_published(self, view_id: str) -> dict[str, Any]:
        # 防止路径穿越
        safe = Path(view_id).name
        path = self._published_dir / f"{safe}.json"
        if not path.exists():
            raise NotFoundError(f"已发布视图 {view_id} 不存在")
        return json.loads(path.read_text(encoding="utf-8"))

    def list_published(self) -> list[dict[str, Any]]:
        result = []
        for path in sorted(self._published_dir.glob("view-*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            result.append({
                "view_id": data["view_id"],
                "name": data.get("name"),
                "published_by": data.get("published_by"),
                "published_at": data.get("published_at"),
                "scenario": data.get("version", {}).get("scenario"),
                "as_of": data.get("version", {}).get("as_of"),
                "fingerprint": data.get("version", {}).get("fingerprint"),
            })
        return result

    @staticmethod
    def _require(p: dict, key: str) -> Any:
        if key not in p or p[key] in (None, ""):
            raise DomainError(f"缺少必填字段：{key}")
        return p[key]

    @staticmethod
    def _date_str(value: Any) -> str:
        return iso_date(value).isoformat()
