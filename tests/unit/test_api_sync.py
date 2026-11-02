"""046：接口同步应用层测试（新增/更新/下线/manual_override/幂等）。"""

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.models.api_test import ApiDefinition
from app.models.project import Project
from app.services.api_sync import apply_extracted_apis
from core.api_extract.base import ExtractedApi


@pytest_asyncio.fixture
async def db():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.db_models  # noqa: F401  确保全部模型注册
    from app.database import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def project_id(db):
    project = Project(name="demo")
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project.id


def _api(method: str, path: str, summary: str = "", **kw) -> ExtractedApi:
    return ExtractedApi(
        method=method,
        path=path,
        summary=summary,
        extractor=kw.pop("extractor", "spring_source"),
        source_file=kw.pop("source_file", "src/Controller.java"),
        source_line=kw.pop("source_line", 10),
        confidence=kw.pop("confidence", "high"),
        **kw,
    ).normalized()


async def _all(db, project_id: int) -> dict[tuple[str, str], ApiDefinition]:
    rows = (
        await db.execute(select(ApiDefinition).where(ApiDefinition.project_id == project_id))
    ).scalars().all()
    return {(r.method, r.path): r for r in rows}


@pytest.mark.asyncio
async def test_inserts_new_definitions(db, project_id):
    stats = await apply_extracted_apis(
        db,
        project_id=project_id,
        apis=[_api("GET", "/users", "用户列表"), _api("POST", "/users", "创建用户")],
        extractor="spring_source",
    )
    await db.commit()

    assert (stats.added, stats.updated, stats.removed) == (2, 0, 0)
    rows = await _all(db, project_id)
    assert set(rows) == {("GET", "/users"), ("POST", "/users")}
    get_user = rows[("GET", "/users")]
    assert get_user.name == "用户列表"
    assert get_user.extractor == "spring_source"
    assert get_user.status == "active"
    assert get_user.source_line == 10


@pytest.mark.asyncio
async def test_second_run_is_idempotent(db, project_id):
    apis = [_api("GET", "/users", "用户列表")]
    await apply_extracted_apis(db, project_id=project_id, apis=apis, extractor="spring_source")
    await db.commit()

    stats = await apply_extracted_apis(
        db, project_id=project_id, apis=apis, extractor="spring_source"
    )
    assert (stats.added, stats.updated, stats.unchanged) == (0, 0, 1)


@pytest.mark.asyncio
async def test_updates_changed_fields(db, project_id):
    await apply_extracted_apis(
        db, project_id=project_id, apis=[_api("GET", "/users", "旧摘要")], extractor="spring_source"
    )
    await db.commit()

    stats = await apply_extracted_apis(
        db,
        project_id=project_id,
        apis=[_api("GET", "/users", "新摘要", confidence="low", confidence_reason="常量")],
        extractor="spring_source",
    )
    await db.commit()

    assert stats.updated == 1
    row = (await _all(db, project_id))[("GET", "/users")]
    assert row.summary == "新摘要"
    assert row.confidence == "low"
    assert row.confidence_reason == "常量"


@pytest.mark.asyncio
async def test_manual_override_is_protected(db, project_id):
    await apply_extracted_apis(
        db, project_id=project_id, apis=[_api("GET", "/users", "旧摘要")], extractor="spring_source"
    )
    await db.commit()

    row = (await _all(db, project_id))[("GET", "/users")]
    row.manual_override = True
    row.summary = "人工改的摘要"
    await db.commit()

    stats = await apply_extracted_apis(
        db, project_id=project_id, apis=[_api("GET", "/users", "自动摘要")], extractor="spring_source"
    )
    await db.commit()

    assert stats.skipped_manual == 1
    assert stats.updated == 0
    row = (await _all(db, project_id))[("GET", "/users")]
    assert row.summary == "人工改的摘要"
    assert row.manual_override is True


@pytest.mark.asyncio
async def test_missing_auto_definitions_become_offline(db, project_id):
    await apply_extracted_apis(
        db,
        project_id=project_id,
        apis=[_api("GET", "/keep"), _api("DELETE", "/gone")],
        extractor="spring_source",
    )
    await db.commit()

    stats = await apply_extracted_apis(
        db, project_id=project_id, apis=[_api("GET", "/keep")], extractor="spring_source"
    )
    await db.commit()

    assert stats.removed == 1
    rows = await _all(db, project_id)
    assert rows[("GET", "/keep")].status == "active"
    assert rows[("DELETE", "/gone")].status == "offline"  # 不删除，保留用例引用


@pytest.mark.asyncio
async def test_offline_does_not_touch_manual_or_other_extractors(db, project_id):
    await apply_extracted_apis(
        db,
        project_id=project_id,
        apis=[_api("GET", "/auto-one"), _api("GET", "/manual-one")],
        extractor="spring_source",
    )
    await apply_extracted_apis(
        db,
        project_id=project_id,
        apis=[_api("GET", "/openapi-one", extractor="openapi")],
        extractor="openapi",
    )
    await db.commit()

    rows = await _all(db, project_id)
    rows[("GET", "/manual-one")].manual_override = True
    await db.commit()

    stats = await apply_extracted_apis(
        db, project_id=project_id, apis=[], extractor="spring_source"
    )
    await db.commit()

    # 只有 spring_source 来源且未被人工接管的条目会下线
    assert stats.removed == 1
    rows = await _all(db, project_id)
    assert rows[("GET", "/auto-one")].status == "offline"
    assert rows[("GET", "/manual-one")].status == "active"
    assert rows[("GET", "/openapi-one")].status == "active"


@pytest.mark.asyncio
async def test_reactivates_offline_definition(db, project_id):
    await apply_extracted_apis(
        db, project_id=project_id, apis=[_api("GET", "/x")], extractor="spring_source"
    )
    await apply_extracted_apis(db, project_id=project_id, apis=[], extractor="spring_source")
    await db.commit()
    assert (await _all(db, project_id))[("GET", "/x")].status == "offline"

    stats = await apply_extracted_apis(
        db, project_id=project_id, apis=[_api("GET", "/x")], extractor="spring_source"
    )
    await db.commit()

    assert stats.added == 0
    assert stats.updated == 1
    assert (await _all(db, project_id))[("GET", "/x")].status == "active"


def _init_spring_repo(path):
    """本地 Spring 仓库：src/main/java + @RestController。"""
    from dulwich import porcelain

    path.mkdir(parents=True, exist_ok=True)
    porcelain.init(str(path))
    java = path / "src/main/java/com/demo/UserController.java"
    java.parent.mkdir(parents=True, exist_ok=True)
    java.write_text(
        '@RestController\n@RequestMapping("/api/users")\n'
        "public class UserController {\n"
        '  @GetMapping("/{id}")\n'
        "  public Object get(@PathVariable(\"id\") Long id) { return null; }\n"
        "}\n",
        encoding="utf-8",
    )
    porcelain.add(repo=str(path), paths=["src/main/java/com/demo/UserController.java"])
    porcelain.commit(
        repo=str(path),
        message=b"init",
        author=b"tester <t@example.invalid>",
        committer=b"tester <t@example.invalid>",
    )


@pytest.mark.asyncio
async def test_sync_binding_clones_extracts_and_records(db, project_id, tmp_path):
    from app.models.api_extract import ApiSyncRecord, CodeRepoBinding
    from app.services.api_sync import sync_binding

    src = tmp_path / "origin"
    _init_spring_repo(src)
    work = tmp_path / "work"

    binding = CodeRepoBinding(
        project_id=project_id,
        repo_url=str(src),
        branch="",
        extractor="auto",
        auth_type="https_token",
    )
    db.add(binding)
    await db.commit()
    await db.refresh(binding)

    outcome = await sync_binding(db, binding, repo_dir=str(work))
    assert outcome.status == "success", outcome.error
    assert outcome.extractors == ["spring_source"]
    assert outcome.stats.added == 1
    assert outcome.head_commit

    rows = await _all(db, project_id)
    assert ("GET", "/api/users/{id}") in rows
    assert rows[("GET", "/api/users/{id}")].source_file == (
        "src/main/java/com/demo/UserController.java"
    )
    assert rows[("GET", "/api/users/{id}")].source_line == 4

    records = (await db.execute(select(ApiSyncRecord))).scalars().all()
    assert len(records) == 1
    assert records[0].status == "success"
    assert records[0].head_commit == outcome.head_commit

    # 第二次同步：没有新提交，直接 no_change（不重复解析）
    outcome2 = await sync_binding(db, binding, repo_dir=str(work))
    assert outcome2.status == "no_change"
    assert outcome2.stats.added == 0


@pytest.mark.asyncio
async def test_sync_binding_clone_failure_is_recorded(db, project_id, tmp_path):
    from app.models.api_extract import ApiSyncRecord, CodeRepoBinding
    from app.services.api_sync import sync_binding

    binding = CodeRepoBinding(
        project_id=project_id,
        repo_url=str(tmp_path / "missing"),
        branch="",
        extractor="auto",
    )
    db.add(binding)
    await db.commit()
    await db.refresh(binding)

    outcome = await sync_binding(db, binding, repo_dir=str(tmp_path / "work"))
    assert outcome.status == "failed"
    assert outcome.error

    records = (await db.execute(select(ApiSyncRecord))).scalars().all()
    # 先落 running 记录，失败后追加 failed 记录
    assert records and records[-1].status == "failed"


@pytest.mark.asyncio
async def test_mixed_extractors_keep_per_item_source(db, project_id):
    """auto 模式下 OpenAPI + Spring 混合结果：每条接口保留自己的提取器。"""
    apis = [
        _api("GET", "/orders", "订单", extractor="openapi"),
        _api("GET", "/users", "用户", extractor="spring_source"),
    ]
    await apply_extracted_apis(
        db,
        project_id=project_id,
        apis=apis,
        extractor="openapi",
        offline_extractors={"openapi", "spring_source"},
    )
    await db.commit()

    rows = await _all(db, project_id)
    assert rows[("GET", "/orders")].extractor == "openapi"
    assert rows[("GET", "/users")].extractor == "spring_source"
    assert rows[("GET", "/orders")].source == "openapi3"
    assert rows[("GET", "/users")].source == "spring"


def _commit_file(repo, rel, text, message):
    from dulwich import porcelain
    from pathlib import Path

    p = Path(repo) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    porcelain.add(repo=str(repo), paths=[rel])
    who = b"tester <tester@example.invalid>"
    return porcelain.commit(
        repo=str(repo), message=message.encode(), author=who, committer=who
    ).decode()


@pytest.mark.asyncio
async def test_incremental_sync_only_parses_changed_files(db, project_id, tmp_path):
    from app.models.api_extract import ApiSyncRecord, CodeRepoBinding
    from app.services.api_sync import sync_binding
    from dulwich import porcelain

    src = tmp_path / "origin"
    src.mkdir()
    porcelain.init(str(src))
    _commit_file(
        src,
        "src/main/java/com/demo/A.java",
        '@RestController\n@RequestMapping("/a")\nclass A { @GetMapping("/x") public Object x() { return null; } }\n',
        "init a",
    )
    _commit_file(
        src,
        "src/main/java/com/demo/B.java",
        '@RestController\n@RequestMapping("/b")\nclass B { @GetMapping("/y") public Object y() { return null; } }\n',
        "init b",
    )
    work = tmp_path / "work"
    binding = CodeRepoBinding(
        project_id=project_id, repo_url=str(src), branch="", extractor="auto"
    )
    db.add(binding)
    await db.commit()
    await db.refresh(binding)

    first = await sync_binding(db, binding, repo_dir=str(work))
    assert first.status == "success"
    assert first.stats.added == 2

    # 只改 A.java
    _commit_file(
        src,
        "src/main/java/com/demo/A.java",
        '@RestController\n@RequestMapping("/a")\nclass A { @GetMapping("/x") public Object x() { return null; } @PostMapping("/z") public Object z() { return null; } }\n',
        "change a",
    )
    second = await sync_binding(db, binding, repo_dir=str(work))
    assert second.status == "success"
    assert second.incremental is True
    assert second.changed_files == ["src/main/java/com/demo/A.java"]
    assert second.stats.added == 1  # 只新增 A 的新方法

    rows = await _all(db, project_id)
    assert rows[("GET", "/b/y")].status == "active"  # 未变更文件不受影响
    assert rows[("GET", "/a/x")].status == "active"
    assert rows[("POST", "/a/z")].status == "active"

    record = (
        await db.execute(select(ApiSyncRecord).order_by(ApiSyncRecord.id.desc()).limit(1))
    ).scalar_one()
    assert record.details["incremental"] is True
