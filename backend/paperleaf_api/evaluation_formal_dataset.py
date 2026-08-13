"""构建 PaperLeaf 正式隐藏集。

本模块只重组 QASPER 的人工问题与人工证据，不生成答案或证据。输入必须是尚未
运行检索的 holdout 候选；输出公开问题与私有 oracle 分离，便于在首次运行前锁定
哈希。跨论文题由三个真实单篇问题组成，其 Ground Truth 是三组人工证据的笛卡尔积。
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
from collections import Counter
from pathlib import Path

from .evaluation_dataset import (
    EvaluationDatasetManifest,
    EvaluationPaper,
    ExpectedEvidenceGroup,
    read_manifest,
)
from .evaluation_holdout import (
    HoldoutOracleRecord,
    HoldoutQuestion,
    read_oracle,
    read_questions,
)


def _rank(seed: str, value: str) -> str:
    return hashlib.sha256(f"{seed}:{value}".encode()).hexdigest()


def _write_jsonl(path: Path, records: list[object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(record.model_dump_json(exclude_defaults=True) for record in records) + "\n",
        encoding="utf-8",
    )


def _load_pairs(
    questions_path: Path, oracle_path: Path
) -> list[tuple[HoldoutQuestion, HoldoutOracleRecord]]:
    questions = read_questions(questions_path)
    oracle = read_oracle(oracle_path)
    oracle_by_id = {item.id: item for item in oracle}
    if len(oracle_by_id) != len(oracle) or {item.id for item in questions} != set(oracle_by_id):
        raise ValueError("候选问题与 oracle 的 ID 不一致")
    return [(item, oracle_by_id[item.id]) for item in questions]


def _paper_map(*manifests: EvaluationDatasetManifest) -> dict[str, EvaluationPaper]:
    result: dict[str, EvaluationPaper] = {}
    for manifest in manifests:
        for paper in manifest.papers:
            existing = result.get(paper.id)
            if existing is not None and existing != paper:
                raise ValueError(f"同一论文存在不一致清单：{paper.id}")
            result[paper.id] = paper
    return result


def _combined_groups(records: list[HoldoutOracleRecord]) -> list[ExpectedEvidenceGroup]:
    alternatives = [record.acceptable_evidence_groups for record in records]
    if any(not groups for groups in alternatives):
        raise ValueError("跨论文来源题缺少人工证据组")
    combined: list[ExpectedEvidenceGroup] = []
    for selection in itertools.product(*alternatives):
        items = []
        seen: set[tuple[str, int]] = set()
        for group in selection:
            for item in group.items:
                key = (item.paper_id, item.physical_page)
                if key not in seen:
                    seen.add(key)
                    items.append(item)
        combined.append(ExpectedEvidenceGroup(items=items))
        if len(combined) > 64:
            raise ValueError("跨论文可接受证据组合超过 64，拒绝截断 Ground Truth")
    return combined


def build_formal_hidden_dataset(
    *,
    answerable_manifest_path: Path,
    answerable_questions_path: Path,
    answerable_oracle_path: Path,
    unanswerable_manifest_path: Path,
    unanswerable_questions_path: Path,
    unanswerable_oracle_path: Path,
    output_dir: Path,
    oracle_output: Path,
    dataset_id: str,
    created_at: str,
    selection_seed: str,
) -> dict[str, object]:
    """生成固定为 50 单篇、30 跨篇、10 多证据、10 不可回答的隐藏集。"""

    answerable_manifest = read_manifest(answerable_manifest_path)
    unanswerable_manifest = read_manifest(unanswerable_manifest_path)
    answerable_pairs = _load_pairs(answerable_questions_path, answerable_oracle_path)
    unanswerable_pairs = _load_pairs(unanswerable_questions_path, unanswerable_oracle_path)

    if any(not oracle.answerable for _, oracle in answerable_pairs):
        raise ValueError("可回答候选中混入不可回答题")
    if any(oracle.answerable for _, oracle in unanswerable_pairs):
        raise ValueError("不可回答候选中混入可回答题")
    multi = sorted(
        (item for item in answerable_pairs if item[1].category == "multi_page"),
        key=lambda item: _rank(selection_seed, item[0].id),
    )
    single = sorted(
        (item for item in answerable_pairs if item[1].category != "multi_page"),
        key=lambda item: _rank(selection_seed, item[0].id),
    )
    unanswerable = sorted(
        unanswerable_pairs, key=lambda item: _rank(selection_seed, item[0].id)
    )
    if len(multi) < 10 or len(single) < 50 or len(unanswerable) < 10:
        raise RuntimeError(
            "正式隐藏集配额不足："
            f"single={len(single)}/50, multi={len(multi)}/10, "
            f"unanswerable={len(unanswerable)}/10"
        )
    selected_single = single[:50]
    selected_multi = multi[:10]
    selected_unanswerable = unanswerable[:10]
    if len({item[0].paper_ids[0] for item in selected_single + selected_multi}) < 50:
        raise RuntimeError("60 道可回答题未覆盖至少 50 篇隔离论文")

    papers = _paper_map(answerable_manifest, unanswerable_manifest)
    output_questions: list[HoldoutQuestion] = []
    output_oracle: list[HoldoutOracleRecord] = []

    def add_source(
        pair: tuple[HoldoutQuestion, HoldoutOracleRecord], *, category: str
    ) -> None:
        question, oracle = pair
        output_questions.append(question.model_copy(update={"source_dataset": dataset_id}))
        output_oracle.append(oracle.model_copy(update={"category": category}))

    for pair in selected_single:
        add_source(pair, category="single_paper")
    for pair in selected_multi:
        add_source(pair, category="multi_evidence")
    for pair in selected_unanswerable:
        add_source(pair, category="unanswerable")

    # 只使用单篇、可回答且每个证据替代组均为单页的来源题，保证 K=5 对三篇论文
    # 仍有理论可达性。30 组通过循环偏移复用来源问题，但每组内部论文不重复。
    cross_sources = [
        pair
        for pair in selected_single
        if all(len(group.items) == 1 for group in pair[1].acceptable_evidence_groups)
    ]
    if len(cross_sources) < 30:
        raise RuntimeError(f"可用于跨论文组合的单页来源题不足：{len(cross_sources)}/30")
    titles = {paper_id: paper.title for paper_id, paper in papers.items()}
    for index in range(30):
        offsets = (index, index + 17, index + 34)
        sources = [cross_sources[offset % len(cross_sources)] for offset in offsets]
        source_papers = [pair[0].paper_ids[0] for pair in sources]
        if len(set(source_papers)) != 3:
            raise RuntimeError("跨论文确定性分组产生重复论文")
        case_id = f"formal-cross-{index + 1:03d}"
        query_lines = ["请分别回答以下三篇论文对应的问题，并按论文标题分项作答："]
        for ordinal, ((question, _oracle), paper_id) in enumerate(
            zip(sources, source_papers), 1
        ):
            query_lines.append(f"{ordinal}. 《{titles[paper_id]}》：{question.query}")
        output_questions.append(
            HoldoutQuestion(
                id=case_id,
                query="\n".join(query_lines),
                paper_ids=source_papers,
                source_dataset=dataset_id,
                source_question_id="composite:"
                + "+".join(pair[0].source_question_id for pair in sources),
            )
        )
        output_oracle.append(
            HoldoutOracleRecord(
                id=case_id,
                answerable=True,
                acceptable_evidence_groups=_combined_groups([pair[1] for pair in sources]),
                category="cross_paper",
            )
        )

    if len(output_questions) != 100 or len(output_oracle) != 100:
        raise AssertionError("正式隐藏集必须恰好包含 100 道题")
    used_paper_ids = {
        paper_id for question in output_questions for paper_id in question.paper_ids
    }
    selected_papers = sorted((papers[paper_id] for paper_id in used_paper_ids), key=lambda p: p.id)
    if len(selected_papers) < 50:
        raise RuntimeError(f"正式隐藏集论文不足：{len(selected_papers)}/50")
    categories = Counter(item.category for item in output_oracle)
    manifest = EvaluationDatasetManifest(
        dataset_id=dataset_id,
        version=created_at,
        created_at=created_at,
        annotation_license="CC-BY-4.0",
        paper_count=len(selected_papers),
        case_count=100,
        answerable_count=90,
        unanswerable_count=10,
        category_counts=dict(sorted(categories.items())),
        papers=selected_papers,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "manifest.json").write_text(
        manifest.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    _write_jsonl(output_dir / "questions.jsonl", output_questions)
    _write_jsonl(oracle_output, output_oracle)
    receipt = {
        "dataset_id": dataset_id,
        "paper_count": len(selected_papers),
        "case_count": 100,
        "category_counts": dict(sorted(categories.items())),
        "selection_seed": selection_seed,
        "ground_truth_provenance": "qasper_human_annotations",
        "cross_paper_construction": "three_qasper_questions_and_cartesian_evidence_groups",
        "source_dataset_ids": [
            answerable_manifest.dataset_id,
            unanswerable_manifest.dataset_id,
        ],
    }
    (output_dir / "build-receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description="组合 PaperLeaf 正式 QASPER 隐藏集")
    parser.add_argument("--answerable-manifest", required=True, type=Path)
    parser.add_argument("--answerable-questions", required=True, type=Path)
    parser.add_argument("--answerable-oracle", required=True, type=Path)
    parser.add_argument("--unanswerable-manifest", required=True, type=Path)
    parser.add_argument("--unanswerable-questions", required=True, type=Path)
    parser.add_argument("--unanswerable-oracle", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--oracle-output", required=True, type=Path)
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--created-at", required=True)
    parser.add_argument("--selection-seed", required=True)
    args = parser.parse_args()
    result = build_formal_hidden_dataset(
        answerable_manifest_path=args.answerable_manifest,
        answerable_questions_path=args.answerable_questions,
        answerable_oracle_path=args.answerable_oracle,
        unanswerable_manifest_path=args.unanswerable_manifest,
        unanswerable_questions_path=args.unanswerable_questions,
        unanswerable_oracle_path=args.unanswerable_oracle,
        output_dir=args.output_dir,
        oracle_output=args.oracle_output,
        dataset_id=args.dataset_id,
        created_at=args.created_at,
        selection_seed=args.selection_seed,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
