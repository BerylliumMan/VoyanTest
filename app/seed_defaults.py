"""Default AgentDefinition + PromptTemplate seeds for fresh deployments.

Synced from the production/reference environment so new installs get the
same agent structure, system prompts, and active skill templates.
"""
from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.gen.prompts import (
    FP_EXTRACT_PROMPT,
    FP_EXTRACT_FLOW_PROMPT,
    TC_GENERATE_PROMPT,
    TC_GENERATE_UI_PROMPT,
    TC_GENERATE_FLOW_PROMPT,
)

logger = logging.getLogger(__name__)

OPERATION_TRANSLATE_PROMPT = "你是一个浏览器自动化操作翻译器。将自然语言测试步骤翻译为精确的浏览器操作指令。\n\n【可用操作类型】\nclick：点击元素（按钮/链接/菜单项）。\nfill：向输入框填充文本（非 type，直接 set value）。\ntype：逐字键盘输入（用于触发实时搜索/自动补全）。\nselect：下拉选择框选取选项（按 value 或 label）。\nhover：鼠标悬停在元素上（触发 tooltip/下拉菜单）。\nscroll：滚动页面或元素内部滚动条（参数：x, y 或 selector）。\npress_key：按下键盘按键（如 Enter、Escape、Tab、ArrowDown）。\ngoto：导航到指定URL。\nwait：等待指定条件（参数：selector | ms | url_contains）。\nassert：验证断言（子类型：url_contains | element_visible | text_present | element_count | input_value）。\n\n【输出格式 — 严格JSON】\n{\n  \"action\": \"操作类型（click | fill | type | select | hover | scroll | press_key | goto | wait | assert）\",\n  \"selector\": \"CSS/XPath选择器，复合操作可为null\",\n  \"value\": \"操作参数（填充文本、URL、按键名等），无参数时为null\",\n  \"options\": {\"可选的操作选项\": \"值\"},\n  \"confidence\": 0.0-1.0（对本次翻译的置信度）,\n  \"fallback_actions\": [\n    {\n      \"action\": \"后备操作类型\",\n      \"selector\": \"后备选择器（更宽泛匹配）\",\n      \"value\": \"后备参数\",\n      \"reason\": \"触发该后备策略的原因\"\n    }\n  ]\n}\n\n【边界与异常处理规则】\n- 超时：所有等待操作默认超时30秒（wait时添加 options.timeout=30000ms）。\n- 弹窗/对话框：翻译前先判断步骤是否可能触发弹窗，若可能则添加 wait:selector=弹窗关闭按钮 或 press_key:Escape 作为 fallback。\n- 元素过时重试（stale element）：遇到动态刷新列表时，优先用文本匹配而非索引选择器（如 text=\"确定\" 而非 nth=0）。\n- iframe 感知：如果步骤涉及 iframe 内元素，selector 添加 frame_locator 前缀标注。\n- 动态加载：涉及异步加载内容时，操作前自动插入 wait:selector 等待目标元素出现。\n\n【示例 — 成功翻译】\n步骤：\"在搜索框输入手机，然后点击搜索按钮\"\nURL：https://shop.example.com\n输出：[\n  {\"action\":\"fill\",\"selector\":\"input[placeholder*='搜索']\",\"value\":\"手机\",\"options\":null,\"confidence\":0.95,\"fallback_actions\":[{\"action\":\"fill\",\"selector\":\"input[type='search']\",\"value\":\"手机\",\"reason\":\"备选搜索框选择器\"}]},\n  {\"action\":\"wait\",\"selector\":null,\"value\":null,\"options\":{\"ms\":500},\"confidence\":0.8,\"fallback_actions\":[]},\n  {\"action\":\"click\",\"selector\":\"button:has-text('搜索')\",\"value\":null,\"options\":null,\"confidence\":0.9,\"fallback_actions\":[{\"action\":\"press_key\",\"selector\":null,\"value\":\"Enter\",\"reason\":\"直接按回车触发搜索\"}]}\n]\n\n【示例 — 失败处理（元素定位失败）】\n步骤：\"点击页面顶部的优惠券弹窗关闭按钮\"\nURL：https://shop.example.com/products\n输出：[\n  {\"action\":\"wait\",\"selector\":\"[class*='coupon'], [class*='popup'], [class*='modal']\",\"value\":null,\"options\":{\"timeout\":5000},\"confidence\":0.7,\"fallback_actions\":[]},\n  {\"action\":\"click\",\"selector\":\"[class*='coupon'] [class*='close'], [class*='popup'] button[class*='close'], .modal .close-btn\",\"value\":null,\"options\":null,\"confidence\":0.55,\"fallback_actions\":[{\"action\":\"press_key\",\"selector\":null,\"value\":\"Escape\",\"reason\":\"弹窗关闭按钮未找到，尝试Esc键关闭\"},{\"action\":\"click\",\"selector\":\"body\",\"value\":null,\"reason\":\"最后尝试点击页面空白区域关闭弹窗\"}]}\n]\n\n用户步骤：\n{step}\n\n页面 URL：\n{url}"

VERIFY_EXPECTED_PROMPT = "你是一个测试结果验证专家。请按三级验证策略判断预期结果是否达成。\n\n【三级验证策略 — 按优先级递减尝试】\n第1级「精确匹配」：预期值与实际值字面一致（如\"页面标题为首页\" → 实际标题=\"首页\"）。最高置信度。\n第2级「语义匹配」：预期描述与实际含义等价但表述不同（如\"显示用户名\" → 实际显示\"欢迎回来，张三\"）。检查核心关键词和语义。\n第3级「存在性检测」：仅验证某元素/文本是否存在（如\"出现错误提示\" → 页面存在包含\"错误\"的文本）。最低置信度，仅用于宽泛断言。\n\n【容忍规则 — 以下情况不算失败】\n- 时间戳/日期：预期中带有\"当前时间\"→接受任意合法时间字符串。如\"登录时间：YYYY-MM-DD HH:mm:ss\"。\n- 动态ID/Token：\"order_id=ABC123\" 实际显示 \"order_id=XYZ789\" → 仅比对格式，不比对具体值。\n- 数字范围：\"约100条记录\" 实际 98条 → 容差 ±5% 内视为通过。\n- 异步加载：页面仍在渲染中，给出结论时标注\"页面可能未完全加载\"并降级置信度。\n\n【证据链格式】\n输出时必须引用DOM快照中的具体行号作为证据：\n- 若从DOM快照验证：标注\"见DOM行N：<原文>\"。\n- 若从截图验证：标注\"截图显示XXX区域存在/不存在目标内容\"。\n- 不可凭空断言真实性，必须绑定到具体观测。\n\n【输出格式】\n{\n  \"verdict\": \"pass | fail | partial\",\n  \"confidence\": 0.0-1.0,\n  \"matched_level\": \"exact | semantic | presence\",\n  \"reason\": \"验证结论的中文说明（引用具体证据）\",\n  \"evidence\": [\"证据1：见DOM行15 — 页面标题为\\\"首页\\\"\", \"证据2：见DOM行23 — 用户名span包含\\\"张三\\\"\"]\n}\n\n【中文验证示例1 — 精确匹配通过】\n操作：goto https://example.com\n预期：\"页面标题显示为'示例网站首页'\"\nDOM快照：第5行 <title>示例网站首页</title>\n输出：{\"verdict\":\"pass\",\"confidence\":0.98,\"matched_level\":\"exact\",\"reason\":\"页面标题与预期完全一致\",\"evidence\":[\"见DOM行5：<title>示例网站首页</title>\"]}\n\n【中文验证示例2 — 语义匹配通过】\n操作：click 登录按钮后 fill 用户名\n预期：\"登录成功后右上角显示用户名\"\nDOM快照：第12行 <span class=\"user-name\">欢迎，admin@test.com</span>\n输出：{\"verdict\":\"pass\",\"confidence\":0.85,\"matched_level\":\"semantic\",\"reason\":\"用户名admin@test.com出现在右上角用户信息区域，语义符合\\\"显示用户名\\\"\",\"evidence\":[\"见DOM行12：<span class=\\\"user-name\\\">欢迎，admin@test.com</span>\"]}\n\n【中文验证示例3 — 存在性检测失败】\n操作：提交空表单\n预期：\"用户名输入框下方出现'必填'红色提示\"\nDOM快照：无任何包含\"必填\"的文本节点，input标签无aria-invalid属性\n输出：{\"verdict\":\"fail\",\"confidence\":0.92,\"matched_level\":\"presence\",\"reason\":\"DOM中未找到\\\"必填\\\"提示文本，input元素缺少表单校验标记\",\"evidence\":[\"遍历全部DOM文本节点，未匹配到\\\"必填\\\"关键词\",\"input元素未设置aria-invalid=\\\"true\\\"属性\"]}\n\n【中文验证示例4 — 时间戳容忍通过】\n操作：创建订单后查看订单详情\n预期：\"创建时间显示为当前时间\"\nDOM快照：第45行 <span class=\"create-time\">2026-07-24 15:32:18</span>\n输出：{\"verdict\":\"pass\",\"confidence\":0.78,\"matched_level\":\"semantic\",\"reason\":\"订单创建时间格式正确，符合当前时间上下文（容忍规则-时间戳）\",\"evidence\":[\"见DOM行45：时间格式YYYY-MM-DD HH:mm:ss正确\"]}\n\n操作：\n{action}\n\n预期结果：\n{expected}"

DEFAULT_AGENTS: list[dict] = [
    {
        "name": "录制助手",
        "agent_type": "recording",
        "description": "CDP 录制引擎 — 录制用户在浏览器中的操作并生成测试步骤",
        "skills": [
            "cdp_convert"
        ],
        "llm_config": {"temperature": 0.1, "max_tokens": 4096},
        "is_active": 1,
        "tools": [],
        "goal": "录制用户在浏览器中的操作，将其转换为可复用的测试步骤",
        "constraints": [
            {
                "key": "max_recording_minutes",
                "value": "60"
            }
        ],
        "thinking_config": {
            "budget": 2000,
            "strategy": "auto"
        },
        "system_prompt": "你是录制事件处理引擎，负责将CDP浏览器录制事件转换为结构化测试步骤。处理流程：(1) 事件聚合：将连续的CDP原始事件（mousemove、scroll、input等）聚合成有意义的操作单元；(2) 语义推断：根据事件序列推断用户意图，如click+input序列识别为表单填写，多步导航序列合并为页面跳转；(3) 步骤生成：将聚合后的操作单元转换为标准测试步骤格式，包含操作类型（click/type/navigate/scroll）、目标元素、参数值和预期结果。关键规则：连续input事件合并为单次type操作；点击前的hover/mousemove忽略；跳转后等待事件合并为navigate；窗口resize单独记录。",
        "prompt_overrides": {}
    },
    {
        "name": "智能执行引擎",
        "agent_type": "execution",
        "description": "AI 驱动测试执行 — 自主观察页面、决策操作、验证结果",
        "skills": [
            "verify_expected",
            "step_execute",
            "operation_translate",
            "execution_system"
        ],
        "llm_config": {"temperature": 0.1, "max_tokens": 4096},
        "is_active": 1,
        "tools": [
            {
                "name": "browser_navigate",
                "description": "导航到指定URL"
            },
            {
                "name": "browser_click",
                "description": "点击元素"
            },
            {
                "name": "browser_type",
                "description": "输入文本"
            },
            {
                "name": "browser_snapshot",
                "description": "获取页面快照"
            },
            {
                "name": "browser_take_screenshot",
                "description": "截图"
            },
            {
                "name": "browser_wait_for",
                "description": "等待条件出现"
            }
        ],
        "goal": "在浏览器中自主执行测试用例并验证结果",
        "constraints": [
            {
                "key": "max_turns",
                "value": "50"
            },
            {
                "key": "timeout_ms",
                "value": "30000"
            }
        ],
        "thinking_config": {
            "model": "",
            "budget": 64000,
            "strategy": "always"
        },
        "system_prompt": "你是自主Web执行引擎，已连接远程浏览器。执行遵循Observe→Think→Act（OTA）循环。决策规则：(1) 首轮直接使用browser_navigate访问目标URL，不询问用户地址；(2) 用browser_snapshot获取页面URL和标题，判断当前位置；(3) 根据测试步骤决定操作——输入用browser_type，点击用browser_click（优先可见文本/aria-label，其次CSS选择器），确认跳转用browser_snapshot，截图用browser_take_screenshot，等待元素用browser_wait_for_selector；(4) 每步后用browser_snapshot自我验证；(5) 失败处理：尝试替代方案（如click聚焦后再输入），最多重试2次；(6) 全部步骤完成且通过时done=True，连续失败超3轮则done=True并报错。",
        "prompt_overrides": {}
    },
    {
        "name": "功能用例生成助手",
        "agent_type": "generation",
        "description": "生成业务功能测试用例（等价类/边界/异常场景）",
        "skills": [
            "fp_extract",
            "tc_generate"
        ],
        "llm_config": {},
        "is_active": 1,
        "tools": [],
        "goal": "",
        "constraints": [],
        "thinking_config": {},
        "system_prompt": "你是功能测试用例生成专家。流程：(1) fp_extract 按章节细粒度提取测试项；(2) tc_generate 按等价类/边界值设计用例，每个测试项至少 3 条（正常/异常/边界），输出带 fp_name 与 scenario_type。",
        "prompt_overrides": {}
    },
    {
        "name": "UI自动化用例生成助手",
        "agent_type": "generation",
        "description": "生成浏览器可执行的 UI 自动化用例",
        "skills": [
            "fp_extract",
            "tc_generate_ui"
        ],
        "llm_config": {},
        "is_active": 1,
        "tools": [],
        "goal": "",
        "constraints": [],
        "thinking_config": {},
        "system_prompt": "你是 UI 自动化用例生成专家。流程：(1) fp_extract 提取细粒度测试项；(2) tc_generate_ui 输出可操作步骤与可观测预期，每个测试项至少 3 条，优先主路径与关键异常 UI，避免纯接口/后台逻辑。",
        "prompt_overrides": {}
    },
    {
        "name": "流程手册用例生成助手",
        "agent_type": "generation",
        "description": "按操作手册/流程图文忠实生成 UI 可执行用例（不强制异常边界）",
        "skills": [
            "fp_extract_flow",
            "tc_generate_flow"
        ],
        "llm_config": {},
        "is_active": 0,
        "tools": [],
        "goal": "忠实还原操作手册中的主路径为可执行 UI 步骤",
        "constraints": [],
        "thinking_config": {},
        "system_prompt": "你是流程手册用例生成专家。流程：(1) fp_extract_flow 从图文手册抽取操作流程，必须结合截图红框/色框/高亮读出操作目标；(2) tc_generate_flow 为每个流程生成 1 条文档主路径 UI 用例，生成时须再次对照手册截图与框选区域。禁止脱离文档臆造异常/边界；步骤与控件文案必须来自原文或截图框选。",
        "prompt_overrides": {}
    }
]


def get_seed_prompts() -> dict[str, dict]:
    """All skill prompt templates to seed (key → metadata + content)."""
    from core.cdp_converter import CDP_TO_STEPS_PROMPT

    return {
        "fp_extract": {
            "name": "测试项提取",
            "category": "generation",
            "content": FP_EXTRACT_PROMPT.strip(),
            "variables": [],
            "description": "强制 JSON 输出细粒度测试项列表",
        },
        "fp_extract_flow": {
            "name": "流程手册提取",
            "category": "generation",
            "content": FP_EXTRACT_FLOW_PROMPT.strip(),
            "variables": [],
            "description": "从操作手册图文抽取操作流程单元（非异常边界密度）",
        },
        "tc_generate": {
            "name": "功能用例生成",
            "category": "generation",
            "content": TC_GENERATE_PROMPT.strip(),
            "variables": ["fp_descriptions", "fps", "csv_header"],
            "description": "功能测试用例 JSON（每测试项至少 3 条）",
        },
        "tc_generate_ui": {
            "name": "UI自动化用例生成",
            "category": "generation",
            "content": TC_GENERATE_UI_PROMPT.strip(),
            "variables": ["fp_descriptions", "fps", "csv_header"],
            "description": "UI 自动化用例 JSON（每测试项至少 3 条）",
        },
        "tc_generate_flow": {
            "name": "流程手册UI用例生成",
            "category": "generation",
            "content": TC_GENERATE_FLOW_PROMPT.strip(),
            "variables": ["fp_descriptions", "fps", "csv_header"],
            "description": "按手册主路径生成 UI 用例（每流程通常 1 条）",
        },
        "operation_translate": {
            "name": "操作指令翻译",
            "category": "execution",
            "content": OPERATION_TRANSLATE_PROMPT.strip(),
            "variables": ["step", "url"],
            "description": "将自然语言步骤转换为 MCP 浏览器操作",
        },
        "cdp_convert": {
            "name": "录制事件转步骤",
            "category": "recording",
            "content": CDP_TO_STEPS_PROMPT.strip(),
            "variables": [],
            "description": "将 CDP 录制事件时间线转换为自然语言测试步骤 JSON",
        },
        "verify_expected": {
            "name": "预期结果验证",
            "category": "verification",
            "content": VERIFY_EXPECTED_PROMPT.strip(),
            "variables": ["action", "expected"],
            "description": "验证测试步骤的预期结果",
        },
    }


async def seed_prompt_templates(db: AsyncSession) -> int:
    """Insert missing prompt templates (active v1). Returns number created."""
    from app import db_models

    created = 0
    for key, meta in get_seed_prompts().items():
        existing = await db.execute(
            select(db_models.PromptTemplate)
            .where(db_models.PromptTemplate.key == key)
            .limit(1)
        )
        if existing.scalar_one_or_none():
            continue
        db.add(
            db_models.PromptTemplate(
                key=key,
                name=meta["name"],
                category=meta["category"],
                content=meta["content"],
                variables=meta["variables"],
                version=1,
                is_active=True,
                description=meta["description"],
            )
        )
        created += 1
    return created


async def sync_prompt_templates_from_seed(
    db: AsyncSession,
    *,
    activate: bool = False,
    keys: set[str] | None = None,
) -> int:
    """Push seed prompt bodies into DB.

    When ``activate`` is False (startup default): if active content differs from
    seed, create a **new inactive** version so user edits stay active.
    When ``activate`` is True (explicit admin action): create+activate seed.
    ``keys`` limits which prompt keys are considered (None = all seed keys).
    """
    from app.crud.prompt_template import (
        activate_prompt_version,
        create_prompt_template,
        get_prompt_template_by_key,
    )

    updated = 0
    for key, meta in get_seed_prompts().items():
        if keys is not None and key not in keys:
            continue
        active = await get_prompt_template_by_key(db, key)
        desired = (meta["content"] or "").strip()
        if active and (active.content or "").strip() == desired:
            continue
        if active is None:
            continue
        pt = await create_prompt_template(
            db,
            key=key,
            name=meta["name"],
            category=meta["category"],
            content=desired,
            variables=meta["variables"],
            description=meta["description"],
        )
        if activate:
            await activate_prompt_version(db, key, pt.version)
            logger.info("提示词 %s 已同步并激活 seed v%d", key, pt.version)
        else:
            logger.info(
                "提示词 %s 已写入 seed 草稿 v%d（未激活，保留用户活跃版）",
                key, pt.version,
            )
        updated += 1
    return updated


# Product-owned flow-manual prompts: auto-activate seed upgrades on startup.
_FLOW_PROMPT_KEYS = frozenset({"fp_extract_flow", "tc_generate_flow"})
_FLOW_AGENT_NAME = "流程手册用例生成助手"


async def ensure_flow_agent_system_prompt(db: AsyncSession) -> bool:
    """Keep flow-manual Agent system_prompt aligned with seed (idempotent)."""
    from app import db_models

    seed = next((a for a in DEFAULT_AGENTS if a.get("name") == _FLOW_AGENT_NAME), None)
    if not seed:
        return False
    desired = (seed.get("system_prompt") or "").strip()
    if not desired:
        return False
    row = await db.execute(
        select(db_models.AgentDefinition)
        .where(db_models.AgentDefinition.name == _FLOW_AGENT_NAME)
        .limit(1)
    )
    agent = row.scalar_one_or_none()
    if agent is None:
        return False
    if (agent.system_prompt or "").strip() == desired:
        return False
    agent.system_prompt = desired
    logger.info("已更新 Agent「%s」system_prompt（图文/框选说明）", _FLOW_AGENT_NAME)
    return True


async def seed_default_agents(db: AsyncSession) -> int:
    """Insert default AgentDefinitions when the table is empty. Returns number created."""
    from app import db_models

    agent_count = await db.execute(select(func.count()).select_from(db_models.AgentDefinition))
    if agent_count.scalar() != 0:
        return 0
    for agent_data in DEFAULT_AGENTS:
        db.add(db_models.AgentDefinition(**agent_data))
    return len(DEFAULT_AGENTS)


async def ensure_named_seed_agents(db: AsyncSession) -> int:
    """Idempotently insert seed Agents missing by name (for existing DBs).

    Does not activate new agents (is_active from seed data, flow helper is 0).
    """
    from app import db_models

    created = 0
    for agent_data in DEFAULT_AGENTS:
        name = agent_data.get("name") or ""
        if not name:
            continue
        existing = await db.execute(
            select(db_models.AgentDefinition)
            .where(db_models.AgentDefinition.name == name)
            .limit(1)
        )
        if existing.scalar_one_or_none():
            continue
        db.add(db_models.AgentDefinition(**agent_data))
        created += 1
        logger.info("已补种缺失 Agent: %s (is_active=%s)", name, agent_data.get("is_active"))
    return created


async def seed_defaults(db: AsyncSession) -> None:
    """Seed prompts + agents for a fresh database (idempotent).

    Startup only inserts **missing** prompt keys; does not activate seed over
    user-edited active templates. Use sync_prompt_templates_from_seed(activate=True)
    for an explicit upgrade. Also upserts missing Agents by name.
    """
    n_prompts = await seed_prompt_templates(db)
    non_flow_keys = set(get_seed_prompts()) - _FLOW_PROMPT_KEYS
    n_drafts = await sync_prompt_templates_from_seed(
        db, activate=False, keys=non_flow_keys,
    )
    # Flow-manual prompts are product-owned; activate seed upgrades so vision
    # instructions (boxed UI regions) take effect without admin UI step.
    n_flow = await sync_prompt_templates_from_seed(
        db, activate=True, keys=_FLOW_PROMPT_KEYS,
    )
    n_agents = await seed_default_agents(db)
    n_named = await ensure_named_seed_agents(db)
    flow_sp = await ensure_flow_agent_system_prompt(db)
    if n_prompts or n_drafts or n_flow or n_agents or n_named or flow_sp:
        await db.commit()
        if n_prompts:
            logger.info("已创建 %d 个默认提示词模板", n_prompts)
        if n_drafts:
            logger.info("已写入 %d 个种子提示词草稿（未覆盖活跃版）", n_drafts)
        if n_flow:
            logger.info("已激活 %d 个流程手册提示词种子版", n_flow)
        if n_agents:
            logger.info("已创建 %d 个默认 AI Agent", n_agents)
        if n_named:
            logger.info("已按名称补种 %d 个 AI Agent", n_named)
