# RAG 离线评测协议

该目录只定义协议，不附带虚构评测集或成绩。准备具有合法来源的固定 PDF 后，分别生成：

- `cases.jsonl`：人工标注问题、是否可回答、正确物理页和可选正确 Chunk。
- `predictions.jsonl`：某一固定 Git commit、配置和模型产生的检索及回答结果。

校验并计算指标：

```bash
python -m paperleaf_api.evaluation \
  --cases cases.jsonl \
  --predictions predictions.jsonl \
  --output metrics.json \
  -k 5
```

每次对比必须固定文献、问题、模型、随机参数和评测代码，并在实验记录中保存 baseline 与
candidate commit。工具输出原始分子和分母；分母为零时 `value` 为 `null`，不得补写成绩。

指标包括 Recall@K、引用物理页准确率、引用覆盖率、关键词核对率、不可回答错误作答率、
非法引用数，以及端到端延迟中位数和 p95。关键词核对只适合确定性事实，不代替人工评审。

