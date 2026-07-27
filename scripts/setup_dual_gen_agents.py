"""Ensure dual generation agents + tc_generate_ui prompt exist and are wired."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.gen.prompts import (
    FP_EXTRACT_PROMPT,
    TC_GENERATE_PROMPT,
    TC_GENERATE_UI_PROMPT,
)


FUNC_AGENT_NAME = "功能用例生成助手"
UI_AGENT_NAME = "UI自动化用例生成助手"

FUNC_SYSTEM = (
    "你是功能测试用例生成专家，负责将需求文档转为高质量功能测试用例。"
    "工作流程：(1) fp_extract 提取细粒度测试项；(2) tc_generate 按等价类/边界值设计业务场景用例，"
    "每个测试项至少 3 条（正常/异常/边界）。"
)

UI_SYSTEM = (
    "你是 UI 自动化用例生成专家，负责将需求文档转为浏览器可执行的 UI 自动化用例。"
    "工作流程：(1) fp_extract 提取细粒度测试项；(2) tc_generate_ui 输出可操作步骤与可观测预期，"
    "每个测试项至少 3 条，优先主路径与关键异常 UI，避免纯接口/后台逻辑用例。"
)


async def _upsert_prompt(db, key: str, name: str, content: str, variables: list[str], description: str):
    from app.crud.prompt_template import (
        activate_prompt_version,
        create_prompt_template,
        get_prompt_template_by_key,
    )

    active = await get_prompt_template_by_key(db, key)
    if active and active.content.strip() == content.strip():
        print(f"prompt {key} v{active.version} already up to date")
        return active
    pt = await create_prompt_template(
        db,
        key=key,
        name=name,
        category="generation",
        content=content.strip(),
        variables=variables,
        description=description,
    )
    await activate_prompt_version(db, key, pt.version)
    print(f"activated prompt {key} v{pt.version}")
    return pt


async def _upsert_agent(db, *, name: str, description: str, skills: list[str], system_prompt: str, make_active: bool):
    from sqlalchemy import select
    from app import db_models
    from app.models.schemas import AgentDefinitionCreate, AgentDefinitionUpdate
    from app.crud import agent_definition as ad_crud
    from app.agent_resolver import invalidate_agent_cache

    result = await db.execute(
        select(db_models.AgentDefinition).where(db_models.AgentDefinition.name == name)
    )
    existing = result.scalar_one_or_none()
    if existing is None:
        obj = await ad_crud.create_agent_definition(
            db,
            AgentDefinitionCreate(
                name=name,
                agent_type="generation",
                description=description,
                skills=skills,
                llm_config={},
                prompt_overrides={},
                system_prompt=system_prompt,
                is_active=make_active,
            ),
        )
        print(f"created agent id={obj.id} name={name} active={make_active}")
    else:
        obj = await ad_crud.update_agent_definition(
            db,
            existing.id,
            AgentDefinitionUpdate(
                description=description,
                skills=skills,
                system_prompt=system_prompt,
                is_active=make_active,
            ),
        )
        print(f"updated agent id={obj.id} name={name} active={make_active}")
    invalidate_agent_cache("generation")
    return obj


async def main() -> None:
    from app.database import AsyncSessionLocal, init_db_engine

    ok = await init_db_engine()
    if not ok:
        raise RuntimeError("init_db_engine failed")

    async with AsyncSessionLocal() as db:
        await _upsert_prompt(
            db, "fp_extract", "功能点提取", FP_EXTRACT_PROMPT, [], "强制 JSON 输出功能点列表",
        )
        await _upsert_prompt(
            db, "tc_generate", "功能用例生成", TC_GENERATE_PROMPT,
            ["fp_descriptions", "fps", "csv_header"], "功能测试用例 JSON",
        )
        await _upsert_prompt(
            db, "tc_generate_ui", "UI自动化用例生成", TC_GENERATE_UI_PROMPT,
            ["fp_descriptions", "fps", "csv_header"], "UI 自动化用例 JSON",
        )

        # Both generation agents stay active; Gen page picks by agent_id
        await _upsert_agent(
            db,
            name=FUNC_AGENT_NAME,
            description="生成业务功能测试用例（等价类/边界/异常场景）",
            skills=["fp_extract", "tc_generate"],
            system_prompt=FUNC_SYSTEM,
            make_active=True,
        )
        await _upsert_agent(
            db,
            name=UI_AGENT_NAME,
            description="生成浏览器可执行的 UI 自动化用例",
            skills=["fp_extract", "tc_generate_ui"],
            system_prompt=UI_SYSTEM,
            make_active=True,
        )

        # Rename legacy "用例生成助手" if present and keep as functional alias inactive
        from sqlalchemy import select
        from app import db_models
        from app.models.schemas import AgentDefinitionUpdate
        from app.crud import agent_definition as ad_crud

        result = await db.execute(
            select(db_models.AgentDefinition).where(
                db_models.AgentDefinition.name == "用例生成助手"
            )
        )
        legacy = result.scalar_one_or_none()
        if legacy and legacy.name != FUNC_AGENT_NAME:
            await ad_crud.update_agent_definition(
                db,
                legacy.id,
                AgentDefinitionUpdate(
                    skills=["fp_extract", "tc_generate"],
                    system_prompt=FUNC_SYSTEM,
                    description="（旧）功能用例生成，建议使用「功能用例生成助手」",
                    is_active=False,
                ),
            )
            print(f"deactivated legacy agent id={legacy.id} name=用例生成助手")

    print("DONE")


if __name__ == "__main__":
    asyncio.run(main())
