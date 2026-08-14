# PaperLeaf v1 / v2 / v3 真实对照

状态：`quality_pending`。本次对照完整执行 30 道涉及 3～10 篇论文的任务，共 90 个
真实模型 Run。28 道来自 test split，另有预先固定的 2 道 dev 任务用于满足 30 题工程
样本量；所有失败均保留在分母。数据集仍标记为 `draft`，且人工盲评尚未完成，因此
本报告只能证明工程行为和自动指标，不能声称 v2/v3 的回答质量优于 v1。

## 自动结果

| 指标 | single_agent_v1 | compare_map_reduce_v2 | specialist_subgraph_v3 |
|---|---:|---:|---:|
| 完成率 | 29/30（96.7%） | 29/30（96.7%） | 23/30（76.7%） |
| 引用物理页合法率 | 130/132（98.5%） | 156/156（100.0%） | 79/79（100.0%） |
| required-paper coverage | 93/139（66.9%） | 96/139（69.1%） | 46/139（33.1%） |
| 模型辅助主张支持率 | 153/168（91.1%） | 144/197（73.1%） | 97/97（100.0%） |
| 延迟 p50 / p95 | 90.8 / 173.1 秒 | 105.2 / 191.7 秒 | 116.5 / 203.7 秒 |
| 模型调用数 | 227 | 359 | 420 |
| 工具调用数 | 33 | 0 | 0 |
| 已观测输入 / 输出 Token 估算 | 222,052 / 12,345 | 272,493 / 11,573 | 320,349 / 67,019 |
| 部分 Token 的 cache-miss 情景成本 | $0.0345 | $0.0414 | $0.0636 |

成本使用 2026-08-14 冻结的 DeepSeek `deepseek-v4-flash` 官方价格。由于未持久化
Provider cache hit/miss Token，且部分 planner/grader 调用没有 Token 遥测；表中仅对已
观测 Token 按 cache miss 计价，不是总成本、上界或实际账单。

## 分支结果

- v2：90/90 个确定性 Map-Reduce 检索分支成功，没有分支超时；
- v3：计划 87 个 Specialist 分支，成功 67（77.0%），超时 14（16.1%），Schema
  失败 6（6.9%）；
- v3 有 1/30 Run 回退 v1；其整体完成率和 required-paper coverage 均明显低于 v1/v2。

v3 的 97/97 主张支持率不能脱离完成率解读：失败 Run 没有产生可评分主张，属于
“少说少错”的选择偏差，不能写成 v3 回答准确率 100%。

## 人工盲评

30 道三路盲评包已生成，当前真实评分人数为 0，状态为
`awaiting_human_review`。因此事实正确性、完整性、引用有用性和总体偏好均无结果。
私有盲评映射未提交仓库，避免破坏盲法。

## Worker 强杀恢复

另运行 1 个不计入 30 题 A/B 指标的真实 v3 故障实验。三个 Specialist 启动后，s3 与
s1 已分别完成，s2 尚未完成时使用 `docker kill` 强制终止唯一 Worker；没有手工修改
Job、租约或 Checkpoint。30 分钟租约自然过期后，新 Worker 将同一 Job 领取为第 2 次
执行，只重新启动未完成的 s2，已完成的 s1/s3 后续成功执行次数均为 0。s2 在恢复后
超时，系统基于已完成分支做 partial merge，父 Run 最终 `completed`。

旧 claim token 的迟到事件写入探针返回拒绝，数据库未出现探针事件。两次领取对应两个
脱敏事件 epoch，事件序号连续且唯一。该实验可以证明这一崩溃点上的父 Run 持久恢复、
已完成分支不重复执行和 claim-token fencing；不能外推为任意崩溃点都能恢复，也不能
把 s2 的超时写成三分支全部成功。

## 原始证据

| 文件 | SHA-256 |
|---|---|
| `test-capture.json` | `6e41cc49664f19d6e34a77f27f0990faab8058ed8159a69708fc10aa2bd69029` |
| `dev-capture.json` | `9260ae82dfb6e8f02623e93556649fcdf5bde9d8a9d1bb0f8421f26715414f51` |
| `test-blind-review.jsonl` | `bd470a5151452f7fefcea44b9390672313c616344d316b7418dce1df7ab1b81c` |
| `dev-blind-review.jsonl` | `cd0ca1a1eaff58c354ba6965aedd51d2b56b0e48da9b43d7209643e04b3a9ea1` |
| `metrics.json` | `ddc7f6981609ff2f9e8254d07de7cf88947fcc4ae9f78e215a25fd6929ef5adc` |
| `worker-recovery-capture.json` | `16d6032ac61e0c7fa29eadc9ce1fc3bda527993debe4e03a98af4fb13b305d6f` |
| `worker-recovery.json` | `d8f21a01d2268b417a6369ef431a6a5ed0a4941bc2cdeea51a8db87827f8e0f6` |

`dev-capture.json` 的 6 个 Run 在模型执行后因容器挂载目录权限而未能第一次写盘；
恢复器随后只读复用既有终态 Run，并验证用户、版本和冻结论文范围，没有再次调用模型。

## 可说与不可说

可以说：实现并真实运行了 single-agent、并行 Map-Reduce 和有界 Specialist 三种编排，
并用 30 题/90 Run 对完成率、引用、覆盖、延迟、调用量、Token 和分支失败做了同口径
审计。

不能说：v2 或 v3 提升了回答质量；v3 比单 Agent 更稳定；模型辅助支持率等于人工
准确率；估算成本等于实际 API 账单。
