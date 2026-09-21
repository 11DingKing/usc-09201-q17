# 林业项目组合驾驶舱

面向集体林权改革与林下产业协作的县级项目组合驾驶舱：药材、康养、特色林、
森林人家四类项目按**统一里程碑、统一口径**上报投资、产能、就业、增收与生态
风险事件，支持季度调度情景推演、项目合并、跨年度目标、权限批注与不可变发布。

## 运行

```bash
python3 -m service.main            # 默认 :3000，数据目录 data/
PORT=3000 DATA_DIR=data python3 -m service.main
python3 -m unittest discover -s tests   # 15 个测试
curl http://127.0.0.1:3000/health
```

仅依赖 Python 3.11 标准库；事件以 JSONL 落盘，发布视图以 JSON 快照固化，
启动时自动重放恢复（含半行尾记录截断）。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/api/events` | 统一事件入口（body 带 `scenario` 即写情景叠加） |
| GET | `/api/events[?scenario=&all=true]` | 事件审计流（保留来源/批次/口径） |
| GET | `/api/scenarios` | 情景清单 |
| GET | `/api/dashboard?as_of=&scenario=` | 组合视图：指标、排序、预警、决策建议 |
| GET | `/api/compare?as_of=&scenario=` | 基线与情景的同步差异 |
| GET | `/api/version?as_of=&scenario=` | 参与计算事件的 SHA-256 指纹 |
| POST | `/api/publish` | 固化不可变视图 `{as_of, scenario, name}` |
| GET | `/api/published[/{view_id}]` | 已发布视图（恢复后仍为原版本） |
| POST/GET | `/api/annotations` | 权限批注（读取按 `role` + audience 过滤） |

## 事件类型

立项 `project_registered`、投资/增收/就业/产能/带农户数 `*_recorded`、
里程碑排期与达成 `milestone_scheduled/reached`、生态风险 `risk_recorded`、
约束触发/解除 `constraint_triggered/lifted`、项目合并 `project_merged`、
跨年度目标 `target_set`、口径版本 `caliber_defined`、情景 `scenario_registered`、
批注 `annotation_added`。

## 快速示例

```bash
curl -s -XPOST localhost:3000/api/events -H 'Content-Type: application/json' -d '{
  "type": "risk_recorded", "occurred_on": "2026-06-27",
  "source": "县林业局", "source_batch": "季度调度",
  "project_id": "P-SL", "level": "高",
  "scenario": "Q2调度"
}'
curl -s "localhost:3000/api/compare?as_of=2026-06-30&scenario=Q2%E8%B0%83%E5%BA%A6"
```

领域规则见 [`docs/domain.md`](docs/domain.md)。
