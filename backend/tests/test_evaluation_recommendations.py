from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from paperleaf_api.evaluation_recommendations import (
    HUMAN_ATTESTATION,
    load_human_annotations,
    precision_at_k,
)


def _bundle(tmp_path: Path, *, annotator_id: str = "researcher-a") -> tuple[Path, Path]:
    queries = tmp_path / "queries.json"
    collection = tmp_path / "collection.json"
    candidates = tmp_path / "candidates.jsonl"
    annotations = tmp_path / "annotations.jsonl"
    queries.write_text('[{"query_id":"q1","query":"推荐五篇"}]\n', encoding="utf-8")
    collection.write_text('{"snapshot_id":"scope-1","papers":[]}\n', encoding="utf-8")
    candidate_rows = [
        {
            "query_id": "q1",
            "candidate_id": f"c{index}",
            "rank": index,
            "title": f"Paper {index}",
        }
        for index in range(1, 6)
    ]
    candidates.write_text(
        "".join(json.dumps(item) + "\n" for item in candidate_rows),
        encoding="utf-8",
    )
    annotations.write_text(
        "".join(
            json.dumps(
                {
                    **item,
                    "dataset_id": "frozen-v1",
                    "relevant": item["rank"] in {1, 2, 4},
                    "annotator_id": annotator_id,
                }
            )
            + "\n"
            for item in candidate_rows
        ),
        encoding="utf-8",
    )
    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "dataset_id": "frozen-v1",
                "frozen_at": "2026-08-11T00:00:00+08:00",
                "artifacts": {
                    "queries": {"path": queries.name, "sha256": digest(queries)},
                    "collection_snapshot": {
                        "path": collection.name,
                        "sha256": digest(collection),
                    },
                    "candidates": {"path": candidates.name, "sha256": digest(candidates)},
                },
                "annotators": [
                    {
                        "id": annotator_id,
                        "type": "human",
                        "attestation": HUMAN_ATTESTATION,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return annotations, manifest


def test_precision_at_five_uses_complete_frozen_human_labels(tmp_path: Path) -> None:
    annotations, manifest = _bundle(tmp_path)

    result = precision_at_k(load_human_annotations(annotations, manifest), k=5)

    assert result["value"] == 0.6
    assert result["query_count"] == 1
    assert result["evidence_level"] == "frozen_human_annotation"


def test_model_name_cannot_be_registered_as_human_annotator(tmp_path: Path) -> None:
    annotations, manifest = _bundle(tmp_path, annotator_id="gpt-5")

    with pytest.raises(ValueError, match="疑似模型身份"):
        load_human_annotations(annotations, manifest)


def test_changed_candidate_output_invalidates_annotation_bundle(tmp_path: Path) -> None:
    annotations, manifest = _bundle(tmp_path)
    candidates = tmp_path / "candidates.jsonl"
    candidates.write_text(candidates.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="SHA-256 不匹配"):
        load_human_annotations(annotations, manifest)


def test_unlabeled_template_cannot_be_reported_as_human_metric(tmp_path: Path) -> None:
    annotations, manifest = _bundle(tmp_path)
    rows = [json.loads(line) for line in annotations.read_text(encoding="utf-8").splitlines()]
    rows[0]["relevant"] = None
    annotations.write_text(
        "".join(json.dumps(item) + "\n" for item in rows), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="尚未完成人工"):
        load_human_annotations(annotations, manifest)
