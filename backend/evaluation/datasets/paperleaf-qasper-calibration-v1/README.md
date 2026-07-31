# PaperLeaf QASPER 校准集 v1

这是用于开发期分析的公开校准集，不是隐藏测试集，也不能重新称为盲测结果。

- 来源：QASPER `validation` 切分；
- 29 篇 arXiv v1 论文，60 个问题；
- 48 个可回答问题、12 个不可回答问题；
- 65 个可复现的物理页证据锚点；
- 每个问题可包含多个等价答案对应的替代证据组。

`cases.jsonl` 公开问题、答案类别和页级证据，用于选择候选、分析失败样本及校准拒答。
`build-receipt.json` 固定选择种子、匹配阈值、来源切分和转换失败计数。PDF 不进入仓库。

完整来源与许可见 [QASPER 数据归属](../../QASPER-ATTRIBUTION.md)。聚合结果见
[校准报告](../../results/paperleaf-qasper-calibration-v1/REPORT.md)。
