"""PaperLeaf 正式隐藏集的预注册、冻结和完整性校验。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from .evaluation_dataset import read_manifest, validate_dataset
from .evaluation_holdout import (
    merge_questions_and_oracle,
    read_oracle,
    read_questions,
)

FORMAL_VARIANTS = (
    "production_baseline",
    "plain_embedding_control",
    "contextual_embedding",
    "per_paper_retrieval",
    "weak_query_rewrite",
    "multigranular_page_reranker",
    "final_combined",
)


class ExcludedDatasetLock(BaseModel):
    dataset_id: str
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    paper_ids_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    paper_count: int = Field(ge=1)


class FormalEvaluationLock(BaseModel):
    schema_version: Literal[1] = 1
    dataset_id: str
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    questions_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    oracle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    excluded_datasets: list[ExcludedDatasetLock] = Field(min_length=1)
    candidate_variants: list[str] = Field(min_length=6)
    protocol: dict[str, Any]
    locked_at: str
    status: Literal["frozen_before_first_run"] = "frozen_before_first_run"

    @model_validator(mode="after")
    def validate_protocol(self) -> FormalEvaluationLock:
        if tuple(self.candidate_variants) != FORMAL_VARIANTS:
            raise ValueError("正式消融方案必须与预注册顺序完全一致")
        if self.protocol.get("k") != 5:
            raise ValueError("正式评测 K 必须固定为 5")
        if self.protocol.get("expected_case_count") != 100:
            raise ValueError("正式隐藏集必须固定为 100 题")
        if self.protocol.get("bootstrap_samples", 0) < 10_000:
            raise ValueError("正式评测 Bootstrap 至少 10000 次")
        return self


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_text(path: Path, *, newline: str) -> bytes:
    text = path.read_text(encoding="utf-8")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return newline.join(normalized.split("\n")).encode("utf-8")


def matches_locked_text_sha(path: Path, expected: str) -> bool:
    """接受相同文本的 LF/CRLF 哈希，内容变化仍会失败。

    GitHub Actions 使用 LF checkout，而冻结文件最初在 Windows 以 CRLF 写入。
    冻结对象是规范化文本内容，不应把平台换行误报为数据漂移。
    """

    candidates = {
        sha256_file(path),
        hashlib.sha256(_canonical_text(path, newline="\n")).hexdigest(),
        hashlib.sha256(_canonical_text(path, newline="\r\n")).hexdigest(),
    }
    return expected in candidates


def _base_id(value: str) -> str:
    return re.sub(r"v\d+$", "", value.removeprefix("arxiv:"))


def _paper_ids_sha256(ids: set[str]) -> str:
    return hashlib.sha256(("\n".join(sorted(ids)) + "\n").encode()).hexdigest()


def create_formal_lock(
    *,
    manifest_path: Path,
    questions_path: Path,
    oracle_path: Path,
    exclusion_manifest_paths: list[Path],
    locked_at: str | None = None,
) -> FormalEvaluationLock:
    manifest = read_manifest(manifest_path)
    questions = read_questions(questions_path)
    oracle = read_oracle(oracle_path)
    cases = merge_questions_and_oracle(questions, oracle)
    validation = validate_dataset(manifest, cases)
    if validation.get("case_count") != 100 or manifest.paper_count < 50:
        raise ValueError("正式隐藏集必须包含至少 50 篇论文和恰好 100 道题")
    expected_categories = {
        "single_paper": 50,
        "cross_paper": 30,
        "multi_evidence": 10,
        "unanswerable": 10,
    }
    if manifest.category_counts != expected_categories:
        raise ValueError(f"正式隐藏集类别配额错误：{manifest.category_counts}")
    hidden_ids = {_base_id(paper.id) for paper in manifest.papers}
    exclusions: list[ExcludedDatasetLock] = []
    for path in exclusion_manifest_paths:
        excluded = read_manifest(path)
        ids = {_base_id(paper.id) for paper in excluded.papers}
        overlap = sorted(hidden_ids & ids)
        if overlap:
            raise ValueError(f"隐藏集与 {excluded.dataset_id} 论文重叠：{overlap}")
        exclusions.append(
            ExcludedDatasetLock(
                dataset_id=excluded.dataset_id,
                manifest_sha256=sha256_file(path),
                paper_ids_sha256=_paper_ids_sha256(ids),
                paper_count=len(ids),
            )
        )
    if len({item.dataset_id for item in exclusions}) != len(exclusions):
        raise ValueError("排除数据集不能重复")
    return FormalEvaluationLock(
        dataset_id=manifest.dataset_id,
        manifest_sha256=sha256_file(manifest_path),
        questions_sha256=sha256_file(questions_path),
        oracle_sha256=sha256_file(oracle_path),
        excluded_datasets=exclusions,
        candidate_variants=list(FORMAL_VARIANTS),
        protocol={
            "k": 5,
            "expected_case_count": 100,
            "minimum_paper_count": 50,
            "category_quotas": expected_categories,
            "chunking_strategy": "structure_aware_v2",
            "embedding_revision": 2,
            "bootstrap_samples": 10_000,
            "bootstrap_seed": 20260814,
            "hidden_run_policy": "single_formal_batch_no_post_reveal_tuning",
            "legacy_minilm_reranker": "disabled",
            "human_review_minimum": 30,
        },
        locked_at=locked_at or datetime.now(UTC).isoformat(),
    )


def verify_formal_lock(
    lock: FormalEvaluationLock,
    *,
    manifest_path: Path,
    questions_path: Path,
    oracle_path: Path,
    exclusion_manifest_paths: list[Path],
) -> dict[str, Any]:
    current = create_formal_lock(
        manifest_path=manifest_path,
        questions_path=questions_path,
        oracle_path=oracle_path,
        exclusion_manifest_paths=exclusion_manifest_paths,
        locked_at=lock.locked_at,
    )
    for field in (
        "dataset_id",
        "manifest_sha256",
        "questions_sha256",
        "oracle_sha256",
        "excluded_datasets",
        "candidate_variants",
        "protocol",
    ):
        if getattr(current, field) != getattr(lock, field):
            raise ValueError(f"正式评测冻结协议发生漂移：{field}")
    return {
        "status": "verified",
        "dataset_id": lock.dataset_id,
        "paper_count": read_manifest(manifest_path).paper_count,
        "case_count": len(read_questions(questions_path)),
        "lock_sha256": hashlib.sha256(
            (lock.model_dump_json(indent=2) + "\n").encode()
        ).hexdigest(),
    }


def verify_public_formal_inputs(
    lock: FormalEvaluationLock,
    *,
    manifest_path: Path,
    questions_path: Path,
    exclusion_manifest_paths: list[Path],
) -> dict[str, Any]:
    """在 CI 不读取私有 oracle 的情况下校验公开冻结材料。"""

    if not matches_locked_text_sha(manifest_path, lock.manifest_sha256):
        raise ValueError("正式评测 manifest 哈希漂移")
    if not matches_locked_text_sha(questions_path, lock.questions_sha256):
        raise ValueError("正式评测 questions 哈希漂移")
    manifest = read_manifest(manifest_path)
    questions = read_questions(questions_path)
    if manifest.dataset_id != lock.dataset_id or len(questions) != 100:
        raise ValueError("正式评测公开数据集 ID 或题数不一致")
    known_papers = {paper.id for paper in manifest.papers}
    unknown = sorted(
        {
            paper_id
            for question in questions
            for paper_id in question.paper_ids
            if paper_id not in known_papers
        }
    )
    if unknown:
        raise ValueError(f"正式评测问题引用未知论文：{unknown}")
    locked_exclusions = {item.dataset_id: item for item in lock.excluded_datasets}
    if len(exclusion_manifest_paths) != len(locked_exclusions):
        raise ValueError("正式评测排除清单数量漂移")
    hidden_ids = {_base_id(paper.id) for paper in manifest.papers}
    for path in exclusion_manifest_paths:
        excluded = read_manifest(path)
        expected = locked_exclusions.get(excluded.dataset_id)
        ids = {_base_id(paper.id) for paper in excluded.papers}
        if (
            expected is None
            or not matches_locked_text_sha(path, expected.manifest_sha256)
            or _paper_ids_sha256(ids) != expected.paper_ids_sha256
            or len(ids) != expected.paper_count
        ):
            raise ValueError(f"排除数据集冻结信息漂移：{excluded.dataset_id}")
        if hidden_ids & ids:
            raise ValueError(f"隐藏集与 {excluded.dataset_id} 发生论文重叠")
    return {
        "status": "verified_without_private_oracle",
        "dataset_id": manifest.dataset_id,
        "paper_count": manifest.paper_count,
        "case_count": len(questions),
        "oracle_sha256": lock.oracle_sha256,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="创建或校验 PaperLeaf 正式评测冻结协议")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--questions", required=True, type=Path)
    parser.add_argument("--oracle", required=True, type=Path)
    parser.add_argument("--exclude-manifest", action="append", type=Path, required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if args.verify:
        lock = FormalEvaluationLock.model_validate_json(args.output.read_text(encoding="utf-8"))
        result = verify_formal_lock(
            lock,
            manifest_path=args.manifest,
            questions_path=args.questions,
            oracle_path=args.oracle,
            exclusion_manifest_paths=args.exclude_manifest,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.output.exists():
        raise FileExistsError("冻结协议已存在，禁止覆盖")
    lock = create_formal_lock(
        manifest_path=args.manifest,
        questions_path=args.questions,
        oracle_path=args.oracle,
        exclusion_manifest_paths=args.exclude_manifest,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(lock.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(lock.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
