---
name: build_research_map
version: 1
description: 构建问题到方法、实验、结果和局限的研究逻辑图
allowed_tools:
  - search_current_paper
  - build_structure_graph
max_tool_steps: 2
requires_evidence: true
web_policy: disabled
approval_policy: none
---
从论文证据提取研究问题、背景、方法、数据、实验、结果与局限节点。节点必须带合法物理页引用，边只能连接已有节点并形成可解释的无环研究逻辑。
