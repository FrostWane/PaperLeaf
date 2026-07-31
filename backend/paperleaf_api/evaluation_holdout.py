"""公开问题、私有答案和预注册配置分离的隐藏 holdout 协议。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from .evaluation import EvaluationCase, EvaluationPrediction, evaluate
from .evaluation_dataset import (
    ExpectedEvidenceGroup,
    FrozenEvaluationCase,
    read_manifest,
    validate_dataset,
)
from .evaluation_offline import OfflineRetrievalIndex, QueryRanking, _prediction


class HoldoutQuestion(BaseModel):
    id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    paper_ids: list[str] = Field(min_length=1)
    split: Literal["holdout"] = "holdout"
    source_dataset: str = Field(min_length=1)
    source_question_id: str = Field(min_length=1)


class HoldoutOracleRecord(BaseModel):
    id: str = Field(min_length=1)
    answerable: bool
    acceptable_evidence_groups: list[ExpectedEvidenceGroup] = Field(default_factory=list)
    acceptable_answer_keyword_groups: list[list[str]] = Field(default_factory=list)
    category: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_answerability(self) -> HoldoutOracleRecord:
        if self.answerable and not self.acceptable_evidence_groups:
            raise ValueError("可回答 holdout 记录必须包含证据组")
        if not self.answerable and self.acceptable_evidence_groups:
            raise ValueError("不可回答 holdout 记录不能包含证据组")
        return self


class HoldoutLock(BaseModel):
    schema_version: Literal[1] = 1
    dataset_id: str = Field(min_length=1)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    questions_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    oracle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_variants: list[str] = Field(min_length=1)
    protocol: dict[str, Any]
    locked_at: str

    @model_validator(mode="after")
    def validate_variants(self) -> HoldoutLock:
        if len(set(self.candidate_variants)) != len(self.candidate_variants):
            raise ValueError("candidate_variants 不能重复")
        return self


class HoldoutRevealReceipt(BaseModel):
    schema_version: Literal[1] = 1
    dataset_id: str
    lock_sha256: str
    result_sha256: str
    evaluation_status: Literal["blind_holdout_first_run"]
    revealed_at: str


SUPPORTED_HOLDOUT_VARIANTS = frozenset(
    {"rrf_page", "rrf_page_quality_gate", "rrf_page_adaptive"}
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_text_bytes(path: Path, *, newline: str = "\n") -> bytes:
    """以显式换行格式编码文本，避免 Git checkout 改变锁校验结果。"""

    text = path.read_text(encoding="utf-8")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return newline.join(normalized.split("\n")).encode("utf-8")


def _sha256_canonical_text(path: Path) -> str:
    return hashlib.sha256(_canonical_text_bytes(path)).hexdigest()


def _matches_locked_text_sha(path: Path, expected: str) -> bool:
    """兼容修复前由 Windows CRLF 生成的锁，同时保持内容变更可检测。"""

    candidates = {
        sha256_file(path),
        _sha256_canonical_text(path),
        hashlib.sha256(_canonical_text_bytes(path, newline="\r\n")).hexdigest(),
    }
    return expected in candidates


def _read_jsonl(path: Path, model: type[BaseModel]) -> list[BaseModel]:
    records: list[BaseModel] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            records.append(model.model_validate_json(line))
        except Exception as exc:
            raise ValueError(f"{path}:{line_number} 不是合法记录") from exc
    return records


def read_questions(path: Path) -> list[HoldoutQuestion]:
    return [HoldoutQuestion.model_validate(item) for item in _read_jsonl(path, HoldoutQuestion)]


def read_oracle(path: Path) -> list[HoldoutOracleRecord]:
    return [
        HoldoutOracleRecord.model_validate(item)
        for item in _read_jsonl(path, HoldoutOracleRecord)
    ]


def merge_questions_and_oracle(
    questions: list[HoldoutQuestion],
    oracle: list[HoldoutOracleRecord],
) -> list[FrozenEvaluationCase]:
    question_ids = [item.id for item in questions]
    oracle_ids = [item.id for item in oracle]
    if len(set(question_ids)) != len(question_ids):
        raise ValueError("holdout questions 存在重复 id")
    if len(set(oracle_ids)) != len(oracle_ids):
        raise ValueError("holdout oracle 存在重复 id")
    if set(question_ids) != set(oracle_ids):
        raise ValueError(
            "holdout ID 不匹配："
            f"missing_oracle={sorted(set(question_ids) - set(oracle_ids))}, "
            f"unknown_oracle={sorted(set(oracle_ids) - set(question_ids))}"
        )
    oracle_by_id = {item.id: item for item in oracle}
    return [
        FrozenEvaluationCase(
            id=question.id,
            query=question.query,
            paper_ids=question.paper_ids,
            answerable=oracle_by_id[question.id].answerable,
            acceptable_evidence_groups=oracle_by_id[
                question.id
            ].acceptable_evidence_groups,
            acceptable_answer_keyword_groups=oracle_by_id[
                question.id
            ].acceptable_answer_keyword_groups,
            category=oracle_by_id[question.id].category,
            split="holdout",
        )
        for question in questions
    ]


def create_lock(
    *,
    dataset_id: str,
    manifest_path: Path,
    questions_path: Path,
    oracle_path: Path,
    candidate_variants: list[str],
    protocol: dict[str, Any],
    locked_at: str | None = None,
) -> HoldoutLock:
    return HoldoutLock(
        dataset_id=dataset_id,
        manifest_sha256=_sha256_canonical_text(manifest_path),
        questions_sha256=_sha256_canonical_text(questions_path),
        oracle_sha256=_sha256_canonical_text(oracle_path),
        candidate_variants=candidate_variants,
        protocol=protocol,
        locked_at=locked_at or datetime.now(UTC).isoformat(),
    )


def verify_lock(
    lock: HoldoutLock,
    *,
    manifest_path: Path,
    questions_path: Path,
    oracle_path: Path,
) -> None:
    paths = {
        "manifest_sha256": manifest_path,
        "questions_sha256": questions_path,
        "oracle_sha256": oracle_path,
    }
    mismatches = [
        name
        for name, path in paths.items()
        if not _matches_locked_text_sha(path, getattr(lock, name))
    ]
    if mismatches:
        raise ValueError(f"holdout lock 校验失败：{mismatches}")


def verify_public_holdout_inputs(
    *,
    lock_path: Path,
    manifest_path: Path,
    questions_path: Path,
    exclusion_manifest_path: Path | None = None,
) -> dict[str, object]:
    """CI 无需私有 oracle 即可校验公开输入、范围与预注册哈希。"""

    lock = HoldoutLock.model_validate_json(lock_path.read_text(encoding="utf-8"))
    verify_exclusion_protocol(
        lock,
        manifest_path=manifest_path,
        exclusion_manifest_path=exclusion_manifest_path,
    )
    mismatches = [
        field
        for field, path in (
            ("manifest_sha256", manifest_path),
            ("questions_sha256", questions_path),
        )
        if not _matches_locked_text_sha(path, getattr(lock, field))
    ]
    if mismatches:
        raise ValueError(f"holdout 公开输入校验失败：{mismatches}")
    manifest = read_manifest(manifest_path)
    questions = read_questions(questions_path)
    if manifest.dataset_id != lock.dataset_id:
        raise ValueError("lock.dataset_id 与 manifest 不一致")
    if len(questions) != manifest.case_count:
        raise ValueError(
            f"公开问题数量 {len(questions)} 与 manifest.case_count "
            f"{manifest.case_count} 不一致"
        )
    question_ids = [question.id for question in questions]
    if len(set(question_ids)) != len(question_ids):
        raise ValueError("holdout questions 存在重复 id")
    known_papers = {paper.id for paper in manifest.papers}
    unknown_papers = sorted(
        {
            paper_id
            for question in questions
            for paper_id in question.paper_ids
            if paper_id not in known_papers
        }
    )
    if unknown_papers:
        raise ValueError(f"公开问题引用未知论文：{unknown_papers}")
    return {
        "dataset_id": manifest.dataset_id,
        "paper_count": manifest.paper_count,
        "question_count": len(questions),
        "candidate_variants": lock.candidate_variants,
        "oracle_sha256": lock.oracle_sha256,
    }


def _base_paper_id(value: str) -> str:
    return re.sub(r"v\d+$", "", value.removeprefix("arxiv:"))


def verify_exclusion_protocol(
    lock: HoldoutLock,
    *,
    manifest_path: Path,
    exclusion_manifest_path: Path | None,
) -> None:
    expected_sha = lock.protocol.get("excluded_manifest_sha256")
    if expected_sha is None:
        return
    if exclusion_manifest_path is None:
        raise ValueError("预注册协议要求提供排除数据集 manifest")
    if not isinstance(expected_sha, str) or not _matches_locked_text_sha(
        exclusion_manifest_path, expected_sha
    ):
        raise ValueError("排除数据集 manifest 哈希与预注册协议不一致")
    manifest = read_manifest(manifest_path)
    excluded_manifest = read_manifest(exclusion_manifest_path)
    current_ids = {_base_paper_id(item.id) for item in manifest.papers}
    excluded_ids = {_base_paper_id(item.id) for item in excluded_manifest.papers}
    overlap = sorted(current_ids & excluded_ids)
    if overlap:
        raise ValueError(f"holdout 与排除数据集存在论文交集：{overlap}")


def load_locked_cases(
    *,
    lock_path: Path,
    manifest_path: Path,
    questions_path: Path,
    oracle_path: Path,
    pdf_dir: Path | None = None,
) -> tuple[HoldoutLock, list[EvaluationCase], dict[str, object]]:
    lock = HoldoutLock.model_validate_json(lock_path.read_text(encoding="utf-8"))
    verify_lock(
        lock,
        manifest_path=manifest_path,
        questions_path=questions_path,
        oracle_path=oracle_path,
    )
    frozen = merge_questions_and_oracle(
        read_questions(questions_path), read_oracle(oracle_path)
    )
    manifest = read_manifest(manifest_path)
    if manifest.dataset_id != lock.dataset_id:
        raise ValueError("lock.dataset_id 与 manifest 不一致")
    validation = validate_dataset(manifest, frozen, pdf_dir=pdf_dir)
    return (
        lock,
        [EvaluationCase.model_validate(case.model_dump()) for case in frozen],
        validation,
    )


def write_first_reveal_receipt(
    *,
    receipt_path: Path,
    lock_path: Path,
    result_path: Path,
    dataset_id: str,
) -> HoldoutRevealReceipt:
    if receipt_path.exists():
        raise FileExistsError("holdout 已经揭盲；后续运行必须标记为 diagnostic_after_reveal")
    receipt = HoldoutRevealReceipt(
        dataset_id=dataset_id,
        lock_sha256=sha256_file(lock_path),
        result_sha256=sha256_file(result_path),
        evaluation_status="blind_holdout_first_run",
        revealed_at=datetime.now(UTC).isoformat(),
    )
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(
        receipt.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    return receipt


def _run_candidate(
    index: OfflineRetrievalIndex,
    case: EvaluationCase,
    *,
    variant: str,
    k: int,
) -> QueryRanking:
    if variant in {"rrf_page", "rrf_page_quality_gate"}:
        return index.fused(case.query, case.paper_ids, limit=k, page_dedup=True)
    if variant == "rrf_page_adaptive":
        return index.adaptive_fused(case.query, case.paper_ids, limit=k)
    raise ValueError(f"未注册的 holdout 候选：{variant}")


def evaluate_locked_holdout(
    *,
    lock_path: Path,
    manifest_path: Path,
    questions_path: Path,
    oracle_path: Path,
    pdf_dir: Path,
    result_path: Path,
    receipt_path: Path,
    mode: Literal["blind-first-run", "diagnostic-after-reveal"],
    exclusion_manifest_path: Path | None = None,
) -> tuple[dict[str, Any], HoldoutRevealReceipt | None]:
    """只运行预注册候选；公开结果不包含逐题预测或私有 oracle。"""

    if mode == "blind-first-run" and (receipt_path.exists() or result_path.exists()):
        raise FileExistsError("首次揭盲输出或回执已存在，拒绝重复运行")
    if mode == "diagnostic-after-reveal" and not receipt_path.is_file():
        raise FileNotFoundError("诊断重跑要求已有首次揭盲回执")
    if mode == "diagnostic-after-reveal" and result_path.exists():
        raise FileExistsError("诊断重跑必须使用新的输出路径，不能覆盖首次结果")

    # 先核对预注册实现，再读取私有 oracle，避免实现漂移后接触隐藏答案。
    lock = HoldoutLock.model_validate_json(lock_path.read_text(encoding="utf-8"))
    verify_exclusion_protocol(
        lock,
        manifest_path=manifest_path,
        exclusion_manifest_path=exclusion_manifest_path,
    )
    unknown = sorted(set(lock.candidate_variants) - SUPPORTED_HOLDOUT_VARIANTS)
    if unknown:
        raise ValueError(f"锁中存在未注册候选：{unknown}")
    implementation_path = Path(__file__).with_name("evaluation_offline.py")
    locked_implementation = lock.protocol.get("retrieval_implementation_sha256")
    if not isinstance(locked_implementation, str) or not _matches_locked_text_sha(
        implementation_path, locked_implementation
    ):
        raise ValueError("检索实现哈希与预注册协议不一致")
    k = int(lock.protocol.get("k", 0))
    if k <= 0:
        raise ValueError("预注册协议的 k 必须为正数")
    if int(lock.protocol.get("hash_dimensions", 0)) != 8192:
        raise ValueError("当前隐藏集运行器只支持锁定 hash_dimensions=8192")
    if "rrf_page_quality_gate" in lock.candidate_variants:
        quality_path = Path(__file__).parent / "rag" / "retrieval_quality.py"
        locked_quality = lock.protocol.get("quality_gate_implementation_sha256")
        if not isinstance(locked_quality, str) or not _matches_locked_text_sha(
            quality_path, locked_quality
        ):
            raise ValueError("质量门禁实现哈希与预注册协议不一致")
    locked_scorer = lock.protocol.get("evaluation_implementation_sha256")
    if locked_scorer is not None:
        scorer_path = Path(__file__).with_name("evaluation.py")
        if not isinstance(locked_scorer, str) or not _matches_locked_text_sha(
            scorer_path, locked_scorer
        ):
            raise ValueError("评分实现哈希与预注册协议不一致")

    lock, cases, validation = load_locked_cases(
        lock_path=lock_path,
        manifest_path=manifest_path,
        questions_path=questions_path,
        oracle_path=oracle_path,
        pdf_dir=pdf_dir,
    )

    manifest = read_manifest(manifest_path)
    index_started = time.perf_counter()
    index = OfflineRetrievalIndex.from_pdf_dir(
        manifest_path,
        pdf_dir,
        target_tokens=manifest.chunking.target_tokens,
        overlap_tokens=manifest.chunking.overlap_tokens,
    )
    index_ms = round((time.perf_counter() - index_started) * 1000)
    predictions: dict[str, list[EvaluationPrediction]] = {}
    for variant in lock.candidate_variants:
        records: list[EvaluationPrediction] = []
        for case in cases:
            started = time.perf_counter()
            ranking = _run_candidate(index, case, variant=variant, k=k)
            latency_ms = max(0, round((time.perf_counter() - started) * 1000))
            records.append(
                _prediction(
                    case,
                    ranking,
                    latency_ms=latency_ms,
                    threshold=None,
                    quality_gate=variant == "rrf_page_quality_gate",
                )
            )
        predictions[variant] = records

    status = (
        "blind_holdout_first_run"
        if mode == "blind-first-run"
        else "diagnostic_after_reveal"
    )
    result: dict[str, Any] = {
        "schema_version": 1,
        "dataset": validation,
        "evaluation_status": status,
        "lock_sha256": sha256_file(lock_path),
        "protocol": lock.protocol,
        "runtime": {
            "index_build_ms": index_ms,
            "chunk_count": len(index.chunks),
        },
        "variants": {
            name: {"metrics": evaluate(cases, variant_predictions, k=k)}
            for name, variant_predictions in predictions.items()
        },
    }
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    receipt = None
    if mode == "blind-first-run":
        receipt = write_first_reveal_receipt(
            receipt_path=receipt_path,
            lock_path=lock_path,
            result_path=result_path,
            dataset_id=lock.dataset_id,
        )
    return result, receipt


def _main_lock(args: argparse.Namespace) -> None:
    protocol = json.loads(args.protocol)
    lock = create_lock(
        dataset_id=args.dataset_id,
        manifest_path=args.manifest,
        questions_path=args.questions,
        oracle_path=args.oracle,
        candidate_variants=args.candidate_variant,
        protocol=protocol,
        locked_at=args.locked_at,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(lock.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(json.dumps(lock.model_dump(), ensure_ascii=False, indent=2))


def _main_verify(args: argparse.Namespace) -> None:
    lock, cases, validation = load_locked_cases(
        lock_path=args.lock,
        manifest_path=args.manifest,
        questions_path=args.questions,
        oracle_path=args.oracle,
        pdf_dir=args.pdf_dir,
    )
    print(
        json.dumps(
            {
                "dataset_id": lock.dataset_id,
                "candidate_variants": lock.candidate_variants,
                "case_count": len(cases),
                "validation": validation,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def _main_verify_public(args: argparse.Namespace) -> None:
    result = verify_public_holdout_inputs(
        lock_path=args.lock,
        manifest_path=args.manifest,
        questions_path=args.questions,
        exclusion_manifest_path=args.exclusion_manifest,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def _main_run(args: argparse.Namespace) -> None:
    result, receipt = evaluate_locked_holdout(
        lock_path=args.lock,
        manifest_path=args.manifest,
        questions_path=args.questions,
        oracle_path=args.oracle,
        pdf_dir=args.pdf_dir,
        result_path=args.output,
        receipt_path=args.receipt,
        mode=args.mode,
        exclusion_manifest_path=args.exclusion_manifest,
    )
    print(
        json.dumps(
            {
                "dataset_id": result["dataset"]["dataset_id"],
                "evaluation_status": result["evaluation_status"],
                "candidate_variants": list(result["variants"]),
                "output": str(args.output),
                "receipt": receipt.model_dump() if receipt else None,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="管理 PaperLeaf 隐藏 holdout 锁")
    subparsers = parser.add_subparsers(dest="command", required=True)
    lock_parser = subparsers.add_parser("lock")
    lock_parser.add_argument("--dataset-id", required=True)
    lock_parser.add_argument("--manifest", required=True, type=Path)
    lock_parser.add_argument("--questions", required=True, type=Path)
    lock_parser.add_argument("--oracle", required=True, type=Path)
    lock_parser.add_argument("--candidate-variant", action="append", required=True)
    lock_parser.add_argument("--protocol", required=True)
    lock_parser.add_argument("--locked-at")
    lock_parser.add_argument("--output", required=True, type=Path)
    lock_parser.set_defaults(handler=_main_lock)

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--lock", required=True, type=Path)
    verify_parser.add_argument("--manifest", required=True, type=Path)
    verify_parser.add_argument("--questions", required=True, type=Path)
    verify_parser.add_argument("--oracle", required=True, type=Path)
    verify_parser.add_argument("--pdf-dir", type=Path)
    verify_parser.set_defaults(handler=_main_verify)

    public_parser = subparsers.add_parser("verify-public")
    public_parser.add_argument("--lock", required=True, type=Path)
    public_parser.add_argument("--manifest", required=True, type=Path)
    public_parser.add_argument("--questions", required=True, type=Path)
    public_parser.add_argument("--exclusion-manifest", type=Path)
    public_parser.set_defaults(handler=_main_verify_public)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--lock", required=True, type=Path)
    run_parser.add_argument("--manifest", required=True, type=Path)
    run_parser.add_argument("--questions", required=True, type=Path)
    run_parser.add_argument("--oracle", required=True, type=Path)
    run_parser.add_argument("--pdf-dir", required=True, type=Path)
    run_parser.add_argument("--output", required=True, type=Path)
    run_parser.add_argument("--receipt", required=True, type=Path)
    run_parser.add_argument("--exclusion-manifest", type=Path)
    run_parser.add_argument(
        "--mode",
        choices=("blind-first-run", "diagnostic-after-reveal"),
        default="blind-first-run",
    )
    run_parser.set_defaults(handler=_main_run)
    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
