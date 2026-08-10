# 多轮联网论文发现上下文修复报告

## 现象

用户首轮要求“联网推荐 5 篇尚未入库的相关论文”时，PaperLeaf 能够使用
OpenAlex 返回结果。但续问“有没有更近的论文，如 2026 年的”时，系统选择了
`compare_papers`，并连续四次调用本地 `search_library`。回答最终从 PDF 参考文献中
拼出 2024–2025 年的条目，没有调用 OpenAlex，也没有保留“五篇、排除已入库”的
上一轮约束。

故障 Run：`0a39f64e-a4db-4ae5-a904-81c2d1345a9d`。

## 根因

历史消息已经进入了最终回答模型，但没有以结构化任务状态进入 Skill 路由和 Tool 规划。
路由器仍只看当前短句“有没有更近的论文”，不知道它是上一轮论文发现任务的
年份收窄。同时 OpenAlex Tool 只有 `query` 和 `limit`，即使将“2026”写进查询词，
也缺少服务端年份过滤契约。

## 修复

1. Context Engine 新增可审计的 `active_task`，仅保存任务名、联网要求、推荐数量、
   排除已入库和年份范围，不保存隐藏推理。
2. “更近、最新、换一批、再推荐、2026 年”等短追问可继承最近的明确论文发现任务。
   即使上一次追问由旧版错误处理，也可从更早的明确任务中恢复。
3. 明确切换到解释、总结、方法、实验、脑图或比较时，立即停止任务继承。
4. 续问任务使用 `context_task_inheritance` 强约束选择 `find_related_papers`，模型不得将其
   覆盖为本地比较任务。
5. OpenAlex Tool 增加 `year_from` / `year_to`，MCP 服务将其转换为 OpenAlex
   `from_publication_date` / `to_publication_date` 过滤。
6. MCP Schema 修订从 1 提升为 2，Redis 缓存键随修订变化，不会复用旧 Schema 的结果。
7. 精确年份查询数量不足时，只报告真实数量；结果为空时明确说明外部数据源未返回，
   不再用本地 PDF 参考文献或其他年份补齐。

## 关键文件

| 文件 | 作用 |
|---|---|
| `backend/paperleaf_api/agent/context.py` | 多轮发现任务识别、约束继承和任务切换 |
| `backend/paperleaf_api/agent_execution.py` | Skill 强约束路由和会话 `active_task` 持久化 |
| `backend/paperleaf_api/agent/function_tools.py` | OpenAlex 年份参数契约与自动调用 |
| `academic_mcp/academic_search_mcp/server.py` | OpenAlex 服务端年份过滤 |
| `backend/paperleaf_api/agent/graph.py` | 精确年份、数量不足和空结果的确定性输出 |
| `backend/paperleaf_api/smoke_discovery_followup.py` | 可重复的真实多轮 DeepSeek + OpenAlex 验收 |

## 测试证据

### 自动化回归

- 上下文、Tool、Graph 和持久 Agent 目标回归：67 项通过。
- Ruff：後端、学术 MCP 和测试代码全部通过。
- Python `compileall`：后端与学术 MCP 通过。

### 真实模型与真实基础设施

环境：Docker Compose、DeepSeek 聊天模型、OpenAlex API、PostgreSQL、Redis 和独立 MCP 服务。

- 成功 Run：`8d348fcf-5b44-4b33-9635-8ff54695d92b`
- 会话：`150cb147-fa3a-423a-bb6a-9fa70e9c4860`
- 状态：`completed`
- Skill：`find_related_papers`
- 路由来源：`context_task_inheritance`
- 持久化 Tool：`mcp__academic__search_openalex` / `succeeded`
- Tool 参数：`query=DeepDTA, limit=8, year_from=2026, year_to=2026`
- OpenAlex 耗时：2442 ms
- 总耗时：25498 ms
- 最终输入：5434 / 21307 Token
- 输出：5/5 篇均为 2026 年，带出版物和 DOI，来源均为 OpenAlex
- 本地 Chunk 引用：0
- 结构性验收：通过

## 局限

- OpenAlex 是书目元数据源，结果未导入并解析 PDF 前，不能作为原文事实引用。
- 年份继承只对明确的论文发现续问生效，无法判定时会停止继承，避免把普通问答错发到外部服务。
- 外部数据源的收录时间存在延迟；“未返回 2026 年论文”只代表当次查询，不代表学界不存在相关论文。

## 第二轮可靠性扩展

在真实多轮修复后，又补充了来源、数量、范围、工具失败和预算五类排列组合，解决了以下缺口：

1. 来源策略同时识别“使用 OpenAlex”“只用 Semantic Scholar”“不要使用 OpenAlex”以及
   `without OpenAlex` 等中英文肯定、排他和否定表达。否定约束会随论文发现任务进入下一轮，
   被排除的工具即使由模型提出也只记录拒绝，不会读取缓存或访问远端。
2. 推荐数量支持 `5 篇`、`五篇`、`five papers`，范围为 1～10；下一轮可以用新的数量覆盖旧值。
3. 集合去重改为加载服务端授权范围内的全部论文标题。只向模型预览少量标题，但最终候选去重使用
   完整集合，不再遗漏第 9 篇及之后的论文。
4. 推荐理由移除 DTA、affinity、ligand 等领域写死词，改为根据当前集合与候选标题的共同主题生成；
   外部结果不足时只返回实际候选，不让模型补造到目标数量。
5. 同一发现工具每个 Run 最多真正执行一次；Schema 参数错误仍允许修正一次。失败、拒绝和空结果
   会保留审计记录并回退旧检索链路，不能仅因“产生过 Tool Call”而跳过检索。
6. Tool Call/Result 作为原子对进入最终 Context Envelope。大 JSON 先压缩为合法结构，仍超限时按整对
   丢弃，不能裁出半个 JSON 或拆散调用与结果。
7. 正式 Graph 增加语义证据支持评分。普通 PDF 回答在引用 ID、论文和物理页合法后，还要通过结论
   与证据的支持检查；外部书目元数据走独立的清洗契约，不伪装成 PDF 页级证据。
8. OpenAlex DOI 候选可以进入人工导入审批。确认后重新查询 DOI 元数据，只允许 HTTPS、公网地址、
   逐次校验重定向、合法 PDF 类型和大小限制；没有可验证开放 PDF 时安全拒绝。
9. 聊天事件现在展示实际工具名称，例如“查询 OpenAlex”“查询 Semantic Scholar”“搜索 arXiv”，
   同时保留“检索、生成、核验”等总体阶段。

### 组合测试

- 确定性上下文矩阵：38 项，覆盖三种数量表达、三个年份、三种来源策略和两轮覆盖，全部通过。
- `context-harness-v1` 确定性评测：100/100，无失败，`final_input_exceeded=0`；该结果不使用外部模型。
- 后端全量：407 项通过、6 项按外部测试条件跳过；Ruff 通过。
- 前端全量：21 个文件、113 项通过；TypeScript、ESLint（0 错误）和生产构建通过。
- 真实模型矩阵第一轮：10/10 结构性通过，覆盖默认来源、仅 OpenAlex、仅 Semantic Scholar、
  排除 OpenAlex 和仅 arXiv 的连续追问。Semantic Scholar 当时被上游限流，系统返回受控降级，
  没有把本地片段伪装成外部推荐。
- 部署最终限次补丁后的真实复测：4/4 结构性通过；4 个 Run 均选择
  `find_related_papers`，每个工具在单个 Run 内最多出现一次。
- 最新真实 Run：`0e73ef95-2edf-4cb4-8f7f-aafa0b93a73c`、
  `114bbf2b-3255-4740-91fc-8d9e0902de53`、
  `3280167f-e9ad-4c08-b3d2-8b4638127bce`、
  `b3545367-1077-489b-98ed-5196ae8db493`。

这些真实结果只能证明当前 Docker、DeepSeek、Ollama、Redis、PostgreSQL 和 MCP 配置下的结构性
闭环，不代表外部学术服务的长期网络性能或推荐语义准确率。Semantic Scholar 的公共接口仍可能限流；
OpenAlex 和 arXiv 也可能因网络、收录延迟或查询主题返回少于目标数量。

### 新增关键文件

| 文件 | 作用 |
|---|---|
| `backend/paperleaf_api/agent/discovery_policy.py` | 数量解析与中英文来源肯定、排他、否定策略 |
| `backend/paperleaf_api/agent/context_budget.py` | Tool 原子对压缩和最终硬预算 |
| `backend/paperleaf_api/arxiv_import.py` | arXiv/DOI 开放 PDF 的统一安全导入 |
| `backend/paperleaf_api/evaluation_harness_live.py` | 连续多轮真实模型组合矩阵与上游降级分类 |
| `backend/tests/test_discovery_context_matrix.py` | 38 项确定性上下文排列组合 |
| `backend/tests/test_open_access_import.py` | DOI 开放 PDF 导入与安全边界 |
