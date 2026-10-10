"""034 US3：CaseRevision —— chat/page 双来源、前后快照、摘要（T029，先红）。

注意：模拟生产模式——每个阶段使用独立会话（同一会话内 refresh 过 eager 关系
会出现身份映射陈旧集合，是测试假象而非实现问题）。
"""
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.crud import testcase as case_crud
from app.models.batch import RunLog
from app.models.chat import CaseRevision
from app.models.schemas import TestCaseUpdate, TestStepUpdate
from app.models.testcase import TestCase, TestStep


@pytest_asyncio.fixture
async def case_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: TestCase.__table__.create(c, checkfirst=True))
        await conn.run_sync(lambda c: TestStep.__table__.create(c, checkfirst=True))
        await conn.run_sync(lambda c: RunLog.__table__.create(c, checkfirst=True))
        await conn.run_sync(lambda c: CaseRevision.__table__.create(c, checkfirst=True))
    Session = async_sessionmaker(engine, expire_on_commit=False)
    yield Session
    await engine.dispose()


async def _seed_case(Session) -> int:
    async with Session() as db:
        case = TestCase(project_id=1, name="登录用例", priority="medium")
        db.add(case)
        await db.commit()
        await db.refresh(case)
        cid = case.id
        db.add(TestStep(case_id=cid, step_order=1, description="打开登录页", parsed_result=""))
        db.add(TestStep(case_id=cid, step_order=2, description="点击 Login", parsed_result="进入商品页"))
        await db.commit()
        return cid


@pytest.mark.asyncio
async def test_update_writes_revision_chat_source(case_session_factory):
    Session = case_session_factory
    cid = await _seed_case(Session)
    async with Session() as db:
        body = TestCaseUpdate(name="登录用例", steps=[
            TestStepUpdate(step_order=1, description="打开登录页", parsed_result=""),
            TestStepUpdate(step_order=2, description="点击【Login】等待跳转", parsed_result="进入商品页"),
        ])
        updated = await case_crud.update_test_case(
            db, cid, body,
            revision_user_id=7, revision_source="chat",
            revision_conversation_id=3, revision_summary="第 2 步改等待跳转",
        )
        assert updated is not None
        revs = (await db.execute(select(CaseRevision))).scalars().all()
    assert len(revs) == 1
    r = revs[0]
    assert r.source == "chat" and r.conversation_id == 3 and r.user_id == 7
    assert r.summary == "第 2 步改等待跳转"
    assert r.before_snapshot["steps"][1]["description"] == "点击 Login"
    assert r.after_snapshot["steps"][1]["description"] == "点击【Login】等待跳转"


@pytest.mark.asyncio
async def test_update_writes_revision_page_source(case_session_factory):
    Session = case_session_factory
    cid = await _seed_case(Session)
    async with Session() as db:
        body = TestCaseUpdate(name="登录用例（改名）", steps=[
            TestStepUpdate(step_order=1, description="打开登录页", parsed_result=""),
        ])
        await case_crud.update_test_case(
            db, cid, body, revision_user_id=9, revision_source="page",
        )
        revs = (await db.execute(select(CaseRevision))).scalars().all()
    assert len(revs) == 1
    assert revs[0].source == "page" and revs[0].conversation_id is None
    assert revs[0].before_snapshot["name"] == "登录用例"
    assert revs[0].after_snapshot["name"] == "登录用例（改名）"
    assert len(revs[0].before_snapshot["steps"]) == 2
    assert len(revs[0].after_snapshot["steps"]) == 1


@pytest.mark.asyncio
async def test_update_without_revision_user_writes_nothing(case_session_factory):
    Session = case_session_factory
    cid = await _seed_case(Session)
    async with Session() as db:
        body = TestCaseUpdate(name="登录用例", steps=[
            TestStepUpdate(step_order=1, description="打开登录页", parsed_result=""),
        ])
        await case_crud.update_test_case(db, cid, body)
        revs = (await db.execute(select(CaseRevision))).scalars().all()
    assert revs == []
