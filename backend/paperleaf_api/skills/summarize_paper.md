---
name: summarize_paper
version: 1
description: 生成带物理页证据的结构化论文总结
allowed_tools:
  - search_current_paper
  - summarize_paper
max_tool_steps: 2
requires_evidence: true
web_policy: disabled
approval_policy: none
---
按研究问题、核心方法、实验设置、主要结果和局限组织总结。每个事实必须关联本轮合法证据，不输出无法从论文支持的套话。
