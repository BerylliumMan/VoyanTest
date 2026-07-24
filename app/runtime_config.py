"""运行时全局配置（内存级，各模块共享）。

提供 FastAPI 路由（app/）和核心执行引擎（core/）之间的单向读写契约：
- ``app/routers/`` 模块通过 HTTP API 写入。
- ``core/runner/`` 模块在用例执行时读取。

重启后重置为默认值。
"""

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession


class HealingConfig(BaseModel):
    enabled: bool = True
    max_retries: int = 3
    threshold: float = 0.8


healing_config = HealingConfig()


async def get_prompt(db: AsyncSession, key: str, **variables: str) -> str:
    """获取活跃版本的提示词模板并渲染变量。

    Args:
        db: 数据库会话
        key: 提示词模板标识（如 fp_extract, tc_generate）
        variables: 模板变量（如 text=需求文档, fps=功能点列表）

    Returns:
        渲染后的提示词文本。查询失败时返回基础 fallback 提示词。
    """
    from app.crud import get_prompt_template_by_key

    pt = await get_prompt_template_by_key(db, key)
    if pt is None:
        # Fallback: return basic prompt with text
        return "请根据以下内容分析：\n" + str(variables.get("text", ""))

    rendered = pt.content
    for var_name, var_value in variables.items():
        # Support both {{var}} and {var} syntax
        rendered = rendered.replace("{{" + var_name + "}}", str(var_value))
        rendered = rendered.replace("{" + var_name + "}", str(var_value))
    return rendered


async def resolve_prompt_for_agent(
    db: AsyncSession,
    agent_type: str,
    prompt_key: str,
    variables: dict | None = None,
) -> str:
    """解析 Agent 特定提示词，优先使用 AgentDefinition 覆盖，否则回退到 PromptTemplate。

    Args:
        db: 数据库会话
        agent_type: Agent 类型（generation / execution / recording）
        prompt_key: 提示词模板标识（如 fp_extract, tc_generate）
        variables: 模板变量（如 text=需求文档, fps=功能点列表）

    Returns:
        渲染后的提示词文本。
    """
    import logging

    logger = logging.getLogger(__name__)
    from app.crud.agent_definition import get_active_by_type

    vars_dict = variables or {}

    agent_def = await get_active_by_type(db, agent_type)

    if agent_def is None:
        logger.debug(
            "No active AgentDefinition for %s, using default prompt", agent_type
        )
        return await get_prompt(db, prompt_key, **vars_dict)

    skills = agent_def.skills or []
    if skills and prompt_key not in skills:
        logger.warning(
            "Prompt key '%s' not in agent skills list %s for agent_type '%s'",
            prompt_key,
            skills,
            agent_type,
        )

    prompt_overrides = agent_def.prompt_overrides or {}
    if prompt_key in prompt_overrides:
        rendered = prompt_overrides[prompt_key]
        for var_name, var_value in vars_dict.items():
            rendered = rendered.replace("{{" + var_name + "}}", str(var_value))
            rendered = rendered.replace("{" + var_name + "}", str(var_value))
        return rendered

    # Layer 2: agent-level system_prompt
    if agent_def.system_prompt and agent_def.system_prompt.strip():
        rendered = agent_def.system_prompt
        for var_name, var_value in vars_dict.items():
            rendered = rendered.replace("{{" + var_name + "}}", str(var_value))
            rendered = rendered.replace("{" + var_name + "}", str(var_value))
        return rendered

    return await get_prompt(db, prompt_key, **vars_dict)
