# PaperLeaf 容量与可观测性里程碑

本里程碑位于“本地真实闭环稳定”之后、“专业消息队列与 Kubernetes”之前。目标不是堆叠中间件，而是先让系统能回答三个问题：请求慢在哪里、当前还能承受多少负载、扩容后是否真的改善。

## 当前已完成：Redis 运行态基础

- Docker Compose 增加独立 Redis 服务和健康检查。
- API 使用 Redis 原子维护 Agent 提交的固定窗口限流。
- 限流按用户隔离，用户 ID 和幂等键经 SHA-256 后才进入 Redis Key。
- 相同 `Idempotency-Key` 的请求复用首次限流判定，不因网络重试重复计数。
- Redis 故障时退化为当前 API 进程内限流；`/ready` 明确报告 `available` 或 `degraded`。
- Redis 不保存用户、论文、会话、消息、任务、Agent Run、引用或 PDF 正文。

当前 Redis 数据是可丢失的，因此关闭 RDB/AOF 持久化并使用 `noeviction`。PostgreSQL 继续是业务状态的唯一真相源；Redis 重启只会重置短期计数，不会破坏问答和任务恢复。

完整取舍见 [ADR-0017：Redis 仅承载短期运行态](decisions/0017-redis-runtime-state.md)。

## 当前已完成：RAG 链路可观测

- 每个新 Agent Run 持久保存内容无关的 RAG Trace，覆盖意图、范围、召回通道、证据等级、失败类别、Chunk 策略和阶段耗时；
- 管理员页面提供 24 小时、7 天、30 天的证据漏斗、P50/P95、通道、意图和失败分布；
- 管理员可以同时查看 Redis 内存、Key、连接与降级状态，但不能读取问题、PDF 或回答正文；
- API 与 Worker 暴露低基数 Prometheus 指标，Compose 自动启动 Prometheus 和预置 Grafana 面板；
- PostgreSQL 持久摘要用于可审计聚合，Prometheus 用于趋势，两者均不记录 Cookie、密钥、提示词、PDF 正文或模型回答正文。

详细指标口径见 [RAG 可观测性](observability.md)，取舍见 [ADR-0018](decisions/0018-rag-observability.md)。

仍未完成的全链路观测包括 OpenTelemetry Trace ID、API 路由延迟、Agent 排队时间、模型首字/Token 成本、任务租约恢复、PostgreSQL 连接池和慢查询。这些项目应在容量压测证明需要后逐项加入。

## 下一阶段：容量基线与压测

使用 Locust 建立三组相互独立的负载模型：

1. 轻请求：登录、文献列表、集合树和会话恢复。
2. 数据请求：PDF Range、上传和任务状态轮询。
3. 慢请求：Agent 提交、SSE 重连、全文翻译和结构化产物任务。

测试必须记录机器配置、数据规模、并发模型、持续时间和模型是否真实调用。输出原始数据以及改造前后的 p95、错误率、队列等待时间和吞吐量，不能只报告单个平均值。

## 后续阶段 C：水平扩容

- FastAPI 保持无状态，先增加 API 副本并通过共享 Redis 执行统一限流。
- Worker 按 `agent_run`、解析/OCR、Embedding、翻译和产物生成拆分资源池。
- 引入 PgBouncer 控制 API/Worker 扩容后的数据库连接数。
- PostgreSQL 作业表仍使用租约、领取令牌和 `SKIP LOCKED`；只有任务表开始影响业务数据库时才评估 Celery + RabbitMQ。
- SSE 的可恢复真相仍在 PostgreSQL 事件表；Redis 后续可以作为低延迟唤醒通道，但不能替代事件持久化。

## 升级触发条件

只有出现对应证据时才引入下一层技术：

| 观测结果 | 优先动作 |
|---|---|
| API CPU 饱和且业务查询本身正常 | 增加 API 副本 |
| Agent 排队时间持续升高 | 增加 Agent Worker 或调整模型并发 |
| 翻译阻塞即时问答 | 拆分 Worker 任务池 |
| 数据库连接接近上限 | 引入 PgBouncer |
| PostgreSQL 任务轮询影响 CRUD | 评估 Celery + RabbitMQ |
| 向量检索持续挤占业务数据库 | 评估独立向量服务 |
| 多机、多副本和滚动发布难以手工维护 | 评估 Kubernetes |

## 本里程碑完成标准

- Redis 断开时 API 仍可用，且状态和降级次数可观测。
- 多 API 实例对同一用户执行一致限流。
- 负载测试可以复现，并保留原始配置与结果。
- 能从聚合视图定位意图、召回、证据、生成、答案支持与引用校验阶段耗时。
- 至少完成一次单实例与多实例对比，结论包含瓶颈和成本，而不只写“性能提升”。
