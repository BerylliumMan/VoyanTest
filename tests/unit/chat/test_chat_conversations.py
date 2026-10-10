"""034 US1：会话 CRUD / 所有权隔离 / 消息分页 / 变更记录（T010，先红）。"""
import pytest

from app.crud import chat as crud


@pytest.mark.asyncio
async def test_conversation_crud_and_ownership(chat_db):
    c = await crud.create_conversation(chat_db, user_id=7, project_id=24)
    assert c.id and c.status == "active" and c.title == "新对话"

    assert await crud.get_conversation(chat_db, c.id, user_id=7) is not None
    assert await crud.get_conversation(chat_db, c.id, user_id=8) is None  # 用户级隔离
    assert await crud.get_conversation(chat_db, c.id) is not None  # 内部不带过滤

    updated = await crud.update_conversation(chat_db, c, title="改名", status="archived", refs=[{"type": "case", "id": 1}])
    assert updated.title == "改名" and updated.status == "archived" and updated.refs[0]["id"] == 1

    assert len((await crud.list_conversations(chat_db, 7))["items"]) == 1
    assert (await crud.list_conversations(chat_db, 8))["items"] == []
    assert (await crud.list_conversations(chat_db, 7, status="active"))["items"] == []

    await crud.delete_conversation(chat_db, c)
    assert await crud.get_conversation(chat_db, c.id) is None


@pytest.mark.asyncio
async def test_message_pagination_and_get(chat_db):
    c = await crud.create_conversation(chat_db, user_id=1)
    created = []
    for i in range(5):
        created.append(await crud.create_message(chat_db, conversation_id=c.id, role="user", content=f"m{i}"))

    page = await crud.list_messages(chat_db, c.id, limit=2)
    assert [m.content for m in page["items"]] == ["m3", "m4"] and page["has_more"] is True

    page2 = await crud.list_messages(chat_db, c.id, before_id=page["items"][0].id, limit=2)
    assert [m.content for m in page2["items"]] == ["m1", "m2"] and page2["has_more"] is True

    page3 = await crud.list_messages(chat_db, c.id, before_id=page2["items"][0].id, limit=2)
    assert [m.content for m in page3["items"]] == ["m0"] and page3["has_more"] is False

    got = await crud.get_message(chat_db, c.id, created[2].id)
    assert got is not None and got.content == "m2"
    assert await crud.get_message(chat_db, c.id + 999, created[2].id) is None

    after = await crud.list_messages_after(chat_db, c.id, after_id=created[2].id)
    assert [m.content for m in after] == ["m3", "m4"]

    await crud.delete_message(chat_db, created[4])
    assert len((await crud.list_messages(chat_db, c.id))["items"]) == 4


@pytest.mark.asyncio
async def test_update_message_and_cards(chat_db):
    c = await crud.create_conversation(chat_db, user_id=1)
    m = await crud.create_message(chat_db, conversation_id=c.id, role="assistant", content="R1")
    m2 = await crud.update_message(chat_db, m, content="R2", cards=[{"type": "run", "batch_id": 1}])
    assert m2.content == "R2" and m2.cards[0]["batch_id"] == 1
    m3 = await crud.update_message_cards(chat_db, m2, [{"type": "run", "batch_id": 1, "status": "done"}])
    assert m3.cards[0]["status"] == "done"


@pytest.mark.asyncio
async def test_case_revisions_append_only(chat_db):
    r1 = await crud.create_case_revision(
        chat_db, case_id=5, user_id=1, source="chat", conversation_id=1,
        before_snapshot={"steps": []}, after_snapshot={"steps": ["a"]}, summary="第 1 步新增",
    )
    await crud.create_case_revision(
        chat_db, case_id=5, user_id=2, source="page", conversation_id=None,
        before_snapshot={"steps": ["a"]}, after_snapshot={"steps": ["a", "b"]},
    )
    rows = await crud.list_case_revisions(chat_db, 5)
    assert len(rows) == 2 and rows[0].source == "page" and rows[1].source == "chat"
    assert rows[1].id == r1.id
    assert (await crud.list_case_revisions(chat_db, 999)) == []
