"""驾驶舱 HTTP 接口（标准库实现）。

路由：
- GET  /health
- POST /api/events                统一事件入口（基线；body.scenario 可写情景叠加）
- GET  /api/events                事件审计流（?scenario= 仅叠加事件）
- GET  /api/scenarios
- GET  /api/dashboard?as_of=&scenario=   组合视图（排序/预警/决策依据）
- GET  /api/compare?as_of=&scenario=     基线 vs 情景差异
- GET  /api/version?as_of=&scenario=     视图版本指纹
- POST /api/publish               固化不可变视图 {as_of, scenario, name}
- GET  /api/published[/{view_id}]
- GET/POST /api/annotations       批注（读取需 ?role=，按可见范围过滤）
"""

from __future__ import annotations

import json
import os
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import analytics
from .engine import Cockpit
from .models import DomainError, NotFoundError, today_iso
from .storage import EventStore


def build_cockpit(data_dir: str | os.PathLike[str] | None = None) -> Cockpit:
    """根据数据目录组装驾驶舱（启动时自动重放恢复）。"""

    base = Path(data_dir or os.environ.get("DATA_DIR", "data"))
    store = EventStore(base / "events.jsonl")
    return Cockpit(store, base / "published")


class CockpitHandler(BaseHTTPRequestHandler):
    """请求处理器；cockpit 实例由 ``create_server`` 注入类属性。"""

    cockpit: Cockpit | None = None

    # ---------- 基础工具 ----------

    def _send_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise DomainError(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(data, dict):
            raise DomainError("请求体必须是 JSON 对象")
        return data

    def _query(self) -> dict[str, str]:
        parsed = parse_qs(urlparse(self.path).query)
        return {k: v[-1] for k, v in parsed.items()}

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return

    # ---------- 路由 ----------

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            if path == "/health":
                self._send_json(200, {"status": "ok"})
            elif path == "/api/events":
                self._handle_list_events()
            elif path == "/api/scenarios":
                self._send_json(200, {"scenarios": self.cockpit.scenarios()})
            elif path == "/api/dashboard":
                self._handle_dashboard()
            elif path == "/api/compare":
                self._handle_compare()
            elif path == "/api/version":
                self._handle_version()
            elif path == "/api/published":
                self._send_json(200, {"views": self.cockpit.list_published()})
            elif path.startswith("/api/published/"):
                self._send_json(200, self.cockpit.load_published(path.rsplit("/", 1)[1]))
            elif path == "/api/annotations":
                self._handle_list_annotations()
            else:
                self._send_json(404, {"error": "not_found"})
        except (DomainError, NotFoundError, ValueError) as exc:
            self._send_json(400 if isinstance(exc, (DomainError, ValueError)) else 404,
                            {"error": type(exc).__name__, "message": str(exc)})
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            self._send_json(500, {"error": "internal_error"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            data = self._read_json()
            if path == "/api/events":
                scenario = data.pop("scenario", None) or None
                event = self.cockpit.intake(data, scenario=scenario)
                self._send_json(201, {"event": event})
            elif path == "/api/annotations":
                event = self.cockpit.intake({
                    "type": "annotation_added",
                    **data,
                })
                self._send_json(201, {"annotation_id": event["event_id"]})
            elif path == "/api/publish":
                self._handle_publish(data)
            else:
                self._send_json(404, {"error": "not_found"})
        except DomainError as exc:
            self._send_json(400, {"error": "DomainError", "message": str(exc)})
        except NotFoundError as exc:
            self._send_json(404, {"error": "NotFoundError", "message": str(exc)})
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            self._send_json(500, {"error": "internal_error"})

    # ---------- 业务端点 ----------

    def _handle_list_events(self) -> None:
        query = self._query()
        if query.get("all") == "true":
            events = self.cockpit.store.all_events_including_scenarios()
            scope = "*"
        else:
            scope = query.get("scenario") or None
            events = self.cockpit.store.all_events(scope)
        self._send_json(200, {
            "scenario": scope,
            "count": len(events),
            "events": [e.to_dict() for e in events],
        })

    def _handle_dashboard(self) -> None:
        query = self._query()
        as_of = query.get("as_of") or today_iso()
        scenario = query.get("scenario") or None
        self._send_json(200, analytics.build_view(
            self.cockpit.store, as_of=as_of, scenario=scenario))

    def _handle_compare(self) -> None:
        query = self._query()
        as_of = query.get("as_of") or today_iso()
        scenario = query.get("scenario") or None
        if not scenario:
            raise DomainError("情景对比必须提供 scenario 参数")
        baseline = analytics.build_view(self.cockpit.store, as_of=as_of)
        scen = analytics.build_view(self.cockpit.store, as_of=as_of, scenario=scenario)
        self._send_json(200, analytics.compare_views(baseline, scen))

    def _handle_version(self) -> None:
        query = self._query()
        as_of = query.get("as_of") or today_iso()
        self._send_json(200, self.cockpit.version_fingerprint(
            query.get("scenario") or None, as_of))

    def _handle_publish(self, data: dict) -> None:
        as_of = data.get("as_of") or today_iso()
        scenario = data.get("scenario")
        name = data.get("name") or f"调度视图-{as_of}"
        role = data.get("published_by") or "领导"
        view = analytics.build_view(self.cockpit.store, as_of=as_of, scenario=scenario)
        view["version"] = self.cockpit.version_fingerprint(scenario, as_of)
        saved = self.cockpit.publish(view, name=name, published_by=role)
        self._send_json(201, saved)

    def _handle_list_annotations(self) -> None:
        query = self._query()
        role = query.get("role")
        if not role:
            raise DomainError("读取批注必须提供 role 参数")
        items = self.cockpit.annotations(
            role,
            target_type=query.get("target_type"),
            target_id=query.get("target_id"),
        )
        self._send_json(200, {"role": role, "count": len(items), "annotations": items})


def create_server(
    host: str = "0.0.0.0",
    port: int = 0,
    data_dir: str | os.PathLike[str] | None = None,
) -> ThreadingHTTPServer:
    """创建可由应用与测试共同使用的服务实例。"""

    cockpit = build_cockpit(data_dir)

    class _Handler(CockpitHandler):
        pass

    _Handler.cockpit = cockpit
    server = ThreadingHTTPServer((host, port), _Handler)
    server.cockpit = cockpit  # type: ignore[attr-defined]
    return server
