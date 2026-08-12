# ADR 0020：复杂跨论文任务采用有界研究编排

状态：已接受（Phase 1 灰度实现）  
日期：2026-08-12

## 背景

PaperLeaf 当前生产主链是单 Agent、确定性 LangGraph 与有界 Function Tool Harness。并行工具调用不等于多个 Agent：它们没有独立任务、隔离上下文、分支预算和结构化合并协议。

跨三至十篇论文进行比较时，单次全局召回容易被少数论文占满，也难以显式记录某篇论文没有命中、某个分支超时或不同论文存在冲突。与此同时，把上传、单篇问答或写操作改成自由 Agent Team 会显著增加权限、恢复、成本和状态复杂度。

## 决策

只为复杂跨论文任务增加版本化、有界的研究编排：

```text
compare_papers + 3—10 篇冻结论文 + 功能开关
→ Coordinator 生成强类型 ResearchPlan
→ 最多 3 个只读 Evidence Scout 并行检索
→ Deterministic Merger 重新校验 Chunk 与论文范围
→ 现有 Answerer
→ 现有引用合法性与逐条语义支持门禁
```

Phase 1 是“并行 Map-Reduce RAG”，不是已经验证优于单 Agent 的 Agent Team。只有后续 Specialist 子图通过同问题、同范围的真实模型 A/B，项目才会使用“多 Agent 协作”这一表述。

### 唯一规划者

进入 `research_v2` 后不再运行 Function Tool 的二次规划。Coordinator 只生成 `ResearchPlan`，不陈述论文事实，不执行写操作。Phase 1 的 Coordinator 使用确定性规划，以便复现和回滚；后续若引入模型规划，仍必须输出相同 Schema 并通过服务端校验。

### 权限与状态

- API 在提交时冻结 `paper_ids`；分支只能获得其子集。
- 子任务作为父 Run 内部状态存在，不创建子 AgentRun，避免破坏“同一会话仅一个活动 Run”的唯一约束。
- 子任务只能调用 `search_library`，没有导入、删除、Memory 或 MCP 权限。
- 子任务不共享可变 ProviderPolicy，不访问外部 Provider。
- 父 Run 保存 `orchestration_version`；`v1` 与 `research_v2` 可并存和回滚。
- 分支事件只公开角色、状态、论文数量、证据数量、耗时和错误分类，不公开问题正文、证据正文或隐藏推理。

### 预算

- 最大并行分支数为 3。
- 每个分支最多 2 个只读检索步骤，并有独立 wall-clock 上限。
- 父编排有总超时，且必须短于 Worker 租约。
- Merger 设置每篇、每页和总证据上限。
- 任一分支失败时保留成功结果；全部失败或合并后无证据时回退 v1。

### 确定性合并

- 按 `subtask_id` 收集，不使用最后写入覆盖。
- 只接受本次 Scout 实际返回、且属于任务论文范围的 Chunk。
- 按 Chunk ID 和 `(paper_id, physical_page)` 去重。
- 证据按论文轮询，避免单篇论文占满上下文。
- `support`、`contradict` 和 `unclear` 分开记录；Phase 1 不在没有模型判据时臆测冲突。
- 失败分支、缺失论文与覆盖范围写入 `MergeReport` 和聚合轨迹。

## 适用与不适用范围

启用条件：`compare_papers`、冻结范围含 3—10 篇论文、`PAPERLEAF_MULTI_AGENT_ENABLED=true`。

以下任务继续走 v1：单篇问答、总结、原文定位、PDF 处理、翻译、论文发现、单纯多源搜索、写操作与权限校验。

## 回退

- 动态开关关闭后，新 Run 走 `v1`。
- Run 创建时冻结版本，部署期间不会中途切图。
- 计划非法、预算超限、全部分支失败或合并无证据时记录原因并走 v1。
- 旧代码可忽略新增字段；数据库迁移只增加可空/有默认值列。

## 上线门禁

在冻结集和真实模型 A/B 上同时观察：主张支持、引用覆盖、跨论文覆盖、来源多样性、冲突呈现、错误作答率、p50/p95、模型调用数、Token 与回退率。

安全项必须为零：非法工具、越权论文、跨用户证据、未经审批写入与成功的 Prompt Injection。质量没有提升至少 5 个百分点，或 p95/Token 超过 v1 约两倍，则停留在 Phase 1 或关闭 `research_v2`，不得声称多 Agent 更优。

