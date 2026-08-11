"""绑定冻结输入与人工声明的论文推荐 Precision@K 评测。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

HUMAN_ATTESTATION = "本人基于冻结查询、集合快照与候选输出独立完成标注，未使用模型代替人工判断。"
_MODEL_ID_RE = re.compile(
    r"(?:gpt|chatgpt|claude|gemini|deepseek|qwen|llama|mistral|copilot|\bllm\b|\bbot\b|\bagent\b)",
    re.IGNORECASE,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        item = json.loads(raw)
        if not isinstance(item, dict):
            raise ValueError(f"{path.name} 第 {line_number} 行不是 JSON 对象")
        rows.append(item)
    return rows


def load_evaluation_manifest(path: Path) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or not str(manifest.get("dataset_id") or "").strip():
        raise ValueError("评测清单缺少 dataset_id")
    try:
        datetime.fromisoformat(str(manifest.get("frozen_at") or "").replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("评测清单 frozen_at 不是 ISO 时间") from error
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ValueError("评测清单缺少冻结产物")
    loaded: dict[str, Any] = {}
    for name in ("queries", "collection_snapshot", "candidates"):
        descriptor = artifacts.get(name)
        if not isinstance(descriptor, dict):
            raise ValueError(f"评测清单缺少 {name} 产物")
        artifact_path = (path.parent / str(descriptor.get("path") or "")).resolve()
        if not artifact_path.is_file() or path.parent.resolve() not in artifact_path.parents:
            raise ValueError(f"冻结产物 {name} 路径无效")
        expected = str(descriptor.get("sha256") or "").casefold()
        actual = _sha256(artifact_path)
        if not re.fullmatch(r"[0-9a-f]{64}", expected) or actual != expected:
            raise ValueError(f"冻结产物 {name} 的 SHA-256 不匹配")
        loaded[name] = (
            _read_jsonl(artifact_path)
            if artifact_path.suffix.casefold() == ".jsonl"
            else json.loads(artifact_path.read_text(encoding="utf-8"))
        )
    annotators = manifest.get("annotators")
    if not isinstance(annotators, list) or not annotators:
        raise ValueError("评测清单没有登记人工标注者")
    registered: dict[str, dict[str, Any]] = {}
    for item in annotators:
        if not isinstance(item, dict):
            raise ValueError("人工标注者登记格式错误")
        annotator_id = str(item.get("id") or "").strip()
        if (
            not annotator_id
            or _MODEL_ID_RE.search(annotator_id)
            or item.get("type") != "human"
            or item.get("attestation") != HUMAN_ATTESTATION
        ):
            raise ValueError("人工标注者声明无效或疑似模型身份")
        registered[annotator_id] = item
    query_ids = {
        str(item.get("query_id") or "")
        for item in loaded["queries"]
        if isinstance(item, dict)
    }
    candidates: dict[tuple[str, str], dict[str, Any]] = {}
    for item in loaded["candidates"]:
        query_id = str(item.get("query_id") or "")
        candidate_id = str(item.get("candidate_id") or "")
        key = (query_id, candidate_id)
        if not query_id or query_id not in query_ids or not candidate_id or key in candidates:
            raise ValueError("冻结候选未绑定唯一有效的 query_id/candidate_id")
        candidates[key] = item
    return {
        **manifest,
        "_registered_annotators": registered,
        "_queries": loaded["queries"],
        "_collection_snapshot": loaded["collection_snapshot"],
        "_candidates": candidates,
    }


def load_human_annotations(path: Path, manifest_path: Path) -> list[dict[str, Any]]:
    manifest = load_evaluation_manifest(manifest_path)
    rows = _read_jsonl(path)
    dataset_id = str(manifest["dataset_id"])
    annotators = manifest["_registered_annotators"]
    candidates = manifest["_candidates"]
    for line_number, item in enumerate(rows, 1):
        if item.get("relevant") not in {True, False}:
            raise ValueError(f"第 {line_number} 行尚未完成人工相关性标注")
        if str(item.get("dataset_id") or "") != dataset_id:
            raise ValueError(f"第 {line_number} 行未绑定当前冻结评测集")
        annotator_id = str(item.get("annotator_id") or "").strip()
        if annotator_id not in annotators:
            raise ValueError(f"第 {line_number} 行标注者未登记为人工")
        key = (str(item.get("query_id") or ""), str(item.get("candidate_id") or ""))
        frozen = candidates.get(key)
        if frozen is None:
            raise ValueError(f"第 {line_number} 行候选不属于冻结输出")
        if int(item.get("rank") or 0) != int(frozen.get("rank") or 0):
            raise ValueError(f"第 {line_number} 行候选排名与冻结输出不一致")
        if str(item.get("title") or "").strip() != str(frozen.get("title") or "").strip():
            raise ValueError(f"第 {line_number} 行标题与冻结输出不一致")
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
        "evidence_level": "frozen_human_annotation",
        "query_count": len(per_query),
        "candidate_count": len(rows),
        "value": (
            sum(item["precision"] for item in per_query) / len(per_query) if per_query else None
        ),
        "per_query": per_query,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="计算绑定冻结产物的人工论文推荐 Precision@K")
    parser.add_argument("annotations", type=Path)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--k", type=int, default=5)
    args = parser.parse_args()
    result = precision_at_k(
        load_human_annotations(args.annotations, args.manifest),
        k=args.k,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
