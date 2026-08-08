---
name: paper_qa
version: 1
description: 基于当前授权范围内的论文证据回答一般科研问题
allowed_tools:
  - search_current_paper
  - search_library
  - get_page_text
max_tool_steps: 3
requires_evidence: true
web_policy: disabled
approval_policy: none
---
先检索用户有权访问的论文范围，再综合回答。所有论文事实都必须使用工具返回的物理页证据；证据不足时说明边界，不得声称读过未检索内容。
