# 林业项目组合驾驶舱

集体林权改革与林下产业协作场景下的县级项目组合调度系统。药材、康养、特色林、
森林人家等项目按**统一里程碑**接收投资、产能、就业、增收、带农户数与生态风险事件，
解决各单位汇总口径不一、只看"完成率"无法判断资金与资源该转向哪里的问题。

## 核心能力

- **只追加事件链**：统一 seq、来源单位、发生日期、报送时口径版本；幂等报送、
  更正留链（supersedes）、晚到补报与早期版本切片并存。
- **版本化视图**：按 `as_of / at_seq / caliber_version` 任意切片，完成率只是
  视图字段之一；口径带生效日期与重述因子，新视图统一新口径。
- **项目合并**：按生效日归集指标/目标，风险取最高、延期不洗白，历史视图不合并。
- **预警与决策排序**：延期、生态约束、带农户数下降、目标滞后四类预警；
  多维决策评分输出资源转向建议，缺报显式标注而非当作零完成率。
- **情景比较**：一次季度调度可同时下调产能、提升风险、补录收益，对比排序、
  预警、评分、建议的同步变化；批准后 append-only 固化。
- **批注权限**：作者本人或 admin 才能改删，越权返回 403 不覆盖，编辑全程留痕。
- **发布冻结 + 恢复**：已发布视图保存完整 payload，系统重启后逐字节保持原版本。

仅使用 Python 标准库，无外部依赖。

## 运行

```bash
python3 -m service.main          # 默认 http://0.0.0.0:3000，数据文件 data/state.json
PORT=3100 DATA_FILE=/tmp/state.json python3 -m service.main
```

## 测试

```bash
python3 -m unittest              # 30 个测试：领域规则 + HTTP 端到端 + 落盘恢复
```

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/projects` | 登记项目 |
| POST | `/api/metrics` | 报送五类指标（支持 `idempotency_key`、`supersedes`） |
| POST | `/api/risks` | 报送生态风险（1–5 级、约束标记） |
| POST | `/api/milestones` | 报送里程碑（计划/实际日期判定延期） |
| POST | `/api/merges` | 项目合并（带生效日） |
| POST | `/api/targets/annual` `/api/targets/multiyear` | 年度 / 跨年度目标 |
| POST | `/api/calibers` | 新口径版本（生效日 + 重述因子） |
| GET | `/api/view?as_of=&at_seq=&caliber=&scenario=` | 版本化组合视图（排序/预警/建议） |
| GET | `/api/events?kind=&project=` | 只追加事件审计链 |
| POST/GET | `/api/scenarios`、`/api/scenarios/{id}/adjust|risk|backfill|commit` | 情景试算与固化 |
| GET | `/api/compare?scenario=` | 基线 vs 情景：排序/预警/决策差异 |
| POST/GET | `/api/snapshots[/{name}]` | 发布冻结视图 / 读取发布时原版本 |
| POST/PATCH/DELETE | `/api/annotations[/{id}]` | 批注（`X-User`/`X-Role` 头鉴权，非 ASCII 百分号编码） |
| GET/POST | `/api/state/export` `/api/state/import` | 状态导出与整体恢复 |

## 快速示例

```bash
# 报送产能
curl -X POST localhost:3000/api/metrics -H 'Content-Type: application/json' \
  -d '{"project_id":"P-YC","metric":"capacity","value":500,
       "effective_date":"2026-06-30","source":"农业农村局"}'

# 当前视图：排序、预警、决策建议
curl 'localhost:3000/api/view?as_of=2026-09-21'
```

领域规则详见 [docs/domain.md](docs/domain.md)。
业务数据与敏感配置应存放在受控环境中，运行时数据默认写入 `data/`（已在 `.gitignore`）。
