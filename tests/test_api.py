"""HTTP API 端到端测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from service.app import CockpitApp, create_server
from tests.fixtures import build_store
from tests.httpbase import HttpTestBase


class ApiFlowTest(HttpTestBase):
    """从报送、视图、预警到快照的完整接口链路。"""

    def setUp(self) -> None:
        super().setUp()
        # 用富数据夹具替换空存储
        store = build_store()
        self.app.reset(store)
        self.store = store

    def test_health_and_view(self) -> None:
        status, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok"})

        status, view = self.request("GET", "/api/view?as_of=2026-09-21")
        self.assertEqual(status, 200)
        self.assertEqual(view["spec"]["at_seq"], self.store.seq)
        self.assertIn("P-KY", view["ranking"])
        self.assertTrue(any(
            w["type"] == "schedule_delay" for w in view["warnings"]
        ))

    def test_report_and_view_slice(self) -> None:
        status, resp = self.request("POST", "/api/risks", {
            "project_id": "P-YC", "risk_level": 5, "constraint": True,
            "effective_date": "2026-09-15", "source": "生态环境局",
        })
        self.assertEqual(status, 201)
        new_seq = resp["seq"]

        status, view = self.request("GET", "/api/view?as_of=2026-09-21")
        self.assertEqual(status, 200)
        self.assertTrue(view["projects"]["P-YC"]["risk"]["constraint"])

        # 切片到该事件之前：约束尚不存在
        status, old = self.request(
            "GET", f"/api/view?as_of=2026-09-21&at_seq={new_seq - 1}"
        )
        self.assertEqual(status, 200)
        self.assertFalse(old["projects"]["P-YC"]["risk"]["constraint"])

    def test_missing_field_returns_400(self) -> None:
        status, body = self.request("POST", "/api/metrics", {
            "project_id": "P-YC", "value": 100,
            "effective_date": "2026-09-01",
        })
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "bad_request")

    def test_idempotency_conflict_returns_400(self) -> None:
        payload = {
            "project_id": "P-YC", "metric": "income", "value": 1,
            "effective_date": "2026-09-01", "source": "乡村振兴局",
            "idempotency_key": "DUP-001",
        }
        status, _ = self.request("POST", "/api/metrics", payload)
        self.assertEqual(status, 201)
        status, body = self.request("POST", "/api/metrics", payload)
        self.assertEqual(status, 400)

    def test_full_scenario_dispatch_over_http(self) -> None:
        # 发布调度前快照
        status, snap = self.request("POST", "/api/snapshots", {
            "name": "Q3前视图", "as_of": "2026-09-21",
        }, headers={"X-User": "李主任"})
        self.assertEqual(status, 201)
        old_seq = snap["at_seq"]

        # 建情景 → 三项调整 → 比较
        status, scn = self.request("POST", "/api/scenarios", {
            "name": "Q3调度", "as_of": "2026-09-21",
        }, headers={"X-User": "王县长"})
        self.assertEqual(status, 201)
        sid = scn["id"]

        status, _ = self.request("POST", f"/api/scenarios/{sid}/adjust", {
            "project_id": "P-YC", "metric": "capacity", "delta": -200,
        })
        self.assertEqual(status, 200)
        status, _ = self.request("POST", f"/api/scenarios/{sid}/risk", {
            "project_id": "P-TS", "risk_level": 5, "constraint": True,
        })
        self.assertEqual(status, 200)
        status, _ = self.request("POST", f"/api/scenarios/{sid}/backfill", {
            "project_id": "P-KY", "metric": "income", "value": 8000000,
            "effective_date": "2026-06-30", "source": "乡村振兴局(补录)",
        })
        self.assertEqual(status, 200)

        status, cmp_ = self.request("GET", f"/api/compare?scenario={sid}")
        self.assertEqual(status, 200)
        self.assertEqual(cmp_["ranking_scenario"][0], "P-TS")
        self.assertTrue(any(
            w["project_id"] == "P-TS" and w["type"] == "ecological_constraint"
            for w in cmp_["warnings_new"]
        ))

        # 固化
        status, result = self.request("POST", f"/api/scenarios/{sid}/commit", {},
                                      headers={"X-User": "常务副县长"})
        self.assertEqual(status, 200)
        self.assertGreater(result["committed_seq"], old_seq)

        # 已发布视图冻结
        status, frozen = self.request("GET", "/api/snapshots/Q3前视图")
        self.assertEqual(status, 200)
        self.assertEqual(frozen["at_seq"], old_seq)
        self.assertFalse(
            frozen["payload"]["projects"]["P-TS"]["risk"]["constraint"]
        )
        # 当前视图已更新
        status, current = self.request("GET", "/api/view?as_of=2026-09-21")
        self.assertTrue(current["projects"]["P-TS"]["risk"]["constraint"])

    def test_annotation_permissions_over_http(self) -> None:
        status, ann = self.request("POST", "/api/annotations", {
            "content": "财政评审：康养投资需附拨付计划",
            "project_id": "P-KY",
        }, headers={"X-User": "张财政", "X-Role": "财政局"})
        self.assertEqual(status, 201)
        aid = ann["id"]

        # 其他单位修改被拒
        status, body = self.request("PATCH", f"/api/annotations/{aid}", {
            "content": "被农业局改写",
        }, headers={"X-User": "李农业", "X-Role": "农业农村局"})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "forbidden")

        # 删除同样被拒
        status, _ = self.request("DELETE", f"/api/annotations/{aid}",
                                 headers={"X-User": "李农业", "X-Role": "农业农村局"})
        self.assertEqual(status, 403)

        # 作者可改
        status, edited = self.request("PATCH", f"/api/annotations/{aid}", {
            "content": "财政评审：拨付计划已补齐",
        }, headers={"X-User": "张财政", "X-Role": "财政局"})
        self.assertEqual(status, 200)
        self.assertEqual(edited["edits"], 1)

        # admin 可删
        status, _ = self.request("DELETE", f"/api/annotations/{aid}",
                                 headers={"X-User": "管理员", "X-Role": "admin"})
        self.assertEqual(status, 200)
        status, listed = self.request(
            "GET", "/api/annotations?project=P-KY"
        )
        self.assertEqual(status, 200)
        self.assertEqual(listed, [])

    def test_events_audit_chain(self) -> None:
        status, events = self.request("GET", "/api/events?kind=risk")
        self.assertEqual(status, 200)
        self.assertTrue(all(e.get("risk_level") for e in events))
        status, ky_events = self.request("GET", "/api/events?project=P-KY")
        self.assertEqual(status, 200)
        self.assertTrue(all(
            e.get("project_id") == "P-KY"
            or e.get("source_id") == "P-KY"
            or e.get("target_id") == "P-KY"
            for e in ky_events
        ))
        seqs = [e["seq"] for e in ky_events]
        self.assertEqual(seqs, sorted(seqs))


class PersistenceOverRestartTest(unittest.TestCase):
    """系统恢复后已发布视图保持原版本（真实落盘 + 新进程对象模拟重启）。"""

    def test_snapshot_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_file = Path(tmp) / "state.json"
            store = build_store()
            app = CockpitApp(store, data_file=data_file)
            builder = app.builder
            view = builder.build(as_of=date(2026, 6, 30))
            payload = json.dumps(view, sort_keys=True, ensure_ascii=False)
            store.publish_snapshot(
                name="半年视图", payload=view, created_by="李主任",
                at_seq=view["spec"]["at_seq"], as_of=date(2026, 6, 30),
                caliber_version=1, scenario_id=None,
            )
            app.persist()

            # 模拟系统重启：新建应用从同一文件恢复
            restarted = CockpitApp(data_file=data_file)
            self.assertEqual(restarted.store.seq, store.seq)
            restored_payload = json.dumps(
                restarted.store.snapshots["半年视图"].payload,
                sort_keys=True, ensure_ascii=False,
            )
            self.assertEqual(payload, restored_payload)

            # 恢复后接收新事件，旧快照不受影响
            restarted.store.report_risk(
                project_id="P-YC", risk_level=5, constraint=True,
                effective_date="2026-09-15", source="生态环境局",
            )
            restarted.persist()
            again = CockpitApp(data_file=data_file)
            self.assertEqual(
                json.dumps(again.store.snapshots["半年视图"].payload,
                           sort_keys=True, ensure_ascii=False),
                payload,
            )
            self.assertTrue(again.builder.build(
                as_of=date(2026, 9, 21))["projects"]["P-YC"]["risk"]["constraint"])


if __name__ == "__main__":
    unittest.main()
