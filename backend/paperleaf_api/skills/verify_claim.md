---
name: verify_claim
version: 1
description: 使用论文原文证据核实用户给出的具体主张
allowed_tools:
  - search_current_paper
  - search_library
  - get_page_text
  - get_crossref_metadata
max_tool_steps: 4
requires_evidence: true
web_policy: local_first
approval_policy: none
---
拆出待核实主张，查找直接支持、反驳或无法判断的原文。元数据只用于身份核对，不用于替代正文证据；输出明确区分支持、部分支持和证据不足。
