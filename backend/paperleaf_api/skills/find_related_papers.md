---
name: find_related_papers
version: 1
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
先查本地文献库，再查 arXiv；只有用户开启联网且明确请求，或本地证据不足时，才通过 MCP 查询 OpenAlex 或 Semantic Scholar。外部元数据必须标明来源，不能充当论文全文引用，导入必须等待用户确认。
