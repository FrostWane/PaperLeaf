import json

import pytest

from paperleaf_api.evaluation_unanswerable_diagnostic import _read_revealed_unanswerable


def test_revealed_diagnostic_refuses_to_shrink_denominator(tmp_path) -> None:
    path = tmp_path / "answers.jsonl"
    path.write_text(
        json.dumps(
            {
                "case_id": "u1",
                "query": "缺失事实是什么？",
                "scope_paper_ids": ["arxiv:1512.03385v1"],
                "answerable": False,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="恰好包含 10"):
        _read_revealed_unanswerable(path)
