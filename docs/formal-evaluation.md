# PaperLeaf 正式评测流程

正式评测分为已揭盲诊断和论文隔离隐藏集两层。诊断集允许做错误分析；隐藏集只在
协议、数据和代码冻结后运行一个正式批次。

## 证据文件

每个方案必须生成：

- `run_manifest.json`：代码、镜像、数据、配置、Embedding 与 Chunk 快照；
- `per_query_results.jsonl`：逐题 Top-40、Top-5、通道分数、改写、Gold 页排名和耗时；
- `metrics.json`：带分子、分母的指标；
- 聚合目录中的 `REPORT.md`：结果、置信区间、负结果和边界。

仓库内文本制品的公开 SHA-256 统一先把换行规范为 LF，再计算哈希；Windows 首次冻结
形成的历史锁允许等价 CRLF/LF 文本校验。这样 Git checkout 换行不会被误判为数据漂移，
任意非换行内容变化仍会失败。

正式隐藏集位于 `backend/evaluation/datasets/paperleaf-formal-hidden-v1`。首次正式运行
前，oracle 必须私有保存，仅把 SHA-256 冻结在 `lock.json`；正式批次结束后可以公开
oracle 供第三方复算，但该集合随即变为已揭盲数据，不能再次产生“首次隐藏集”结论。
原始运行结果可直接提交，或提交不可变对象地址和对应 SHA-256。

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

- 页级 micro Recall@5：每题先选 Top-5 命中比例最高的人工可接受证据组，再把所有题的
  命中 Gold 页数相加作为分子、所选 Gold 组页数相加作为分母；它是页级加权汇总；
- 页级 macro Recall@5：先计算每题所选 Gold 组的命中比例，再对题目等权平均；它与
  micro 的分母和估计目标不同，报告不得混用名称或区间；
- 完整证据组命中@5 要求某一完整人工证据组全部进入 Top-5；
- MRR@5 使用首个合法 Gold 页的倒数排名；
- 跨论文 required-paper coverage@5 只在跨论文可回答题上计算；
- 配对 Bootstrap 使用固定种子和 10000 次按题配对抽样；micro 区间在每个 Bootstrap
  样本中重新累加候选与基线各自的页级分子/分母后求比值差，macro 区间才对逐题比例差
  求平均；
- LLM Judge 只能称辅助评审，不能写成人工准确率。

多 Agent 对照中的 Token 是 PaperLeaf 确定性估算，不是 Provider 账单。估算成本使用
`backend/evaluation/pricing/` 下按日期冻结的官方价格快照；由于当前没有持久化
DeepSeek 的 cache hit/miss Token，且部分 planner/grader 调用没有 Token 遥测；报告只对
已观测 Token 按 cache miss 计价，不能写成总成本、上界或实际消费金额。

人工盲评至少需要真实人员完成 30 道答案的事实正确性、完整性、引用有用性与总体
偏好。评分表未由真实人员填写前，整体结论保持 `human_review_pending`。

端到端回答使用独立的 `answer-protocol.json` 预注册：100 题全部走
`single_agent_v1`，检索固定为 `final_combined`，最多并发 3 个真实后台 Run。
执行器从数据库重新核验每条引用的用户、论文范围、Chunk 与物理页，并生成
`per_query_answers.jsonl`、`metrics.json` 和 30 题未评分的
`human_blind_review.jsonl`。空白评分不能计作人工结果，任何一题缺失都会使整批失败。

Embedding 消融通过 `evaluation_corpus_prepare --force-reindex` 显式重建同一批 Chunk
的向量。执行 plain 对照时使用 revision 1 / `chunk_text_v1`，随后恢复 revision 2 /
`paper_context_v2`；两个空间不能混用，执行器会校验每篇论文的统一指纹。

Worker 强杀恢复必须让租约自然过期，不得手工把 Job 改回队列。公开证据只保留脱敏
event epoch、分支状态和次数，不导出 claim token；新 Worker 领取期间用旧 token 做
一次受控迟到写入探针，只有被 fencing 拒绝且数据库无探针事件时才算通过。
