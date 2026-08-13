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

仓库还包含两个来自 QASPER 人工标注的外部评测子集：

- `paperleaf-qasper-calibration-v1`：QASPER validation 的 29 篇论文、60 个公开问题与页级标签，
  只用于开发和候选选择；
- `paperleaf-qasper-holdout-v1`：QASPER test 的 55 篇论文、120 个公开问题，答案与 183 个页级
  锚点保存在仓库外；`lock.json` 固定公开输入、私有 oracle、候选和检索实现哈希。
- `paperleaf-qasper-selective-holdout-v2`：与校准集论文交集为 0 的 23 篇论文、54 个公开问题，
  用于同时衡量正确引用覆盖、错误作答、过度拒答和选择性风险；私有 oracle 不进入仓库。

QASPER 衍生标注使用 CC BY 4.0，详情见 [QASPER 数据归属](QASPER-ATTRIBUTION.md)。

## 隐藏集协议

CI 不需要私有答案即可确认公开问题和锁没有漂移：

```bash
python -m paperleaf_api.evaluation_holdout verify-public \
  --lock evaluation/datasets/paperleaf-qasper-holdout-v1/lock.json \
  --manifest evaluation/datasets/paperleaf-qasper-holdout-v1/manifest.json \
  --questions evaluation/datasets/paperleaf-qasper-holdout-v1/questions.jsonl
```

拥有私有 oracle 与 PDF 的维护者可执行完整 `verify`。`run --mode blind-first-run` 在结果或
回执已存在时会拒绝重复揭盲；后续只允许显式标记 `diagnostic-after-reveal`。首次结果表明
校准集上的自适应 MRR 增益没有在 holdout 泛化，详见
[首次盲测报告](results/paperleaf-qasper-holdout-v1/REPORT.md)。因此该候选没有进入生产默认链路。

v2 还会校验与校准集的论文隔离：

```bash
python -m paperleaf_api.evaluation_holdout verify-public \
  --lock evaluation/datasets/paperleaf-qasper-selective-holdout-v2/lock.json \
  --manifest evaluation/datasets/paperleaf-qasper-selective-holdout-v2/manifest.json \
  --questions evaluation/datasets/paperleaf-qasper-selective-holdout-v2/questions.jsonl \
  --exclusion-manifest evaluation/datasets/paperleaf-qasper-calibration-v1/manifest.json
```

v2 首次盲测显示，现有质量门禁虽然减少不可回答误答，却同时造成 50% 的可回答题过度拒答，
且选择性风险上升；详见[选择性回答隐藏集报告](results/paperleaf-qasper-selective-holdout-v2/REPORT.md)。
该结果是负结果，不用于调参或宣传准确率提升。

## 指标边界

聚合指标保留分子、分母和比率，包括页级 Recall@K、MRR@K、首个引用物理页准确率、
引用覆盖率、关键词代理、不可回答错误作答率、可回答题过度拒答率、正确引用可回答覆盖率、
选择性风险、非法引用数和本机延迟。关键词代理检查首个
检索片段是否含确定性答案词，不等同于 LLM 回答正确率；本机延迟也不用于跨机器宣传。

`evaluation.py` 仍可独立计算任意预测文件：

```bash
python -m paperleaf_api.evaluation \
  --cases evaluation/datasets/paperleaf-rag-v1/cases.jsonl \
  --predictions predictions.jsonl \
  --output metrics.json \
  -k 5
```

## 生产同源检索评测

离线哈希向量不能代表线上 pgvector。配置好 PostgreSQL 与 Ollama，并将冻结论文导入、按当前
向量契约重新索引后，可直接调用生产 `SQLLibrarySearch`：

```powershell
docker compose run --rm -T --no-deps `
  -v "${PWD}\backend\evaluation:/app/evaluation:ro" `
  -v "${PWD}\backend\outputs:/app/outputs" `
  api python -m paperleaf_api.evaluation_production `
  --manifest /app/evaluation/datasets/paperleaf-rag-v1/manifest.json `
  --cases /app/evaluation/datasets/paperleaf-rag-v1/cases.jsonl `
  --user-email admin@example.com --split test -k 5 `
  --retrieval-mode per_paper_specific `
  --output /app/outputs/production-rag.json
```

`--retrieval-mode` 支持 `unified`、`per_paper_same` 和 `per_paper_specific` 三组消融。
可以重复传入 `--case-id` 做小范围烟测。预检会核对论文版本、Chunk、切分策略和向量指纹；
不满足时输出 `not_executed`，不会自动缩小 scope。该协议只测检索，回答与引用质量字段明确为
`not_measured`。本轮实现与边界见
[生产 RAG 检索升级报告](../../docs/reports/2026-08-13-rag-retrieval-upgrade.md)。
