import asyncio
from types import SimpleNamespace

from sqlalchemy.dialects import postgresql

from paperleaf_api.agent.tools import (
    LibrarySearchInput,
    SQLLibrarySearch,
    _deterministic_supplemental_query,
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


def test_deterministic_comparison_query_removes_only_framework_words() -> None:
    assert (
        _deterministic_supplemental_query(
            "Compare the large-scale pre-training data described for ViT and CLIP."
        )
        == "large-scale pre-training data described ViT CLIP"
    )


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

    async def _scoped_overview_evidence(self, request: LibrarySearchInput) -> list[Evidence]:
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


class _EmptySessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *_args):
        return None


class _NoProviderRouter:
    providers = []

    def has_provider(self, _purpose):
        return False


def test_retrieval_diagnostics_are_isolated_between_concurrent_tasks() -> None:
    search = SQLLibrarySearch(config=SimpleNamespace(), model_router=_NoProviderRouter())

    async def scenario() -> list[tuple[str | None, tuple[str, ...]]]:
        ready = asyncio.Event()
        release = asyncio.Event()

        async def request(reason: str):
            search.last_vector_fallback_reason = reason
            search.last_rewrite_reasons = (reason,)
            ready.set()
            await release.wait()
            return search.last_vector_fallback_reason, search.last_rewrite_reasons

        first = asyncio.create_task(request("first"))
        await ready.wait()
        ready.clear()
        second = asyncio.create_task(request("second"))
        await ready.wait()
        release.set()
        return await asyncio.gather(first, second)

    assert asyncio.run(scenario()) == [
        ("first", ("first",)),
        ("second", ("second",)),
    ]


class _PerPaperSearch(SQLLibrarySearch):
    def __init__(self) -> None:
        super().__init__(
            config=SimpleNamespace(
                rag_candidate_pool_size=40,
                rag_per_paper_retrieval_enabled=True,
                rag_per_paper_candidate_limit=2,
                rag_weak_query_rewrite_enabled=True,
                rag_query_rewrite_max_queries=2,
                rag_reranker_enabled=False,
                embedding_dimensions=None,
            ),
            model_router=_NoProviderRouter(),
        )
        self.paper_calls = []
        self.rewrite_calls = []

    async def _embed_query(self, _query):
        return None

    async def _keyword_rows(
        self,
        _session,
        _request,
        query,
        *,
        paper_id=None,
        row_limit=None,
    ):
        self.paper_calls.append((query, paper_id, row_limit))
        if paper_id is None:
            return []
        chunk = SimpleNamespace(
            id=f"{paper_id}:p1:c0",
            physical_page=1,
            text=f"{paper_id} retrieval evidence",
        )
        paper = SimpleNamespace(
            id=paper_id,
            title=paper_id,
            chunking_strategy="structure_aware_v2",
        )
        return [(chunk, paper, 1.0)]

    async def _rewrite_queries(self, query, *, reasons=()):
        self.rewrite_calls.append((query, tuple(reasons)))
        return ()


def test_sql_search_runs_independent_channels_and_balances_papers(monkeypatch) -> None:
    monkeypatch.setattr(
        "paperleaf_api.agent.tools.get_session_factory",
        lambda: lambda: _EmptySessionContext(),
    )
    search = _PerPaperSearch()

    evidence = asyncio.run(
        search(
            LibrarySearchInput(
                user_id="u1",
                query="比较三个方法的局限",
                paper_ids=["p1", "p2", "p3"],
                limit=3,
                ensure_paper_coverage=True,
            )
        )
    )

    assert {paper_id for _query, paper_id, _limit in search.paper_calls} == {
        "p1",
        "p2",
        "p3",
    }
    assert [item.paper_id for item in evidence] == ["p1", "p2", "p3"]
    assert [item.paper_id for item in search.last_candidate_snapshot] == ["p1", "p2", "p3"]
    assert search.rewrite_calls
    assert "broad_or_comparison_intent" in search.rewrite_calls[0][1]


def test_paper_specific_mode_keeps_global_baseline_then_queries_each_paper(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "paperleaf_api.agent.tools.get_session_factory",
        lambda: lambda: _EmptySessionContext(),
    )

    class PaperSpecificSearch(_PerPaperSearch):
        async def _paper_titles(self, _session, request):
            return {paper_id: f"Title {paper_id}" for paper_id in request.paper_ids}

        async def _rewrite_paper_queries(self, _query, paper_titles, *, reasons=()):
            assert "broad_or_comparison_intent" in reasons
            return {paper_id: f"method for {paper_id}" for paper_id in paper_titles}

    search = PaperSpecificSearch()
    evidence = asyncio.run(
        search(
            LibrarySearchInput(
                user_id="u1",
                query="比较三个方法的局限",
                paper_ids=["p1", "p2", "p3"],
                limit=3,
                ensure_paper_coverage=True,
                per_paper_query_mode="paper_specific",
            )
        )
    )

    assert search.paper_calls[0][1] is None
    assert {paper_id for _query, paper_id, _limit in search.paper_calls[1:]} == {
        "p1",
        "p2",
        "p3",
    }
    assert [item.paper_id for item in evidence] == ["p1", "p2", "p3"]
