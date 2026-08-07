"""验证后台概括闭环，不保留测试数据库。

默认推荐 ``--synthetic``。使用 ``--pdf`` 会把该 PDF 的代表性文本发送给当前配置的
外部模型服务，运行者必须先确认自己有权这样处理文献内容。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import fitz  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from paperleaf_api import db, worker  # noqa: E402
from paperleaf_api.artifacts import load_paper_source_revision  # noqa: E402
from paperleaf_api.models import (  # noqa: E402
    Base,
    Job,
    Paper,
    PaperArtifact,
    PaperChunk,
    PaperPage,
    PaperStatus,
    User,
    UserRole,
)
from paperleaf_api.rag.chunking import PageText, chunk_pages  # noqa: E402
from paperleaf_api.repository import SQLAlchemyRepository  # noqa: E402


def _synthetic_pages() -> list[PageText]:
    texts = [
        "SyntheticDTA studies drug-target affinity prediction. The research asks whether sequence-only neural networks can provide accurate affinity estimates without molecular complexes.",
        "Prior work relies on handcrafted descriptors or three-dimensional structures. These inputs are costly and unavailable for many candidate compounds and proteins.",
        "The proposed method uses separate one-dimensional convolutional encoders for SMILES drug strings and amino-acid protein sequences. Their representations are concatenated and passed to a regression head.",
        "Experiments use two fictional open benchmarks named S-KIBA and S-Davis. Training, validation, and test entities are separated to reduce duplicate leakage.",
        "The model is compared with ridge regression, kernel regression, and a multilayer perceptron. Concordance index and mean squared error are the predefined metrics.",
        "SyntheticDTA improves concordance index from 0.71 to 0.79 on S-KIBA and reduces mean squared error from 0.34 to 0.27 on S-Davis.",
        "An ablation removes either the drug encoder or protein encoder. Both removals reduce accuracy, suggesting that the two sequence representations provide complementary signals.",
        "The study is limited to curated synthetic benchmarks, does not evaluate unseen protein families, and does not provide uncertainty calibration or prospective laboratory validation.",
        "The conclusion is that sequence-only convolutional models are a useful baseline, but claims should not be extended to clinical discovery without external and prospective validation.",
    ]
    return [
        PageText("smoke-paper", index, text)
        for index, text in enumerate(texts, start=1)
    ]


async def run(pdf_path: Path | None) -> dict:
    if pdf_path is None:
        pages = _synthetic_pages()
        title = "SyntheticDTA fictional smoke-test paper"
        filename = "synthetic-dta.pdf"
        size_bytes = sum(len(page.text.encode("utf-8")) for page in pages)
    else:
        with fitz.open(pdf_path) as document:
            pages = [
                PageText("smoke-paper", index + 1, page.get_text("text"))
                for index, page in enumerate(document)
            ]
        title = pdf_path.stem
        filename = pdf_path.name
        size_bytes = pdf_path.stat().st_size
    chunks = chunk_pages(pages)
    if not chunks:
        raise RuntimeError("PDF 没有可用于概括的文本")

    with tempfile.TemporaryDirectory(prefix="paperleaf-artifact-") as temp_dir:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{Path(temp_dir) / 'smoke.db'}"
        )
        previous_engine, previous_factory = db._engine, db._session_factory
        factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        db._engine, db._session_factory = engine, factory
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with factory() as session:
                session.add(
                    User(
                        id="smoke-user",
                        email="smoke@example.com",
                        password_hash="not-used",
                        role=UserRole.user,
                        active=True,
                        must_change_password=False,
                    )
                )
                session.add(
                    Paper(
                        id="smoke-paper",
                        owner_id="smoke-user",
                        title=title,
                        authors=[],
                        filename=filename,
                        storage_key="smoke/pdf",
                        mime_type="application/pdf",
                        size_bytes=size_bytes,
                        sha256="b" * 64,
                        page_count=len(pages),
                        status=PaperStatus.ready,
                    )
                )
                for page in pages:
                    session.add(
                        PaperPage(
                            id=f"page-{page.physical_page}",
                            paper_id=page.paper_id,
                            physical_page=page.physical_page,
                            text=page.text,
                        )
                    )
                for chunk in chunks:
                    session.add(
                        PaperChunk(
                            id=chunk.id,
                            page_id=f"page-{chunk.physical_page}",
                            paper_id=chunk.paper_id,
                            physical_page=chunk.physical_page,
                            chunk_index=chunk.chunk_index,
                            text=chunk.text,
                            token_count=chunk.token_count,
                        )
                    )
                await session.commit()

            repository = SQLAlchemyRepository("smoke-session-secret")
            revision = await load_paper_source_revision("smoke-user", "smoke-paper")
            started = time.perf_counter()
            job = await repository.enqueue_paper_artifact(
                "smoke-paper",
                "smoke-user",
                "summary",
                revision,
                preserve_existing=False,
            )
            submit_ms = round((time.perf_counter() - started) * 1000, 1)
            if not job:
                raise RuntimeError("后台概括任务未创建")
            claimed = await worker.claim_job()
            if not claimed or claimed.id != job.id:
                raise RuntimeError("Worker 未领取概括任务")
            generation_started = time.perf_counter()
            try:
                await worker.process_job(claimed)
            except Exception as exc:
                await worker.fail_job(claimed, exc)
                raise
            generation_seconds = round(time.perf_counter() - generation_started, 2)

            async with factory() as session:
                persisted_job = await session.get(Job, job.id)
                artifact = await session.scalar(
                    select(PaperArtifact).where(
                        PaperArtifact.paper_id == "smoke-paper",
                        PaperArtifact.type == "summary",
                    )
                )
                if artifact is None:
                    raise RuntimeError("后台任务未写入概括产物")
                facts = [
                    fact["text"]
                    for section in (artifact.structured_payload or {}).get("sections", [])
                    for fact in section.get("facts", [])
                ]
                return {
                    "submit_ms": submit_ms,
                    "generation_seconds": generation_seconds,
                    "job_status": persisted_job.status.value,
                    "artifact_status": artifact.status,
                    "section_count": len(
                        (artifact.structured_payload or {}).get("sections", [])
                    ),
                    "citation_count": len(
                        (artifact.structured_payload or {}).get("citations", [])
                    ),
                    "all_facts_contain_chinese": bool(facts)
                    and all(re.search(r"[\u3400-\u9fff]", fact) for fact in facts),
                    "markdown_contains_english_fallback": "提取式概览"
                    in (artifact.markdown or ""),
                }
        finally:
            await engine.dispose()
            db._engine, db._session_factory = previous_engine, previous_factory


def main() -> None:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--pdf",
        type=Path,
        help="使用真实 PDF；其代表性文本会发送给当前配置的外部模型",
    )
    source.add_argument(
        "--synthetic",
        action="store_true",
        help="使用完全虚构且无隐私的英文 DTA 文本（推荐）",
    )
    args = parser.parse_args()
    result = asyncio.run(run(args.pdf.resolve() if args.pdf else None))
    print(json.dumps(result, ensure_ascii=False))
    if not (
        result["job_status"] == "completed"
        and result["artifact_status"] == "ready"
        and result["section_count"] == 5
        and result["all_facts_contain_chinese"]
        and not result["markdown_contains_english_fallback"]
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
