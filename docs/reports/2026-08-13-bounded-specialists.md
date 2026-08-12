# 有界 Specialist 多 Agent 开发与真实验证报告

日期：2026-08-13  
结论：功能链路已实现并完成三篇真实论文灰度；结构性结果可用，但质量评测仍为 `quality_pending`。

## 现象与根因

Phase 1 只有并行检索，分支没有独立模型上下文。首次接入 Specialist 后，真实运行又暴露两个问题：

1. 回答与整份语义支持核验都可能受单次 Provider 超时影响，导致已有合法引用却整稿失败。
2. 合并证据虽然来自三篇论文，最终综合仍可能偏向其中两篇，缺少显式的论文覆盖输入。

根因分别是支持核验一次提交全部主张，以及最终 Answerer 只看到扁平证据，没有论文维度的 Specialist 结构化发现。

## 本轮实现

- 新增 LangGraph `Send` Specialist 子图：唯一 Coordinator、最多三个隔离分支、自定义 reducer、确定性合并和 PostgreSQL Checkpoint 命名空间。
- Specialist 只读取自己的论文与证据别名，无会话历史、Memory、联网和写工具。
- v3 尝试后禁止 Function Tool Harness 再规划；失败直接进入标准检索。
- 综合上下文显式携带论文标题和逐论文发现，并限制总证据和主张长度。
- 支持核验按四条主张分批、最多两批并发；单批失败时保留其他已核验主张，全部不可用时才标记临时未核验。
- 聊天区只显示拆分、分组分析、合并和回退摘要，不显示提示词、论文 ID、Chunk ID 或隐藏推理。
- 删除重索引、删除任务和 MCP 状态中的实现术语，只保留用户动作、影响和必要警告。

关键实现位于：

- `backend/paperleaf_api/agent/research_specialist_graph.py`
- `backend/paperleaf_api/agent/research_specialists.py`
- `backend/paperleaf_api/agent_execution.py`
- `backend/paperleaf_api/rag/evidence_support_batching.py`
- `components/chat-workspace.tsx`

## 自动化证据

- Specialist Schema、上下文隔离、别名校验、并发、超时、部分失败、全失败回退、reducer 重放和恢复测试通过。
- 持久 Agent 集成测试验证：只有一个父 Run、三个 Specialist、Function Tool planner 调用为零、综合上下文不含历史或 Memory、事件只含白名单字段。
- 分批支持核验覆盖全局索引映射、引用隔离、部分批失败与全部失败。
- 前端覆盖 v2/v3 恢复、并行活动互不覆盖、部分失败/回退和普通问答不显示并行区。

最终全量结果：后端 511 项中 503 passed / 8 skipped，Ruff 全绿；前端 21 个文件、120/120，TypeScript 和生产构建通过，ESLint 0 error（两条 TanStack Table 已知兼容提示）。

浏览器真实回归额外发现并修复：跨文献工作台会从 localStorage 恢复到“同工作区但不同集合”的旧会话，造成切换集合后历史停在末页且聊天为空。恢复条件现要求会话类型和集合 ID 同时匹配，找不到时选择当前集合最近会话；实测 Specialist 集合回到第 1 页并打开最新保留 Run。

最终全量命令与准确计数见 `docs/testing.md`，不得用 Mock 结果替代下面的真实模型记录。

## 真实模型与基础设施记录

环境：Docker Compose、PostgreSQL/pgvector、Redis、MinIO、DeepSeek 聊天模型、Ollama Embedding。集合 `[系统验收] Specialist 多Agent` 保留了 ResNet、ViT 和 CLIP 三篇真实开放论文。

| Run | 终态 | 耗时 | 结果与发现 |
|---|---|---:|---|
| `56e6514d-e475-4a20-b205-dd45c655f38b` | failed | 190925 ms | 三个 Specialist 成功，最终回答连续超时，暴露回答容错缺口 |
| `e605b921-8eeb-4a6b-aa10-affe481c5c14` | completed | 178970 ms | 结构化综合完成，但引用只覆盖两篇论文 |
| `6721d05d-75b7-4ffa-b77b-1794bccc7dd5` | completed | 202946 ms | 三篇均被引用，支持结果为 supported/partial |
| `682ab92d-a602-4e95-b7e6-c438e1783cc8` | completed | 150880 ms | 三篇均被引用，共 7 个引用，支持结果为 supported/partial |

最后一次相对前一次同问题迭代少 52066 ms，下降 25.66%。这不是受控 A/B：Provider 延迟、缓存和当时服务状态均可能影响结果，因此只能说明本轮紧凑综合在该次运行中降低了耗时，不能外推为稳定性能提升。

## 已知局限

- 48 例跨论文草案需要 20 篇冻结论文；当前管理员库只满足一部分范围，不能合法执行完整 v1/v2/v3 A/B。
- 数据集仍为 draft，尚无人工盲评；不得宣称 v3 语义质量优于旧链路。
- 首次回答仍可能遇到 Provider 60 秒超时并进入同证据紧凑重试，最新真实 Run 总耗时仍约 151 秒。
- PostgreSQL Checkpoint 已用于真实运行，恢复逻辑有自动化测试；本轮没有在真实模型生成中强制杀死 Worker 来证明分支级跨进程恢复。
- LangGraph 与作业系统提供 at-least-once 恢复和写入围栏，不保证外部模型调用 exactly-once。

## 当前判断

权限、范围、引用和单规划者等确定性门禁已经具备；三论文真实链路能够产生覆盖三篇、带物理页引用的回答。由于完整冻结集和人工盲评尚未完成，当前仅适合在本地灰度使用，默认配置继续关闭 Specialist 功能。
