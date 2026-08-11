# Agent TaskFrame 与 Run 级 ProviderPolicy 返工报告

日期：2026-08-11

## 结论

本轮没有继续扩充“换一批、再推荐、有没有某年”短语表，而是把多轮任务连续性改为
`TaskFrame`：模型通过原生 Function Call 判断当前消息是继续、更新、替换、结束还是与
上一任务无关；Harness 只接受强类型槽位并执行确定性合并、权限校验和审计。

Provider 预算同时从 Function Tool 循环的局部变量提升为整个 Agent Run 共享的
`ProviderRunPolicy`。文献库、arXiv、OpenAlex 和 Semantic Scholar 的已尝试次数由
Function Harness 与 LangGraph 共用；指定来源失败或空结果后，旧 Graph 不能再访问已禁止
或已达到预算的数据源。

## 现象、根因与修复

### 1. 指定来源失败后旧 Graph 可能越权兜底

- 现象：Function Tool 未产生可用结果时 `tool_mode_active=false`，旧 Graph 可能再次调用
  arXiv，违反“只用 Semantic Scholar”；显式 arXiv 也可能在同一 Run 被访问两次。
- 根因：来源计数是 `FunctionToolHarness.run()` 的局部字典，LangGraph 不知道已尝试来源、
  禁用来源和剩余预算。
- 修复：新增可序列化 `ProviderRunPolicy`，保存 `requested/denied/attempted/max_attempts/blocked`。
  Function Harness 在真实访问前占用预算；Schema 在联网前失败才退还预算。Graph 的本地库和
  arXiv 节点使用同一份状态，条件边也在路由前检查预算。
- 真实证据：Run `de9af7f3-8177-4853-bdf9-1d0afa488e06` 中 Semantic Scholar 返回
  `SEMANTIC_SCHOLAR_RATE_LIMITED`；最终策略为 `requested=[semantic_scholar]`、
  `denied=[arxiv,openalex]`，实际没有访问 arXiv 或 OpenAlex。严格推荐质量门禁仍将空结果判为
  失败，没有把受控降级冒充推荐成功。

### 2. 纯来源/纯数量追问依赖短语白名单

- 现象：“改用 Semantic Scholar”“改成三篇”可能不命中续问正则，从而丢失年份、数量、
  排除已入库和上一批实体。
- 根因：Context Engine 先判断固定续问表达，再决定是否恢复 `active_task`；它没有对当前话语
  与既有任务的语义关系建模。
- 修复：新增 `TaskFrameDecision`，字段包括 `operation/task_name/updated_fields/values/confidence`。
  模型只提交本轮明确变化的槽位，Harness 保留未修改槽位和 `shown_entities`，拒绝未知任务、
  未知字段、非法来源、越界数量和年份。
- Provider 兼容修复：真实 DeepSeek 思考模式不支持强制 `tool_choice`，会返回 HTTP 400。
  现在使用原生非强制 Function Calling，并以系统约束要求只调用 `resolve_task_frame`；自由文本
  不会被解析成状态。模型无合法 Tool Call、超时或置信度低于 0.55 时才使用窄化确定性降级。
- 真实证据：
  - Run `611bdb51-e57e-4efe-a0a3-83175834fd02`：年份追问由
    `model_function_call` 解析，置信度 0.95，保留 5 篇与排除库内约束；
  - Run `de9af7f3-8177-4853-bdf9-1d0afa488e06`：仅说“改用 Semantic Scholar”，
    置信度 0.97，保留 5 篇、2026 年、排除库内和已展示候选；
  - Run `0cfec8ee-ea55-4861-a83f-d2d013085633`：仅说“改成三篇”，置信度 1.0，
    保留 OpenAlex、2026 年和历史候选，最终展示 3 篇。

### 3. 从 Markdown 反查实际展示候选

- 现象：短标题、Markdown 转义字符和超长标题可能导致 `shown_entities` 漏记或误记。
- 根因：完成回答后对 Markdown 做规范化标题子串搜索，结构化候选在 Graph 输出边界丢失。
- 修复：Graph 直接返回 `displayed_recommendations` 与
  `displayed_recommendation_entities`；会话状态只消费这两个结构化字段。Markdown 仅用于展示，
  不再是业务真相源。arXiv 的 `published` 也进入结构化预览，避免年份在上下文压缩时丢失。

### 4. Live 推荐门禁可能真空通过

- 现象：旧门禁只检查标题是否重复和问题中显式年份；空表格、数量不足、返回库内论文或主题
  无关候选可能通过。
- 修复：Live Run 读取 Graph 的结构化候选和最终 `active_task`，检查：
  - 表格非空且数量等于最终 TaskFrame；
  - 年份符合继承后的区间；
  - 不包含集合已有标题；
  - 结构化候选数与可见表格一致；
  - 至少 80% 候选具有标题/摘要重排产生的主题相关性信号；
  - 每个底层 Provider 最多访问一次，禁止来源访问数为零。

### 5. Precision@5 “人工”证据不足

- 现象：只填写任意非空 `annotator` 即可计算，无法阻止 `gpt-5` 等明显模型身份，也没有绑定
  查询、集合和候选输出。
- 修复：评测 manifest 现在冻结查询、完整集合快照和候选 JSONL 的 SHA-256；标注行必须绑定
  `dataset_id/query_id/candidate_id/rank/title`，标注者必须登记为 `type=human` 并签署固定人工
  声明。已知模型、Bot、LLM 和 Agent 名称会被拒绝，任一冻结文件变化也会使评测失败。
- 边界：声明与哈希能建立可审计证据链，但不能从技术上证明键盘后的自然人身份；正式报告仍需
  保留标注人数、日期、分歧和原始文件。本轮没有伪造 Precision@5 数值。

## 自动化与真实测试

- 后端全量：430 passed / 6 skipped，共收集 436 项；6 项是可选外部基础设施测试。
- Ruff：全部通过。
- `context-harness-v1`：100/100，证据级别
  `deterministic_no_external_model`，最终输入超限 0。
- 最终真实矩阵：5/6，证据级别 `real_model_real_infrastructure`：
  - 默认推荐与 2026 年追问：2/2；
  - OpenAlex 2026 五篇：1/1；
  - “改用 Semantic Scholar”：TaskFrame、来源权限和限次均通过，但 Provider 429，推荐质量因
    空结果失败；
  - OpenAlex 2026 五篇与“改成三篇”：2/2。
- 该 5/6 不外推为语义准确率；唯一失败没有用其他来源或模型猜测补齐，因此安全控制正确，
  但用户推荐目标没有完成。

## 关键文件

- `backend/paperleaf_api/agent/context.py`：TaskFrame 校验、合并与确定性降级。
- `backend/paperleaf_api/agent/function_tools.py`：DeepSeek 原生 TaskFrame Function Call 与共享预算。
- `backend/paperleaf_api/agent/provider_policy.py`：Run 级来源策略、预算和审计快照。
- `backend/paperleaf_api/agent/graph.py`：Graph 共同遵守预算并返回结构化展示实体。
- `backend/paperleaf_api/agent_execution.py`：TaskFrame 主路径、状态持久化与结构化候选消费。
- `backend/paperleaf_api/evaluation_harness_live.py`：严格连续推荐门禁。
- `backend/paperleaf_api/evaluation_recommendations.py`：冻结产物与人工声明验证。

## 仍未覆盖的边界

- Semantic Scholar 免费接口仍可能限流；系统会受控失败，不会违反来源约束，但不能保证每次凑满
  用户要求的数量。
- TaskFrame 目前覆盖 `find_related_papers`。后续如果把翻译、结构图或导入也做成长期多轮任务，
  应新增独立 Task Schema，而不是把所有任务塞入同一宽泛 JSON。
- 人工 Precision@5 尚未完成真实标注；当前仅完成不可伪造自动通过的证据链脚手架。
- 模型 Function Call 仍可能不返回或返回非法字段；这时窄化降级保证可用性，Context Snapshot 会
  明确记录 `model_function_call` 或 `deterministic_fallback`，不能把两者混报。
