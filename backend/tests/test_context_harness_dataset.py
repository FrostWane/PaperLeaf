import json
from collections import Counter
from pathlib import Path


def test_context_harness_dataset_is_frozen_and_balanced() -> None:
    root = Path(__file__).parents[1] / "evaluation" / "context-harness-v1"
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    cases = [
        json.loads(line)
        for line in (root / "cases.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert manifest["frozen"] is True
    assert manifest["case_count"] == 100 == len(cases)
    assert len({case["id"] for case in cases}) == 100
    assert Counter(case["category"] for case in cases) == {
        category: 10 for category in manifest["categories"]
    }
    assert all("query" in case and "expected" in case for case in cases)
