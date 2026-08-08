---
name: compare_papers
version: 1
description: 在集合或全库范围内比较多篇论文的方法与结论
allowed_tools:
  - search_library
  - get_page_text
max_tool_steps: 4
requires_evidence: true
web_policy: disabled
approval_policy: none
---
按论文分别检索可比证据，再围绕相同维度比较研究问题、方法、实验、结果和局限。不得因为某篇论文没有命中就推断其结论。
