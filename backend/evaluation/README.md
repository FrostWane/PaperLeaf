# RAG 离线评测

PaperLeaf 把数据集冻结、检索实验和生成模型评测分开。仓库内包含人工注释与聚合指标，
不重新分发论文 PDF，也不把无模型基线包装成神经语义检索。

## 数据集

`datasets/paperleaf-rag-v1/` 固定了：

- 20 篇 arXiv 论文的精确版本、官方下载地址、SHA-256 与物理页数；
- 120 个问题，其中可回答 100 个、不可回答 20 个；
- 定义、方法、实验设置、结果、局限、多论文对比和恶意指令问题；
- 30 个 dev 问题用于阈值校准，90 个 test 问题用于对比报告；
- 110 个带论文 ID、物理页和页内文本锚点的证据标注。

`annotations.json` 便于人工审阅，`cases.jsonl` 是评测程序读取的冻结产物。修改注释后重新生成：

```bash
python -m paperleaf_api.evaluation_build \
  --annotations evaluation/datasets/paperleaf-rag-v1/annotations.json \
  --output evaluation/datasets/paperleaf-rag-v1/cases.jsonl
```

CI 不下载论文，只校验清单、配额、ID 和页码范围：

```bash
python -m paperleaf_api.evaluation_dataset \
  --manifest evaluation/datasets/paperleaf-rag-v1/manifest.json \
  --cases evaluation/datasets/paperleaf-rag-v1/cases.jsonl
```

本地把清单中的 PDF 下载到独立目录后，增加 `--pdf-dir <目录>`。校验器会检查全部文件哈希、
页数，并确认每个证据锚点确实出现在指定物理页。PDF 不应提交到仓库。

## 无密钥检索基线

下列命令执行词/字符哈希向量、BM25、RRF、页去重、邻页加权、跨论文范围多样化和拒答阈值实验：

```bash
python -m paperleaf_api.evaluation_offline \
  --manifest evaluation/datasets/paperleaf-rag-v1/manifest.json \
  --cases evaluation/datasets/paperleaf-rag-v1/cases.jsonl \
  --pdf-dir <PDF目录> \
  --output evaluation/results/paperleaf-rag-v1/metrics.json \
  --report evaluation/results/paperleaf-rag-v1/REPORT.md \
  -k 5
```

哈希向量只提供不依赖 API Key 的确定性下限，不能代替生产环境的嵌入模型。拒答阈值只根据
dev 集选择，报告默认单列 test 集。原始预测可能包含论文片段，只有明确传入
`--predictions-dir` 才会写出，且不应提交到公共仓库。

## 可选神经检索与重排诊断

安装可选依赖后，可以用本地 ONNX 模型比较真实 dense retrieval 和 Cross-Encoder：

```bash
python -m pip install -e ".[dev,eval]"
python -m paperleaf_api.evaluation_neural \
  --manifest evaluation/datasets/paperleaf-rag-v1/manifest.json \
  --cases evaluation/datasets/paperleaf-rag-v1/cases.jsonl \
  --pdf-dir <PDF目录> \
  --cache-dir <模型缓存目录> \
  --output <metrics.json> \
  --report <REPORT.md> \
  --reranker-only --rerank-focus-window -k 5
```

默认 embedding 为 MIT 许可的 `BAAI/bge-small-en-v1.5`；默认重排器为 Apache-2.0 的
`Xenova/ms-marco-MiniLM-L-6-v2`。模型只在显式运行该命令时下载，默认 API 与 Worker 不依赖
FastEmbed。FastEmbed 官方说明见其[支持模型列表](https://qdrant.github.io/fastembed/examples/Supported_Models/)
与[重排文档](https://qdrant.tech/documentation/fastembed/fastembed-rerankers/)。

v1 的 test 已经被用于诊断，后续实验必须明确标记 `diagnostic_not_blind`，不能把它重新称为
盲测 holdout。0.4.0 的诊断结果与未采用方案见[诊断报告](results/paperleaf-rag-v1/DIAGNOSTIC-0.4.md)。

## 指标边界

聚合指标保留分子、分母和比率，包括页级 Recall@K、MRR@K、首个引用物理页准确率、
引用覆盖率、关键词代理、不可回答错误作答率、非法引用数和本机延迟。关键词代理检查首个
检索片段是否含确定性答案词，不等同于 LLM 回答正确率；本机延迟也不用于跨机器宣传。

`evaluation.py` 仍可独立计算任意预测文件：

```bash
python -m paperleaf_api.evaluation \
  --cases evaluation/datasets/paperleaf-rag-v1/cases.jsonl \
  --predictions predictions.jsonl \
  --output metrics.json \
  -k 5
```
