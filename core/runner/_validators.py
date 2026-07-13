# core/runner/_validators.py
"""URL 校验 / 环境 Cookie 解析 / Auth Cookie 注入。"""
import logging
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger(__name__)

# 导航 URL 校验 — 本工具为内部测试工具，不设 IP 限制


def _validate_nav_url(url: str | None) -> str | None:
    """校验导航 URL。内部测试工具不做 IP 限制"""
    if not url:
        return None
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        if not host:
            return url
        return url
    except (ValueError, TypeError, AttributeError) as exc:
        logger.warning("URL 校验异常: %s -> %s", url, exc, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Auth cookie 注入
# ---------------------------------------------------------------------------


async def _resolve_env_cookies(db, base_url_override: str | None) -> list[dict]:
    """根据 base_url_override 查找匹配的环境记录，返回该环境的 cookies 列表。

    没有 override / 找不到 / 没有 cookies → 返回空列表（不影响流程）。
    支持 AsyncSession（使用 ``await db.execute(select(...))``）。
    """
    if not base_url_override:
        return []
    try:
        from app import db_models
        result = await db.execute(
            select(db_models.Environment)
            .where(db_models.Environment.base_url == base_url_override)
            .order_by(db_models.Environment.is_default.desc(), db_models.Environment.id.asc())
        )
        env = result.scalars().first()
        if not env:
            return []
        cookies = env.cookies
        if not cookies:
            return []
        if not isinstance(cookies, list):
            logger.warning("Environment %s cookies 字段不是列表: %s", env.id, type(cookies).__name__)
            return []
        return cookies
    except (SQLAlchemyError, ValueError, TypeError) as exc:
        # SQLAlchemyError: DB 查询失败；ValueError/TypeError: cookies 字段反序列化错误
        logger.warning("读取环境 cookies 失败 (base_url=%s): %s", base_url_override, exc, exc_info=True)
        return []


async def _inject_auth_cookies(
    mcp_manager,
    cookies: list[dict],
    nav_url: str | None,
) -> int:
    """通过 MCP browser_set_cookie 注入 cookies 列表。

    每个 cookie 字典支持: {name, value, domain?, path?, expires?, httpOnly?, secure?, sameSite?}
    若 cookie 未指定 domain，从 nav_url 提取 hostname 作为默认 domain。

    返回成功注入的数量。任一 cookie 失败仅记录 warning，不抛出。
    """
    if not cookies:
        return 0

    default_domain: str | None = None
    if nav_url:
        try:
            default_domain = urlparse(nav_url).hostname
        except (ValueError, TypeError, AttributeError):
            default_domain = None

    success_count = 0
    for cookie in cookies:
        if not isinstance(cookie, dict):
            logger.warning("跳过非法 cookie 项（非字典）: %r", cookie)
            continue
        name = cookie.get("name")
        value = cookie.get("value", "")
        if not name:
            logger.warning("跳过缺少 name 的 cookie: %r", cookie)
            continue

        args: dict = {
            "name": name,
            "value": str(value),
        }
        domain = cookie.get("domain") or default_domain
        if domain:
            args["domain"] = domain
        if cookie.get("path"):
            args["path"] = cookie["path"]
        elif "path" not in args:
            args["path"] = "/"
        for opt_key in ("expires", "httpOnly", "secure", "sameSite", "url"):
            if opt_key in cookie and cookie[opt_key] is not None:
                args[opt_key] = cookie[opt_key]

        try:
            result = await mcp_manager.call_tool("browser_set_cookie", args)
            if result.get("success"):
                success_count += 1
                logger.info("Cookie 注入成功: %s @ %s", name, args.get('domain', '<no-domain>'))
            else:
                logger.warning(
                    f"Cookie 注入失败: {name} -> {result.get('text') or result.get('error', 'unknown')}"
                )
        except (RuntimeError, ConnectionError, OSError, ValueError, TypeError) as exc:
            # 单 cookie 注入失败不中断整批；只记录 warning
            logger.warning("Cookie 注入异常: %s -> %s", name, exc, exc_info=True)

    if success_count:
        logger.info("已注入 %s/%s 个 cookies", success_count, len(cookies))
    return success_count
