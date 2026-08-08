---
name: find_related_papers
version: 1
description: 在本地证据不足或用户明确要求时查找相关公开论文
allowed_tools:
  - search_library
  - search_arxiv
  - find_related_papers
  - request_import
max_tool_steps: 4
requires_evidence: false
web_policy: explicit_only
approval_policy: write_actions
---
先查本地文献库；只有用户开启联网且明确请求，或本地证据不足时才能搜索公开学术元数据。外部元数据不能充当论文全文引用，导入必须等待用户确认。
