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
import tempfile
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

ACTION_WHITELIST = (
    "goto", "click", "fill", "press", "select",
    # B1 探索动作面扩展（2026-10-14）：滚动/悬停/勾选/批量填表/对话框/后退/标签页
    "scroll", "hover", "check", "fill_form", "dialog", "navigate_back", "tabs",
    # B5 只读探针（2026-10-15）：links/nav/storage_keys/frameworks 固定只读 JS
    "js_probe",
    "wait", "solve_captcha", "done",
)

# B5：只读探针 JS（固定库，模型只能选名字，不能自写代码；全部纯读取）
_JS_PROBES = {
    "links": (
        "() => JSON.stringify({links: Array.from(document.querySelectorAll('a[href]'))"
        ".map(a => ({t: (a.innerText || a.getAttribute('aria-label') || '').trim()"
        ".replace(/\\s+/g, ' ').slice(0, 40), h: a.href}))"
        ".filter(x => x.h && x.h.indexOf('http') === 0).slice(0, 100)})"
    ),
    "nav": (
        "() => JSON.stringify({links: Array.from(document.querySelectorAll("
        "'nav a[href], header a[href], aside a[href], [role=menubar] a[href], "
        "[role=menu] a[href], .menu a[href], .sidebar a[href], .navbar a[href]'))"
        ".map(a => ({t: (a.innerText || a.getAttribute('aria-label') || '').trim()"
        ".replace(/\\s+/g, ' ').slice(0, 40), h: a.href}))"
        ".filter(x => x.h && x.h.indexOf('http') === 0).slice(0, 100)})"
    ),
    "storage_keys": (
        "() => { const keys = (s) => { try { return Object.keys(s); } catch (e) { return []; } };"
        " return JSON.stringify({local: keys(window.localStorage), session: keys(window.sessionStorage)}); }"
    ),
    "frameworks": (
        "() => { const g = []; ['__NUXT__', '__NEXT_DATA__', 'Vue', 'React', 'angular', 'jQuery']"
        ".forEach(k => { try { if (window[k] !== undefined) g.push(k); } catch (e) {} });"
        " return JSON.stringify({globals: g,"
        " scripts: Array.from(document.querySelectorAll('script[src]')).map(s => s.src).slice(0, 30),"
        " title: document.title}); }"
    ),
}

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
    # 视觉：每轮决策附当前页面截图（模型可直接读到布局/弹窗/验证码；2026-10-14 实测
    # deepseek-v4.1-flash 多模态可用）。关闭后退回纯文本快照。
    vision: bool = True
    vision_max_width: int = 1152
    # 035+：文档驱动——用户上传的测试文档（docx/pdf/xlsx 提取文本，已截断）。
    # 非空时按文档中的步骤/检查项执行测试（提示词注入【参考文档】块）。
    document_text: str = ""


EXPLORER_SYSTEM_PROMPT = """你是 VoyanTest 的「网页探索器」：在真实浏览器里以纯 GUI 黑盒方式操作网页，产出可供测试用例生成使用的观察报告。

【目标理解】
【目标】可能是两种之一：
A. 探索性目标（如「查看主要功能」）：自行规划浏览路径，优先访问主要功能页面；
B. 操作性 / 测试描述（如「点登录，输入账号密码，进入设置页」「测试设置页留空保存的校验提示」）：严格按描述顺序逐步操作；其中「测试/验证/观察」类要求，在安全准则内执行代表性操作并观察真实结果（提示文案、跳转、校验状态、数据变化），把观察到的真实行为记录为功能点（note/features）；描述执行完成后，再补充观察当前页面与相关入口；未按描述验证到的点，在 summary 中如实说明（不要脑补结果）。
C. 提供【参考文档】时：文档内容即测试要求（来自用户上传的测试文档）。按文档中可执行的步骤/检查项操作与验证；文档中不可执行、超出安全准则或与当前站点无关的要求跳过，并在 summary 中如实说明。

【行为准则 — 严格遵守】
0.（视觉，若随消息附有页面截图）截图展示的是当前页面真实渲染结果：可用于判断布局、
   弹窗/遮挡、加载是否完成、图形验证码等内容；但元素动作仍必须使用【候选元素】列表中的 ref，
   截图不作为元素定位依据（截图与快照冲突时，以快照候选为准）。
1. 只能操作【候选元素】列表中真实存在的元素（用其 ref）；禁止编造元素、禁止猜测选择器；
1b.（文案点击兜底）当目标控件在候选里没有自己的 ref（span/div 包裹的非标准控件、候选里只有
   承载它的容器）时：给 click 加 "text" 字段——值必须与【页面文本】/快照中的按钮文案逐字一致
   （如 "text": "新增"）；系统会按该文案定位真实元素并点击。text 不在当前快照中会被拒绝；
2. 禁止用 JS 注入或修改 URL 来改变页面状态；goto 只允许回到起始地址或站内已访问过的地址；
3. 禁止破坏性操作：点击名称含「退出/注销/删除/移除/清空/支付/付款/下单/结算/购买」的元素一律不做，改为记录并跳过；
4. 需要登录时：在登录表单里用 <<USERNAME>> 填账号、<<PASSWORD>> 填密码（占位符原样输出，系统会替换；绝不输出真实凭据），然后点击登录按钮，并确认登录成功（地址变化 / 出现导航菜单 / 出现用户名）；**登录表单优先用 fill_form 一次填完账号+密码（避免分步填写期间验证码/页面过期）**；
5. 登录失败只重试一次（核对表单后），仍失败就如实收尾（summary 说明失败原因）；
5b.（图形验证码）登录表单含验证码（输入框占位/标签含「验证码」，旁边有验证码图片）时：
   按 账号 → 密码 → 验证码 → 登录 的顺序操作；验证码一步输出 solve_captcha（ref=验证码输入框的 ref；
   系统会自动识别并填写，不需要 value，不要询问用户、不要猜测内容）。若登录后仍停留在登录页或提示
   「验证码错误」，输出 solve_captcha 且 refresh=true（先点击验证码图片刷新再识别）重试，最多 3 次
   （验证码错误的重试不受规则 5 的一次限制），仍失败按规则 5 如实收尾。若重试时登录页已被重置（用户名/密码
   输入框为空）：「必须先重新填写账号与密码，再 solve_captcha（refresh=true），最后点击登录」——
   禁止只改验证码就直接提交。算术型验证码（图片是「1+2=?」这类）系统会自动计算答案，同样用 solve_captcha；
   若系统识别失败（步骤会显示失败原因），先 refresh=true 重试；仍失败则用 note 记录该情况并继续其他步骤；
   若附有页面截图且验证码清晰可读：也可以直接用 fill 把读到的字符填进验证码输入框（截图可直接读图）；
   不确定或读不清时优先 solve_captcha（本地识别确定性更高）；
6. 浏览策略：优先点击顶部导航 / 侧边菜单等入口，逐个访问主要功能页面；每到一个新页面，用 note 记录「页面标题：主要功能点」；
7. 操作失败时先重新观察（看最新快照）再决定，不要重复同样的动作；点击失败时系统会自动做遮挡诊断（步骤记录里会附「被遮挡/在视口外/禁用」等结论）——按诊断提示处理（关闭/移开遮挡层、滚动到目标、等动画结束）后再试，不要原样重复点击；动作（点击/提交/跳转）完成后系统会自动给出
   最新页面快照（含跳转结果），不要用 wait 步骤等待页面加载或跳转结果；wait 仅用于页面内延时场景（如等提示出现），值用秒数；
8. 步数有限：先做最有价值的事（登录 → 主菜单 → 核心功能页 2-4 个）；
9. 目标达成或主要功能已覆盖 → 立即返回 done，并在 summary/features 给出结论；禁止虚构没看到的功能；
9b.（只读探针）js_probe 不修改页面，用于补齐「看不到的」结构信息：
    links=页面全部链接（含折叠菜单/隐藏入口，可发现快照里没有的页面）；nav=导航区链接；
    storage_keys=localStorage/sessionStorage 的键名（不含值）；frameworks=框架/脚本线索。
    探针发现的新页面仍需逐页实访验证后才算「看到」，不要在 summary 里把没访问过的链接写成已验证功能。

【输出格式 — 只输出一个 JSON 对象，不要任何其他文字、不要代码块】
{
  "thinking": "1-3 句：当前页面是什么、下一步做什么及原因",
  "action": "goto|click|fill|press|select|scroll|hover|check|fill_form|dialog|navigate_back|tabs|js_probe|wait|solve_captcha|done",
  "ref": "元素动作必填：候选列表中的 ref（如 e12）；solve_captcha 填验证码输入框的 ref；fill_form 不需要（见 fields）",
  "text": "click 可选兜底：目标控件的按钮文案（必须与页面文本逐字一致；仅当候选里没有该控件的 ref、只有承载容器时使用，如 \"新增\"）",
  "value": "fill=输入值（凭据用占位符）；press=按键（如 Enter）；select=选项文本；wait=等待文本或秒数；scroll=像素数|up|down；dialog=accept|dismiss；tabs=list|new|select:N|close；js_probe=links|nav|storage_keys|frameworks；solve_captcha 不需要 value",
  "fields": "fill_form 专用：JSON 数组，一次填写多个字段，如 [{\"target\":\"e11\",\"name\":\"用户名\",\"type\":\"textbox\",\"value\":\"<<USERNAME>>\"},{\"target\":\"e13\",\"name\":\"密码\",\"type\":\"textbox\",\"value\":\"<<PASSWORD>>\"}]（type 可为 textbox/checkbox/radio/combobox/slider；凭据用占位符）",
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

    async def network(self, limit: int = 10) -> list[str]:
        """B2：网络问题请求（FAILED/4xx/5xx）摘要；失败静默。"""
        try:
            r = await self._mcp.call_tool("browser_network_requests", {"static": False})
            if not isinstance(r, dict) or not r.get("success", True):
                return []
            from core.run_evidence import network_problem_lines

            return network_problem_lines(str(r.get("text") or ""), limit)
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

    async def network(self, limit: int = 10) -> list[str]:
        """B2：网络问题请求摘要（客户端 act 通道；旧客户端未知动作 → 静默空）。"""
        try:
            res = await asyncio.wait_for(
                self._manager.send_act(self._agent, self._run_id, {"name": "network", "args": {}}),
                timeout=30,
            )
            if not (isinstance(res, dict) and res.get("success")):
                return []
            from core.run_evidence import network_problem_lines

            return network_problem_lines(
                str(res.get("text") or res.get("result") or ""), limit,
            )
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
    from core.locator_candidates import prioritize_candidates

    selected, omitted = prioritize_candidates(tuple(candidates), _MAX_CANDIDATES_IN_PROMPT)
    lines: list[str] = []
    for idx, c in enumerate(selected, start=1):
        label = (c.name or c.text or "").strip().replace("\n", " ")[:60]
        lines.append(f"[{idx}] {c.ref} | {c.role} | \"{label}\"")
    if omitted:
        lines.append(
            f"（候选共 {len(candidates)} 个，此处显示 {len(selected)} 个："
            f"优先列出按钮/输入框等操作控件，被省略的多为列表行内链接/复选框）"
        )
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
    doc_block = ""
    if cfg.document_text:
        doc_block = f"\n【参考文档（用户上传的测试文档节选；按文档内容执行测试）】\n{cfg.document_text}\n"
    return f"""【目标】{cfg.goal}
{doc_block}【起始站点】{cfg.url}
【登录凭据】账号占位符 {_USERNAME_PLACEHOLDER}；密码占位符 {_PASSWORD_PLACEHOLDER}（fill 时原样使用，系统自动替换，禁止输出真实值）

【已访问页面】
{vp}

【当前页面】
标题：{page.get('title') or '(未知)'}
地址：{page.get('url') or '(未知)'}

【页面文本（节选，供如实记录提示/校验/数据文案；不要推测未出现的文本）】
{page_text or '（无）'}

【候选元素】（只能引用这些 ref；若某目标文案在【页面文本】出现却不在候选里，
多为非标准控件或候选过多被省略——先重新观察/滚动，禁止编造 ref）
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


def frame_to_data_url(path: str, max_width: int = 1152, quality: int = 72) -> Optional[str]:
    """截图文件 → data URL（JPEG 压缩降 token；PIL 不可用时原样 base64 兜底）。"""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except Exception:
        return None
    if not raw:
        return None
    try:
        import io as _io

        from PIL import Image

        im = Image.open(_io.BytesIO(raw)).convert("RGB")
        if im.width > max_width:
            im = im.resize((max_width, int(im.height * max_width / im.width)), Image.LANCZOS)
        buf = _io.BytesIO()
        im.save(buf, format="JPEG", quality=quality)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        mime = "image/png" if raw[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
        return f"data:{mime};base64," + base64.b64encode(raw).decode()


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
    if action_name == "fill_form":
        return "批量填写表单"
    if action_name == "scroll":
        return f"滚动 {str(decision.get('value') or 'down')[:20]}"
    if action_name == "dialog":
        return f"处理浏览器对话框（{str(decision.get('value') or 'accept')[:20]}）"
    if action_name == "navigate_back":
        return "浏览器后退"
    if action_name == "tabs":
        return f"标签页 {str(decision.get('value') or 'list')[:20]}"
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
    if action_name == "hover":
        return f"悬停 {label}"
    if action_name == "check":
        return f"勾选/取消勾选 {label}"
    if action_name == "wait":
        return f"等待 {decision.get('value')}"
    return action_name


def _substitute_credentials(action: dict, cfg: BrowseConfig) -> dict:
    """fill/fill_form 值占位符 → 真实凭据（仅在此处接触明文，不落任何日志）。"""
    if str(action.get("action")) not in ("fill", "fill_form", "press_key"):
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


def _dedupe_seq(seq) -> list:
    """去重（空白/标点/序号归一后比较），保持原顺序。"""
    seen: set[str] = set()
    out: list = []
    for item in seq or []:
        key = re.sub(r"[\s\d\.、，,：:；;（）()【】\[\]「」『』\-—_]+", "", str(item or "")).lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def build_report_materials(report: dict) -> str:
    """观察报告 → 生成管线材料（2026-10-19 二次重写：切断元信息污染源）。

    第一版修复后实测仍漏两类污染：① 模型把「### 探索过程（节选）」「### 网络请求异常」
    章节标题当作页面名，造出「打开【探索过程】」类步骤；② 控制台/网络噪声被当成
    「用例」（如「静态资源请求失败记录状态校验」）。根因是原始过程记录进材料后，
    模型总能把其中的标题/行文当成页面元素。本次直接**不再输出**过程/控制台/网络/
    链接等原始记录——材料只保留：生成要求 + 站点与结论 + 已观察页面（去重）+
    主要功能点（去重）；功能点为空时给出「宁少勿滥」的诚实提示。
    """
    pages: list[tuple[str, str]] = []
    seen_pages: set[tuple[str, str]] = set()
    for p in report.get("pages") or []:
        url = str(p.get("url") or "").strip()
        title = str(p.get("title") or "").strip() or "(无标题)"
        if not url:
            continue
        key = (url, title)
        if key in seen_pages:
            continue
        seen_pages.add(key)
        pages.append((title, url))
    features = _dedupe_seq([str(f).strip() for f in (report.get("features") or []) if str(f or "").strip()])

    lines = [
        "# UI 自动化测试用例生成材料（来源：浏览器探索观察）",
        "",
        "【生成要求 — 优先遵守】",
        "1. 本材料是一份「页面观察记录」，不是需求文档：只根据下方「主要功能点」与「已观察页面」生成用例；",
        "2. 禁止把本材料的标题、章节名、元信息（如「页面探索观察报告」「起始站点」「探索过程」「网络请求异常」「站内链接」等字样）当作页面元素、步骤目标或断言对象；",
        "3. 用例标题必须具体（动作 + 对象，如「按价格排序商品列表」），禁止「打开首页」这类无信息量标题；同一页面最多一条「打开」步骤，且只在用例需要时保留；",
        "4. 相同或高度相似的用例只保留一条；重复的功能点合并为一条用例；",
        "5. 只写实际观察到的页面与功能，不得编造未观察到的弹窗、数据或流程；",
        f"6. 主要功能点共 {len(features)} 条——请为每条功能点生成 1 条最完整的用例（总数控制在 {len(features)} 条上下）；同一功能点不要生成多条相似用例；已观察页面不必单独生成「打开页面」用例（登录跳转作为必要前置除外）。",
        "",
        f"- 起始站点：{report.get('started_url') or ''}",
        f"- 探索结论：{report.get('summary') or '（未总结）'}",
        "",
        "## 已观察页面",
    ]
    for title, url in pages:
        lines.append(f"- {title}：{url}")
    lines.append("")
    lines.append("## 主要功能点")
    for f in features:
        lines.append(f"- {f}")
    if not features:
        lines.append("（本次探索未及总结主要功能点：请仅依据上述已观察页面与探索结论生成基础用例，不得编造未观察到的功能；宁少勿滥。）")
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
        "network": [],
        "link_map": [],
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

    vision_state = {"on": bool(cfg.vision)}

    async def _vision_frame() -> Optional[str]:
        """当前页面截图 → data URL（视觉决策用）；失败静默返回 None。"""
        if screenshot_dir:
            path = os.path.join(screenshot_dir, "_live_frame.png")
        else:
            path = os.path.join(tempfile.gettempdir(), f"vt_browse_live_{os.getpid()}.png")
        try:
            shot = await browser.screenshot(path)
        except Exception:
            return None
        if not shot:
            return None
        return frame_to_data_url(str(shot), max_width=int(cfg.vision_max_width or 1152))

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
            frame = await _vision_frame() if vision_state["on"] else None
            user_content = prompt
            if frame:
                user_content = [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": frame}},
                ]

            async def _call_llm(content):
                return await asyncio.wait_for(
                    llm([
                        {"role": "system", "content": EXPLORER_SYSTEM_PROMPT},
                        {"role": "user", "content": content},
                    ]),
                    timeout=max(20, int(deadline - time.time())),
                )

            try:
                raw = await _call_llm(user_content)
            except asyncio.TimeoutError:
                stopped = "timeout"
                break
            except Exception as exc:
                if frame is not None:
                    # 网关/模型不支持图片（换过模型）→ 纯文本降级重试一次，其余轮次关闭视觉
                    logger.warning(
                        "browse vision call failed, fallback to text-only: %s", str(exc)[:200]
                    )
                    vision_state["on"] = False
                    try:
                        raw = await _call_llm(prompt)
                    except asyncio.TimeoutError:
                        stopped = "timeout"
                        break
                    except Exception as exc2:
                        report["error"] = f"探索模型调用失败: {str(exc2)[:160]}"
                        stopped = "error"
                        break
                else:
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

            # B5：只读探针（固定库；不改页面状态）
            if action_name == "js_probe":
                probe_name = str(decision.get("value") or "links").strip().lower()
                from app.config import get_settings as _gs_jp

                def _js_probe_on() -> bool:
                    try:
                        from app.runtime_config import chat_browse_config as _cb

                        return bool(getattr(_cb, "js_probe", getattr(_gs_jp(), "chat_browse_js_probe", True)))
                    except Exception:
                        return bool(getattr(_gs_jp(), "chat_browse_js_probe", True))

                if not _js_probe_on():
                    journal.append({
                        "action": "js_probe", "target": probe_name, "ok": False,
                        "error": "只读探针已关闭（系统设置 chat_browse_js_probe）",
                    })
                    continue
                if probe_name not in _JS_PROBES:
                    journal.append({
                        "action": "js_probe", "target": probe_name, "ok": False,
                        "error": f"未知探针（可用：{'/'.join(_JS_PROBES)}）",
                    })
                    continue
                _probe_data = await browser.evaluate_json(_JS_PROBES[probe_name])
                _probe_ok = isinstance(_probe_data, dict)
                _probe_summary = ""
                if _probe_ok and probe_name in ("links", "nav"):
                    _links = [
                        str(x.get("h") or "")
                        for x in (_probe_data.get("links") or [])
                        if isinstance(x, dict) and x.get("h")
                    ]
                    _probe_summary = f"{len(_links)} 个链接：" + "、".join(_links[:12])
                    if probe_name == "links" and _links:
                        _existing = set(report.get("link_map") or [])
                        report["link_map"] = sorted(_existing | set(_links))[:60]
                elif _probe_ok and probe_name == "storage_keys":
                    _local = [str(k) for k in (_probe_data.get("local") or [])][:20]
                    _sess = [str(k) for k in (_probe_data.get("session") or [])][:20]
                    _probe_summary = (
                        f"localStorage 键：{('、'.join(_local) or '无')}；"
                        f"sessionStorage 键：{('、'.join(_sess) or '无')}"
                    )
                elif _probe_ok and probe_name == "frameworks":
                    _gl = "、".join(str(g) for g in (_probe_data.get("globals") or [])) or "未检出"
                    _probe_summary = (
                        f"框架线索：{_gl}；脚本 {len(_probe_data.get('scripts') or [])} 个；"
                        f"标题「{_probe_data.get('title') or ''}」"
                    )
                journal.append({
                    "action": "js_probe", "target": probe_name,
                    "value": _probe_summary[:400] if _probe_ok else None,
                    "ok": _probe_ok,
                    "error": None if _probe_ok else "探针执行失败",
                })
                await _emit({
                    "kind": "step",
                    "label": f"只读探针 {probe_name}" + ("（完成）" if _probe_ok else "（失败）"),
                    "url": page.get("url"),
                    "step_count": len(journal),
                    "ok": _probe_ok,
                    "elapsed": round(time.time() - start_ts, 1),
                })
                continue

            # 元素动作的 ref 校验（防编造）
            action = {"action": action_name}
            if action_name in ("click", "fill", "press", "select", "hover", "check", "fill_form"):                # B1：fill_form 的 fields 走独立字段（内部 target 逐个校验）
                if action_name == "fill_form":
                    fields = decision.get("fields")
                    if not isinstance(fields, list) or not fields:
                        journal.append({
                            "action": "fill_form", "ok": False,
                            "error": "fields 缺失或不是数组（需给出 target/name/type/value）",
                        })
                        continue
                    candidate_refs = {c.ref for c in obs["candidates"]}
                    bad = [
                        str(f.get("target") or "")
                        for f in fields
                        if isinstance(f, dict) and str(f.get("target") or "") not in candidate_refs
                    ]
                    if bad:
                        journal.append({
                            "action": "fill_form", "ok": False,
                            "error": f"fields 中 ref 不在当前候选元素中: {bad[:3]}，请重新观察",
                        })
                        continue
                    action["value"] = json.dumps(fields, ensure_ascii=False)
                    label = "批量填写表单 " + "、".join(
                        str(f.get("name") or f.get("target"))[:14]
                        for f in fields[:4] if isinstance(f, dict)
                    )
                    executed = _substitute_credentials(action, cfg)
                    res = await browser.act(executed)
                    ok = bool(res.get("success"))
                    journal.append({
                        "action": "fill_form",
                        "target": f"{len(fields)} 个字段",
                        "value": _masked_value(action, cfg),
                        "ok": ok,
                        "error": (str(res.get("error"))[:160] if not ok else None),
                        "note": note or None,
                    })
                    await _emit({
                        "kind": "step",
                        "label": label,
                        "url": page.get("url"),
                        "step_count": len(journal),
                        "ok": ok,
                        "elapsed": round(time.time() - start_ts, 1),
                    })
                    if ok:
                        await asyncio.sleep(0.8)
                        obs = await _observe()
                        page = obs["page"]
                    continue
                ref = str(decision.get("ref") or "").strip()
                click_text_used: str | None = None
                candidate_refs = {c.ref for c in obs["candidates"]}
                if (
                    ref not in candidate_refs
                    and action_name == "click"
                    and str(decision.get("text") or "").strip()
                ):
                    # 文案点击兜底（2026-10-19 实测第三种形态）：span 包文本的按钮在
                    # 快照里没有自己的 ref（只有承载容器甚至无 ref）——用页面真实存在的
                    # 文案点击（必须与当前快照逐字一致；仅 click，防编造）。
                    click_text = str(decision.get("text") or "").strip()
                    if click_text not in (obs.get("snapshot") or ""):
                        journal.append({
                            "action": "click", "target": f"text:{click_text[:30]}",
                            "ok": False,
                            "error": "text 不在当前页面快照中（必须与页面文本逐字一致）",
                        })
                        continue
                    if _DANGEROUS_RE.search(click_text):
                        journal.append({
                            "action": "click", "target": f"text:{click_text[:30]}",
                            "ok": False,
                            "error": f"禁止破坏性操作（{click_text[:30]}），请跳过",
                            "note": note or None,
                        })
                        continue
                    click_text_used = click_text
                    action["selector"] = f"text={click_text}"
                else:
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
            elif action_name == "scroll":
                action["value"] = str(decision.get("value") or "down")
            elif action_name == "dialog":
                action["value"] = str(decision.get("value") or "accept")
            elif action_name == "navigate_back":
                action["action"] = "navigate_back"
            elif action_name == "tabs":
                token = str(decision.get("value") or "list").strip().lower()
                if token.startswith("close"):
                    journal.append({
                        "action": "tabs", "value": token, "ok": False,
                        "error": "探索期禁止关闭标签页（close），list/new/select 可用",
                    })
                    continue
                action["value"] = token
            elif action_name == "wait":
                action["value"] = str(decision.get("value") or "1")

            # B1 修复：press → press_key（此前 whitelist 允许 press 但执行链路无此动作名）
            if str(action.get("action")) == "press":
                action["action"] = "press_key"

            # 时间线分工：note 作为独立文本行（上面的 note 事件），步骤行用动作描述
            # （ZCode 式「工具行 · 已完成」），避免同一句话出现两遍
            label = _action_label(action_name, decision, obs["candidates"])
            executed = _substitute_credentials(action, cfg)
            res = await browser.act(executed)
            ok = bool(res.get("success"))
            step = {
                "action": action_name,
                "target": (
                    f"text:{click_text_used}" if click_text_used
                    else _cand_label(action.get("selector"), obs["candidates"])
                ),
                # 含凭据的值一律打码，不落日志/卡片
                "value": _masked_value(action, cfg),
                "ok": ok,
                "error": (str(res.get("error"))[:160] if not ok else None),
                "note": note or None,
            }
            journal.append(step)

            # B4：点击类失败 → 遮挡/视口诊断（只读；诊断附入步骤记录供下一轮参考）
            if not ok and action_name in (
                "click", "double_click", "right_click", "check", "hover", "press_key"
            ):
                try:
                    from app.config import get_settings as _gs_occ

                    if bool(getattr(_gs_occ(), "exec_occlusion_probe", True)):
                        from core.browser_probe import probe_occlusion

                        async def _occ_caller(tool: str, payload: dict) -> dict:
                            data = await browser.evaluate_json(str(payload.get("function") or ""))
                            if not isinstance(data, dict):
                                return {"success": False}
                            return {"success": True, "text": json.dumps(data, ensure_ascii=False)}

                        _cand = next(
                            (c for c in obs["candidates"] if c.ref == action.get("selector")), None,
                        )
                        _occ_target = ""
                        if _cand is not None:
                            _occ_target = str(
                                getattr(_cand, "name", "") or getattr(_cand, "text", "") or ""
                            ).strip()
                        _probe = await probe_occlusion(_occ_caller, _occ_target)
                        _occ_note = str(_probe.get("note") or "")
                        if not _occ_note and re.search(
                            r"intercepts pointer|拦截|遮挡", str(res.get("error") or ""), re.I
                        ):
                            _occ_note = (
                                "目标被页面元素遮挡（点击被拦截——通常为弹窗/遮罩层覆盖）："
                                "先关闭/移开遮挡层再操作"
                            )
                        if _occ_note:
                            step["error"] = (
                                str(step.get("error") or "") + f" | {_occ_note}"
                            )[:260]
                except Exception:
                    logger.debug("explore 遮挡诊断跳过", exc_info=True)

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

        # 3) 收尾证据：console + 网络问题请求（只读，失败静默）
        report["stopped_reason"] = stopped
        if not report.get("summary"):
            report["summary"] = _auto_summary(journal, pages, stopped)
        try:
            console = await browser.console(20)
        except Exception:
            console = []
        report["console"] = console
        try:
            from app.config import get_settings as _gs_net_br

            if bool(getattr(_gs_net_br(), "exec_network_evidence", True)):
                report["network"] = await browser.network(10)
        except Exception:
            report["network"] = report.get("network") or []
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
