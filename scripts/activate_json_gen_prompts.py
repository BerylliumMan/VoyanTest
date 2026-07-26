"""Create and activate JSON-format fp_extract / tc_generate prompt versions."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.gen.prompts import FP_EXTRACT_PROMPT, TC_GENERATE_PROMPT


async def main() -> None:
    from app.database import AsyncSessionLocal, init_db_engine
    from app.crud.prompt_template import (
        activate_prompt_version,
        create_prompt_template,
        get_prompt_template_by_key,
    )

    ok = await init_db_engine()
    if not ok:
        raise RuntimeError("init_db_engine failed; set DATABASE_URL or configure /setup")

    async with AsyncSessionLocal() as db:
        for key, name, content, variables, desc in (
            (
                "fp_extract",
                "功能点提取",
                FP_EXTRACT_PROMPT.strip(),
                [],
                "强制 JSON 输出功能点列表",
            ),
            (
                "tc_generate",
                "测试用例生成",
                TC_GENERATE_PROMPT.strip(),
                ["fp_descriptions", "fps", "csv_header"],
                "强制 JSON 输出测试用例列表",
            ),
        ):
            pt = await create_prompt_template(
                db,
                key=key,
                name=name,
                category="generation",
                content=content,
                variables=variables,
                description=desc,
            )
            await activate_prompt_version(db, key, pt.version)
            active = await get_prompt_template_by_key(db, key)
            has_fp = "functional_points" in active.content
            has_steps = '"steps"' in active.content
            print(f"activated {key} v{active.version} json_fp={has_fp} json_tc={has_steps}")


if __name__ == "__main__":
    asyncio.run(main())
