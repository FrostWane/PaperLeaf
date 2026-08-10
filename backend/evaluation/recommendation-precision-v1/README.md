# 论文推荐人工相关性标注

此目录用于计算聊天 Agent 联网论文推荐的 `Precision@5`。它与类型过滤、去重和排序单元测试分开：自动化测试只能证明规则按设计执行，不能替代人工对“是否与研究主题相关”的判断。

标注步骤：

1. 固定若干真实文献集合和查询，不向标注者展示排序分数。
2. 为每个查询导出至少 10 个候选，随机化展示顺序。
3. 人工依据标题、摘要和查询主题标记 `relevant=true/false`，填写 `annotator`。
4. 恢复系统排序和 `rank` 后，以每个查询前 5 条计算 Precision@5。
5. 有争议的样本由第二位标注者复核，并在报告中保留分歧数，不能覆盖原始记录。

运行：

```powershell
python -m paperleaf_api.evaluation_recommendations annotations.jsonl --k 5
```

`annotations.template.jsonl` 只是字段模板，`relevant` 和 `annotator` 未填写前评测器会拒绝计算，避免把 AI 或自动规则输出冒充人工指标。
