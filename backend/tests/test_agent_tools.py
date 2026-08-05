import asyncio

from sqlalchemy.dialects import postgresql

from paperleaf_api.agent.tools import (
    LibrarySearchInput,
    SQLLibrarySearch,
    _is_scoped_overview_query,
    _keyword_search_query,
)
from paperleaf_api.rag.citations import Evidence


def test_keyword_search_query_uses_distinct_content_terms_with_or() -> None:
    assert _keyword_search_query("What main method does this paper propose?") == (
        "main OR method OR propose"
    )


def test_keyword_search_query_rejects_punctuation_only_input() -> None:
    assert _keyword_search_query("？？？") == ""


class _CapturedRows:
    def all(self) -> list[object]:
        return []


class _CapturingSession:
    statement = None

    async def execute(self, statement):
        self.statement = statement
        return _CapturedRows()


def test_chinese_keyword_search_keeps_database_fallback_without_rewrite_model() -> None:
    search = SQLLibrarySearch()
    session = _CapturingSession()

    asyncio.run(
        search._keyword_rows(
            session,
            LibrarySearchInput(user_id="u1", query="注意力机制"),
            "注意力机制",
        )
    )

    assert session.statement is not None
    sql = str(session.statement.compile(dialect=postgresql.dialect()))
    assert "similarity" in sql
    assert "LIKE" in sql
    assert "websearch_to_tsquery" not in sql


def test_scoped_overview_query_recognizes_summary_but_not_specific_method_question() -> None:
    assert _is_scoped_overview_query("这篇文章讲了什么内容") is True
    assert _is_scoped_overview_query("请总结这篇论文") is True
    assert _is_scoped_overview_query("What is this paper about?") is True
    assert _is_scoped_overview_query("这篇论文使用了什么损失函数？") is False


class FastPathSearch(SQLLibrarySearch):
    def __init__(self) -> None:
        self.fast_path_calls = 0

    async def _scoped_overview_evidence(
        self, request: LibrarySearchInput
    ) -> list[Evidence]:
        self.fast_path_calls += 1
        return [
            Evidence(
                "p1:p1:c0",
                "p1",
                "测试论文",
                1,
                "Abstract text",
                retrieval_channels=("scoped_overview",),
            )
        ]


def test_selected_paper_overview_uses_deterministic_fast_path() -> None:
    search = FastPathSearch()

    evidence = asyncio.run(
        search(
            LibrarySearchInput(
                user_id="u1",
                query="这篇文章讲了什么内容",
                paper_ids=["p1"],
            )
        )
    )

    assert search.fast_path_calls == 1
    assert evidence[0].paper_id == "p1"
