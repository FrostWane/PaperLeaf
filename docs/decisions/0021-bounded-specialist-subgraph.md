# ADR 0021：跨论文综合采用有界 Specialist 子图

状态：已接受（默认关闭、真实环境灰度）  
日期：2026-08-13

## 背景

`compare_map_reduce_v2` 能把检索按论文分组并行执行，但分支只负责召回证据，不是具有独立上下文和结构化产出的模型 Specialist。把模型调用直接塞进原有并发函数，会继续缺少分支级 Checkpoint、稳定 reducer 和可审计预算；同时运行 Function Tool 规划器还会形成双重规划。

## 决策

新增版本化的 `specialist_subgraph_v3`，仅用于 3～10 篇论文的复杂比较或综合：

```text
冻结论文范围与编排版本
→ 唯一 Coordinator 确定性拆分任务
→ LangGraph Send 并行 1～3 个 Specialist
→ 服务端校验 E 别名、论文、Chunk 与物理页
→ 可交换、结合、幂等的 reducer
→ 确定性证据合并
→ 独立综合上下文
→ 既有引用校验与分批语义支持核验
```

每个 Specialist 只看到自己的论文子集、任务维度和服务端检索出的 `E1…En` 证据，不接收会话历史、长期记忆、兄弟分支结果或工具权限。模型生成的主张只用于组织候选证据，最终回答仍只信任由服务端 Retriever 读取、并在合并时再次通过范围和物理页校验的 Evidence。

## 状态、恢复与回退

- 全部工作共用一个父 `AgentRun`，不创建子 Run。
- Research Graph 使用 `specialist_subgraph_v3/research` Checkpoint 命名空间；最终回答使用 `specialist_subgraph_v3/final`，标准回退使用 `single_agent_v1/fallback`。
- reducer 以稳定 `subtask_id` 合并分支包，不依赖完成顺序；同代重放选择确定性结果。
- 单分支失败或超时可保留其他结果；全部失败、计划非法、预算不足或综合失败时回退标准检索，不再启动第二个 Function Tool 规划器。
- Worker 取消和租约令牌继续作为写入围栏。已完成分支可通过 PostgreSQL Checkpoint 恢复；不承诺外部模型调用 exactly-once。

## 预算与权限

- 最多 3 个分支；每支有独立输入、输出和 wall-clock 上限。
- Coordinator 不读取正文，Specialist 无 MCP、导入、删除、Shell、文件系统或 Memory 写入能力。
- 综合模型只接收用户目标、论文标题、结构化候选主张、重新校验后的证据和覆盖缺口，不继承普通聊天上下文。
- 最终 Context Envelope 仍受父 Run 硬上限控制；支持核验按主张分批，每批只携带其实际引用证据。

## 产品口径

该实现可准确称为“有界多 Specialist 协作”或“有界多 Agent 子图”，不称自治 Agent 团队或 Agent Swarm。`PAPERLEAF_SPECIALIST_AGENTS_ENABLED` 默认关闭；未完成同问题、同范围、同模型的冻结集 A/B 和人工盲评前，不声明质量优于 v1/v2。

## 结果

优势是分支上下文真正隔离、跨论文覆盖可观测、Checkpoint 与合并语义明确，并复用现有权限和引用门禁。代价是模型调用数和尾延迟上升；Provider 抖动会显著影响首次回答耗时，因此上线必须同时观察完成率、论文覆盖、主张支持、p95、估算 Token 和回退率。
