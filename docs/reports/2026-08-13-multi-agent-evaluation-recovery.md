# 多 Agent 对照、冲突证据与故障恢复报告

日期：2026-08-13

## 结论

本轮没有得到“v3 一定优于 v1/v2”的结论。完成的 4 条冻结用例显示：三版结构性完成率和引用合法性都很好，但 v2 的跨论文覆盖暂时最好，v3 更慢且仍有分支超时与 Schema 失败。人工盲评尚未填写，因此质量结论保持 `quality_pending`。

与此同时，真实 Worker 强杀实验发现并修复了一个自动化测试未覆盖的问题：LangGraph `Send` 同一 superstep 在屏障完成前退出时，已完成分支会随父节点重放。修复后，父 Research Graph 与每个 Specialist 都有独立 PostgreSQL Checkpoint thread，新 Worker 只重跑未完成分支。

## 三版本定义

| 版本 | 路径 | 特点 |
|---|---|---|
| v1 | `single_agent_v1` | 标准检索、生成和引用核验 |
| v2 | `compare_map_reduce_v2` | 确定性分组并行检索与合并，不增加 Specialist 模型 |
| v3 | `specialist_subgraph_v3` | 最多三个隔离 Specialist、确定性 reducer、冲突集合和独立综合上下文 |

采集器固定同一问题、论文范围、语料快照、模型配置和执行协议，并保存输入、范围、语料、模型与结果哈希。Token 仍是 `estimated_not_provider_billed_usage`，不能冒充账单成本。

人工盲评包与版本答案映射分为两个文件：评审者只能拿到不含 v1/v2/v3 映射的
`*-blind.jsonl`；评测负责人完成收集后，再用独立的 `*-blind-key.jsonl` 解盲，避免在同一文件里
通过隐藏字段泄漏版本。

## 真实对照结果

本机 Docker Compose、DeepSeek、Ollama、PostgreSQL/pgvector 和 Redis 下完成 4 个 Case × 3 个版本，共 12 个真实模型 Run。

| 指标 | v1 | v2 | v3 |
|---|---:|---:|---:|
| 完成 | 4/4 | 4/4 | 4/4 |
| 回退 | 0/4 | 0/4 | 0/4 |
| 合法物理页引用 | 15/15 | 15/15 | 12/12 |
| 非法引用 | 0 | 0 | 0 |
| 冻结主张证据覆盖 | 1/14 | 1/14 | 1/14 |
| 所需论文覆盖 | 8/11 | 10/11 | 6/11 |
| 已输出主张支持 | 16/16 | 15/15 | 18/18 |
| p95 总耗时 | 93.1 秒 | 95.5 秒 | 131.7 秒 |
| 模型调用 | 27 | 31 | 42 |
| 工具调用 | 4 | 0 | 0 |
| 主链估算输入/输出 Token | 26560 / 1190 | 27479 / 1241 | 18591 / 1258 |
| v3 Specialist 估算输入/输出 Token | — | — | 18619 / 5592 |
| 分支异常 | — | — | 超时 2、Schema 3 |

这些数字的直接含义是：当前 v3 没有取得覆盖收益，且有明显额外延迟和模型调用成本。它的价值目前更多体现在独立上下文、故障隔离、冲突建模与可观测性，而非已经证明的质量提升。

## 冲突证据

Specialist 主张新增：

```text
claim_key
claim
stance = support | contradict | unclear
evidence_aliases
paper_ids（服务端从合法证据回填）
confidence
```

服务端只接受本分支合法 `E1…En` 别名，并重新验证 Chunk、论文范围和物理页。相同 `claim_key` 或达到确定性相似阈值的跨论文主张进入 `ConflictSet`；support 与 contradict 同时保留，Synthesizer 被要求按论文和实验条件并列回答。模型自由文本仍不能直接成为 Citation。

## 真实 Worker 强杀实验

实验 Run：`158bc990-7a63-409a-8a18-bc6cc0f44f0b`。

1. 三个 Specialist 均开始；s2、s3 完成，s1 仍在运行。
2. 强制停止 `paperleaf-worker-1`。
3. PostgreSQL 已存在独立 `specialist-research-v3` 与 `specialist-branch-s1/s2/s3` Checkpoint thread。
4. 将唯一测试 Job 的 `claimed_at` 推进到 31 分钟前，等价模拟默认 30 分钟租约过期。
5. 新 Worker 领取同一 Job；attempt 从 1 增加到 2，claim token 已轮换，父 Agent Run ID 不变。
6. 接管后只出现 s1 的第二次 start；s2/s3 没有重新启动。s1 完成后进入 merge、最终回答和引用核验，Run 完成。
7. 用旧 claim token 写入 `recovery:stale-claim-probe`，Repository 返回拒绝，数据库对应事件数为 0。

第一次强杀实验曾出现 s2/s3 重跑。该失败不是被忽略的噪声，而是推动“分支独立 thread”修复的真实证据。当前语义仍是 at-least-once：崩溃时正在运行但尚未写入 terminal checkpoint 的分支会重跑；已经 terminal 的分支不会重跑。

## 分支观测

管理员 Harness 聚合新增：

- v2/v3 版本分布；
- 每个 Specialist 的耗时、证据数、主张数、估算 Token 与可用 Provider Token；
- succeeded、timeout、Schema/Provider/Scope 错误分类；
- 论文覆盖、去重数、冲突数；
- 子任务、合并和最终引用/语义核验 P50/P95；
- 部分完成、回退次数与安全枚举原因。

这些指标只保存低基数计数与耗时，不保存用户问题、论文 ID、Chunk ID、主张正文或隐藏推理。

## 自动化与前端证据

- 后端本轮全量回归：510 项通过、8 项按可选外部环境跳过。
- 冲突、恢复、评测与观测定向回归：44 项通过。
- 前端类型检查通过；完整前端套件共 120 项通过。
- Docker API、Worker、Web 重建成功；真实服务健康。
- 浏览器登录态验收能看到 Harness 聚合与真实 v2/v3 样本；管理页和问答页控制台错误、警告均为 0。

## 已知边界

- 48 例数据集仍是 draft，完整语料与人工标注尚未全部冻结。
- 当前仅 4 条用例具备完整三版本真实结果，不能外推到 48 例或其他模型。
- 人工 factuality/usefulness/conflict handling 尚未评分。
- Provider 没有为所有模型调用返回实际计费 Token，因此只报告估算 Token，不计算货币成本。
- v3 的 Schema 失败率和分支超时仍需通过更紧的输出 Schema、证据裁剪和角色级超时继续优化。
- Specialist 暂不开放完整工具循环；只有 A/B 证明静态证据召回不足，才考虑最多 1～2 次、限本分支论文范围的只读检索。
