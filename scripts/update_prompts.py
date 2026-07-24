#!/usr/bin/env python3
"""
更新数据库中的 PromptTemplate 版本和 AgentDefinition 系统提示词。

功能：
1. 确保 prompt_templates / agent_definitions 表 schema 正确（自动迁移旧表结构）
2. 从 app/main.py 解析 4 个 PromptTemplate 的种子内容
3. 为每个 key 创建新版本（version 递增），设置 is_active=True，旧版本 is_active=False
4. 更新 3 个 AgentDefinition（id=15/16/17）的 system_prompt
5. 幂等：多次运行结果一致

用法：
    python scripts/update_prompts.py
    python scripts/update_prompts.py --db-url "postgresql+asyncpg://user:pass@host/db"
    python scripts/update_prompts.py --dry-run
"""

import argparse
import asyncio
import ast
import json
import logging
import os
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker


# ── 路径解析 ──────────────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
DB_CONFIG_FILE = os.path.join(PROJECT_ROOT, "data", ".db_config.json")

# ── 日志 ──────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
)
logger = logging.getLogger("update_prompts")


# ═════════════════════════════════════════════════════════════════════════════
# AgentDefinition 的新 system_prompt
# ═════════════════════════════════════════════════════════════════════════════

NEW_AGENT_SYSTEM_PROMPTS: dict[int, str] = {
    15: (  # recording
        "你是录制事件处理引擎，负责将CDP浏览器录制事件转换为结构化测试步骤。"
        "处理流程：(1) 事件聚合：将连续的CDP原始事件（mousemove、scroll、input等）聚合成有意义的操作单元；"
        "(2) 语义推断：根据事件序列推断用户意图，如click+input序列识别为表单填写，多步导航序列合并为页面跳转；"
        "(3) 步骤生成：将聚合后的操作单元转换为标准测试步骤格式，包含操作类型（click/type/navigate/scroll）、目标元素、参数值和预期结果。"
        "关键规则：连续input事件合并为单次type操作；点击前的hover/mousemove忽略；跳转后等待事件合并为navigate；窗口resize单独记录。"
    ),
    16: (  # generation
        "你是测试用例生成专家，负责将用户需求文档转换为高质量结构化测试用例。"
        "工作流程：(1) fp_extract阶段：分析需求文本，提取完整功能点列表，每个功能点包含所属模块、功能名称、分类、功能描述和优先级（P0/P1/P2）；"
        "(2) tc_generate阶段：基于功能点列表逐一生成可执行测试用例，覆盖正向流程、异常场景和边界值。"
        "质量要求：功能点需全面覆盖需求内容，不遗漏任何功能；测试用例步骤必须可执行，包含预期URL、元素定位方式、具体操作和预期结果；"
        "测试策略优先遵循等价类划分>边界值分析>错误推测法。"
        "输出格式：功能点JSON数组和测试用例JSON数组。"
    ),
    17: (  # execution
        "你是自主Web执行引擎，已连接远程浏览器。"
        "执行遵循Observe→Think→Act（OTA）循环。"
        "决策规则：(1) 首轮直接使用browser_navigate访问目标URL，不询问用户地址；"
        "(2) 用browser_snapshot获取页面URL和标题，判断当前位置；"
        "(3) 根据测试步骤决定操作——输入用browser_type，点击用browser_click（优先可见文本/aria-label，其次CSS选择器），"
        "确认跳转用browser_snapshot，截图用browser_take_screenshot，等待元素用browser_wait_for_selector；"
        "(4) 每步后用browser_snapshot自我验证；"
        "(5) 失败处理：尝试替代方案（如click聚焦后再输入），最多重试2次；"
        "(6) 全部步骤完成且通过时done=True，连续失败超3轮则done=True并报错。"
    ),
}


# ═════════════════════════════════════════════════════════════════════════════
# 数据库连接解析
# ═════════════════════════════════════════════════════════════════════════════

def resolve_database_url(cli_url: str | None = None) -> str | None:
    """按优先级解析数据库 URL。

    优先级：CLI 参数 --db-url > 环境变量 DATABASE_URL > data/.db_config.json
    """
    if cli_url:
        return cli_url
    env_url = os.getenv("DATABASE_URL")
    if env_url:
        return env_url
    if os.path.exists(DB_CONFIG_FILE):
        try:
            with open(DB_CONFIG_FILE) as f:
                cfg = json.load(f)
                url = cfg.get("database_url")
                if url:
                    return url
        except Exception:
            logger.debug("读取 %s 失败", DB_CONFIG_FILE)
    return None


def mask_url(url: str) -> str:
    """隐藏数据库 URL 中的密码部分。"""
    if "@" in url:
        return url.split("://")[0] + "://***@" + url.split("@")[-1]
    return url


# ═════════════════════════════════════════════════════════════════════════════
# 从 app/main.py 解析种子内容
# ═════════════════════════════════════════════════════════════════════════════

def parse_seed_prompts(main_file_path: str) -> dict[str, str]:
    """从 app/main.py 的 _run_startup_init 函数中解析 PromptTemplate 种子内容。

    遍历 AST，找到所有 PromptTemplate(...) 调用，提取 key 和 content 参数。
    返回 {key: content} 的字典。
    """
    with open(main_file_path, encoding="utf-8") as f:
        tree = ast.parse(f.read())

    prompts: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func_name: str | None = None
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
        if func_name != "PromptTemplate":
            continue

        kwargs: dict[str, object] = {}
        for kw in node.keywords:
            if kw.arg not in ("key", "content"):
                continue
            try:
                kwargs[kw.arg] = ast.literal_eval(kw.value)
            except (ValueError, TypeError):
                logger.warning("无法解析 PromptTemplate 的 %s 参数值", kw.arg)
                continue

        if "key" in kwargs and "content" in kwargs:
            key = kwargs["key"]
            content = kwargs["content"]
            if isinstance(key, str) and isinstance(content, str):
                prompts[key] = content

    return prompts


# ═════════════════════════════════════════════════════════════════════════════
# Schema 迁移 — 确保 prompt_templates / agent_definitions 使用最新 schema
# ═════════════════════════════════════════════════════════════════════════════

async def _column_exists(session: AsyncSession, table: str, column: str) -> bool:
    """检查指定表的列是否存在。"""
    result = await session.execute(
        text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :t AND column_name = :c"
        ),
        {"t": table, "c": column},
    )
    return result.fetchone() is not None


async def _table_exists(session: AsyncSession, table: str) -> bool:
    """检查指定表是否存在。"""
    result = await session.execute(
        text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = :t"
        ),
        {"t": table},
    )
    return result.fetchone() is not None


async def _safe_execute(session: AsyncSession, sql: str, description: str) -> bool:
    """安全执行 DDL 语句，忽略预期错误。返回是否成功。"""
    try:
        await session.execute(text(sql))
        logger.info("  ✓ %s", description)
        return True
    except Exception as exc:
        # 忽略列已存在、约束已存在等正常情况
        err_msg = str(exc)
        if any(kw in err_msg.lower() for kw in (
            "already exists", "duplicate", "does not exist",
            "cannot drop", "cannot alter", "is not a table",
        )):
            logger.debug("  - %s（已存在，跳过）", description)
            return False
        logger.warning("  ✗ %s：%s", description, err_msg)
        return False


async def ensure_prompt_templates_schema(session: AsyncSession) -> None:
    """确保 prompt_templates 表拥有最新的 schema 列。

    兼容从旧 schema（template_key/label/template_content）迁移到新 schema
    （key/name/content + version/is_active/variables/category/description/created_at）。
    所有操作均使用 IF NOT EXISTS / DROP NOT NULL，确保幂等。
    """
    logger.info("── 检查 prompt_templates 表结构 ──")

    has_key = await _column_exists(session, "prompt_templates", "key")
    is_old_schema = not has_key

    if is_old_schema:
        logger.info("检测到旧 schema，开始迁移 prompt_templates 表结构…")

    # 添加新 schema 所需的所有列（IF NOT EXISTS 确保幂等）
    await _safe_execute(
        session,
        "ALTER TABLE prompt_templates ADD COLUMN IF NOT EXISTS key VARCHAR(100)",
        "添加列: key",
    )
    await _safe_execute(
        session,
        "ALTER TABLE prompt_templates ADD COLUMN IF NOT EXISTS name VARCHAR(200)",
        "添加列: name",
    )
    await _safe_execute(
        session,
        "ALTER TABLE prompt_templates ADD COLUMN IF NOT EXISTS category VARCHAR(50)",
        "添加列: category",
    )
    await _safe_execute(
        session,
        "ALTER TABLE prompt_templates ADD COLUMN IF NOT EXISTS content TEXT",
        "添加列: content",
    )
    await _safe_execute(
        session,
        "ALTER TABLE prompt_templates ADD COLUMN IF NOT EXISTS variables JSONB",
        "添加列: variables",
    )
    await _safe_execute(
        session,
        "ALTER TABLE prompt_templates ADD COLUMN IF NOT EXISTS version INTEGER",
        "添加列: version",
    )
    await _safe_execute(
        session,
        "ALTER TABLE prompt_templates ADD COLUMN IF NOT EXISTS is_active BOOLEAN",
        "添加列: is_active",
    )
    await _safe_execute(
        session,
        "ALTER TABLE prompt_templates ADD COLUMN IF NOT EXISTS description TEXT",
        "添加列: description",
    )
    await _safe_execute(
        session,
        "ALTER TABLE prompt_templates ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ",
        "添加列: created_at",
    )

    if is_old_schema:
        # 迁移旧列数据到新列
        await session.execute(text(
            "UPDATE prompt_templates SET key = template_key WHERE key IS NULL"
        ))
        await session.execute(text(
            "UPDATE prompt_templates SET name = label WHERE name IS NULL"
        ))
        await session.execute(text(
            "UPDATE prompt_templates SET content = template_content WHERE content IS NULL"
        ))
        await session.execute(text(
            "UPDATE prompt_templates SET category = 'generation' WHERE category IS NULL"
        ))
        await session.execute(text(
            "UPDATE prompt_templates SET variables = '[]'::jsonb WHERE variables IS NULL"
        ))
        await session.execute(text(
            "UPDATE prompt_templates SET version = 1 WHERE version IS NULL"
        ))
        await session.execute(text(
            "UPDATE prompt_templates SET is_active = true WHERE is_active IS NULL"
        ))
        await session.execute(text(
            "UPDATE prompt_templates SET created_at = COALESCE(updated_at, NOW()) WHERE created_at IS NULL"
        ))
        logger.info("  ✓ 旧列数据已迁移到新列")

    # 移除旧列的 NOT NULL 约束（新插入行不再填充这些列，必须解除约束）
    for old_col in ("template_key", "label", "template_content", "is_custom"):
        if await _column_exists(session, "prompt_templates", old_col):
            await _safe_execute(
                session,
                f"ALTER TABLE prompt_templates ALTER COLUMN {old_col} DROP NOT NULL",
                f"移除 NOT NULL: {old_col}",
            )

    # 确保 updated_at 列存在（旧 schema 提供，新 schema 也需要）
    if not await _column_exists(session, "prompt_templates", "updated_at"):
        await _safe_execute(
            session,
            "ALTER TABLE prompt_templates ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW()",
            "添加列: updated_at",
        )

    # 添加唯一约束（忽略重复错误）
    await _safe_execute(
        session,
        "ALTER TABLE prompt_templates ADD CONSTRAINT uq_prompt_templates_key_version UNIQUE (key, version)",
        "添加唯一约束: (key, version)",
    )

    await session.commit()
    logger.info("prompt_templates 表结构检查完成")


async def ensure_agent_definitions_schema(session: AsyncSession) -> None:
    """确保 agent_definitions 表存在且拥有最新 schema。"""
    logger.info("── 检查 agent_definitions 表结构 ──")

    table_exists = await _table_exists(session, "agent_definitions")
    if not table_exists:
        logger.info("agent_definitions 表不存在，开始创建…")
        await session.execute(text("""
            CREATE TABLE agent_definitions (
                id              SERIAL PRIMARY KEY,
                name            VARCHAR(100) NOT NULL UNIQUE,
                agent_type      VARCHAR(50) NOT NULL,
                description     TEXT DEFAULT '',
                skills          JSONB NOT NULL DEFAULT '[]',
                llm_config      JSONB NOT NULL DEFAULT '{}',
                prompt_overrides JSONB NOT NULL DEFAULT '{}',
                system_prompt   TEXT,
                tools           JSONB DEFAULT '[]',
                goal            TEXT DEFAULT '',
                constraints     JSONB DEFAULT '[]',
                thinking_config JSONB DEFAULT '{}',
                is_active       INTEGER NOT NULL DEFAULT 1,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
        await session.commit()
        logger.info("  ✓ agent_definitions 表已创建")
        return

    # 检查并添加可能缺失的列
    col_migrations = [
        ("system_prompt", "TEXT"),
        ("tools", "JSONB DEFAULT '[]'"),
        ("goal", "TEXT DEFAULT ''"),
        ("constraints", "JSONB DEFAULT '[]'"),
        ("thinking_config", "JSONB DEFAULT '{}'"),
    ]
    for col_name, col_type in col_migrations:
        if not await _column_exists(session, "agent_definitions", col_name):
            await _safe_execute(
                session,
                f"ALTER TABLE agent_definitions ADD COLUMN IF NOT EXISTS {col_name} {col_type}",
                f"添加列: agent_definitions.{col_name}",
            )
    await session.commit()
    logger.info("agent_definitions 表结构检查完成")


# ═════════════════════════════════════════════════════════════════════════════
# PromptTemplate 版本更新
# ═════════════════════════════════════════════════════════════════════════════

# 映射 prompt template key 到种子中定义的 name
PROMPT_META: dict[str, dict] = {
    "fp_extract":         {"name": "功能点提取",   "category": "generation",  "description": "从需求文档提取功能点列表并结构化输出",           "variables": ["text"]},
    "tc_generate":        {"name": "测试用例生成", "category": "generation",  "description": "根据功能点生成结构化测试用例（含等价类/边界值覆盖）", "variables": ["fps"]},
    "operation_translate": {"name": "操作指令翻译", "category": "execution",   "description": "将自然语言步骤翻译为浏览器操作JSON（含回退策略）",  "variables": ["step", "url"]},
    "verify_expected":    {"name": "预期结果验证", "category": "verification", "description": "用三级策略验证测试步骤的预期结果（含时间戳容忍）",   "variables": ["action", "expected"]},
}


async def update_prompt_templates(
    session: AsyncSession,
    seed_content: dict[str, str],
) -> dict:
    """为每个 PromptTemplate key 检查最新版本内容，若不同则创建新版本。

    返回 {"created": [key, ...], "skipped": [key, ...]}。
    幂等：多次运行结果一致。
    """
    created: list[str] = []
    skipped: list[str] = []
    keys_to_check = ["fp_extract", "tc_generate", "operation_translate", "verify_expected"]

    for key in keys_to_check:
        new_content = seed_content.get(key)
        if new_content is None:
            logger.warning("未在 app/main.py 中找到 key=%s 的种子内容，跳过", key)
            skipped.append(key)
            continue

        # 查询该 key 的最新版本
        result = await session.execute(
            text(
                "SELECT id, key, name, category, content, variables, version, is_active, description "
                "FROM prompt_templates WHERE key = :key ORDER BY version DESC LIMIT 1"
            ),
            {"key": key},
        )
        row = result.fetchone()

        if row is None:
            # 数据库中无此 key 的记录 → 创建初始版本 1
            meta = PROMPT_META.get(key, {})
            await session.execute(
                text(
                    "INSERT INTO prompt_templates "
                    "(key, name, category, content, variables, version, is_active, description, created_at, updated_at) "
                    "VALUES (:key, :name, :category, :content, :variables, :version, :is_active, :description, NOW(), NOW())"
                ),
                {
                    "key": key,
                    "name": meta.get("name", key),
                    "category": meta.get("category", "general"),
                    "content": new_content,
                    "variables": json.dumps(meta.get("variables", [])),
                    "version": 1,
                    "is_active": True,
                    "description": meta.get("description", ""),
                },
            )
            logger.info("key=%s：数据库无记录，创建 v1", key)
            created.append(key)
            continue

        # 映射行数据
        row_dict = row._mapping
        latest_id: int = row_dict["id"]
        latest_content: str = row_dict["content"] or ""
        latest_version: int = row_dict["version"] or 1
        latest_name: str = row_dict["name"] or ""
        latest_category: str = row_dict["category"] or "general"
        latest_variables: list = row_dict["variables"] or []
        latest_description: str | None = row_dict["description"]

        # 比对内容（去除尾部空白以容忍微小差异）
        if latest_content.strip() == new_content.strip():
            logger.info("key=%s v%d：内容一致，跳过", key, latest_version)
            skipped.append(key)
            continue

        # 内容不同 → 创建新版本
        new_version = latest_version + 1
        await session.execute(
            text(
                "INSERT INTO prompt_templates "
                "(key, name, category, content, variables, version, is_active, description, created_at, updated_at) "
                "VALUES (:key, :name, :category, :content, :variables, :version, :is_active, :description, NOW(), NOW())"
            ),
            {
                "key": key,
                "name": latest_name,
                "category": latest_category,
                "content": new_content,
                "variables": json.dumps(latest_variables),
                "version": new_version,
                "is_active": True,
                "description": latest_description,
            },
        )

        # 将旧版本设为非活跃
        await session.execute(
            text("UPDATE prompt_templates SET is_active = false WHERE id = :id"),
            {"id": latest_id},
        )

        logger.info(
            "key=%s：创建 v%d（旧版本 v%d 已标记 is_active=false）",
            key, new_version, latest_version,
        )
        created.append(key)

    await session.commit()
    return {"created": created, "skipped": skipped}


# ═════════════════════════════════════════════════════════════════════════════
# AgentDefinition 系统提示词更新
# ═════════════════════════════════════════════════════════════════════════════

async def update_agent_definitions(
    session: AsyncSession,
    agent_prompts: dict[int, str],
) -> dict:
    """更新 AgentDefinition 的 system_prompt 字段。幂等。

    返回 {"updated": [id, ...], "skipped": [id, ...]}。
    """
    updated: list[int] = []
    skipped: list[int] = []

    for agent_id, new_prompt in agent_prompts.items():
        result = await session.execute(
            text("SELECT id, name, system_prompt FROM agent_definitions WHERE id = :id"),
            {"id": agent_id},
        )
        row = result.fetchone()
        if row is None:
            logger.warning(
                "AgentDefinition id=%d：不存在（可能尚未由应用启动初始化创建），跳过",
                agent_id,
            )
            skipped.append(agent_id)
            continue

        row_dict = row._mapping
        current_prompt: str | None = row_dict["system_prompt"]
        agent_name: str = row_dict["name"] or f"agent#{agent_id}"

        if current_prompt is not None and current_prompt.strip() == new_prompt.strip():
            logger.info(
                "AgentDefinition id=%d (%s)：system_prompt 一致，跳过",
                agent_id, agent_name,
            )
            skipped.append(agent_id)
            continue

        await session.execute(
            text("UPDATE agent_definitions SET system_prompt = :prompt WHERE id = :id"),
            {"prompt": new_prompt, "id": agent_id},
        )
        logger.info("AgentDefinition id=%d (%s)：system_prompt 已更新", agent_id, agent_name)
        updated.append(agent_id)

    await session.commit()
    return {"updated": updated, "skipped": skipped}


# ═════════════════════════════════════════════════════════════════════════════
# 主入口
# ═════════════════════════════════════════════════════════════════════════════

async def main() -> None:
    parser = argparse.ArgumentParser(
        description="更新 PromptTemplate 版本和 AgentDefinition 系统提示词（幂等）"
    )
    parser.add_argument(
        "--db-url",
        help="数据库连接 URL（优先级：本参数 > DATABASE_URL 环境变量 > data/.db_config.json）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅显示将要执行的操作，不实际写入数据库",
    )
    parser.add_argument(
        "--skip-schema",
        action="store_true",
        help="跳过 schema 迁移步骤（仅当表结构已正确时使用）",
    )
    args = parser.parse_args()

    # 解析数据库 URL
    db_url = resolve_database_url(args.db_url)
    if not db_url:
        logger.error(
            "未找到数据库连接信息。请通过以下任一方式提供：\n"
            "  1) --db-url 命令行参数\n"
            "  2) DATABASE_URL 环境变量\n"
            "  3) data/.db_config.json 文件"
        )
        sys.exit(1)

    logger.info("连接数据库：%s", mask_url(db_url))

    # 解析种子内容
    main_file = os.path.join(PROJECT_ROOT, "app", "main.py")
    if not os.path.exists(main_file):
        logger.error("找不到 app/main.py：%s", main_file)
        sys.exit(1)

    seed_prompts = parse_seed_prompts(main_file)
    logger.info("从 app/main.py 解析到 %d 个 PromptTemplate 种子内容：%s",
                len(seed_prompts), ", ".join(sorted(seed_prompts.keys())))

    if args.dry_run:
        logger.info("=== DRY RUN 模式，不执行写入 ===")
        for key, content in sorted(seed_prompts.items()):
            logger.info("  key=%s, 内容长度=%d", key, len(content))
        for agent_id, prompt in sorted(NEW_AGENT_SYSTEM_PROMPTS.items()):
            logger.info("  AgentDefinition id=%d, system_prompt 长度=%d", agent_id, len(prompt))
        return

    # 连接数据库并执行
    engine = create_async_engine(db_url, echo=False)
    async_session = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    try:
        async with async_session() as session:
            # ── Schema 迁移 ──
            if not args.skip_schema:
                await ensure_prompt_templates_schema(session)
                await ensure_agent_definitions_schema(session)
            else:
                logger.info("跳过 schema 迁移（--skip-schema）")

            # ── 更新 PromptTemplate ──
            logger.info("── 开始更新 PromptTemplate ──")
            pt_result = await update_prompt_templates(session, seed_prompts)

            # ── 更新 AgentDefinition ──
            logger.info("── 开始更新 AgentDefinition ──")
            ag_result = await update_agent_definitions(session, NEW_AGENT_SYSTEM_PROMPTS)

            # ── 汇总输出 ──
            logger.info("")
            logger.info("══════════════════════════════════════")
            logger.info("           更新完成汇总")
            logger.info("══════════════════════════════════════")
            logger.info(
                "PromptTemplate: 新建 %d 个版本 (%s) / 跳过 %d (%s)",
                len(pt_result["created"]),
                ", ".join(pt_result["created"]) if pt_result["created"] else "无",
                len(pt_result["skipped"]),
                ", ".join(pt_result["skipped"]) if pt_result["skipped"] else "无",
            )
            logger.info(
                "AgentDefinition: 已更新 %d (%s) / 跳过 %d (%s)",
                len(ag_result["updated"]),
                ", ".join(str(i) for i in ag_result["updated"]) if ag_result["updated"] else "无",
                len(ag_result["skipped"]),
                ", ".join(str(i) for i in ag_result["skipped"]) if ag_result["skipped"] else "无",
            )
    finally:
        await engine.dispose()
        logger.info("数据库连接已关闭")


if __name__ == "__main__":
    asyncio.run(main())
