"""人工标注的论文推荐 Precision@K 评测。"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


def load_human_annotations(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        item = json.loads(raw)
        if item.get("relevant") not in {True, False}:
            raise ValueError(f"第 {line_number} 行尚未完成人工相关性标注")
        if not str(item.get("annotator") or "").strip():
            raise ValueError(f"第 {line_number} 行缺少人工标注者")
        rows.append(item)
    return rows


def precision_at_k(rows: list[dict[str, Any]], *, k: int = 5) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in rows:
        query_id = str(item.get("query_id") or "").strip()
        if not query_id:
            raise ValueError("query_id 不能为空")
        grouped[query_id].append(item)
    per_query: list[dict[str, Any]] = []
    for query_id, items in sorted(grouped.items()):
        ordered = sorted(items, key=lambda item: int(item.get("rank") or 0))
        if len(ordered) < k:
            raise ValueError(f"查询 {query_id} 只有 {len(ordered)} 条，不能计算 P@{k}")
        top = ordered[:k]
        relevant = sum(item["relevant"] is True for item in top)
        per_query.append({"query_id": query_id, "relevant": relevant, "precision": relevant / k})
    return {
        "metric": f"Precision@{k}",
        "query_count": len(per_query),
        "candidate_count": len(rows),
        "value": (
            sum(item["precision"] for item in per_query) / len(per_query) if per_query else None
        ),
        "per_query": per_query,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="计算人工标注的论文推荐 Precision@K")
    parser.add_argument("annotations", type=Path)
    parser.add_argument("--k", type=int, default=5)
    args = parser.parse_args()
    result = precision_at_k(load_human_annotations(args.annotations), k=args.k)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
