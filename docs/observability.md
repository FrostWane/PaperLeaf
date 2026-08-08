# RAG 可观测性

PaperLeaf 同时提供两类互补视图：管理员页面读取 PostgreSQL 中的持久运行摘要，适合查看当前实例的业务质量；Prometheus/Grafana 读取 API 与 Worker 的时序指标，适合观察趋势、延迟和进程资源。两者都不保存问题正文、PDF 内容、回答正文、用户 ID、论文 ID 或 Agent Run ID。

## 管理员页面

管理员进入“管理 → RAG 运行质量”，可以切换 24 小时、7 天和 30 天窗口。接口为：

```text
GET /api/v1/admin/observability?window=24h
```

普通用户访问返回 `403`。统计最多读取窗口内最近 5000 条 Agent Run；达到上限时页面会明确提示，不能把截断结果当作完整总体。

没有分母时，比例显示为“—”而不是 `0%`；页面同时展示分子、分母、指标生成时间和 Trace 覆盖率。时间窗口请求使用序号门禁，迟到响应不能覆盖用户最后选择的窗口；用户、任务、模型状态和 RAG 指标独立落盘，单个管理接口失败不会让其余成功数据退回演示值。

主要口径：

| 指标 | 分子 | 分母 |
|---|---|---|
| 运行失败率 | 状态为 `failed` 的终态运行 | 全部终态运行 |
| 引用回答率 | 最终保存至少一个合法引用的运行 | 带 V1 RAG Trace 的运行 |
| RAG 降级率 | 证据不足、引用未通过、模型故障等运行 | 带 V1 RAG Trace 的运行 |
| 证据充足率 | 证据评级为 `sufficient` 的运行 | 对应通道或意图的 Trace 运行 |
| 指标覆盖率 | 带 V1 RAG Trace 的运行 | 窗口内终态运行 |

证据漏斗按“已采集运行 → 召回证据 → 证据充足 → 充分证据引用”逐层收缩；最后一步要求证据等级与引用门禁同时通过。阶段耗时分别覆盖意图识别、证据召回、证据评级、回答生成、答案支持检查和引用校验，并展示 P50/P95。召回通道、意图、失败原因和 Chunk 策略都使用服务端限定枚举，禁止把用户输入写入标签。

管理员页还展示 Redis 可用状态、已用/最大内存、Key 数、连接数、活跃用户、处理中任务和 AI 服务状态。Redis 指标只表示短期运行态容量；业务数据容量仍应从 PostgreSQL 与 MinIO 监控。

## Agent Harness 聚合

“管理 → Agent Harness”按 24 小时、7 天或 30 天展示 Context Token 压缩、指代置信度、Skill 路由、Tool 调用和 MCP 成功率；长期记忆只展示当前条目、启用数、有记忆用户数和这些用户的容量快照。接口为：

```text
GET /api/v1/admin/harness/metrics?window=24h
GET /api/v1/admin/mcp/servers
```

Token 压缩率是窗口内压缩前后 Token 总量的削减比例，不是触发压缩的运行比例。指代需澄清率只以存在置信度的 Run 为分母。Skill 完成率只计算 `completed/failed/cancelled` 终态，等待中或运行中的 Run 不进入分母。达到最近 5000 次 Run 或 10000 次 Tool Call 查询上限时页面会显示截断警告。

MCP/Tool 指标只使用服务、工具、状态和错误分类等低基数标签；问题、用户、论文、Session、Chunk、Memory、参数和结果不能进入 Prometheus 标签或管理员聚合。接口失败与真实零样本在前端分开显示，上一次成功结果会明确标注后保留。

## Prometheus 与 Grafana

Docker Compose 默认启动：

- Prometheus：<http://localhost:9090>
- Grafana：<http://localhost:3001>
- API 指标：<http://localhost:8000/metrics/>
- Worker 指标：仅在 Compose 私有网络的 `worker:9101`

Grafana 会自动装载 `PaperLeaf RAG` 面板。管理员账号来自 `.env` 中的 `GRAFANA_ADMIN_USER` 和 `GRAFANA_ADMIN_PASSWORD`。

核心指标：

| 指标 | 类型 | 说明 |
|---|---|---|
| `paperleaf_agent_runs_total` | Counter | 按终态、结果、失败类别、意图和范围累计运行 |
| `paperleaf_agent_run_duration_seconds` | Histogram | Agent 端到端耗时 |
| `paperleaf_rag_stage_duration_seconds` | Histogram | 各 RAG 阶段耗时 |
| `paperleaf_rag_retrieval_channel_total` | Counter | 各召回通道参与次数与召回结果 |
| `paperleaf_rag_evidence_count` | Histogram | 每次运行召回的证据数量 |

Prometheus 指标由进程维护，容器重启后 Counter 会重置；管理员聚合以 PostgreSQL 持久运行摘要为事实源，不受 Prometheus 进程重启影响。多 Worker 部署时，Prometheus 必须抓取每个副本，Grafana 查询再按标签聚合。

## 失败分类

- `no_evidence`：没有召回证据；
- `insufficient_evidence`：有候选片段但质量门禁不通过；
- `unverified_answer`：生成内容未通过答案支持或引用校验；
- `model_timeout`：模型调用超时；
- `model_unavailable`：模型未配置、熔断或不可用；
- `scope_violation`：证据越出服务端冻结的论文范围；
- `cancelled`：用户取消；
- `internal`：其余受控运行异常。

这些分类用于定位链路，不等同于回答事实正确率。Recall@K、引用页准确率、不可回答错误作答率等质量指标仍由冻结评测集计算，不能用线上引用回答率替代。

## 隐私与基数约束

RAG Trace 只持久化稳定枚举、数量、毫秒耗时、证据等级和 Chunk 策略。Prometheus Label 不得增加邮箱、用户 ID、论文 ID、Chunk ID、Session ID、Run ID、问题文本、模型回答或错误堆栈。需要排查单次故障时，应使用有权限控制的应用日志与数据库记录，并继续避免记录 PDF 正文和密钥。

## 当前边界

- 尚未引入 OpenTelemetry 跨服务 Trace ID；当前阶段耗时来自同一次 Worker Graph 执行。
- 管理员聚合单次最多读取 5000 条记录，适合个人和小型自托管实例；更大规模应改为预聚合表或分析存储。
- Redis 不启用持久化，只承担限流等可丢失运行态；它的容量不是 PostgreSQL/MinIO 业务容量。
- 当前不报告模型 Token 成本、数据库连接池、队列等待 P95 或 API 路由级延迟，这些属于下一容量压测阶段。
