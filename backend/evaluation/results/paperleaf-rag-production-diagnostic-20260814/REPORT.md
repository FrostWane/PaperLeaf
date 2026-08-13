# PaperLeaf 正式生产同源 RAG 评测

状态：`diagnostic_not_blind`。

所有方案使用同一问题、论文范围、Chunk 快照与 K=5。旧 MiniLM 句窗重排未启用。

## 检索结果

| 方案 | 页级 micro Recall@5 | MRR@5 | 完整证据组@5 | 跨论文 required-paper coverage@5 | warm p95 |
|---|---:|---:|---:|---:|---:|
| production_baseline | 60.0% | 46.6% | 63.7% | 100.0% | 821 ms |
| plain_embedding_control | 66.7% | 53.6% | 67.5% | 85.0% | 491 ms |
| contextual_embedding | 58.9% | 46.5% | 61.3% | 85.0% | 567 ms |
| per_paper_retrieval | 60.0% | 47.3% | 61.3% | 100.0% | 1011 ms |
| weak_query_rewrite | 61.1% | 47.7% | 63.7% | 85.0% | 1391 ms |
| multigranular_page_reranker | 66.7% | 57.1% | 68.8% | 90.0% | 635 ms |
| final_combined | 65.6% | 56.2% | 68.8% | 100.0% | 1580 ms |

## 配对 Bootstrap

差值为候选减基线；区间跨 0 时不得声称稳定提升。

### 上下文化 Embedding

- page_recall：Δ -0.0688，95% CI [-0.1562, 0.0187]，n=80。
- mrr：Δ -0.0633，95% CI [-0.1180, -0.0115]，n=90。
- complete_group_hit：Δ -0.0556，95% CI [-0.1333, 0.0222]，n=90。
- required_paper_coverage：Δ 0.0000，95% CI [-0.1500, 0.1500]，n=10。

### 逐论文专属检索

- page_recall：Δ 0.0063，95% CI [0.0000, 0.0187]，n=80。
- mrr：Δ 0.0070，95% CI [0.0000, 0.0167]，n=90。
- complete_group_hit：Δ 0.0000，95% CI [0.0000, 0.0000]，n=90。
- required_paper_coverage：Δ 0.1500，95% CI [0.0000, 0.3000]，n=10。

### 弱结果 Query Rewrite

- page_recall：Δ 0.0250，95% CI [-0.0250, 0.0750]，n=80。
- mrr：Δ 0.0104，95% CI [-0.0267, 0.0483]，n=90。
- complete_group_hit：Δ 0.0222，95% CI [-0.0222, 0.0667]，n=90。
- required_paper_coverage：Δ 0.0000，95% CI [0.0000, 0.0000]，n=10。

### 页级多粒度重排

- page_recall：Δ 0.0813，95% CI [0.0250, 0.1437]，n=80。
- mrr：Δ 0.0941，95% CI [0.0391, 0.1537]，n=90。
- complete_group_hit：Δ 0.0667，95% CI [0.0222, 0.1222]，n=90。
- required_paper_coverage：Δ 0.0500，95% CI [0.0000, 0.1500]，n=10。

### 最终组合方案

- page_recall：Δ 0.0563，95% CI [-0.0063, 0.1187]，n=80。
- mrr：Δ 0.0854，95% CI [0.0302, 0.1428]，n=90。
- complete_group_hit：Δ 0.0444，95% CI [0.0000, 0.1000]，n=90。
- required_paper_coverage：Δ 0.0000，95% CI [0.0000, 0.0000]，n=10。

## 边界

- 该报告只覆盖检索；端到端回答、多 Agent 与人工盲评另行落盘。
- 冷启动为新建检索器后的首题，不代表清空操作系统、PostgreSQL 和 Ollama 缓存。
- 隐藏集运行后不得依据错误继续调参；后续复跑必须标记为揭盲后诊断。
