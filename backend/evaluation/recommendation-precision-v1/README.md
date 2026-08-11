# 论文推荐人工相关性标注

此目录用于计算聊天 Agent 联网论文推荐的 `Precision@5`。它与类型过滤、去重和排序单元测试分开：自动化测试只能证明规则按设计执行，不能替代人工对“是否与研究主题相关”的判断。

标注步骤：

1. 将查询、完整集合快照和 Provider 候选分别冻结为文件，并把三者 SHA-256 写入 manifest。
2. 为每个查询导出至少 10 个候选，冻结 `query_id/candidate_id/rank/title` 后再标注。
3. 在 manifest 登记真实人工标注者并签署人工声明；已知模型名称、Bot 或 Agent 身份会被拒绝。
4. 人工依据标题、摘要和查询主题标记 `relevant=true/false`，填写 `annotator_id`。
5. 以每个查询前 5 条计算 Precision@5；任何输入文件变化都会导致哈希校验失败。
6. 有争议的样本由第二位标注者复核，并在报告中保留分歧数，不能覆盖原始记录。

运行：

```powershell
python -m paperleaf_api.evaluation_recommendations annotations.jsonl \
  --manifest manifest.json --k 5
```

模板目录演示完整证据链，但候选仍是占位内容，不能用于发布指标。正式评测必须从一次真实、固定的 Provider 输出生成新的 manifest。`relevant` 未填写、标注者未登记、疑似模型身份、候选被替换或集合快照变化时，评测器都会拒绝计算。

声明只能建立可审计的人工证据链，无法从技术上证明键盘后一定是某个自然人。因此发布报告仍需保留标注日期、人数、分歧记录和原始文件，不能把该校验描述成身份认证。
