# core/runner/_validators.py
"""URL 校验 / 环境 Cookie 解析 / Auth Cookie 注入。"""
import ipaddress
import logging
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SSRF 防护 — 禁止导航到内网地址
# ---------------------------------------------------------------------------

_PRIVATE_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
]


def _validate_nav_url(url: str | None) -> str | None:
    """校验导航 URL，阻止 SSRF 到内网地址。返回 None 表示拒绝"""
    if not url:
        return None
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        if not host:
            return url
        # 阻止空主机名、localhost 变体
        if host in ("localhost", "localhost.localdomain", "127.0.0.1", "0.0.0.0", "::1", "[::1]"):
            logger.warning(f"SSRF 防护: 拒绝 localhost URL: {url}")
            return None
        try:
            addr = ipaddress.ip_address(host)
            for net in _PRIVATE_NETS:
                if addr in net:
                    logger.warning(f"SSRF 防护: 拒绝内网地址: {url}")
                    return None
        except ValueError:
            pass  # 域名，不做 IP 检查
        return url
    except Exception as exc:
        logger.warning(f"URL 校验异常: {url} -> {exc}")
        return None


# ---------------------------------------------------------------------------
# Auth cookie 注入
# ---------------------------------------------------------------------------


def _resolve_env_cookies(db, base_url_override: str | None) -> list[dict]:
    """根据 base_url_override 查找匹配的环境记录，返回该环境的 cookies 列表。

    没有 override / 找不到 / 没有 cookies → 返回空列表（不影响流程）。
    """
    if not base_url_override:
        return []
    try:
        from app import db_models
        env = (
            db.query(db_models.Environment)
            .filter(db_models.Environment.base_url == base_url_override)
            .order_by(db_models.Environment.is_default.desc(), db_models.Environment.id.asc())
            .first()
        )
        if not env:
            return []
        cookies = env.cookies
        if not cookies:
            return []
        if not isinstance(cookies, list):
            logger.warning(f"Environment {env.id} cookies 字段不是列表: {type(cookies).__name__}")
            return []
        return cookies
    except Exception as exc:
        logger.warning(f"读取环境 cookies 失败 (base_url={base_url_override}): {exc}")
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
        except Exception:
            default_domain = None

    success_count = 0
    for cookie in cookies:
        if not isinstance(cookie, dict):
            logger.warning(f"跳过非法 cookie 项（非字典）: {cookie!r}")
            continue
        name = cookie.get("name")
        value = cookie.get("value", "")
        if not name:
            logger.warning(f"跳过缺少 name 的 cookie: {cookie!r}")
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
                logger.info(f"Cookie 注入成功: {name} @ {args.get('domain', '<no-domain>')}")
            else:
                logger.warning(
                    f"Cookie 注入失败: {name} -> {result.get('text') or result.get('error', 'unknown')}"
                )
        except Exception as exc:
            logger.warning(f"Cookie 注入异常: {name} -> {exc}")

    if success_count:
        logger.info(f"已注入 {success_count}/{len(cookies)} 个 cookies")
    return success_count
