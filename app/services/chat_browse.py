# app/services/chat_browse.py
# 034 迭代（2026-10-10）：对话内浏览器探索生成 ——
#   browse_generate：服务端浏览器（playwright-mcp）打开站点 → 登录 → 浏览主要功能
#   → 产出观察报告卡片 → 自动基于观察结果发起用例生成（复用既有生成管线）→ 草稿卡。
# 设计参考 ZCode web-gui-tester / control-browser（纯 GUI 黑盒、快照优先、截图存证、
# 禁止破坏性操作、诚实报告）；凭据全程占位符替换，不落日志/卡片/审计。
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from datetime import datetime, timezone

from app import crud
from app.config import get_settings
from app.crud import chat as chat_crud
from core.chat.events import emit
from core.chat.orchestrator import ActionOutcome, register_action_handler, serialize_message

logger = logging.getLogger(__name__)

# 后台任务强引用（防 GC）
_TASKS: set[asyncio.Task] = set()
# 进行中的探索任务索引：browse_id -> (conversation_id, task)——供「停止」按对话定位
_TASK_INDEX: dict[str, tuple[int, asyncio.Task]] = {}


def _reports_root() -> str:
    return os.path.join(os.getcwd(), "reports")


def _browse_dir(conversation_id: int, browse_id: str) -> str:
    return os.path.join(_reports_root(), "chat_browse", f"conv_{conversation_id}", browse_id)


def _web_path(abs_path: str | None) -> str | None:
    """把容器内截图绝对路径转换为前端可访问的 /reports 相对路径。"""
    if not abs_path:
        return None
    try:
        rel = os.path.relpath(abs_path, _reports_root())
        return f"reports/{rel}"
    except ValueError:
        return None


def build_streaming_llm(*, on_event, agent_id: int | None):
    """构造流式决策回调（ZCode 式时间线的「思考」数据源）。

    - 网关返回 reasoning 增量 → 直接作为思考增量；
    - 网关不回推理时 → 从决策 JSON 的 thinking 字段做部分提取（增量）；
    - 调用结束发 thinking_done（全文 + 持续秒数）。
    ``on_event``：async callable(dict)，事件 kinds：thinking_delta / thinking_done。
    """

    async def _llm(messages):
        from app.gen.model_client import call_model
        from core.chat import stream as chat_stream

        partial = chat_stream.PartialDecisionStream()
        seen_reasoning = {"v": False}
        acc: list[str] = []
        started = time.monotonic()

        async def on_reasoning(chunk: str) -> None:
            seen_reasoning["v"] = True
            acc.append(chunk)
            await on_event({"kind": "thinking_delta", "delta": chunk})

        async def on_content(chunk: str) -> None:
            partial.feed(chunk)
            if not seen_reasoning["v"]:
                cur = partial.get("thinking")
                got = "".join(acc)
                if len(cur) > len(got):
                    delta = cur[len(got):]
                    acc.append(delta)
                    await on_event({"kind": "thinking_delta", "delta": delta})

        try:
            return await call_model(
                messages,
                agent_type="assistant",
                agent_id=agent_id,
                stream_callback=on_content,
                reasoning_callback=on_reasoning,
            )
        finally:
            text = "".join(acc).strip()
            if text:
                await on_event({
                    "kind": "thinking_done",
                    "text": text,
                    "duration": round(time.monotonic() - started, 1),
                })

    return _llm


async def _find_card_message(db, conversation_id: int, browse_id: str):
    msgs = await chat_crud.list_messages_after(db, conversation_id)
    for m in reversed(msgs):
        for c in reversed(m.cards or []):
            if isinstance(c, dict) and c.get("type") == "browse" and c.get("browse_id") == browse_id:
                return m, c
    return None, None


async def _write_card(db, message, card: dict) -> None:
    cards = []
    replaced = False
    for c in message.cards or []:
        if isinstance(c, dict) and c.get("type") == "browse" and c.get("browse_id") == card.get("browse_id"):
            cards.append(card)
            replaced = True
        else:
            cards.append(c)
    if not replaced:
        cards.append(card)
    updated = await chat_crud.update_message_cards(db, message, cards)
    await emit(updated.conversation_id, {"type": "message.updated", "message": serialize_message(updated)})


async def _wait_card_message(db, conversation_id: int, browse_id: str, *, attempts: int = 12):
    """任务早于助手消息落库时，重试等待卡片消息出现。"""
    for i in range(attempts):
        msg, _card = await _find_card_message(db, conversation_id, browse_id)
        if msg is not None:
            return msg
        await asyncio.sleep(0.5 if i < 4 else 1.0)
    return None


async def _run_browse_task(
    *,
    conversation_id: int,
    browse_id: str,
    user_id: int,
    project_id: int,
    kind: str,
    url: str,
    goal: str,
    username: str,
    password: str,
    assistant_agent_id: int | None,
    document_text: str = "",
) -> None:
    """后台执行探索并链式发起生成（全程卡片回填；异常收敛为 failed 卡片）。"""
    from app.database import AsyncSessionLocal

    settings = get_settings()
    max_steps = int(getattr(settings, "chat_browse_max_steps", 14) or 14)
    timeout_s = int(getattr(settings, "chat_browse_timeout_s", 300) or 300)

    async def _update(card_mutator) -> None:
        async with AsyncSessionLocal() as db:
            msg = await _wait_card_message(db, conversation_id, browse_id)
            if msg is None:
                return
            _m, card = await _find_card_message(db, conversation_id, browse_id)
            if card is None:
                return
            new_card = dict(card)
            card_mutator(new_card)
            await _write_card(db, msg, new_card)

    def _base_card() -> dict:
        return {
            "type": "browse",
            "browse_id": browse_id,
            "status": "running",
            "url": url,
            "goal": goal,
            "has_credentials": bool(username or password),
            "steps": [],
            "timeline": [],
            "pages": [],
            "features": [],
            "summary": "",
            "console": [],
            "network": [],
            "started_at": datetime.now(timezone.utc).isoformat(),
            "elapsed": 0,
            "generation": None,
        }

    await _update(lambda c: c.update(_base_card()) if c else None)

    last_write = 0.0
    # ZCode 式时间线：待落库项（节流跳过写库时滞留，下一次写入或收尾时统一冲刷，不丢内容）
    _pending_timeline: list[dict] = []

    async def _on_event(evt: dict) -> None:
        nonlocal last_write
        kind = str(evt.get("kind") or "")
        # 思考增量：仅 WS 瞬时推送（不落库；完整文本在 thinking_done 落时间线）
        if kind == "thinking_delta":
            delta = str(evt.get("delta") or "")
            if delta:
                await emit(conversation_id, {
                    "type": "browse.stream",
                    "conversation_id": conversation_id,
                    "browse_id": browse_id,
                    "delta": delta,
                })
            return

        item = None
        if kind == "thinking_done":
            text = str(evt.get("text") or "").strip()
            if text:
                item = {
                    "type": "thinking",
                    "text": text[:2000],
                    "duration": evt.get("duration"),
                    "at": round(float(evt.get("elapsed") or 0), 1),
                }
            # 通知前端结束瞬时思考流（该块已随下一次写库落时间线）
            await emit(conversation_id, {
                "type": "browse.stream",
                "conversation_id": conversation_id,
                "browse_id": browse_id,
                "done": True,
            })
        elif kind == "note":
            text = str(evt.get("text") or "").strip()
            if text:
                item = {"type": "text", "text": text[:400], "at": round(float(evt.get("elapsed") or 0), 1)}
        elif kind == "step":
            item = {
                "type": "step",
                "label": str(evt.get("label") or "")[:160],
                "url": evt.get("url"),
                "ok": evt.get("ok"),
                "at": round(float(evt.get("elapsed") or 0), 1),
            }
        if item is not None:
            _pending_timeline.append(item)

        now = time.monotonic()
        # thinking/note 内容不可从 report 重建 → 立即写；step 维持 1.5s 节流（收尾由 report 兜底）
        urgent = kind in ("thinking_done", "note", "done")
        if not urgent and now - last_write < 1.5:
            return
        last_write = now
        step = {
            "label": str(evt.get("label") or "")[:160],
            "url": evt.get("url"),
            "ok": evt.get("ok"),
            "at": round(float(evt.get("elapsed") or 0), 1),
        }
        pending_now = list(_pending_timeline)
        _pending_timeline.clear()

        def _mut(c: dict) -> None:
            if kind == "step":
                c["steps"] = (c.get("steps") or [])[-24:] + [step]
            if pending_now:
                c["timeline"] = ((c.get("timeline") or []) + pending_now)[-120:]
            c["elapsed"] = evt.get("elapsed") or c.get("elapsed")

        await _update(_mut)

    report: dict | None = None
    try:
        from core.chat.browse import BrowseConfig, build_report_materials, run_exploration

        _llm = build_streaming_llm(on_event=_on_event, agent_id=assistant_agent_id)

        cfg = BrowseConfig(
            url=url, goal=goal, username=username, password=password,
            max_steps=max_steps, timeout_s=timeout_s,
            captcha_enabled=bool(getattr(settings, "chat_browse_captcha_enabled", True)),
            vision=bool(getattr(settings, "chat_browse_vision", True)),
            document_text=document_text,
        )

        # 浏览器后端：会话选中客户端 → 客户端浏览器（用户机器上，可见）；
        # 未选客户端（服务端）→ 容器内无头浏览器
        browser = None
        browser_mode = "server"
        agent_name = None
        from app.database import AsyncSessionLocal as _DB

        async with _DB() as _db:
            _conv = await chat_crud.get_conversation(_db, conversation_id)
            conv_agent_id = int(getattr(_conv, "agent_id", 0) or 0) if _conv is not None else 0
            if conv_agent_id > 0:
                from app import db_models

                agent = await _db.get(db_models.Agent, conv_agent_id)
                agent_name = getattr(agent, "name", None)
        if conv_agent_id > 0 and agent_name:
            from agent.manager import agent_manager
            from core.chat.browse import ClientBrowser

            browser = ClientBrowser(agent_manager, agent_name, f"browse_{browse_id}")
            browser_mode = "client"
        elif conv_agent_id > 0:
            logger.warning("browse: 会话客户端 #%s 不存在，回退服务端浏览器", conv_agent_id)
        if browser is None:
            from core.chat.browse import ServerBrowser

            browser = ServerBrowser()

        report = await run_exploration(
            cfg,
            llm=_llm,
            browser=browser,
            screenshot_dir=_browse_dir(conversation_id, browse_id),
            on_event=_on_event,
        )
        report["browser_mode"] = browser_mode
        report["agent_name"] = agent_name
    except Exception as exc:  # noqa: BLE001
        logger.exception("chat browse task failed")
        report = {
            "ok": False, "summary": "", "features": [], "pages": [], "console": [],
            "network": [],
            "steps": [], "stopped_reason": "error", "error": str(exc)[:300],
        }

    pages = [
        {
            "url": p.get("url"),
            "title": p.get("title"),
            "screenshot": _web_path(p.get("screenshot")),
        }
        for p in (report.get("pages") or [])
    ]
    failed = (not report.get("ok")) or report.get("stopped_reason") == "error"
    final_status = "failed" if failed and not pages else "done"

    # 链式生成：有页面观察结果即发起（失败/超时但有部分页面时也生成，卡片如实标注）
    generation: dict = {"started": False, "gen_session_id": None, "error": None}
    if pages:
        try:
            from app.services import chat_gen

            async with AsyncSessionLocal() as db:
                conv = await chat_crud.get_conversation(db, conversation_id)
                if conv is not None:
                    materials = build_report_materials(report)
                    card, err, session_id = await chat_gen.start_generation(
                        db, conversation=conv, kind=kind, source="text",
                        description=materials, file_ref=None, user_id=user_id,
                    )
                    if card is not None:
                        generation = {"started": True, "gen_session_id": session_id, "error": None}
                        # 草稿卡单独成一条助手消息（生成 watcher 按 session_id 回填）
                        note_bits = [f"基于浏览器探索结果生成{'UI' if kind == 'ui' else '功能'}用例草稿"]
                        if failed:
                            note_bits.append(
                                f"（探索未完整结束：{report.get('stopped_reason')}，草稿基于已观察到的内容）"
                            )
                        msg = await chat_crud.create_message(
                            db, conversation_id=conversation_id, role="assistant",
                            content="；".join(note_bits) + "。",
                            cards=[card],
                        )
                        await emit(conversation_id, {
                            "type": "message.created",
                            "message": serialize_message(msg),
                        })
                    else:
                        generation = {"started": False, "gen_session_id": None, "error": err}
        except Exception as exc:  # noqa: BLE001
            logger.exception("chat browse chain generation failed")
            generation = {"started": False, "gen_session_id": None, "error": str(exc)[:200]}

    summary = str(report.get("summary") or "")
    if not pages:
        summary = summary or "未能打开目标站点，未采集到页面内容。"
    if report.get("error"):
        summary = (summary + f"（错误：{report['error'][:160]}）").strip()

    def _final(c: dict) -> None:
        c["status"] = final_status
        c["summary"] = summary
        c["features"] = (report.get("features") or [])[:30]
        c["pages"] = pages
        if report.get("browser_mode"):
            c["browser_mode"] = report.get("browser_mode")
        if report.get("agent_name") is not None:
            c["agent_name"] = report.get("agent_name")
        if _pending_timeline:
            c["timeline"] = ((c.get("timeline") or []) + list(_pending_timeline))[-120:]
            _pending_timeline.clear()
        c["steps"] = [
            {
                "label": _step_label(s),
                "url": None,
                "ok": s.get("ok"),
                "at": None,
            }
            for s in (report.get("steps") or [])[-30:]
        ]
        c["console"] = (report.get("console") or [])[:10]
        c["network"] = (report.get("network") or [])[:10]
        c["stopped_reason"] = report.get("stopped_reason")
        c["elapsed"] = report.get("elapsed_s")
        c["generation"] = generation

    await _update(_final)


def _step_label(step: dict) -> str:
    seg = str(step.get("action") or "")
    if step.get("target"):
        seg += f" [{step['target']}]"
    if step.get("value"):
        seg += f" = {step['value']}"
    if step.get("note"):
        seg += f"（{step['note']}）"
    if step.get("error"):
        seg += f"（{str(step['error'])[:80]}）"
    return seg[:200]


async def _mark_browse_stopped(conversation_id: int, browse_id: str) -> None:
    """回写卡片：停止状态（先写卡片再取消任务，避免卡片停在 running）。"""
    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        msg = await _wait_card_message(db, conversation_id, browse_id)
        if msg is None:
            return
        _m, card = await _find_card_message(db, conversation_id, browse_id)
        if card is None or card.get("status") != "running":
            return
        new_card = dict(card)
        new_card["status"] = "stopped"
        new_card["stopped_reason"] = "stopped"
        base = str(new_card.get("summary") or "").strip()
        new_card["summary"] = (base + "（探索已手动停止）").strip()
        await _write_card(db, msg, new_card)


async def stop_browse_tasks(
    conversation_id: int,
    browse_id: Optional[str] = None,
    *,
    card_writer=None,
) -> list[str]:
    """停止进行中的探索任务（取消 asyncio 任务 + 回写卡片 stopped）。

    返回被停止的 browse_id 列表；``card_writer`` 可注入（测试用）。
    """
    writer = card_writer or _mark_browse_stopped
    stopped: list[str] = []
    for bid, (cid, task) in list(_TASK_INDEX.items()):
        if cid != conversation_id:
            continue
        if browse_id and bid != browse_id:
            continue
        if task.done():
            _TASK_INDEX.pop(bid, None)
            continue
        try:
            await writer(conversation_id, bid)
        except Exception:
            logger.debug("mark browse stopped failed bid=%s", bid, exc_info=True)
        task.cancel()
        try:
            await asyncio.wait({task}, timeout=3)
        except Exception:
            logger.debug("wait browse cancel failed bid=%s", bid, exc_info=True)
        _TASK_INDEX.pop(bid, None)
        _TASKS.discard(task)
        stopped.append(bid)
    return stopped


@register_action_handler("browse_generate")
async def browse_generate_handler(db, conversation, action: dict, ctx: dict) -> ActionOutcome:
    """浏览器探索生成：启动后台探索任务，返回 running 卡片。"""
    settings = get_settings()
    if not getattr(settings, "chat_enabled", True):
        return ActionOutcome(ok=False, note="（智能体对话功能已关闭）")
    if not getattr(conversation, "project_id", None):
        return ActionOutcome(ok=False, note="（请先在对话顶部选择项目，再发起浏览器探索）")
    if not getattr(settings, "chat_browse_enabled", True):
        return ActionOutcome(ok=False, note="（浏览器探索能力已关闭，可在系统设置开启）")

    browse_id = uuid.uuid4().hex[:12]

    # 035+：文档驱动探索——解析附件为参考文档（受控路径；失败则明确拒绝）
    document_text = ""
    document_label = ""
    doc_ref = str(action.get("document_ref") or "").strip()
    if doc_ref:
        from app.services.chat_files import extract_attachment_text, resolve_chat_file

        path = resolve_chat_file(doc_ref)
        if path is None:
            return ActionOutcome(ok=False, note="（附件不存在或路径非法，无法按文档探索；可把要求直接写在消息里）")
        max_chars = int(getattr(settings, "chat_attachment_max_chars", 6000) or 6000)
        document_text, doc_err = await asyncio.to_thread(
            extract_attachment_text, path, max_chars=max_chars
        )
        if not document_text:
            return ActionOutcome(ok=False, note=f"（附件未能提取文本：{doc_err}；可把要求直接写在消息里）")
        document_label = os.path.basename(path)

    # 浏览器后端：会话选中客户端 → 用户机器上可见的浏览器；未选 → 服务端无头
    browser_mode = "server"
    agent_name = None
    conv_agent_id = int(getattr(conversation, "agent_id", 0) or 0)
    if conv_agent_id > 0:
        from app import db_models

        agent = await db.get(db_models.Agent, conv_agent_id)
        agent_name = getattr(agent, "name", None)
        if agent_name:
            browser_mode = "client"
    mode_label = f"客户端「{agent_name}」的浏览器" if browser_mode == "client" else "服务端无头浏览器"
    card = {
        "type": "browse",
        "browse_id": browse_id,
        "status": "running",
        "url": action["url"],
        "goal": action["goal"],
        "browser_mode": browser_mode,
        "agent_name": agent_name,
        "has_credentials": bool(action.get("username") or action.get("password")),
        "document": ({"label": document_label, "chars": len(document_text)} if document_text else None),
        "steps": [],
        "timeline": [],
        "pages": [],
        "features": [],
        "summary": "",
        "console": [],
        "network": [],
        "started_at": datetime.now(timezone.utc).isoformat(),
        "elapsed": 0,
        "generation": None,
    }
    task = asyncio.create_task(_run_browse_task(
        conversation_id=conversation.id,
        browse_id=browse_id,
        user_id=ctx.get("user_id"),
        project_id=conversation.project_id,
        kind=action.get("kind") or "ui",
        url=action["url"],
        goal=action["goal"],
        username=action.get("username") or "",
        password=action.get("password") or "",
        assistant_agent_id=getattr(conversation, "assistant_agent_id", None),
        document_text=document_text,
    ))
    _TASKS.add(task)
    _TASK_INDEX[browse_id] = (conversation.id, task)

    def _cleanup(_t: asyncio.Task) -> None:
        _TASKS.discard(_t)
        cur = _TASK_INDEX.get(browse_id)
        if cur is not None and cur[1] is _t:
            _TASK_INDEX.pop(browse_id, None)

    task.add_done_callback(_cleanup)

    cred_note = "（使用你提供的账号登录）" if card["has_credentials"] else ""
    doc_note = f"（按文档《{document_label}》执行）" if document_text else ""
    return ActionOutcome(
        card=card,
        task_id=browse_id,
        note=(
            f"浏览器探索已启动{cred_note}{doc_note}，将在**{mode_label}**中进行：{action['goal']}。"
            "探索过程会实时回显，完成后自动基于页面观察结果生成用例草稿。"
        ),
    )
