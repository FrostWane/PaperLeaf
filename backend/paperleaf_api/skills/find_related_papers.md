---
name: find_related_papers
version: 3
description: 在本地证据不足或用户明确要求时查找相关公开论文
allowed_tools:
  - search_library
  - search_arxiv
  - find_related_papers
  - mcp__academic__search_openalex
  - mcp__academic__search_semantic_scholar
  - mcp__academic__get_academic_metadata
  - request_import
max_tool_steps: 4
requires_evidence: false
web_policy: explicit_only
approval_policy: write_actions
---
先用已鉴权的完整文献范围形成检索主题。用户已允许联网且没有指定数据源时，由 Harness 至少执行一次可用的学术元数据检索，再由模型按需补充本地文献库、arXiv、OpenAlex 或 Semantic Scholar；用户明确指定或排除数据源时必须服从，并在后续追问中继承。相同检索工具默认只调用一次，外部结果按完整集合的标题和 DOI 去重。

最终回答优先直接整理外部候选，按用户要求的数量输出题目、年份、出版物、DOI/链接、来源和简短推荐理由。文献库证据只用于判断主题与排除已有论文，不得用本地参考文献替代已经返回的外部候选，也不得为外部元数据附加 Chunk 引用。若模型漏项而工具已返回足够候选，Harness 会用经过清洗的元数据补齐确定性清单。外部元数据不能充当论文全文引用，导入必须等待用户确认。
