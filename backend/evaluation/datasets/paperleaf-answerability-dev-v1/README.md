# PaperLeaf Answerability Dev v1

这是不可回答门禁的独立开发集，不是正式隐藏集，也不用于泛化结论。

- 40 个开发用例：20 个可回答、20 个不可回答。
- 用例来自已经揭盲的 `paperleaf-rag-v1` 开发语料，并统一标记为 `dev`。
- 未读取、复制或改写 `paperleaf-formal-hidden-v1` 的 10 个不可回答问题。
- 只允许用于选择可回答性阈值和错误分析；阈值冻结后，正式隐藏集不得再次参与调参。

运行前必须保证 20 篇论文均为 `structure_aware_v2`，且 Embedding revision 与当前契约一致。预检不通过时评测输出 `not_executed`，不能缩小分母。

```powershell
python -m paperleaf_api.evaluation_answerability `
  --manifest evaluation/datasets/paperleaf-answerability-dev-v1/manifest.json `
  --cases evaluation/datasets/paperleaf-answerability-dev-v1/cases.jsonl `
  --user-email admin@paperleaf.local `
  --output evaluation/results/paperleaf-answerability-dev-v1/result.json
```
