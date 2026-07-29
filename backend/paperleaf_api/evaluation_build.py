"""从便于人工审阅的注释清单生成稳定 JSONL 评测用例。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .evaluation_dataset import ExpectedEvidence, FrozenEvaluationCase


def _evidence(paper_id: str, item: dict) -> ExpectedEvidence:
    return ExpectedEvidence(
        paper_id=paper_id,
        physical_page=item["page"],
        anchor=item["anchor"],
    )


def build_cases(annotation_path: Path) -> list[FrozenEvaluationCase]:
    source = json.loads(annotation_path.read_text(encoding="utf-8"))
    cases: list[FrozenEvaluationCase] = []
    for paper_index, paper in enumerate(source["paper_cases"]):
        paper_id = paper["paper_id"]
        for field, category in (
            ("definition", paper["definition_category"]),
            ("method", "method"),
            ("setup", "setup"),
            ("limitation", "limitation"),
        ):
            item = paper[field]
            cases.append(
                FrozenEvaluationCase(
                    id=f"{paper['slug']}-{field}",
                    query=item["query"],
                    paper_ids=[paper_id],
                    answerable=True,
                    expected_evidence=[_evidence(paper_id, item)],
                    expected_answer_keywords=item["keywords"],
                    category=category,
                    split="dev" if field == "definition" else "test",
                )
            )
        cases.append(
            FrozenEvaluationCase(
                id=f"{paper['slug']}-unanswerable",
                query=paper["unanswerable"],
                paper_ids=[paper_id],
                answerable=False,
                category="unanswerable",
                split="dev" if paper_index < 10 else "test",
            )
        )

    for item in source["individual_results"]:
        cases.append(
            FrozenEvaluationCase(
                id=item["id"],
                query=item["query"],
                paper_ids=[item["paper_id"]],
                answerable=True,
                expected_evidence=[_evidence(item["paper_id"], item)],
                expected_answer_keywords=item["keywords"],
                category="result",
                split="test",
            )
        )

    for item in source["cross_paper"]:
        evidence = [
            ExpectedEvidence(
                paper_id=entry["paper_id"],
                physical_page=entry["page"],
                anchor=entry["anchor"],
            )
            for entry in item["evidence"]
        ]
        cases.append(
            FrozenEvaluationCase(
                id=item["id"],
                query=item["query"],
                paper_ids=list(dict.fromkeys(entry.paper_id for entry in evidence)),
                answerable=True,
                expected_evidence=evidence,
                expected_answer_keywords=item["keywords"],
                category="cross_paper",
                split="test",
            )
        )
    return cases


def write_cases(cases: list[FrozenEvaluationCase], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    lines = [case.model_dump_json(exclude_defaults=True) for case in cases]
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 PaperLeaf 冻结 RAG 用例")
    parser.add_argument("--annotations", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    cases = build_cases(args.annotations)
    write_cases(cases, args.output)
    print(json.dumps({"output": str(args.output), "case_count": len(cases)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
