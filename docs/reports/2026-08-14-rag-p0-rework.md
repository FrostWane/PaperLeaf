# PaperLeaf RAG P0 返工报告

日期：2026-08-14

## 结论

本轮停止叠加新的检索功能，集中修正评测口径、运行配置、不可回答门禁、跨论文取证、
引用合法性和 Specialist 稳定性。

当前能够确认的结果是：

- 正式隐藏集的页级 **micro Recall@5** 是 102/159（64.2%）→98/159（61.6%），
  差值 -2.5 个百分点；按题配对重采样并在每次样本内重算 micro 分子、分母得到的
  95% CI 为 `[-8.5, +3.9]`。这不是提升。
- 原报告中的 `[-4.4, +4.8]` 是逐题等权的 **macro Recall@5** 区间，不能再标成
  micro 区间。
- 运行配置现在随 Agent Run 冻结，Worker 使用冻结配置而不是部署后的可变全局值；
  通道、处理器、查询改写、向量降级和重排降级均进入 trace。
- 历史正式批次中的 16 个 `page:*` 伪 Chunk 引用属于已揭盲负结果，原始证据保留；
  当前代码只接受数据库中真实且属于当前用户和冻结 scope 的 Chunk。本轮 10 次真实
  不可回答诊断中，伪 Chunk 引用为 0。
- 独立开发集 40 题用于选择 answerability 阈值，没有读取正式隐藏题；在该开发集上
  可回答/不可回答各 20 题均分类正确。已经揭盲的 10 道不可回答题只作诊断，修复后
  严格口径仍有 3/10 产生回答，不能写成已经达到 0/10。
- `specialist_subgraph_v3` 继续默认关闭。冻结 30 Run 批次中 81 个分支有 0 个超时、
  5 个 Schema 失败；定向安全降级回归将对应 Schema 整分支失败降为 0，并将唯一
  复现的模型超时改成可观测的 retrieval-only 安全降级。

人工盲评尚未由真实评审者填写；新的零论文重叠隐藏集也没有提前建立或运行。二者是
后续正式发布门禁，不以模型自评代替人工，也不为了赶进度重复使用已揭盲数据。

## 1. 指标口径返工

页级 micro Recall@5 的计算步骤：

1. 每题在人工允许的证据组中选 Top-5 命中比例最高的一组；
2. 将全部题命中的 Gold 页数相加作为分子；
3. 将全部题所选 Gold 组页数相加作为分母；
4. 用总分子除以总分母。

页级 macro Recall@5 则先计算每题命中比例，再对题目等权平均。因此多证据题在 micro
中权重更高，两个指标的分母和估计目标不同。

micro 的配对 Bootstrap 以“题”为重采样单位，但每个 Bootstrap 样本内部都重新汇总
候选与基线的页级分子、分母后再求比值差。逐题比例差的均值只用于 macro。

正式结果：

| 指标 | 生产基线 | 最终组合 | 差值 | 配对 95% CI |
|---|---:|---:|---:|---:|
| 页级 micro Recall@5 | 102/159（64.2%） | 98/159（61.6%） | -2.5pp | [-8.5, +3.9] |
| 页级 macro Recall@5 | 逐题等权 | 逐题等权 | 0.0pp | [-4.4, +4.8] |

## 2. 每个 Run 的冻结检索配置

`scope_snapshot.retrieval_config` 保存：

- 候选池、逐论文检索和合并策略；
- Query Rewrite 开关、最大改写数；
- RRF、页级去重和重排配置；
- answerability 开关与阈值；
- Embedding provider、模型、维度、revision、输入格式和指纹；
- Git SHA、来源和是否与容器源码校验一致。

Worker 在执行 Run 时读取这份快照；旧 Run 缺少快照时才走显式兼容路径。真实 Docker
探针确认当前 Worker 使用 Ollama 兼容接口的 `qwen3-embedding:0.6b`、1024 维、revision
2，且运行时 Git SHA 与容器源码一致。

检索 trace 保存低敏字段：各召回通道、处理器、改写原因、向量降级原因、重排降级原因、
候选数量和最终证据数量。不保存用户问题全文到聚合指标。

## 3. 不可回答门禁

开发阈值只来自 `paperleaf-answerability-dev-v1`：20 篇已揭盲开发论文、40 道独立开发题，
其中 20 道可回答、20 道不可回答；与正式隐藏集问题零重叠。选择规则先要求不可回答误答
为 0，再最大化可回答召回，得到冻结阈值 0.50。

已经揭盲的 10 道 QASPER 不可回答题只作 `diagnostic_not_blind`：

- 完成：10/10；
- 拒答：7/10；
- 严格标签下误答：3/10；
- 合法引用：7/7；
- 伪 Chunk 引用：0。

三条剩余样本不能直接用于继续调阈值：

1. “What is the baseline used?” 的论文原文明示 Calixto et al. (2017) 是 baseline，冻结
   `unanswerable` 标签与全文直接冲突，必须人工裁决；
2. “How was the evaluation corpus collected?” 的证据只说明建立语料和统计量，没有说明
   收集过程，属于门禁仍然过宽的真实失败；
3. “Any other bias may be detected?” 同时涉及本文未评估的受保护类别和相关工作的其他偏见，
   问题本身有歧义，需要人工裁决回答是否越过“本文实验结果”的边界。

因此本轮不会为了得到 0/10 而把有直接原文支持的答案强制改成拒答，也不会用这 10 题
继续调阈值。严格数字保持 3/10，并将两条有争议样本送入人工裁决。

## 4. 跨论文取证

跨论文检索不再只把同一个宽泛问题扔给全局 scope。服务端先形成“论文—子问题”任务，
每篇只在自己的论文范围内检索并在论文内重排。Top-5 使用确定性配额合并：前三篇至少
各保留 1 条证据，剩余 2 个位置按全局分数与去重规则补齐，即 `1+1+1+2`。

最终 Chunk 仍执行 owner、论文 scope、物理页和真实数据库记录校验。当前冻结评测的旧结果
不能因实现变更被重写；该策略的泛化收益只能在未来零重叠隐藏集首次运行后声明。

## 5. `page:*` 伪 Chunk 引用

根因是旧降级路径把“只有页文本、没有真实 Chunk”的证据包装成 `page:*` ID，引用校验
只验证了页面范围，没有要求 Chunk 必须存在于数据库。修复后：

- 检索降级必须映射到真实 Page/Chunk；
- Citation Validator 按 owner、paper、physical_page 和 chunk_id 回查；
- 无法映射时宁可不引用，也不制造页级伪 ID；
- 评测器把任何 `page:*` 或数据库不存在的 ID 计为非法引用。

历史 16 条非法引用原样保留在已揭盲结果中；新运行只验证新代码，不倒改旧证据。

## 6. Specialist 稳定性

历史 v3 样本中有 14 个分支超时、6 个 Schema 失败。当前返工包括：

- v3 Feature Flag 默认关闭；
- 每个 Run 冻结 Specialist timeout、总预算和检索配置；
- Provider 返回的 Markdown fence、别名对象、百分比 confidence、中英文维度/stance 做受控
  规范化；
- 首次 Schema 解析失败只进行一次更短的 JSON 修复，不无限重试；
- branch trace 记录 `schema_repair_count`、耗时、证据数和错误分类；
- Embedding 配置与索引契约一致，避免评测容器关闭向量检索而产生伪对照。

真实证据分三层记录，避免把定向回归冒充完整泛化结果：

1. 冻结 `a4ebe83` 的 30 个真实 Run 共 81 个 Specialist 分支：76 成功、0 超时、
   5 Schema 失败，说明 60 秒分支预算没有复现历史 14 个超时，但 Schema 问题仍存在；
2. `de3d92b` 定向复跑 4 个 Schema 失败案例：12 个分支中 11 成功、1 超时、0 Schema
   整分支失败，4 个分支记录 `schema_fallback_used=true`；
3. `0fda1a5` 只复跑上述唯一超时案例：3/3 分支成功，1 个记录
   `timeout_fallback_used=true`，1 个记录 `schema_fallback_used=true`。

安全降级不接受模型的非法主张，只保留已经通过服务端 scope 与真实 Chunk 校验的检索
证据，最终答案仍经过引用与语义支持门禁。后两轮是目标失败样本的回归证据，不等同于
新的 30 Run 稳定性估计，更不能据此宣布 v3 质量优于 v1/v2。

本轮还修复了 Docker 构建瓶颈：`backend/.dockerignore` 将上下文缩到 1.71 MB，并把
Git SHA 注入移到 `pip install` 之后。实测 API 镜像从异常等待数分钟降至 2.9 秒，且依赖层
命中缓存。

## 7. 人工盲评与新隐藏集门禁

人工盲评包已经把答案内容与版本映射拆开。评审者需要对至少 30 道答案独立填写：事实
正确性、完整性、引用有用性和总体偏好。未填写前状态保持 `human_review_pending`，LLM
Judge 不能命名为人工准确率。

新的零论文重叠隐藏集只能在以下内容全部冻结后创建并运行一次：实现、阈值、配置、评测
协议、数据哈希、Docker 镜像和 Git SHA。当前人工盲评尚未完成，因此新隐藏集状态是
`not_executed`，没有缩小分母或生成临时成绩。

## 8. 可说与不可说

可以说：

- 修正了 micro/macro Recall 与配对 Bootstrap 口径，并公开负结果 64.2%→61.6%；
- 实现了每 Run 检索配置冻结、Worker 复现和低敏检索 trace；
- 将不可回答题误答从已揭盲诊断的 10/10 降到严格口径 3/10，同时发现一条明确标签冲突；
- 实现按论文独立取证、`1+1+1+2` 合并和真实 Chunk 引用校验；
- v3 默认关闭，并对 Schema 失败做有界修复与分支观测。
- 在真实失败样本上，Schema/模型超时可降级为仅使用已验证证据，且降级类型进入 trace。

不能说：

- 不能说最终组合提升 Recall；
- 不能把 macro 区间写成 micro 区间；
- 不能说不可回答题已经达到 0/10；
- 不能说 v3 已证明优于 v1/v2；
- 不能把待填写的盲评写成人工结论；
- 不能在新的零重叠隐藏集首次运行前更新简历中的 75.6% 和 85.4%。

## 9. 证据路径

- `backend/evaluation/results/paperleaf-formal-evaluation-20260814/`
- `backend/evaluation/results/paperleaf-answerability-dev-v1/result.json`
- `backend/evaluation/results/paperleaf-unanswerable-revealed-diagnostic-20260814-attempt3/`
- `backend/evaluation/results/paperleaf-formal-hidden-v1-first-run/end_to_end_answers/`
- `backend/evaluation/results/paperleaf-multi-agent-three-way-20260814/`
- `backend/evaluation/results/paperleaf-v3-p0-reliability-20260814/`
