# PaperLeaf 正式生产同源 RAG 评测

状态：`hidden_first_formal_batch`。

所有方案使用同一问题、论文范围、Chunk 快照与 K=5。旧 MiniLM 句窗重排未启用。

## 检索结果

| 方案 | 页级 micro Recall@5 | 页级 macro Recall@5 | MRR@5 | 完整证据组@5 | 跨论文 required-paper coverage@5 | warm p95 |
|---|---:|---:|---:|---:|---:|---:|
| production_baseline | 64.2% | 75.7% | 58.3% | 63.3% | 100.0% | 6406 ms |
| plain_embedding_control | 56.3% | 69.6% | 56.9% | 57.8% | 62.2% | 476 ms |
| contextual_embedding | 59.1% | 71.3% | 59.5% | 58.9% | 53.3% | 594 ms |
| per_paper_retrieval | 58.5% | 70.9% | 53.5% | 62.2% | 100.0% | 1235 ms |
| weak_query_rewrite | 59.7% | 73.9% | 62.9% | 62.2% | 55.6% | 6434 ms |
| multigranular_page_reranker | 60.4% | 73.5% | 55.9% | 61.1% | 56.7% | 556 ms |
| final_combined | 61.6% | 75.7% | 50.1% | 64.4% | 100.0% | 6415 ms |

页级 micro Recall@5 的分母是所有可回答题最佳可接受证据组中的证据页总数；页级 macro Recall@5 的分母是可回答题数，每题等权。二者不得混写。

## 配对 Bootstrap

差值为候选减基线；区间跨 0 时不得声称稳定提升。

### 生产基线与最终组合的 micro 结论

页级 micro Recall@5 为 64.2% （102/159）→ 61.6% （98/159）。

### 上下文化 Embedding

- page_micro_recall：Δ 0.0279，95% CI [-0.0091, 0.0676]，n=90。
- page_macro_recall：Δ 0.0167，95% CI [-0.0222, 0.0556]，n=90。
- mrr：Δ 0.0235，95% CI [-0.0285, 0.0770]，n=100。
- complete_group_hit：Δ 0.0100，95% CI [-0.0200, 0.0400]，n=100。
- required_paper_coverage：Δ -0.0889，95% CI [-0.1667, -0.0222]，n=30。

### 逐论文专属检索

- page_micro_recall：Δ -0.0063，95% CI [-0.0705, 0.0608]，n=90。
- page_macro_recall：Δ -0.0037，95% CI [-0.0407, 0.0370]，n=90。
- mrr：Δ -0.0542，95% CI [-0.0980, -0.0130]，n=100。
- complete_group_hit：Δ 0.0300，95% CI [0.0000, 0.0700]，n=100。
- required_paper_coverage：Δ 0.4667，95% CI [0.3889, 0.5333]，n=30。

### 弱结果 Query Rewrite

- page_micro_recall：Δ 0.0063，95% CI [-0.0252, 0.0397]，n=90。
- page_macro_recall：Δ 0.0259，95% CI [-0.0111, 0.0704]，n=90。
- mrr：Δ 0.0298，95% CI [-0.0190, 0.0770]，n=100。
- complete_group_hit：Δ 0.0300，95% CI [0.0000, 0.0700]，n=100。
- required_paper_coverage：Δ 0.0222，95% CI [0.0000, 0.0556]，n=30。

### 页级多粒度重排

- page_micro_recall：Δ 0.0126，95% CI [-0.0200, 0.0490]，n=90。
- page_macro_recall：Δ 0.0222，95% CI [-0.0222, 0.0704]，n=90。
- mrr：Δ -0.0328，95% CI [-0.0883, 0.0208]，n=100。
- complete_group_hit：Δ 0.0200，95% CI [-0.0200, 0.0600]，n=100。
- required_paper_coverage：Δ 0.0333，95% CI [0.0000, 0.0778]，n=30。

### 最终组合方案

- page_micro_recall：Δ -0.0252，95% CI [-0.0854, 0.0387]，n=90。
- page_macro_recall：Δ 0.0000，95% CI [-0.0444, 0.0481]，n=90。
- mrr：Δ -0.0742，95% CI [-0.1377, -0.0105]，n=100。
- complete_group_hit：Δ 0.0100，95% CI [-0.0300, 0.0500]，n=100。
- required_paper_coverage：Δ 0.0000，95% CI [0.0000, 0.0000]，n=30。

## 边界

- 该报告只覆盖检索；端到端回答、多 Agent 与人工盲评另行落盘。
- 冷启动为新建检索器后的首题，不代表清空操作系统、PostgreSQL 和 Ollama 缓存。
- 隐藏集运行后不得依据错误继续调参；后续复跑必须标记为揭盲后诊断。
