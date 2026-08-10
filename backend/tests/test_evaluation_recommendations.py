from pathlib import Path

import pytest

from paperleaf_api.evaluation_recommendations import (
    load_human_annotations,
    precision_at_k,
)


def test_precision_at_five_uses_complete_human_labeled_queries() -> None:
    rows = [
        {
            "query_id": "q1",
            "candidate_id": f"c{index}",
            "rank": index,
            "relevant": index in {1, 2, 4},
            "annotator": "researcher-a",
        }
        for index in range(1, 6)
    ]

    result = precision_at_k(rows, k=5)

    assert result["value"] == 0.6
    assert result["query_count"] == 1


def test_unlabeled_template_cannot_be_reported_as_human_metric(tmp_path: Path) -> None:
    path = tmp_path / "annotations.jsonl"
    path.write_text(
        '{"query_id":"q1","rank":1,"relevant":null,"annotator":""}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="尚未完成人工"):
        load_human_annotations(path)
