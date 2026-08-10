# OpenAlex 自动论文发现完善报告（2026-08-10）

## 1. 结果

本次更新解决了“用户没有点名 OpenAlex 时，相关论文发现只搜索本地文献或 arXiv”的问题。

最终行为如下：

- 用户已开启“联网学术搜索”；
- 当前 Skill 为 `find_related_papers`；
- 用户没有明确指定 OpenAlex、Semantic Scholar 或 arXiv；
- Harness 会确定性保留一次 OpenAlex 查询，再允许模型在最多四个 Tool Step 内决定是否补充其他来源；
- 外部候选足够时，回答严格按用户要求的数量输出题目、年份、出版物、DOI/链接、来源和推荐理由；
- 当前集合已有论文会按规范化全名和稳定模型名前缀去重；
- OpenAlex 等外部元数据不会伪装成已读取的 PDF 原文，也不会生成虚假的 Chunk 或物理页引用。

最终真实闭环：

- Session：`150cb147-fa3a-423a-bb6a-9fa70e9c4860`
- Run：`d0ce2efa-35df-48e8-923a-7c3854357de6`
- 状态：`completed`
- Skill：`find_related_papers@2`
- 原生 Function Calling：已执行
- Tool：OpenAlex 1 次，成功
- 最终输入：`2883 / 21307 Token`
- 总耗时：`25.274 秒`
- 输出：5/5 篇，5/5 条 DOI，5/5 条来源，库内重复 0，非法引用 0

该会话保留在管理员账号的跨文献问答历史中，标题以 `[实测][自动联网] 未指定数据源` 开头。

## 2. 原始现象与根因

用户指出的旧回答对应 Run `5a7658d0-14ab-40ba-ac32-96c5e7963354`。该 Run 只调用了 4 次
`search_library`，没有 OpenAlex Tool Call，因此回答末尾“并非 OpenAlex 返回结果”的说明是准确的，
但产品此前宣称“未指定来源时可以自动选择 OpenAlex”并不可靠。

修复过程中通过真实运行依次发现了五层问题：

1. **来源路由不确定**：模型可能反复选择本地检索，用尽四步工具预算。
2. **候选上下文被截断**：OpenAlex 返回 8～10 条，但旧 Tool Result 按字符截断，只把第一条留给回答模型。
3. **集合主题选错**：`DeepDTA`、`AttentionDTA`、`SyntheticDTA` 未拆分驼峰缩写，共同的 `DTA` 没被识别，检索词误选成集合第一篇 `AR-RAG`。
4. **模型输出不满足数量契约**：工具已有足量元数据，但紧凑回答受输出长度影响，只返回 3 篇或漏掉 DOI。
5. **外部元数据与 PDF 引用门禁冲突**：不应带 Chunk 引用的外部书目答案，被本地 PDF 引用门禁判成 `UNVERIFIED_ANSWER`；短标题 `DeepDTA` 与 OpenAlex 完整标题也未被视为同一篇。

## 3. 设计与实现

### 3.1 确定性来源策略

`find_related_papers` 在联网开启且用户未指定来源时，先加入一次 OpenAlex 调用。模型仍负责理解意图、
选择 Skill 和规划后续工具，但不能把同一个发现工具重复调用多次。显式指定来源时继续按用户指定来源执行。

### 3.2 主题查询生成

作用域标题先拆分驼峰模型名，再计算标题之间的共享术语。共同主题得分相同时，选择信息量更大的标题。
在本次真实集合中，检索词从错误的 `AR-RAG` 修正为：

```text
DeepDTA: deep drug-target binding affinity prediction
```

该过程不调用 LLM，结果确定且可复现。

### 3.3 Tool Result 分层存储

- PostgreSQL 中继续保存完整、用户隔离的 Tool 审计结果；
- 模型上下文只保留至多 10 条结构化书目信息；
- 标题、年份、出版物、DOI 和来源优先保留；
- 摘要与片段先限长，再按总字符预算结构化删除；
- 不再用原始字符串截断生成半截 JSON，也不会只留下排在第一位的候选。

OpenAlex、Semantic Scholar 返回 `available=false`、限流或缺少 Key 时现在记为失败 Tool Call，
不会再以“成功但 0 条结果”进入 Tool Mode。

### 3.4 数量、去重与发布门禁

当用户明确要求“推荐 N 篇”且外部候选足够时：

1. DeepSeek 完成 Skill 与 Function Tool 规划；
2. Harness 从清洗后的 Tool Result 中按 DOI/标题去重；
3. 使用当前集合的完整标题清单排除已有论文；
4. `DeepDTA` 与 `DeepDTA: ...` 这类至少 6 字符的稳定模型名前缀视为同一篇；
5. Harness 生成固定 Markdown 表格和简短、受控的推荐理由；
6. 只有这个严格分支能以 `external_metadata` 类型发布，普通论文问答仍必须通过 Chunk、论文所有权和物理页引用门禁。

这是有意采用的 Harness 设计：模型负责理解和工具决策，确定性外壳负责数量、来源、权限、去重和输出契约。

## 4. 关键文件

| 文件 | 修改 |
|---|---|
| `backend/paperleaf_api/agent/function_tools.py` | 自动 OpenAlex、单来源调用预算、标题聚类、结构化 Tool 上下文、外部失败状态、作用域标题去重元数据 |
| `backend/paperleaf_api/agent/graph.py` | 外部元数据提取、固定数量输出、短标题/完整标题去重、外部答案标记 |
| `backend/paperleaf_api/agent_execution.py` | 严格限定的 `external_metadata` 发布分支与观测结果 |
| `backend/paperleaf_api/agent/state.py` | 增加外部元数据答案状态 |
| `backend/paperleaf_api/skills/find_related_papers.md` | Skill 升级为 v2，明确来源、数量、去重和引用契约 |
| `backend/paperleaf_api/evaluation_harness_live.py` | 唯一会话标题、期望 Tool 校验和无来源 OpenAlex 场景 |
| `components/settings-view.tsx` | 偏好名称改为“允许联网学术搜索”，解释 arXiv/OpenAlex/Semantic Scholar 的边界 |

## 5. 测试证据

### 自动化

- 后端全量：324 passed，6 skipped；跳过项均为可选外部基础设施。
- Function Tool、Agent Graph、MCP Harness 目标回归：46 passed。
- Ruff：本次 Python 文件 0 error。
- Context Harness：100 个确定性样本全部通过；指代、澄清、Skill、Memory、Tool、授权和审批指标均为 1.0，最终输入超限为 0。
- 前端 Vitest：112/112；受影响设置组件：8/8。
- TypeScript：通过。
- ESLint：0 error，保留 2 个既有 TanStack Table 编译器兼容警告。
- 生产构建：通过；Docker 内 `vinext build` 约 60.8 秒。

证据级别必须区分：上述 Harness 100 例是 `deterministic_no_external_model`，不能冒充真实模型结果。

### 真实基础设施与真实模型

使用 Docker Compose、DeepSeek、PostgreSQL、Redis、学术 MCP 和当前管理员账号执行。关键演进：

| Run | 现象 | 结果 |
|---|---|---|
| `9dad51e0-ce09-4675-882e-a11917c8f4dc` | 未指定来源的旧基线 | 无 OpenAlex，约 191 秒后模型超时 |
| `33da27c1-0fe4-499d-a5ca-a7128486b214` | 首次自动 OpenAlex | OpenAlex 成功，但候选上下文被截断 |
| `3f72efd8-8d0d-4427-8fe5-e68f44306e61` | 修正 DTA 查询 | 外部候选相关，但模型只输出 3 篇 |
| `1078b7aa-cfca-4096-ac0a-dd5ed382b084` | 外部答案发布测试 | 发现 PDF 引用门禁冲突，按失败保留 |
| `d0ce2efa-35df-48e8-923a-7c3854357de6` | 最终闭环 | 25.274 秒完成，5 篇完整推荐，库内重复 0 |

最终 Run 的 OpenAlex Tool 耗时 12 ms，命中了 Redis 缓存，因此不能当作外部网络延迟基线；
同一轮完整 Agent 耗时 25.274 秒才是用户实际等待时间。此前真实未缓存 OpenAlex 调用约 2.1～6.2 秒。

### 构建耗时说明

本次 Docker 四镜像构建总耗时约 85.6 秒，其中 Web 的生产构建约 60.8 秒。构建日志显示主要时间在：

- Client 环境：约 21 秒，转换 4394 个模块；
- SSR 环境：约 11.8 秒，转换 2716 个模块；
- Server Reference：约 10.4 秒，转换 2702 个模块；
- Vinext/RSC 插件处理占 Server Reference 插件时间的大部分。

完整 Vitest 使用单 Worker 为 112/112，总耗时 274.48 秒，其中 jsdom environment 119.17 秒、setup 47.23 秒。
Windows 多 Worker 曾出现进程不退出，因此本次记录的稳定命令是：

```bash
pnpm exec vitest run --maxWorkers=1
```

## 6. 用户如何验证

1. 在“设置 → AI 与翻译”开启“允许联网学术搜索”。
2. 进入“跨文献提问”，选择一个集合或全部文献。
3. 输入：`请根据当前集合的研究主题，联网推荐 5 篇尚未在文献库中的相关论文，列出题目、年份、出版物、DOI，并说明推荐理由。`
4. 问题中不要写 OpenAlex。
5. 回答应包含 5 行表格、5 条来源和缺失元数据说明，不应出现 Chunk ID。
6. 管理员进入“管理 → Harness → Tool/MCP”，可看到 OpenAlex 调用、耗时和状态。

## 7. 已知边界

- OpenAlex 返回的是公开元数据与摘要，不代表 PaperLeaf 已阅读论文全文；全文问答前仍需确认导入并完成索引。
- 混合主题集合当前使用确定性标题聚类选择一个代表查询；若集合完全没有共享术语，相关性仍可能不如用户明确给出研究方向。
- Semantic Scholar 公共接口可能限流；失败会被正确记录，不阻塞已成功的 OpenAlex 结果。
- 最终成功 Run 命中 Redis 缓存，不能外推为所有网络条件下都能在 25 秒完成。
- 固定数量门禁当前最多输出 10 篇，避免单次答案和外部结果过大。
