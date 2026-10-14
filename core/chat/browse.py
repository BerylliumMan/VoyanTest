"""对话浏览器探索：打开站点 → 登录/按描述操作 → 探索主要功能 → 观察报告（供用例生成）。

能力对齐 ZCode 的 web-gui-tester / control-browser（2026-10-10 评审）：
- 纯 GUI 黑盒：只操作快照里真实可见可点的元素（候选集 + ref 校验，杜绝编造）；
- 快照优先，截图存证（起始页 / 登录后 / 每个新页面各一张）；
- 禁止 JS 注入改页面、禁止猜测 URL 跳转、禁止破坏性点击（删除/支付/下单/退出…）；
- 失败诚实报告：登录失败、页面打不开都不伪造成功，以部分结果收尾；
- 有界执行：步数 / 时间上限，超限返回部分结果（stopped_reason 标明）。

两种用法（由 goal 承载）：
1. 探索式：「打开 X 查看主要功能并生成用例」；
2. 描述式：「打开 X，点登录、输入账号密码、进入设置页…」→ 按描述依次操作，
   完成后补充观察、总结功能点，再生成用例。

双后端（与执行链路同口径）：
- ClientBrowser：会话选中的客户端（当前 PC 默认），浏览器在用户机器上可见；
- ServerBrowser：服务端容器无头浏览器（未选客户端时）。

凭据安全：账号/密码以占位符 <<USERNAME>> / <<PASSWORD>> 交给模型，
模型全程不接触真实值，由执行侧在 fill 时替换；日志/卡片/审计一律打码。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

ACTION_WHITELIST = ("goto", "click", "fill", "press", "select", "wait", "solve_captcha", "done")

# 破坏性点击关键词（按可访问名拦截；登录/提交表单不在此列）
_DANGEROUS_RE = re.compile(
    r"退出|注销|登出|删除|移除|清空|支付|付款|下单|结算|提交订单|立即购买|购买"
)

_USERNAME_PLACEHOLDER = "<<USERNAME>>"
_PASSWORD_PLACEHOLDER = "<<PASSWORD>>"

_MAX_CANDIDATES_IN_PROMPT = 80
_MAX_SHOTS = 6
_DEFAULT_MAX_STEPS = 22
_DEFAULT_TIMEOUT_S = 300


@dataclass
class BrowseConfig:
    url: str
    goal: str = "浏览站点主要功能"
    username: str = ""
    password: str = ""
    max_steps: int = _DEFAULT_MAX_STEPS
    timeout_s: int = _DEFAULT_TIMEOUT_S
    captcha_enabled: bool = True


EXPLORER_SYSTEM_PROMPT = """你是 VoyanTest 的「网页探索器」：在真实浏览器里以纯 GUI 黑盒方式操作网页，产出可供测试用例生成使用的观察报告。

【目标理解】
【目标】可能是两种之一：
A. 探索性目标（如「查看主要功能」）：自行规划浏览路径，优先访问主要功能页面；
B. 操作性 / 测试描述（如「点登录，输入账号密码，进入设置页」「测试设置页留空保存的校验提示」）：严格按描述顺序逐步操作；其中「测试/验证/观察」类要求，在安全准则内执行代表性操作并观察真实结果（提示文案、跳转、校验状态、数据变化），把观察到的真实行为记录为功能点（note/features）；描述执行完成后，再补充观察当前页面与相关入口；未按描述验证到的点，在 summary 中如实说明（不要脑补结果）。

【行为准则 — 严格遵守】
1. 只能操作【候选元素】列表中真实存在的元素（用其 ref）；禁止编造元素、禁止猜测选择器；
2. 禁止用 JS 注入或修改 URL 来改变页面状态；goto 只允许回到起始地址或站内已访问过的地址；
3. 禁止破坏性操作：点击名称含「退出/注销/删除/移除/清空/支付/付款/下单/结算/购买」的元素一律不做，改为记录并跳过；
4. 需要登录时：在登录表单里用 <<USERNAME>> 填账号、<<PASSWORD>> 填密码（占位符原样输出，系统会替换；绝不输出真实凭据），然后点击登录按钮，并确认登录成功（地址变化 / 出现导航菜单 / 出现用户名）；
5. 登录失败只重试一次（核对表单后），仍失败就如实收尾（summary 说明失败原因）；
5b.（图形验证码）登录表单含验证码（输入框占位/标签含「验证码」，旁边有验证码图片）时：
   按 账号 → 密码 → 验证码 → 登录 的顺序操作；验证码一步输出 solve_captcha（ref=验证码输入框的 ref；
   系统会自动识别并填写，不需要 value，不要询问用户、不要猜测内容）。若登录后仍停留在登录页或提示
   「验证码错误」，输出 solve_captcha 且 refresh=true（先点击验证码图片刷新再识别）重试，最多 3 次
   （验证码错误的重试不受规则 5 的一次限制），仍失败按规则 5 如实收尾。若重试时登录页已被重置（用户名/密码
   输入框为空）：「必须先重新填写账号与密码，再 solve_captcha（refresh=true），最后点击登录」——
   禁止只改验证码就直接提交。算术型验证码（图片是「1+2=?」这类）系统会自动计算答案，同样用 solve_captcha；
   若系统识别失败（步骤会显示失败原因），先 refresh=true 重试；仍失败则用 note 记录该情况并继续其他步骤；
6. 浏览策略：优先点击顶部导航 / 侧边菜单等入口，逐个访问主要功能页面；每到一个新页面，用 note 记录「页面标题：主要功能点」；
7. 操作失败时先重新观察（看最新快照）再决定，不要重复同样的动作；动作（点击/提交/跳转）完成后系统会自动给出
   最新页面快照（含跳转结果），不要用 wait 步骤等待页面加载或跳转结果；wait 仅用于页面内延时场景（如等提示出现），值用秒数；
8. 步数有限：先做最有价值的事（登录 → 主菜单 → 核心功能页 2-4 个）；
9. 目标达成或主要功能已覆盖 → 立即返回 done，并在 summary/features 给出结论；禁止虚构没看到的功能。

【输出格式 — 只输出一个 JSON 对象，不要任何其他文字、不要代码块】
{
  "thinking": "1-3 句：当前页面是什么、下一步做什么及原因",
  "action": "goto|click|fill|press|select|wait|solve_captcha|done",
  "ref": "元素动作必填：候选列表中的 ref（如 e12）；solve_captcha 填验证码输入框的 ref",
  "value": "fill=输入值（凭据用占位符）；press=按键（如 Enter）；select=选项文本；wait=等待文本或秒数；solve_captcha 不需要 value",
  "url": "action=goto 时的目标地址",
  "refresh": "solve_captcha 可选：true=先点击验证码图片刷新再识别（验证码错误重试时使用）",
  "note": "本步观察到的重要信息（页面功能点，一句话，可选）",
  "summary": "action=done 时必填：中文探索结论（站点、登录结果、主要功能概述、是否发现问题）",
  "features": ["功能点1", "功能点2"]
}"""


# ----------------------------
# 浏览器后端抽象：客户端（可见）/ 服务端（无头）
# ----------------------------

class ServerBrowser:
    """服务端容器内无头浏览器（playwright-mcp）。"""

    mode = "server"
    label = "服务端无头浏览器"

    def __init__(self, mcp_manager=None, *, headless: bool = True):
        self._mcp = mcp_manager
        self._owns = mcp_manager is None
        self._headless = headless
        self._registry = None

    async def start(self) -> None:
        if self._mcp is None:
            from core.playwright_manager import PlaywrightMCPManager

            self._mcp = PlaywrightMCPManager(headless=self._headless)
        if self._owns:
            await self._mcp.start()
        from core.agent_runner.runner import ToolRegistry

        self._registry = ToolRegistry(self._mcp)

    async def stop(self) -> None:
        if self._owns and self._mcp is not None:
            try:
                await self._mcp.stop()
            except Exception:
                logger.debug("ServerBrowser stop failed", exc_info=True)

    async def snapshot(self) -> str:
        return await self._mcp.get_dom_snapshot()

    async def evaluate_json(self, fn: str) -> Optional[dict]:
        try:
            result = await self._mcp.call_tool("browser_evaluate", {"function": fn})
        except Exception as exc:
            logger.debug("browse evaluate 失败: %s", exc)
            return None
        if not result.get("success"):
            return None
        return _decode_json_text(str(result.get("text") or ""))

    async def page_facts(self) -> dict:
        """URL/标题：服务端 evaluate 读取（客户端走 observe 回传，见 ClientBrowser）。"""
        facts = await self.evaluate_json(
            "() => JSON.stringify({url: window.location.href, title: document.title})"
        )
        return {
            "url": str((facts or {}).get("url") or ""),
            "title": str((facts or {}).get("title") or ""),
        }

    async def act(self, action: dict) -> dict:
        assert self._registry is not None, "ServerBrowser.start() 未调用"
        try:
            return await asyncio.wait_for(self._registry.execute(action), timeout=60)
        except asyncio.TimeoutError:
            return {"success": False, "error": "操作超时(60s)"}

    async def screenshot(self, path: str) -> Optional[str]:
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            return await self._mcp.take_screenshot(path)
        except Exception:
            return None

    async def console(self, limit: int = 20) -> list[str]:
        try:
            from core.console_evidence import collect_console_via_mcp

            return await collect_console_via_mcp(self._mcp, limit)
        except Exception:
            return []


class ClientBrowser:
    """客户端 Agent 浏览器（用户机器上，默认有头可见；经 WS 逐条驱动）。"""

    mode = "client"

    def __init__(self, agent_manager, agent_name: str, run_id: str):
        self._manager = agent_manager
        self._agent = agent_name
        self._run_id = run_id
        self._last_page: dict = {"url": "", "title": ""}
        self.label = f"客户端「{agent_name}」的浏览器"

    async def start(self) -> None:
        """冷启动客户端浏览器：get_snapshot 在客户端空闲时会自行启动/恢复 MCP 会话。

        客户端 observe/act 不会拉起 MCP（只有 run_start / get_snapshot 会），
        因此探索前必须先经 get_snapshot 完成冷启动（2026-10-10 实测修复）。
        """
        try:
            res = await asyncio.wait_for(
                self._manager.send_get_snapshot(self._agent, self._run_id),
                timeout=120,
            )
        except Exception as exc:
            raise RuntimeError(f"客户端浏览器启动失败: {exc}") from exc
        if isinstance(res, dict) and res.get("success") is False:
            raise RuntimeError(f"客户端浏览器启动失败: {res.get('error') or '未知错误'}")

    async def stop(self) -> None:
        return None

    async def snapshot(self) -> str:
        res = await asyncio.wait_for(
            self._manager.send_observe(self._agent, self._run_id, want_screenshot=False),
            timeout=60,
        )
        if not isinstance(res, dict) or not res.get("success"):
            err = (res or {}).get("error") if isinstance(res, dict) else "no response"
            raise RuntimeError(f"客户端快照失败: {err}")
        # observe 已回传 page_url/page_title（客户端从快照文本解析），缓存复用；
        # 客户端 evaluate 成功路径不回传 text（_handle_act 仅 console/find 带 text），
        # 因此 URL/标题必须取 observe 回传值（2026-10-10 实测修复）。
        self._last_page = {
            "url": str(res.get("page_url") or ""),
            "title": str(res.get("page_title") or ""),
        }
        return str(res.get("snapshot") or "(empty page)")

    async def page_facts(self) -> dict:
        return dict(self._last_page)

    async def evaluate_json(self, fn: str) -> Optional[dict]:
        try:
            res = await asyncio.wait_for(
                self._manager.send_act(
                    self._agent, self._run_id,
                    {"name": "evaluate", "args": {"value": fn}},
                ),
                timeout=30,
            )
        except Exception as exc:
            logger.debug("client evaluate 失败: %s", exc)
            return None
        if not (isinstance(res, dict) and res.get("success")):
            return None
        text = str(res.get("text") or res.get("result") or "")
        return _decode_json_text(text)

    async def act(self, action: dict) -> dict:
        try:
            res = await asyncio.wait_for(
                self._manager.send_act(self._agent, self._run_id, dict(action)),
                timeout=120,
            )
            return res if isinstance(res, dict) else {"success": False, "error": "客户端无响应"}
        except asyncio.TimeoutError:
            return {"success": False, "error": "客户端操作超时(120s)"}

    async def screenshot(self, path: str) -> Optional[str]:
        try:
            res = await asyncio.wait_for(
                self._manager.send_observe(self._agent, self._run_id, want_screenshot=True),
                timeout=60,
            )
            b64 = (res or {}).get("screenshot_b64")
            if not b64:
                return None
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(base64.b64decode(b64))
            return path
        except Exception:
            return None

    async def console(self, limit: int = 20) -> list[str]:
        try:
            res = await asyncio.wait_for(
                self._manager.send_act(self._agent, self._run_id, {"name": "console", "args": {}}),
                timeout=30,
            )
            if not (isinstance(res, dict) and res.get("success")):
                return []
            from core.console_evidence import dedupe_cap, parse_console_messages

            kept, _ = dedupe_cap(
                parse_console_messages(str(res.get("text") or res.get("result") or "")),
                limit,
            )
            return kept
        except Exception:
            return []


def _decode_json_text(text: str) -> Optional[dict]:
    """容错解析 evaluate 文本（playwright-mcp 的 ### Result 块 + 双重 JSON 编码）。"""
    raw = str(text or "")
    if "### Result" in raw:
        raw = raw.split("### Result", 1)[1].split("###", 1)[0].strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(data, str):  # 双重编码
        try:
            data = json.loads(data)
        except (json.JSONDecodeError, ValueError):
            return None
    return data if isinstance(data, dict) else None


# ----------------------------
# 纯函数
# ----------------------------

def mask_secret(value: str | None) -> str:
    return "******" if value else ""


def redact_text(text: str | None, secrets: list[str]) -> str:
    """把文本中出现的真实凭据替换成掩码（卡片/日志兜底防护）。"""
    out = str(text or "")
    for s in secrets:
        if s and len(s) >= 3:
            out = out.replace(s, "******")
    return out


def _same_site(candidate_url: str, base_url: str) -> bool:
    try:
        c, b = urlparse(candidate_url), urlparse(base_url)
        return bool(c.netloc) and c.netloc == b.netloc
    except Exception:
        return False


def parse_decision(text: str | None) -> Optional[dict]:
    """容错解析模型决策 JSON（容忍代码块/前后缀文本）。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", raw).strip()
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(raw[start : end + 1])
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _render_candidates(candidates) -> str:
    lines: list[str] = []
    for idx, c in enumerate(candidates[:_MAX_CANDIDATES_IN_PROMPT], start=1):
        label = (c.name or c.text or "").strip().replace("\n", " ")[:60]
        lines.append(f"[{idx}] {c.ref} | {c.role} | \"{label}\"")
    return "\n".join(lines) if lines else "（无可见可操作元素）"


def _round_prompt(cfg: BrowseConfig, *, page: dict, candidates, journal: list[dict],
                  visited: dict[str, dict], page_text: str = "") -> str:
    j_lines = []
    for i, st in enumerate(journal[-6:], start=1):
        mark = "✅" if st.get("ok") else "❌"
        seg = f"{i}. {st.get('action')}"
        if st.get("target"):
            seg += f" [{st['target']}]"
        if st.get("value"):
            seg += f" = {st['value']}"
        seg += f" {mark}"
        if st.get("error"):
            seg += f"（{str(st['error'])[:80]}）"
        if st.get("note"):
            seg += f" 记录：{st['note']}"
        j_lines.append(seg)
    vp = "\n".join(
        f"- {p.get('title') or '(无标题)'} | {u}" for u, p in list(visited.items())[-8:]
    ) or "（尚未访问任何页面）"
    usage = (
        f"已用步数 {len(journal)}/{cfg.max_steps}；"
        + ("还够用，继续。" if len(journal) < cfg.max_steps - 3 else "步数将尽，尽快 done 总结。")
    )
    return f"""【目标】{cfg.goal}
【起始站点】{cfg.url}
【登录凭据】账号占位符 {_USERNAME_PLACEHOLDER}；密码占位符 {_PASSWORD_PLACEHOLDER}（fill 时原样使用，系统自动替换，禁止输出真实值）

【已访问页面】
{vp}

【当前页面】
标题：{page.get('title') or '(未知)'}
地址：{page.get('url') or '(未知)'}

【页面文本（节选，供如实记录提示/校验/数据文案；不要推测未出现的文本）】
{page_text or '（无）'}

【候选元素】（只能引用这些 ref）
{_render_candidates(candidates)}

【最近动作】
{chr(10).join(j_lines) if j_lines else '（无）'}

【提示】{usage}
请输出下一步动作 JSON。"""


def _snapshot_text_excerpt(snapshot: str, limit: int = 1400) -> str:
    """从快照中提取页面静态文本节选（toast/校验提示/表格数据等，供模型如实记录）。

    playwright-mcp 快照中静态文本以 ``- text: X`` 或 ``- <role> [ref=..]: X`` 形式出现；
    交互元素（按钮/输入框等）已由候选列表承载，这里只补充非交互文本。
    """
    out: list[str] = []
    content_roles = r"(heading|cell|cellheader|paragraph|strong|emphasis|listitem|term|definition)"
    # toast/提示常落在 generic 节点上（如 `- generic [ref=e2]: "保存成功"`），一并纳入
    inline_roles = (
        r"(generic|section|article|group|region|main|aside|figure|figcaption|"
        + content_roles[1:-1]
        + r")"
    )
    for line in str(snapshot or "").splitlines():
        s = line.strip()
        m = re.match(r"^-\s*text:\s*(.+)$", s)
        if m:
            t = m.group(1).strip().strip('"')
            if t and (not out or out[-1] != t):
                out.append(t)
            continue
        # 冒号形式：- listitem [ref=e13]: Checking the connection
        m = re.match(rf"^-\s*{inline_roles}\b[^:]*:\s*(.+)$", s)
        if m:
            t = m.group(2).strip().strip('"')
            if t and (not out or out[-1] != t):
                out.append(t)
            continue
        # 引号形式：- heading "工作台" [ref=e3] / - cell "编号" [ref=e5]
        m = re.match(rf'^-\s*{content_roles}\b\s+"([^"]+)"', s)
        if m:
            t = m.group(2).strip()
            if t and (not out or out[-1] != t):
                out.append(t)
    return " / ".join(out)[:limit]


def _cand_label(ref: str | None, candidates) -> Optional[str]:
    if not ref:
        return None
    cand = next((c for c in candidates if c.ref == ref), None)
    if not cand:
        return ref
    label = (cand.name or cand.text or "").strip().replace("\n", " ")[:40]
    return f"{cand.role}:{label}" if label else cand.role


def _masked_value(action: dict, cfg: BrowseConfig) -> Optional[str]:
    value = action.get("value")
    if value is None:
        return None
    raw = str(value)
    if _USERNAME_PLACEHOLDER in raw or _PASSWORD_PLACEHOLDER in raw:
        return "******"
    if cfg.username and cfg.username in raw:
        return "******"
    if cfg.password and cfg.password in raw:
        return "******"
    return raw[:120]


def _action_label(action_name: str, decision: dict, candidates) -> str:
    if action_name == "goto":
        return f"导航到 {str(decision.get('url') or '')[:60]}"
    ref = str(decision.get("ref") or "")
    label = _cand_label(ref, candidates) or ref
    if action_name == "click":
        return f"点击 {label}"
    if action_name == "fill":
        return f"输入 {label}"
    if action_name == "press":
        return f"按键 {decision.get('value')} 于 {label}"
    if action_name == "select":
        return f"选择 {label}"
    if action_name == "wait":
        return f"等待 {decision.get('value')}"
    return action_name


def _substitute_credentials(action: dict, cfg: BrowseConfig) -> dict:
    """fill 值占位符 → 真实凭据（仅在此处接触明文，不落任何日志）。"""
    if str(action.get("action")) != "fill":
        return action
    value = str(action.get("value") or "")
    if _USERNAME_PLACEHOLDER in value or _PASSWORD_PLACEHOLDER in value:
        out = dict(action)
        out["value"] = value.replace(_USERNAME_PLACEHOLDER, cfg.username or "").replace(
            _PASSWORD_PLACEHOLDER, cfg.password or ""
        )
        return out
    return action


def _auto_summary(journal: list[dict], pages: dict, stopped: str) -> str:
    """模型未给出 done 总结时的兜底摘要（如实描述，不推测功能）。"""
    titles = [p.get("title") or u for u, p in list(pages.items())[:8]]
    why = {
        "timeout": "达到时间上限",
        "max_steps": "达到步数上限",
        "error": "探索中断",
    }.get(stopped, stopped)
    base = f"共访问 {len(pages)} 个页面"
    if titles:
        base += f"（{'、'.join(titles[:6])}）"
    base += f"；因{why}提前结束，未及总结主要功能。"
    last_errs = [s for s in journal if not s.get("ok") and s.get("error")]
    if last_errs:
        base += f" 最后的问题：{str(last_errs[-1].get('error'))[:120]}"
    return base


def build_report_materials(report: dict) -> str:
    """观察报告 → 生成管线材料（需求文档风格，标注来源为自动观察）。"""
    lines = [
        "# 页面探索观察报告（自动生成，内容均来自真实页面操作，未做推测）",
        "",
        f"- 探索方式：{report.get('browser_label') or '服务端无头浏览器'}",
        f"- 目标：{report.get('goal') or ''}",
        f"- 起始站点：{report.get('started_url') or ''}",
        f"- 探索结论：{report.get('summary') or '（未总结）'}",
        f"- 结束原因：{report.get('stopped_reason') or ''}",
        "",
        "## 页面清单",
    ]
    for p in report.get("pages") or []:
        lines.append(f"- {p.get('title') or '(无标题)'}：{p.get('url') or ''}")
    lines.append("")
    lines.append("## 主要功能点")
    for f in report.get("features") or []:
        lines.append(f"- {f}")
    lines.append("")
    lines.append("## 探索过程（节选）")
    for st in (report.get("steps") or [])[-20:]:
        mark = "成功" if st.get("ok") else "失败"
        seg = f"- {st.get('action')}"
        if st.get("target"):
            seg += f" [{st['target']}]"
        if st.get("value"):
            seg += f" {st['value']}"
        seg += f"（{mark}"
        if st.get("note"):
            seg += f"；记录：{st['note']}"
        if st.get("error"):
            seg += f"；{str(st['error'])[:80]}"
        seg += "）"
        lines.append(seg)
    if report.get("console"):
        lines.append("")
        lines.append("## 页面控制台错误（供用例设计参考，可能不算缺陷）")
        for e in report["console"][:10]:
            lines.append(f"- {e}")
    return "\n".join(lines)


# ----------------------------
# 探索主循环
# ----------------------------

async def run_exploration(
    cfg: BrowseConfig,
    *,
    llm: Callable[[list[dict]], Awaitable[str]],
    browser=None,
    screenshot_dir: Optional[str] = None,
    on_event: Optional[Callable[[dict], Awaitable[None]]] = None,
    max_shots: int = _MAX_SHOTS,
) -> dict:
    """执行有界浏览器探索，返回观察报告（任何异常都收敛为报告，不向上抛）。

    ``browser`` 省略时使用服务端无头浏览器；也可传 ClientBrowser（客户端可见浏览器）。
    """
    owns_browser = browser is None
    journal: list[dict] = []
    pages: dict[str, dict] = {}
    console: list[str] = []
    report: dict = {
        "ok": False,
        "goal": cfg.goal,
        "started_url": cfg.url,
        "summary": "",
        "features": [],
        "pages": [],
        "console": [],
        "steps": [],
        "stopped_reason": "error",
        "error": None,
    }
    start_ts = time.time()
    deadline = start_ts + max(30, int(cfg.timeout_s))

    async def _emit(evt: dict) -> None:
        if on_event is None:
            return
        try:
            await on_event(evt)
        except Exception:
            logger.debug("browse on_event failed", exc_info=True)

    async def _shot(name: str) -> Optional[str]:
        if not screenshot_dir or len(pages) >= max_shots:
            return None
        path = os.path.join(screenshot_dir, name)
        return await browser.screenshot(path)

    async def _page_facts() -> dict:
        return await browser.page_facts()

    async def _observe() -> dict:
        snapshot = await browser.snapshot()
        from core.locator_candidates import (
            actionable_candidates,
            extract_candidates,
        )

        page = await _page_facts()
        candidates = actionable_candidates(extract_candidates(snapshot or ""))
        return {
            "snapshot": snapshot,
            "candidates": candidates,
            "page": page,
            "page_text": _snapshot_text_excerpt(snapshot or ""),
        }

    try:
        if owns_browser:
            browser = ServerBrowser()
        await browser.start()
        report["browser_label"] = getattr(browser, "label", "")

        # 1) 打开起始页
        t0 = time.time()
        res = await browser.act({"action": "goto", "value": cfg.url})
        ok = bool(res.get("success"))
        journal.append({
            "action": "goto", "target": None, "value": cfg.url,
            "ok": ok, "error": (str(res.get("error"))[:160] if not ok else None),
        })
        if ok:
            await asyncio.sleep(0.8)
            obs = await _observe()
            page = obs["page"]
            if page.get("url"):
                shot = await _shot("01_start.png")
                pages[page["url"]] = {
                    "url": page["url"], "title": page.get("title") or "", "screenshot": shot,
                }
        else:
            obs = {"snapshot": "", "candidates": tuple(), "page": {"url": "", "title": ""}}
            page = obs["page"]
        await _emit({
            "kind": "step",
            "label": (
                f"打开 {page.get('title') or cfg.url}" if ok
                else f"打开失败：{str(res.get('error') or '')[:60]}"
            ),
            "url": page.get("url"),
            "step_count": 1,
            "ok": ok,
            "elapsed": round(time.time() - t0, 1),
        })
        if not ok:
            raise RuntimeError(f"打开起始页失败: {res.get('error') or '未知错误'}")

        # 2) 决策循环
        stopped = "max_steps"
        for _round in range(int(cfg.max_steps)):
            if time.time() > deadline:
                stopped = "timeout"
                break
            prompt = _round_prompt(
                cfg, page=obs["page"], candidates=obs["candidates"],
                journal=journal, visited=pages,
                page_text=obs.get("page_text") or "",
            )
            try:
                raw = await asyncio.wait_for(
                    llm([
                        {"role": "system", "content": EXPLORER_SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ]),
                    timeout=max(20, int(deadline - time.time())),
                )
            except asyncio.TimeoutError:
                stopped = "timeout"
                break
            except Exception as exc:
                report["error"] = f"探索模型调用失败: {str(exc)[:160]}"
                stopped = "error"
                break

            decision = parse_decision(raw)
            if decision is None:
                journal.append({
                    "action": "(无效输出)", "ok": False,
                    "error": "模型未返回合法 JSON，已提示重试",
                })
                continue

            action_name = str(decision.get("action") or "").strip().lower()
            note = redact_text(str(decision.get("note") or "").strip()[:200],
                               [cfg.username, cfg.password])
            # ZCode 式时间线：模型的旁白文本先于动作出现（观察报告/决策理由）
            if note:
                await _emit({
                    "kind": "note",
                    "text": note,
                    "elapsed": round(time.time() - start_ts, 1),
                })

            if action_name == "done":
                report["summary"] = redact_text(
                    str(decision.get("summary") or "").strip(),
                    [cfg.username, cfg.password],
                )
                feats = decision.get("features")
                if isinstance(feats, list):
                    report["features"] = [
                        redact_text(str(f).strip()[:200], [cfg.username, cfg.password])
                        for f in feats if str(f).strip()
                    ][:30]
                stopped = "done"
                break

            if action_name not in ACTION_WHITELIST:
                journal.append({
                    "action": action_name or "(空)",
                    "ok": False,
                    "error": f"不在允许的动作内 {ACTION_WHITELIST}",
                })
                continue

            # 验证码：识别 + 自动填写（引擎侧完成，不经过通用动作通道）
            if action_name == "solve_captcha":
                ref = str(decision.get("ref") or "").strip()
                refresh = bool(decision.get("refresh"))
                if not cfg.captcha_enabled:
                    journal.append({
                        "action": action_name, "target": None, "ok": False,
                        "error": "验证码自动识别已关闭（系统设置 chat_browse_captcha_enabled）",
                    })
                    continue
                from core.captcha import solve_captcha_via_browser

                result = await solve_captcha_via_browser(
                    browser, input_ref=(ref or None), refresh=refresh,
                    username=cfg.username or "", password=cfg.password or "",
                )
                captcha_ok = bool(result.get("ok"))
                journal.append({
                    "action": "solve_captcha",
                    "target": (_cand_label(ref, obs["candidates"]) if ref else "验证码图片"),
                    "value": (
                        f"已填写（{result.get('text_len') or 0} 位）" if captcha_ok else None
                    ),
                    "ok": captcha_ok,
                    "error": (str(result.get("error"))[:160] if not captcha_ok else None),
                    "note": f"刷新后重新识别并填写{result.get('note') or ''}" if (captcha_ok and refresh) else (result.get("note") if captcha_ok else None),
                })
                await _emit({
                    "kind": "step",
                    "label": (
                        ("刷新验证码并识别填写" if refresh else "识别验证码并填写")
                        + ("（成功）" if captcha_ok else f"（失败：{str(result.get('error') or '')[:50]}）")
                    ),
                    "url": page.get("url"),
                    "step_count": len(journal),
                    "ok": captcha_ok,
                    "elapsed": round(time.time() - start_ts, 1),
                })
                if captcha_ok:
                    await asyncio.sleep(0.6)
                    obs = await _observe()
                    page = obs["page"]
                continue

            # 元素动作的 ref 校验（防编造）
            action = {"action": action_name}
            if action_name in ("click", "fill", "press", "select"):
                ref = str(decision.get("ref") or "").strip()
                candidate_refs = {c.ref for c in obs["candidates"]}
                if ref not in candidate_refs:
                    journal.append({
                        "action": action_name, "target": ref or None,
                        "ok": False, "error": "ref 不在当前候选元素中，请重新观察",
                    })
                    continue
                if action_name == "click":
                    cand = next((c for c in obs["candidates"] if c.ref == ref), None)
                    label_check = f"{cand.name or ''} {cand.text or ''}" if cand else ""
                    if _DANGEROUS_RE.search(label_check):
                        journal.append({
                            "action": action_name, "target": ref, "ok": False,
                            "error": f"禁止破坏性操作（{label_check.strip()[:30]}），请跳过",
                            "note": note or None,
                        })
                        continue
                action["selector"] = ref
                if action_name in ("fill", "press", "select"):
                    action["value"] = str(decision.get("value") or "")
            elif action_name == "goto":
                target = str(decision.get("url") or "").strip()
                if not target or not _same_site(target, cfg.url):
                    journal.append({
                        "action": "goto", "value": target or None, "ok": False,
                        "error": "goto 只允许回到起始站点内的地址，请改用 click 导航",
                    })
                    continue
                action["value"] = target
            elif action_name == "wait":
                action["value"] = str(decision.get("value") or "1")

            # 时间线分工：note 作为独立文本行（上面的 note 事件），步骤行用动作描述
            # （ZCode 式「工具行 · 已完成」），避免同一句话出现两遍
            label = _action_label(action_name, decision, obs["candidates"])
            executed = _substitute_credentials(action, cfg)
            res = await browser.act(executed)
            ok = bool(res.get("success"))
            step = {
                "action": action_name,
                "target": _cand_label(action.get("selector"), obs["candidates"]),
                # 含凭据的值一律打码，不落日志/卡片
                "value": _masked_value(action, cfg),
                "ok": ok,
                "error": (str(res.get("error"))[:160] if not ok else None),
                "note": note or None,
            }
            journal.append(step)

            if ok:
                await asyncio.sleep(0.8)
                prev_url = page.get("url")
                obs = await _observe()
                page = obs["page"]
                if page.get("url") and page["url"] not in pages:
                    shot = await _shot(f"{len(pages) + 1:02d}_page.png")
                    pages[page["url"]] = {
                        "url": page["url"],
                        "title": page.get("title") or "",
                        "screenshot": shot,
                    }
                elif page.get("url") != prev_url and pages.get(page["url"]):
                    pages[page["url"]]["title"] = page.get("title") or pages[page["url"]]["title"]
            await _emit({
                "kind": "step",
                "label": label,
                "url": page.get("url"),
                "step_count": len(journal),
                "ok": ok,
                "elapsed": round(time.time() - start_ts, 1),
            })

        # 3) 收尾证据：console（只读，失败静默）
        report["stopped_reason"] = stopped
        if not report.get("summary"):
            report["summary"] = _auto_summary(journal, pages, stopped)
        try:
            console = await browser.console(20)
        except Exception:
            console = []
        report["console"] = console
        report["pages"] = list(pages.values())
        report["steps"] = journal
        report["ok"] = bool(pages) and stopped != "error"
        report["elapsed_s"] = round(time.time() - start_ts, 1)
        return report
    except Exception as exc:  # noqa: BLE001 — 探索失败收敛为报告
        logger.warning("browse exploration failed: %s", exc, exc_info=True)
        report["error"] = str(exc)[:300]
        report["stopped_reason"] = "error"
        report["pages"] = list(pages.values())
        report["steps"] = journal
        return report
    finally:
        try:
            await browser.stop()
        except Exception:
            logger.debug("browse browser stop failed", exc_info=True)
