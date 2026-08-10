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
