# 已知边界

`v0.9.0` 是可自托管的 portfolio release，不是 production GA。以下边界不会在发布说明中被弱化或改写。

## 模型与回答质量

- Specialist v3 默认关闭。当前 v1/v2/v3 数据仍为 `quality_pending`，48 例完整对照与人工盲评尚未完成，不能声称多 Agent 提升了回答质量。
- GitHub CI 中的隔离全栈 smoke 使用确定性 OpenAI-compatible stub，只验证上传、索引、后台 Run、SSE、引用、权限和 PDF Range，不评价真实模型的事实正确性。
- 外部模型、OpenAlex、Semantic Scholar、Crossref 与 Ollama 受本地网络、配额和 Provider 行为影响；相关真实模型验证是发布前人工门禁，不属于普通 CI。
- 扫描型 PDF 在 OCR 未配置或识别失败时仍可保存和阅读，但全文检索、选文与引用覆盖会受影响。

## 评测口径

- 历史冻结指标由对应历史 Git SHA 产生，当前 HEAD 仅校验证据包完整性，不能把历史数字写成当前版本重新运行的结果。
- 已揭盲诊断集的最终组合检索出现过 `64.2% → 61.6%` 的负结果；仓库保留该结果，不把它改写为提升。
- RAG、Harness 和多 Agent 指标分别衡量检索、编排与回答证据，不可互相替代；LLM Judge 也不能命名为人工准确率。

## 部署与数据

- 当前备份方案通过暂停 API/Worker 获得 PostgreSQL 与 MinIO 一致快照。演练中的 RPO=0 仅适用于该停写窗口；尚未提供在线 PITR、跨区域副本或生产 SLA。
- 默认 Compose 是单机参考部署。大规模部署仍需外置 PostgreSQL/pgvector、对象存储、Redis、独立 Worker 池、TLS 终止和集中密钥管理。
- 文献库分页目前在客户端对已鉴权结果每 20 篇分页，解决日常浏览和长列表滚动；超大文献库仍应补服务端游标分页以减少 API 载荷。
- 管理员 RAG 页面默认展示最近 7 天。该时间范围没有 Run 时会显示无样本，不代表历史 trace 被删除；可切换到 30 天继续核对。

## Agent 恢复语义

- Agent Job 使用租约、claim token fencing 和 PostgreSQL Checkpoint，提供至少一次执行与旧 Worker 拒写；不宣称任意外部 Provider 调用 exactly-once。
- 已完成 Specialist 分支可从 Checkpoint 恢复，但进程在外部请求返回前崩溃时，该未完成请求可能重新发起。写操作仍必须通过幂等和人工确认门禁。
