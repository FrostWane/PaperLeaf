---
name: trace_original
version: 1
description: 定位追问对应的论文原文、物理页和上下文
allowed_tools:
  - search_current_paper
  - get_page_text
max_tool_steps: 3
requires_evidence: true
web_policy: disabled
approval_policy: none
---
优先使用当前页、选中文字和当前论文定位原文。回答说明原文怎样表述、为何与当前问题相关，并返回可跳转物理页；不得用摘要代替原文。
