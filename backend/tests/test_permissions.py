import asyncio
import uuid

from paperleaf_api.models import JobStatus, PaperStatus, UserRole
from paperleaf_api.repository import MemoryRepository, PaperRecord


def test_paper_lookup_is_scoped_by_owner() -> None:
    async def scenario() -> None:
        repository = MemoryRepository("test-secret")
        alice = await repository.create_user(
            "alice@example.com", "alice-password-123", UserRole.user
        )
        bob = await repository.create_user("bob@example.com", "bob-password-12345", UserRole.user)
        paper = PaperRecord(
            id=str(uuid.uuid4()),
            owner_id=alice.id,
            title="Alice 的论文",
            authors=[],
            year=None,
            abstract=None,
            doi=None,
            arxiv_id=None,
            filename="paper.pdf",
            storage_key="alice/paper.pdf",
            mime_type="application/pdf",
            size_bytes=100,
            sha256="a" * 64,
            page_count=1,
            status=PaperStatus.ready,
        )
        await repository.create_paper(paper)

        assert await repository.get_owned_paper(paper.id, alice.id) is paper
        assert await repository.get_owned_paper(paper.id, bob.id) is None
        assert await repository.update_owned_paper(paper.id, bob.id, title="越权修改") is None
        assert paper.title == "Alice 的论文"

    asyncio.run(scenario())


def test_deactivating_user_revokes_session() -> None:
    async def scenario() -> None:
        repository = MemoryRepository("test-secret")
        user = await repository.create_user(
            "reader@example.com", "reader-password-123", UserRole.user
        )
        await repository.create_session(user.id, "token", 3600)
        assert await repository.user_for_session("token") is user

        await repository.update_user(user.id, active=False)
        assert await repository.user_for_session("token") is None

    asyncio.run(scenario())


def test_collection_and_tag_membership_are_owner_scoped() -> None:
    async def scenario() -> None:
        repository = MemoryRepository("test-secret")
        alice = await repository.create_user(
            "alice@example.com", "alice-password-123", UserRole.user
        )
        bob = await repository.create_user("bob@example.com", "bob-password-12345", UserRole.user)
        paper = PaperRecord(
            id=str(uuid.uuid4()),
            owner_id=alice.id,
            title="Alice 的论文",
            authors=[],
            year=None,
            abstract=None,
            doi=None,
            arxiv_id=None,
            filename="paper.pdf",
            storage_key="alice/paper.pdf",
            mime_type="application/pdf",
            size_bytes=100,
            sha256="b" * 64,
            page_count=1,
            status=PaperStatus.ready,
        )
        await repository.create_paper(paper)
        collection = await repository.create_collection(alice.id, "方法", None)
        tag = await repository.create_tag(alice.id, "已读", "#AFC3CE")

        assert await repository.set_paper_collection(collection.id, paper.id, alice.id, True)
        assert await repository.set_paper_tag(tag.id, paper.id, alice.id, True)
        assert not await repository.set_paper_collection(collection.id, paper.id, bob.id, True)
        assert not await repository.set_paper_tag(tag.id, paper.id, bob.id, True)
        assert await repository.update_collection(collection.id, bob.id, name="越权") is None
        assert await repository.update_tag(tag.id, bob.id, name="越权") is None

    asyncio.run(scenario())


def test_delete_job_is_queued_once() -> None:
    async def scenario() -> None:
        repository = MemoryRepository("test-secret")
        user = await repository.create_user(
            "reader@example.com", "reader-password-123", UserRole.user
        )
        paper = PaperRecord(
            id=str(uuid.uuid4()),
            owner_id=user.id,
            title="待删除",
            authors=[],
            year=None,
            abstract=None,
            doi=None,
            arxiv_id=None,
            filename="paper.pdf",
            storage_key="reader/paper.pdf",
            mime_type="application/pdf",
            size_bytes=100,
            sha256="c" * 64,
            page_count=1,
            status=PaperStatus.ready,
        )
        await repository.create_paper(paper)
        await repository.delete_owned_paper(paper.id, user.id)
        await repository.delete_owned_paper(paper.id, user.id)

        delete_jobs = [job for job in repository.jobs.values() if job.type == "delete_paper"]
        assert len(delete_jobs) == 1
        assert paper.status == PaperStatus.deleting

    asyncio.run(scenario())


def test_reprocessing_ready_paper_creates_one_new_parse_job() -> None:
    async def scenario() -> None:
        repository = MemoryRepository("test-secret")
        user = await repository.create_user(
            "reader@example.com", "reader-password-123", UserRole.user
        )
        paper = PaperRecord(
            id=str(uuid.uuid4()),
            owner_id=user.id,
            title="待重新识别",
            authors=[],
            year=None,
            abstract=None,
            doi=None,
            arxiv_id=None,
            filename="paper.pdf",
            storage_key="reader/paper.pdf",
            mime_type="application/pdf",
            size_bytes=100,
            sha256="d" * 64,
            page_count=1,
            status=PaperStatus.ready,
        )
        await repository.create_paper(paper)
        for job in repository.jobs.values():
            job.status = JobStatus.completed
        initial_job_count = len(repository.jobs)

        updated = await repository.requeue_owned_paper(paper.id, user.id)
        duplicate = await repository.requeue_owned_paper(paper.id, user.id)

        assert updated is paper
        assert paper.status == PaperStatus.queued
        assert len(repository.jobs) == initial_job_count + 1
        assert duplicate is None

    asyncio.run(scenario())
