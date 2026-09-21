"""HTTP 测试夹具：在随机端口上绑定内存应用。"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from urllib.parse import quote

from service.app import CockpitApp, create_server
from service.store import EventStore


class HttpTestBase(unittest.TestCase):
    """启动一个绑定临时 EventStore 的服务实例。"""

    def setUp(self) -> None:
        self.store = EventStore()
        self.app = CockpitApp(self.store, data_file="/tmp/cockpit-test-do-not-persist.json")
        # 不落盘：直接让 persist 成为 no-op，避免测试触碰共享路径
        self.app.persist = lambda: None  # type: ignore[method-assign]
        self.server = create_server("127.0.0.1", 0, app=self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        headers: dict | None = None,
    ) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        encoded_headers = {
            key: quote(value, safe="") for key, value in (headers or {}).items()
        }
        encoded_path = quote(path, safe="/?&=:%")
        req = urllib.request.Request(
            self.base + encoded_path, data=data, method=method,
            headers={"Content-Type": "application/json", **encoded_headers},
        )
        try:
            with urllib.request.urlopen(req) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)
