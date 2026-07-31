# QASPER 数据归属

PaperLeaf 的 `paperleaf-qasper-calibration-v1` 与 `paperleaf-qasper-holdout-v1`
包含从 QASPER 选取并转换的问题与答案标注。QASPER 由 Allen Institute for AI 发布，
采用 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) 许可。

- 数据集主页：[allenai/qasper](https://huggingface.co/datasets/allenai/qasper)
- 论文：Dasigi 等，*A Dataset of Information-Seeking Questions and Answers Anchored in Research Papers*，
  [NAACL 2021](https://aclanthology.org/2021.naacl-main.365/)
- 官方基线：[allenai/qasper-led-baseline](https://github.com/allenai/qasper-led-baseline)

PaperLeaf 所做的修改包括：按固定种子选择子集、把 S2ORC 段落证据映射到 arXiv v1 PDF
物理页、增加可复现文本锚点、把可接受答案合并为替代证据组，以及在 holdout 中把问题与
私有 oracle 分离。修改不代表原作者或 Allen Institute for AI 对 PaperLeaf 的认可。

论文 PDF 不随本仓库重新分发；`manifest.json` 仅保存原始来源链接、精确版本、SHA-256 和页数。
PaperLeaf 自有代码继续使用仓库根目录的 Apache-2.0 许可证，QASPER 衍生标注仍遵循 CC BY 4.0。
