# 联网论文推荐质量与连续换批报告（2026-08-10）

## 结论

本轮补齐了联网论文推荐的五个真实缺口：论文类型过滤、标题与摘要混合重排、连续换批排除、
多轮数量/年份/来源继承、按实际数据源限次，以及整个用户文献库范围的实体去重。

最终真实双轮使用 DeepSeek、Ollama Embedding、PostgreSQL、Redis 和 OpenAlex：

- 首轮返回 5 篇尚未入库候选；
- 只追问“有没有更近的论文，如 2026 年的？”后，仍继承 5 篇和排除已入库约束；
- 第二轮 5/5 均为 2026 年，与第一轮标题重复 0；
- 下一轮排除集只保存首轮页面实际展示的 5 篇，不再保存 Provider 内部未展示候选；
- 两轮均通过新增的“重复批次”和“年份约束丢失”真实门禁。

这只是 2/2 的真实回归，不外推为 99% 语义准确率。Precision@5 必须等待人工标注集完成后再报告。

## 实现与取舍

### 1. 类型过滤与相关性重排

Provider 会多取候选，再依次执行：

```text
公开元数据类型过滤
→ 整库实体去重
→ 标题 + 摘要词项分数
→ Ollama 语义相似度
→ 集合主题共识门禁
→ 稳定排序并截取用户请求数量
```

过滤项包括 Dataset、paratext、撤稿 Work、附件图片、补充材料、编辑内容及其他明确的
非论文类型。OpenAlex 使用 Work `type`、`is_paratext`、`is_retracted`；Semantic Scholar
使用 `publicationTypes`。类型缺失时保守保留，避免把旧 Provider 的正常论文误删。

语义分数不能单独放行候选：候选还需有可解释的实词锚点；三篇及以上的集合若存在主主题，
候选必须命中至少半数作用域文献共同支持的主题词。该取舍优先提高 Precision@5，可能牺牲
少量跨领域召回。Embedding 不可用时仍按确定性词项分数排序。

参考接口：

- [OpenAlex API Reference](https://developers.openalex.org/api-reference/openapi.json)
- [Semantic Scholar Academic Graph API](https://api.semanticscholar.org/api-docs/)

### 2. 真正连续换批

会话的 `entity_state.active_task.shown_entities` 保存已展示论文的全部稳定实体键：

```text
DOI
arXiv ID
OpenAlex / Semantic Scholar External ID
规范化标题
```

短标题与 Provider 长标题可做受控前缀等价，例如库内 `AttentionDTA` 能排除
`AttentionDTA: Drug-Target ...`。最重要的修复是：排除集由最终已发布回答反查，只有页面
实际展示的候选会进入下一轮；Provider 返回但最终未展示的条目不会污染换批状态。

### 3. 约束继承

论文发现任务使用结构化状态保存：

- `requested_count`；
- `year_from` / `year_to`；
- `requested_sources` / `denied_sources`；
- `exclude_library`；
- `shown_entities`。

因此“换一批”“有没有 2026 年的”“改用 Semantic Scholar”等短追问不依赖模型重新猜测
上一轮要求。数量支持 `5 篇`、`五篇`、`five papers` 等中英文形式；新的明确条件覆盖旧条件。
否定来源优先，例如“不要使用 OpenAlex”不会因字符串中出现 OpenAlex 而误触自动查询。

### 4. 按数据源限次

调用预算按底层来源计数，而不是工具名计数：

```text
search_arxiv ─┐
              ├─ arXiv：每个 Run 最多一次
find_related ─┘

search_openalex          → OpenAlex：每个 Run 最多一次
search_semantic_scholar  → Semantic Scholar：每个 Run 最多一次
```

失败、拒绝与空结果仍保留审计记录，但不会通过换一个工具别名重复访问同一 Provider。

### 5. 整库实体去重

推荐范围仍由当前集合决定，但排除范围改为当前用户整个文献库，而不是当前集合前几篇标题。
新增 `papers.academic_external_ids` 保存导入来源实体 ID；数据库迁移为
`20260810_0018`。既有论文默认 `{}`，仍可依靠 DOI、arXiv ID 和标题去重，不需要重新解析、
重新向量化或重新上传 PDF。

## 关键文件

| 文件 | 作用 |
|---|---|
| `backend/paperleaf_api/agent/recommendation_quality.py` | 类型过滤、实体键、整库去重、混合重排与共识门禁 |
| `backend/paperleaf_api/agent/function_tools.py` | 结构化发现任务、Provider 级限次、候选过滤和展示实体审计 |
| `backend/paperleaf_api/agent_execution.py` | 整库排除集、换批状态、仅保存实际展示候选 |
| `backend/paperleaf_api/agent/discovery_policy.py` | 中英文数量与来源肯定/排他/否定解析 |
| `backend/paperleaf_api/agent/graph.py` | 稳定书目表格、数量/年份约束和跨领域推荐理由 |
| `academic_mcp/academic_search_mcp/server.py` | OpenAlex / Semantic Scholar 多取、年份和类型元数据 |
| `backend/paperleaf_api/evaluation_recommendations.py` | 人工 Precision@5 计算与标注完整性校验 |
| `backend/paperleaf_api/evaluation_harness_live.py` | 真实工具尝试、连续批次重复和年份约束门禁 |
| `backend/alembic/versions/20260810_0018_paper_external_ids.py` | 外部学术实体 ID 迁移 |

## 测试证据

### 自动化

- 后端：427 项收集，421 通过、6 项可选基础设施测试跳过；
- 学术 MCP：5/5 通过；
- Ruff：全部通过；
- 前端 Vitest：113/113 通过；
- TypeScript：通过；
- ESLint：0 error，保留 2 个既有 TanStack Table/React Compiler warning；
- deterministic Harness：100 例、failure 0；指代、澄清、Skill、Memory、Tool、权限和审批
  各自冻结指标均为 100%，最终输入超限 0；证据级别是 `deterministic_no_external_model`；
- Alembic：PostgreSQL 当前为 `20260810_0018 (head)`。

### 真实模型与基础设施

集合：`[系统验收] Harness 真实闭环`。

| 顺序 | Run ID | 问题 | 结果 |
|---|---|---|---|
| 1 | `e04edc39-b275-47fd-b765-c85ccaa0e041` | 根据当前集合主题推荐 5 篇尚未入库论文 | OpenAlex 成功，5 篇 |
| 2 | `357967f9-3dd3-452c-9717-77f60de19440` | 有没有更近的论文，如 2026 年的？ | OpenAlex 成功，5/5 为 2026 年 |

两轮属于同一会话 `5d34cc11-9f0d-41af-beed-2689db719563`。真实评测器报告 2/2 结构性通过，
失败列表均为空；第二轮与第一轮标题重复 0。第一轮 OpenAlex Tool 耗时 8969 ms，第二轮
8153 ms，这是当前一次真实网络样本，不代表长期延迟承诺。

数据库复核显示，第二轮 Context Snapshot 的 `shown_entities` 恰好包含第一轮实际展示的
5 个标题实体；数量、2026 年范围和排除已入库约束同时存在。

开发中真实发现并修复过三类问题：Ollama 对跨领域文本产生假高相似、短标题未排除长标题变体、
Provider 内部候选被误写入下一批排除集。这些失败没有被删除或冒充最终成功。

## Precision@5 标注协议

`backend/evaluation/recommendation-precision-v1/annotations.template.jsonl` 是人工标注模板。
每一项必须由人填写 `relevant=true/false`，并记录查询、作用域快照、候选实体和备注。
评测器发现空标注、机器伪标签或每组不足 5 篇时会拒绝出分。

当前没有可诚实报告的人工 Precision@5 数字。建议至少冻结 20 个不同领域/集合查询、每个查询
标注前 5 篇，并同时保存逐查询 P@5、宏平均、领域分布和分歧样本，再决定是否调整阈值。

## 已知边界

- 异质集合可能存在多个真实主题；当前策略会优先集合共识，同时允许与稳定子主题相关的候选。
- 保守相关性门禁可能漏掉标题和摘要完全不共享词项、但概念上真正相关的论文。
- Semantic Scholar 公共 API 仍可能限流；限流会被记录并受控降级，不能当成推荐为空。
- 外部元数据不是 PDF 原文证据；导入并完成页级索引前不能用于论文事实回答。
- 本轮只重新运行 2 个真实连续推荐 Run；完整 100 次 live 门禁的目标仍是至少 99/100，
  本报告不使用 2/2 冒充该目标已达成。
