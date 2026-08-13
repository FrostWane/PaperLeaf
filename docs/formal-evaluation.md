# PaperLeaf 正式评测流程

正式评测分为已揭盲诊断和论文隔离隐藏集两层。诊断集允许做错误分析；隐藏集只在
协议、数据和代码冻结后运行一个正式批次。

## 证据文件

每个方案必须生成：

- `run_manifest.json`：代码、镜像、数据、配置、Embedding 与 Chunk 快照；
- `per_query_results.jsonl`：逐题 Top-40、Top-5、通道分数、改写、Gold 页排名和耗时；
- `metrics.json`：带分子、分母的指标；
- 聚合目录中的 `REPORT.md`：结果、置信区间、负结果和边界。

正式隐藏集位于 `backend/evaluation/datasets/paperleaf-formal-hidden-v1`。私有 oracle
不得上传到公开仓库；其 SHA-256 已冻结在 `lock.json`。原始运行结果可直接提交，或
提交不可变对象地址和对应 SHA-256。

## 预注册方案

1. 当前生产基线；
2. 纯正文 Embedding 对照；
3. 上下文化 Embedding；
4. 逐论文专属检索；
5. 弱结果 Query Rewrite；
6. 确定性页级多粒度重排；
7. 最终组合方案。

旧 MiniLM 句窗重排保持关闭。页级多粒度重排使用整页文本构造完整句窗，并将句窗
分数与原 RRF 分数融合；它不下载或调用 MiniLM。

## 口径

- 页级 micro Recall@5 按每题命中比例最高的人工可接受证据组汇总；
- 完整证据组命中@5 要求某一完整人工证据组全部进入 Top-5；
- MRR@5 使用首个合法 Gold 页的倒数排名；
- 跨论文 required-paper coverage@5 只在跨论文可回答题上计算；
- 配对 Bootstrap 使用固定种子和 10000 次抽样；
- LLM Judge 只能称辅助评审，不能写成人工准确率。

人工盲评至少需要真实人员完成 30 道答案的事实正确性、完整性、引用有用性与总体
偏好。评分表未由真实人员填写前，整体结论保持 `human_review_pending`。
