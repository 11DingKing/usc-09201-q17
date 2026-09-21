"""林业项目组合驾驶舱 HTTP 服务（标准库实现，无外部依赖）。

写接口在 append-only 存储上登记事件并原子落盘；读接口按
as_of / at_seq / caliber_version / scenario 切片产出视图。
已发布视图为冻结快照，系统重启后内容保持发布时版本。
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .domain import MetricType, PermissionError_, ValidationError
from .projection import PortfolioViewBuilder
from .scenarios import ScenarioService
from .store import EventStore

DATA_FILE = Path(os.environ.get("DATA_FILE", "data/state.json"))


class CockpitApp:
    """组装存储、读取模型与情景服务。"""

    def __init__(self, store: EventStore | None = None,
                 data_file: Path | str | None = None) -> None:
        self.data_file = Path(data_file) if data_file else DATA_FILE
        self.store = store or EventStore.load_file(self.data_file) or EventStore()
        self.builder = PortfolioViewBuilder(self.store)
        self.scenarios = ScenarioService(self.store)

    def persist(self) -> None:
        self.store.save_file(self.data_file)

    def reset(self, store: EventStore) -> None:
        self.store = store
        self.builder = PortfolioViewBuilder(store)
        self.scenarios = ScenarioService(store)


def _json_default(value: object) -> object:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, MetricType):
        return value.value
    raise TypeError(f"不可序列化的类型：{type(value)!r}")


def _dumps(payload: object) -> bytes:
    return json.dumps(payload, default=_json_default, ensure_ascii=False).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    """驾驶舱 REST 接口。"""

    server_version = "ForestryCockpit/1.0"

    # ---- 基础 -----------------------------------------------------------

    def _send(self, status: int, payload: object) -> None:
        body = _dumps(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValidationError(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(data, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        return data

    def _actor(self, data: dict) -> tuple[str, str]:
        # 中文身份信息以百分号编码经头传递（HTTP 头只保证 latin-1）
        user = unquote(self.headers.get("X-User") or "") or data.pop("_user", "") or "匿名"
        role = unquote(self.headers.get("X-Role") or "") or data.pop("_role", "") or "viewer"
        return user, role

    def log_message(self, format: str, *args: object) -> None:
        return

    # ---- 路由 -----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            path, query = parsed.path, parse_qs(parsed.query)
            if path == "/health":
                self._send(200, {"status": "ok"})
            elif path == "/api/view":
                self._view(query)
            elif path == "/api/compare":
                self._compare(query)
            elif path == "/api/projects":
                self._list_projects()
            elif path == "/api/events":
                self._events(query)
            elif path == "/api/snapshots":
                self._list_snapshots()
            elif path.startswith("/api/snapshots/"):
                self._get_snapshot(path.rsplit("/", 1)[-1])
            elif path == "/api/annotations":
                self._list_annotations(query)
            elif path == "/api/calibers":
                self._list_calibers()
            elif path == "/api/scenarios":
                self._list_scenarios()
            elif path == "/api/state/export":
                self._send(200, self.server.app.store.to_dict())
            elif path == "/":
                self._dashboard()
            else:
                self._send(404, {"error": "not_found"})
        except (ValidationError, ValueError, KeyError) as exc:
            self._send(400, {"error": "bad_request", "message": str(exc).strip("'\"")})

    def do_POST(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            data = self._read_json()
            if path == "/api/projects":
                self._register_project(data)
            elif path == "/api/metrics":
                self._report_metric(data)
            elif path == "/api/risks":
                self._report_risk(data)
            elif path == "/api/milestones":
                self._report_milestone(data)
            elif path == "/api/merges":
                self._merge(data)
            elif path == "/api/targets/annual":
                self._annual_target(data)
            elif path == "/api/targets/multiyear":
                self._multiyear_target(data)
            elif path == "/api/calibers":
                self._add_caliber(data)
            elif path == "/api/annotations":
                self._add_annotation(data)
            elif path == "/api/snapshots":
                self._publish_snapshot(data)
            elif path == "/api/scenarios":
                self._create_scenario(data)
            elif path.startswith("/api/scenarios/") and path.endswith("/adjust"):
                self._scn_adjust(path.split("/")[3], data)
            elif path.startswith("/api/scenarios/") and path.endswith("/risk"):
                self._scn_risk(path.split("/")[3], data)
            elif path.startswith("/api/scenarios/") and path.endswith("/backfill"):
                self._scn_backfill(path.split("/")[3], data)
            elif path.startswith("/api/scenarios/") and path.endswith("/commit"):
                self._scn_commit(path.split("/")[3], data)
            elif path == "/api/state/import":
                self._import_state(data)
            else:
                self._send(404, {"error": "not_found"})
        except PermissionError_ as exc:
            self._send(403, {"error": "forbidden", "message": str(exc)})
        except (ValidationError, KeyError) as exc:
            message = str(exc).strip("'\"")
            self._send(400, {"error": "bad_request",
                             "message": f"缺少必填字段或内容不合法：{message}"})

    def do_PATCH(self) -> None:  # noqa: N802
        try:
            data = self._read_json()
            if self.path.startswith("/api/annotations/"):
                annotation_id = unquote(self.path.rsplit("/", 1)[-1])
                user, role = self._actor(data)
                annotation = self.server.app.store.edit_annotation(
                    annotation_id=annotation_id, editor=user, role=role,
                    content=data.get("content", ""),
                )
                self.server.app.persist()
                self._send(200, {"id": annotation.id, "content": annotation.content,
                                 "edits": len(annotation.edits)})
            else:
                self._send(404, {"error": "not_found"})
        except PermissionError_ as exc:
            self._send(403, {"error": "forbidden", "message": str(exc)})
        except ValidationError as exc:
            self._send(400, {"error": "bad_request", "message": str(exc)})

    def do_DELETE(self) -> None:  # noqa: N802
        try:
            if self.path.startswith("/api/annotations/"):
                annotation_id = self.path.rsplit("/", 1)[-1]
                user = unquote(self.headers.get("X-User") or "匿名")
                role = unquote(self.headers.get("X-Role") or "viewer")
                self.server.app.store.delete_annotation(
                    annotation_id=annotation_id, editor=user, role=role
                )
                self.server.app.persist()
                self._send(200, {"id": annotation_id, "deleted": True})
            else:
                self._send(404, {"error": "not_found"})
        except PermissionError_ as exc:
            self._send(403, {"error": "forbidden", "message": str(exc)})
        except ValidationError as exc:
            self._send(400, {"error": "bad_request", "message": str(exc)})

    # ---- 写入接口 -------------------------------------------------------

    def _register_project(self, data: dict) -> None:
        project = self.server.app.store.register_project(
            project_id=data["project_id"], name=data["name"],
            category=data.get("category", ""),
            effective_date=data["effective_date"],
            source=data.get("source", "未注明来源"),
        )
        self.server.app.persist()
        self._send(201, {"seq": project.registered_seq, "project_id": project.id})

    def _report_metric(self, data: dict) -> None:
        event = self.server.app.store.report_metric(
            project_id=data["project_id"], metric=data["metric"],
            value=data["value"], effective_date=data["effective_date"],
            source=data.get("source", "未注明来源"), note=data.get("note", ""),
            idempotency_key=data.get("idempotency_key"),
            supersedes=data.get("supersedes"),
            caliber_version=data.get("caliber_version"),
        )
        self.server.app.persist()
        self._send(201, {"seq": event.seq, "caliber_version": event.caliber_version})

    def _report_risk(self, data: dict) -> None:
        event = self.server.app.store.report_risk(
            project_id=data["project_id"], risk_level=data["risk_level"],
            constraint=data.get("constraint", False),
            effective_date=data["effective_date"],
            source=data.get("source", "未注明来源"), note=data.get("note", ""),
            idempotency_key=data.get("idempotency_key"),
        )
        self.server.app.persist()
        self._send(201, {"seq": event.seq})

    def _report_milestone(self, data: dict) -> None:
        event = self.server.app.store.report_milestone(
            project_id=data["project_id"], name=data["name"],
            plan_date=data["plan_date"], actual_date=data.get("actual_date"),
            completed=data.get("completed", False),
            effective_date=data.get("effective_date", data["plan_date"]),
            source=data.get("source", "未注明来源"), note=data.get("note", ""),
        )
        self.server.app.persist()
        self._send(201, {"seq": event.seq})

    def _merge(self, data: dict) -> None:
        event = self.server.app.store.merge_projects(
            source_id=data["source_id"], target_id=data["target_id"],
            effective_date=data["effective_date"],
            source=data.get("source", "未注明来源"), note=data.get("note", ""),
        )
        self.server.app.persist()
        self._send(201, {"seq": event.seq, "source_id": event.source_id,
                         "target_id": event.target_id})

    def _annual_target(self, data: dict) -> None:
        event = self.server.app.store.set_annual_target(
            project_id=data["project_id"], year=int(data["year"]),
            metric=data["metric"], target=data["target"],
            source=data.get("source", "未注明来源"),
            caliber_version=data.get("caliber_version"), note=data.get("note", ""),
        )
        self.server.app.persist()
        self._send(201, {"seq": event.seq})

    def _multiyear_target(self, data: dict) -> None:
        event = self.server.app.store.set_multiyear_target(
            project_id=data["project_id"], start_year=int(data["start_year"]),
            end_year=int(data["end_year"]), metric=data["metric"],
            target=data["target"], source=data.get("source", "未注明来源"),
            caliber_version=data.get("caliber_version"), note=data.get("note", ""),
        )
        self.server.app.persist()
        self._send(201, {"seq": event.seq})

    def _add_caliber(self, data: dict) -> None:
        caliber = self.server.app.store.add_caliber(
            effective_date=data["effective_date"],
            description=data.get("description", ""),
            factors=data.get("factors"),
            source=data.get("source", "未注明来源"),
        )
        self.server.app.persist()
        self._send(201, {"caliber_version": caliber.version,
                         "effective_date": caliber.effective_date.isoformat()})

    def _add_annotation(self, data: dict) -> None:
        user, role = self._actor(data)
        annotation = self.server.app.store.add_annotation(
            author=user, role=role, content=data["content"],
            project_id=data.get("project_id"), metric=data.get("metric"),
            seq_ref=data.get("seq_ref"),
        )
        self.server.app.persist()
        self._send(201, {"id": annotation.id, "author": annotation.author,
                         "role": annotation.role})

    def _publish_snapshot(self, data: dict) -> None:
        user, _ = self._actor(data)
        name = data["name"]
        scenario_id = data.get("scenario_id")
        view = self.server.app.builder.build(
            as_of=data["as_of"], at_seq=data.get("at_seq"),
            caliber_version=data.get("caliber_version"),
            scenario_id=scenario_id,
        )
        snapshot = self.server.app.store.publish_snapshot(
            name=name, payload=view, created_by=user,
            at_seq=view["spec"]["at_seq"], as_of=date.fromisoformat(view["spec"]["as_of"]),
            caliber_version=view["spec"]["caliber_version"],
            scenario_id=scenario_id,
        )
        self.server.app.persist()
        self._send(201, {"name": snapshot.name, "at_seq": snapshot.at_seq,
                         "caliber_version": snapshot.caliber_version,
                         "created_at": snapshot.created_at.isoformat()})

    # ---- 情景接口 -------------------------------------------------------

    def _create_scenario(self, data: dict) -> None:
        user, _ = self._actor(data)
        scenario = self.server.app.scenarios.create(
            name=data["name"], as_of=data["as_of"], created_by=user,
            base_seq=data.get("base_seq"),
        )
        self.server.app.persist()
        self._send(201, {"id": scenario.id, "name": scenario.name,
                         "base_seq": scenario.base_seq})

    def _scn_adjust(self, scenario_id: str, data: dict) -> None:
        scenario = self.server.app.scenarios.adjust_metric(
            f"SCN-{scenario_id}" if not scenario_id.startswith("SCN-") else scenario_id,
            project_id=data["project_id"], metric=data["metric"], delta=data["delta"],
        )
        self.server.app.persist()
        self._send(200, {"id": scenario.id,
                         "metric_deltas": {
                             f"{pid}|{m.value}": v
                             for (pid, m), v in scenario.metric_deltas.items()}})

    def _scn_risk(self, scenario_id: str, data: dict) -> None:
        sid = scenario_id if scenario_id.startswith("SCN-") else f"SCN-{scenario_id}"
        scenario = self.server.app.scenarios.override_risk(
            sid, project_id=data["project_id"], risk_level=data["risk_level"],
            constraint=data.get("constraint", False),
        )
        self.server.app.persist()
        self._send(200, {"id": scenario.id, "risk_overrides": scenario.risk_overrides})

    def _scn_backfill(self, scenario_id: str, data: dict) -> None:
        sid = scenario_id if scenario_id.startswith("SCN-") else f"SCN-{scenario_id}"
        scenario = self.server.app.scenarios.backfill_income(
            sid, project_id=data["project_id"], metric=data.get("metric", "income"),
            value=data["value"], effective_date=data["effective_date"],
            source=data.get("source", "未注明来源"), note=data.get("note", ""),
        )
        self.server.app.persist()
        self._send(200, {"id": scenario.id, "backfills": len(scenario.backfills)})

    def _scn_commit(self, scenario_id: str, data: dict) -> None:
        sid = scenario_id if scenario_id.startswith("SCN-") else f"SCN-{scenario_id}"
        user, _ = self._actor(data)
        result = self.server.app.scenarios.commit(sid, committed_by=user)
        self.server.app.persist()
        self._send(200, result)

    # ---- 读接口 ---------------------------------------------------------

    def _view(self, query: dict) -> None:
        view = self.server.app.builder.build(
            as_of=query.get("as_of", [date.today().isoformat()])[0],
            at_seq=int(query["at_seq"][0]) if query.get("at_seq") else None,
            caliber_version=(
                int(query["caliber"][0]) if query.get("caliber") else None
            ),
            scenario_id=query.get("scenario", [None])[0],
        )
        self._send(200, view)

    def _compare(self, query: dict) -> None:
        sid = query.get("scenario", [None])[0]
        if not sid:
            raise ValidationError("compare 需要 scenario 参数")
        sid = sid if sid.startswith("SCN-") else f"SCN-{sid}"
        caliber = int(query["caliber"][0]) if query.get("caliber") else None
        self._send(200, self.server.app.scenarios.compare(sid, caliber_version=caliber))

    def _list_projects(self) -> None:
        self._send(200, [
            {"project_id": p.id, "name": p.name, "category": p.category,
             "effective_date": p.effective_date.isoformat(), "source": p.source,
             "registered_seq": p.registered_seq}
            for p in sorted(self.server.app.store.projects.values(), key=lambda p: p.id)
        ])

    def _events(self, query: dict) -> None:
        """事件审计链：可按类型/项目过滤，始终按 seq 输出。"""

        kind = query.get("kind", ["all"])[0]
        project = query.get("project", [None])[0]
        chains = {
            "metric": self.server.app.store.metric_events,
            "risk": self.server.app.store.risk_events,
            "milestone": self.server.app.store.milestone_events,
            "merge": self.server.app.store.merge_events,
        }
        events: list = []
        if kind == "all":
            for items in chains.values():
                events.extend(items)
            events.sort(key=lambda e: e.seq)
        else:
            events = list(chains.get(kind, []))
        if project:
            events = [
                e for e in events
                if getattr(e, "project_id", None) == project
                or getattr(e, "source_id", None) == project
                or getattr(e, "target_id", None) == project
            ]
        self._send(200, [json.loads(_dumps(e.__dict__)) for e in events])

    def _list_snapshots(self) -> None:
        self._send(200, [
            {"name": s.name, "at_seq": s.at_seq, "as_of": s.as_of.isoformat(),
             "caliber_version": s.caliber_version, "scenario_id": s.scenario_id,
             "created_by": s.created_by, "created_at": s.created_at.isoformat()}
            for s in sorted(self.server.app.store.snapshots.values(), key=lambda s: s.created_at)
        ])

    def _get_snapshot(self, name: str) -> None:
        name = unquote(name)
        snapshot = self.server.app.store.snapshots.get(name)
        if snapshot is None:
            self._send(404, {"error": "not_found", "message": f"快照不存在：{name}"})
            return
        self._send(200, {
            "name": snapshot.name,
            "at_seq": snapshot.at_seq,
            "as_of": snapshot.as_of.isoformat(),
            "caliber_version": snapshot.caliber_version,
            "scenario_id": snapshot.scenario_id,
            "created_by": snapshot.created_by,
            "created_at": snapshot.created_at.isoformat(),
            "payload": snapshot.payload,  # 发布时冻结的完整内容，原样返回
        })

    def _list_annotations(self, query: dict) -> None:
        project = query.get("project", [None])[0]
        items = [
            a for a in self.server.app.store.annotations.values()
            if not a.deleted and (project is None or a.project_id == project)
        ]
        self._send(200, [
            {"id": a.id, "author": a.author, "role": a.role, "content": a.content,
             "project_id": a.project_id,
             "metric": a.metric.value if a.metric else None, "seq_ref": a.seq_ref,
             "created_at": a.created_at.isoformat(),
             "edit_count": len(a.edits),
             "edits": [
                 {"editor": e.editor, "role": e.role,
                  "edited_at": e.edited_at.isoformat(),
                  "old_content": e.old_content, "new_content": e.new_content}
                 for e in a.edits
             ]}
            for a in sorted(items, key=lambda a: a.created_at)
        ])

    def _list_calibers(self) -> None:
        self._send(200, [
            {"version": c.version, "effective_date": c.effective_date.isoformat(),
             "description": c.description, "source": c.source,
             "factors": {m.value: c.factors[m] for m in MetricType}}
            for c in sorted(self.server.app.store.calibers.values(), key=lambda c: c.version)
        ])

    def _list_scenarios(self) -> None:
        self._send(200, [
            {"id": s.id, "name": s.name, "base_seq": s.base_seq,
             "as_of": s.as_of.isoformat(), "created_by": s.created_by,
             "committed": s.committed, "committed_seq": s.committed_seq,
             "metric_deltas": {
                 f"{pid}|{m.value}": v for (pid, m), v in s.metric_deltas.items()},
             "risk_overrides": s.risk_overrides,
             "backfill_count": len(s.backfills)}
            for s in sorted(self.server.app.store.scenarios.values(), key=lambda s: s.id)
        ])

    def _import_state(self, data: dict) -> None:
        """用导出包整体恢复状态（恢复后已发布视图仍为发布时版本）。"""

        store = EventStore.from_dict(data)
        self.server.app.reset(store)
        self.server.app.persist()
        self._send(200, {"ok": True, "seq": store.seq,
                         "snapshots": len(store.snapshots)})

    def _dashboard(self) -> None:
        body = DASHBOARD_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def create_server(host: str = "0.0.0.0", port: int = 0,
                  app: CockpitApp | None = None) -> ThreadingHTTPServer:
    """创建可由应用与测试共同使用的服务实例，可绑定指定应用上下文。"""

    server = ThreadingHTTPServer((host, port), Handler)
    server.app = app or CockpitApp()
    return server


def main() -> None:
    port = int(os.environ.get("PORT", "3000"))
    server = create_server(port=port)
    print(f"服务已启动：http://0.0.0.0:{port}（数据文件 {server.app.data_file}）")
    server.serve_forever()


DASHBOARD_HTML = "<!doctype html><meta charset='utf-8'><title>林业项目组合驾驶舱</title>"


if __name__ == "__main__":
    main()
