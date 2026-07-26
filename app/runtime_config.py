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


def render_prompt_variables(template: str, **variables: str) -> str:
    """Replace ``{var}`` / ``{{var}}`` placeholders without interpreting other braces.

    DB-backed generation prompts often embed JSON examples with ``{`` / ``}``;
    ``str.format`` would treat those as fields and raise ``KeyError``.
    """
    rendered = template
    for var_name, var_value in variables.items():
        rendered = rendered.replace("{{" + var_name + "}}", str(var_value))
        rendered = rendered.replace("{" + var_name + "}", str(var_value))
    return rendered


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

    return render_prompt_variables(pt.content, **variables)


async def resolve_prompt_for_agent(
    db: AsyncSession,
    agent_type: str,
    prompt_key: str,
    variables: dict | None = None,
    agent_id: int | None = None,
) -> str:
    """解析 Agent 特定提示词，优先使用 AgentDefinition 覆盖，否则回退到 PromptTemplate。

    Args:
        db: 数据库会话
        agent_type: Agent 类型（generation / execution / recording）
        prompt_key: 提示词模板标识（如 fp_extract, tc_generate, tc_generate_ui）
        variables: 模板变量（如 text=需求文档, fps=功能点列表）
        agent_id: 指定 AgentDefinition.id；未指定则使用该类型当前激活 Agent

    Returns:
        渲染后的提示词文本。
    """
    import logging

    logger = logging.getLogger(__name__)
    from app.crud.agent_definition import get_active_by_type, get_agent_definition

    vars_dict = variables or {}

    agent_def = None
    if agent_id is not None:
        agent_def = await get_agent_definition(db, agent_id)
        if agent_def is None:
            logger.warning("AgentDefinition id=%s not found, falling back to active %s",
                           agent_id, agent_type)
        elif agent_def.agent_type != agent_type:
            logger.warning(
                "AgentDefinition id=%s type=%s != requested %s, using as-is",
                agent_id, agent_def.agent_type, agent_type,
            )

    if agent_def is None:
        agent_def = await get_active_by_type(db, agent_type)

    if agent_def is None:
        logger.debug(
            "No active AgentDefinition for %s, using default prompt", agent_type
        )
        return await get_prompt(db, prompt_key, **vars_dict)

    skills = agent_def.skills or []
    if skills and prompt_key not in skills:
        logger.warning(
            "Prompt key '%s' not in agent skills list %s for agent id=%s type=%s",
            prompt_key,
            skills,
            agent_def.id,
            agent_type,
        )

    prompt_overrides = agent_def.prompt_overrides or {}
    if prompt_key in prompt_overrides:
        skill_prompt = render_prompt_variables(prompt_overrides[prompt_key], **vars_dict)
    else:
        # Skill 模板来自 PromptTemplate；system_prompt 只做角色前缀，不能整段替换
        skill_prompt = await get_prompt(db, prompt_key, **vars_dict)

    system_prompt = (agent_def.system_prompt or "").strip()
    if system_prompt:
        system_prompt = render_prompt_variables(system_prompt, **vars_dict)
        return f"{system_prompt}\n\n{skill_prompt}"
    return skill_prompt
