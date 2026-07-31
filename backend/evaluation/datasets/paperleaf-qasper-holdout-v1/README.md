# PaperLeaf QASPER 隐藏集 v1

该数据集用于检验开发期结论能否泛化，公开仓库只保存问题与不可变锁，不保存答案或证据页。

- 来源：QASPER `test` 切分；
- 55 篇 arXiv v1 论文，120 个问题；
- 私有 oracle 包含 96 个可回答问题、24 个不可回答问题和 183 个物理页证据锚点；
- 固定种子选择，每篇最多 3 问，证据匹配阈值为 0.67；
- `lock.json` 固定清单、公开问题、私有 oracle、候选方案与检索实现的 SHA-256。

`questions.jsonl` 不含答案标签；`manifest.json` 固定 PDF 来源、版本、哈希与页数；
`build-receipt.json` 记录确定性构建配置。CI 可在没有私有 oracle 的情况下验证公开输入没有漂移：

```bash
python -m paperleaf_api.evaluation_holdout verify-public \
  --lock evaluation/datasets/paperleaf-qasper-holdout-v1/lock.json \
  --manifest evaluation/datasets/paperleaf-qasper-holdout-v1/manifest.json \
  --questions evaluation/datasets/paperleaf-qasper-holdout-v1/questions.jsonl
```

首次评分已由单次回执封存。后续重跑必须标记 `diagnostic_after_reveal`，不得再称为盲测。
完整来源与许可见 [QASPER 数据归属](../../QASPER-ATTRIBUTION.md)，聚合结果见
[首次盲测报告](../../results/paperleaf-qasper-holdout-v1/REPORT.md)。
