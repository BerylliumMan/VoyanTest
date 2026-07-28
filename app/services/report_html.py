"""批次执行报告 — 静态 HTML 导出渲染。

输出可离线打开的单文件 HTML（截图以 data URI 内嵌）。
"""

from __future__ import annotations

import base64
import html
import logging
import mimetypes
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_REPORTS_ROOT = Path(os.path.abspath("reports"))


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _safe_file(path: str | None) -> Path | None:
    if not path:
        return None
    resolved = Path(os.path.abspath(path))
    if not str(resolved).startswith(str(_REPORTS_ROOT)):
        return None
    return resolved if resolved.is_file() else None


def _screenshot_data_uri(path: str | None) -> str | None:
    """将截图转为 data URI，便于 HTML 离线查看。"""
    safe = _safe_file(path)
    if not safe:
        return None
    try:
        data = safe.read_bytes()
        mime = mimetypes.guess_type(safe.name)[0] or "image/png"
        return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
    except OSError:
        logger.warning("无法读取截图: %s", path, exc_info=True)
        return None


def _fmt_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "—"
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return "—"
    if s < 60:
        return f"{s:.1f}s"
    m, rem = divmod(s, 60)
    return f"{int(m)}m {rem:.0f}s"


def _fmt_time(iso: str | None) -> str:
    """将 ISO 时间格式化为 Asia/Shanghai 本地时间。"""
    if not iso:
        return "—"
    try:
        from datetime import datetime, timezone
        from app.tz import CST

        raw = str(iso).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            # DB/序列化偶发无时区：按 UTC 解释再转 CST，避免再偏 8 小时
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(CST).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return str(iso).replace("T", " ")[:19]


def _status_class(status: str | None) -> str:
    s = (status or "").lower()
    if s in ("passed", "success", "ok"):
        return "ok"
    if s in ("failed", "error", "fail"):
        return "fail"
    if s in ("running", "pending"):
        return "run"
    return "muted"


def _step_ok(step: dict[str, Any]) -> bool:
    if "success" in step:
        return bool(step.get("success"))
    level = (step.get("level") or step.get("status") or "").lower()
    return level in ("info", "success", "passed", "ok")


def render_batch_report_html(detail: dict[str, Any]) -> str:
    """将 ``export_batch_report`` 的字典渲染为完整 HTML 文档。"""
    name = detail.get("name") or f"Batch #{detail.get('id', '')}"
    project = detail.get("project_name") or "—"
    status = detail.get("status") or "—"
    total = int(detail.get("total_cases") or 0)
    passed = int(detail.get("passed") or 0)
    failed = int(detail.get("failed") or 0)
    rate = round(100.0 * passed / total, 1) if total else 0.0
    runs = detail.get("runs") or []

    case_blocks: list[str] = []
    for idx, run in enumerate(runs, start=1):
        r_status = run.get("status") or "—"
        steps = run.get("steps") or []
        step_rows: list[str] = []
        for step in steps:
            ok = _step_ok(step)
            sn = step.get("step_number") or step.get("step_order") or ""
            desc = (
                step.get("original_description")
                or step.get("description")
                or step.get("action")
                or "—"
            )
            err = step.get("error") or ""
            action = step.get("action") or ""
            ver = step.get("verification") or ""
            dur = step.get("duration_ms")
            dur_txt = f"{float(dur):.0f} ms" if isinstance(dur, (int, float)) else ""
            ss_uri = _screenshot_data_uri(step.get("screenshot_path"))

            meta_bits = []
            if action:
                meta_bits.append(f"<code>{_esc(action)}</code>")
            if dur_txt:
                meta_bits.append(_esc(dur_txt))
            meta_html = " · ".join(meta_bits)

            err_html = (
                f'<div class="step-error">{_esc(err)}</div>' if err and not ok else ""
            )
            ver_html = (
                f'<div class="step-note">{_esc(ver)}</div>' if ver else ""
            )
            img_html = (
                f'<figure class="shot"><img src="{ss_uri}" alt="step screenshot" loading="lazy" /></figure>'
                if ss_uri
                else ""
            )

            step_rows.append(
                f"""
            <li class="step {'ok' if ok else 'fail'}">
              <div class="step-head">
                <span class="badge">{'通过' if ok else '失败'}</span>
                <span class="step-no">步骤 {_esc(sn)}</span>
                <span class="step-desc">{_esc(desc)}</span>
              </div>
              {f'<div class="step-meta">{meta_html}</div>' if meta_html else ''}
              {err_html}{ver_html}{img_html}
            </li>"""
            )

        steps_html = (
            f'<ol class="steps">{"".join(step_rows)}</ol>'
            if step_rows
            else '<p class="empty">暂无步骤明细</p>'
        )

        case_blocks.append(
            f"""
        <article class="case {_status_class(r_status)}" id="case-{idx}" data-case-index="{idx}">
          <header class="case-head" role="button" tabindex="0" aria-expanded="false" aria-controls="case-body-{idx}">
            <div class="case-title">
              <span class="chevron" aria-hidden="true"></span>
              <span class="case-index">{idx:02d}</span>
              <h2>{_esc(run.get('case_name') or f"Case #{run.get('case_id')}")}</h2>
            </div>
            <div class="case-meta">
              <span class="pill {_status_class(r_status)}">{_esc(r_status)}</span>
              <span>{_esc(_fmt_duration(run.get('duration')))}</span>
              <span>{len(steps)} 步</span>
            </div>
          </header>
          <div class="case-body" id="case-body-{idx}" hidden>
            {steps_html}
          </div>
        </article>"""
        )

    cases_html = "".join(case_blocks) or '<p class="empty">本批次没有用例运行记录</p>'
    total_runs = len(runs)

    toc_items: list[str] = []
    for i, r in enumerate(runs, start=1):
        case_label = r.get("case_name") or f"Case #{r.get('case_id')}"
        toc_items.append(
            f'<li><a href="#case-{i}" data-jump-case="{i}">{_esc(case_label)}</a>'
            f' <span class="pill {_status_class(r.get("status"))}">{_esc(r.get("status") or "—")}</span></li>'
        )
    toc_html = "".join(toc_items) or "<li>无</li>"

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>VoyanTest 报告 — {_esc(name)}</title>
<style>
  :root {{
    --bg: #eef2f4;
    --ink: #14212b;
    --muted: #5b6b76;
    --line: #d5dee5;
    --paper: #ffffff;
    --brand: #0b6e6a;
    --brand-ink: #083f3d;
    --ok: #1f7a4d;
    --ok-soft: #e6f5ee;
    --fail: #b42318;
    --fail-soft: #fdecea;
    --run: #9a6700;
    --run-soft: #fff6e0;
    --shadow: 0 18px 40px rgba(20, 33, 43, 0.08);
  }}
  * {{ box-sizing: border-box; }}
  html {{ scroll-behavior: smooth; }}
  body {{
    margin: 0;
    color: var(--ink);
    background:
      radial-gradient(1200px 480px at 10% -10%, rgba(11, 110, 106, 0.16), transparent 60%),
      radial-gradient(900px 420px at 100% 0%, rgba(20, 33, 43, 0.08), transparent 55%),
      var(--bg);
    font-family: "IBM Plex Sans", "Noto Sans SC", "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    line-height: 1.55;
  }}
  a {{ color: var(--brand); }}
  .wrap {{
    width: min(1080px, calc(100% - 32px));
    margin: 0 auto;
    padding: 36px 0 72px;
  }}
  .hero {{
    display: grid;
    gap: 18px;
    margin-bottom: 28px;
  }}
  .brand {{
    font-size: 13px;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    color: var(--brand);
    font-weight: 700;
  }}
  h1 {{
    margin: 0;
    font-size: clamp(28px, 4vw, 42px);
    line-height: 1.15;
    letter-spacing: -0.02em;
  }}
  .subtitle {{
    margin: 0;
    color: var(--muted);
    font-size: 15px;
  }}
  .summary {{
    display: grid;
    grid-template-columns: 1.1fr 1.4fr;
    gap: 18px;
    align-items: stretch;
  }}
  @media (max-width: 820px) {{
    .summary {{ grid-template-columns: 1fr; }}
  }}
  .rate-panel, .stats {{
    background: var(--paper);
    border: 1px solid var(--line);
    border-radius: 18px;
    box-shadow: var(--shadow);
  }}
  .rate-panel {{
    padding: 28px 26px;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    min-height: 180px;
  }}
  .rate-num {{
    font-size: clamp(48px, 8vw, 72px);
    font-weight: 700;
    letter-spacing: -0.04em;
    color: var(--brand-ink);
    line-height: 1;
  }}
  .rate-label {{ color: var(--muted); margin-top: 8px; }}
  .bar {{
    margin-top: 22px;
    height: 10px;
    border-radius: 999px;
    background: #e7eef2;
    overflow: hidden;
  }}
  .bar > span {{
    display: block;
    height: 100%;
    width: {rate}%;
    background: linear-gradient(90deg, #0b6e6a, #1aa39a);
  }}
  .stats {{
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 0;
  }}
  .stat {{
    padding: 22px 24px;
    border-bottom: 1px solid var(--line);
    border-right: 1px solid var(--line);
  }}
  .stat:nth-child(2n) {{ border-right: none; }}
  .stat:nth-last-child(-n+2) {{ border-bottom: none; }}
  .stat .k {{ color: var(--muted); font-size: 12px; letter-spacing: 0.04em; }}
  .stat .v {{ margin-top: 8px; font-size: 22px; font-weight: 650; }}
  .stat .v.ok {{ color: var(--ok); }}
  .stat .v.fail {{ color: var(--fail); }}
  .meta-row {{
    margin-top: 16px;
    display: flex;
    flex-wrap: wrap;
    gap: 10px 16px;
    color: var(--muted);
    font-size: 13px;
  }}
  .pill {{
    display: inline-flex;
    align-items: center;
    padding: 2px 10px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 650;
    background: #e8eef2;
    color: var(--muted);
  }}
  .pill.ok {{ background: var(--ok-soft); color: var(--ok); }}
  .pill.fail {{ background: var(--fail-soft); color: var(--fail); }}
  .pill.run {{ background: var(--run-soft); color: var(--run); }}
  .toc {{
    margin: 28px 0 18px;
    padding: 16px 18px;
    background: rgba(255,255,255,0.7);
    border: 1px solid var(--line);
    border-radius: 14px;
  }}
  .toc h3 {{ margin: 0 0 10px; font-size: 13px; color: var(--muted); letter-spacing: 0.08em; text-transform: uppercase; }}
  .toc ol {{ margin: 0; padding-left: 18px; }}
  .toc li {{ margin: 4px 0; }}
  .list-toolbar {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
    margin: 8px 0 4px;
    color: var(--muted);
    font-size: 13px;
  }}
  .list-toolbar-right {{
    display: flex;
    align-items: center;
    gap: 14px;
    flex-wrap: wrap;
  }}
  .page-size-label {{
    display: inline-flex;
    align-items: center;
    gap: 8px;
  }}
  .page-size-label select {{
    appearance: none;
    border: 1px solid var(--line);
    background: var(--paper);
    color: var(--ink);
    border-radius: 8px;
    padding: 4px 28px 4px 10px;
    font: inherit;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    background-image: linear-gradient(45deg, transparent 50%, var(--muted) 50%),
      linear-gradient(135deg, var(--muted) 50%, transparent 50%);
    background-position: calc(100% - 14px) 55%, calc(100% - 9px) 55%;
    background-size: 5px 5px, 5px 5px;
    background-repeat: no-repeat;
  }}
  .page-size-label select:hover {{
    border-color: var(--brand);
  }}
  .page-size-label select:focus-visible {{
    outline: 2px solid var(--brand);
    outline-offset: 1px;
  }}
  .case {{
    background: var(--paper);
    border: 1px solid var(--line);
    border-radius: 18px;
    box-shadow: var(--shadow);
    margin: 16px 0;
    overflow: hidden;
  }}
  .case.ok {{ border-left: 4px solid var(--ok); }}
  .case.fail {{ border-left: 4px solid var(--fail); }}
  .case.run {{ border-left: 4px solid var(--run); }}
  .case-head {{
    display: flex;
    justify-content: space-between;
    gap: 16px;
    align-items: flex-start;
    padding: 18px 20px;
    cursor: pointer;
    user-select: none;
  }}
  .case-head:hover {{ background: #f7fafb; }}
  .case-head:focus-visible {{
    outline: 2px solid var(--brand);
    outline-offset: -2px;
  }}
  .case.open .case-head {{
    border-bottom: 1px solid var(--line);
  }}
  .case-title {{ display: flex; gap: 10px; align-items: baseline; min-width: 0; }}
  .chevron {{
    width: 0;
    height: 0;
    border-top: 5px solid transparent;
    border-bottom: 5px solid transparent;
    border-left: 7px solid var(--muted);
    flex: 0 0 auto;
    transform: translateY(2px);
    transition: transform 0.15s ease;
  }}
  .case.open .chevron {{
    transform: translateY(2px) rotate(90deg);
  }}
  .case-index {{
    font-variant-numeric: tabular-nums;
    color: var(--brand);
    font-weight: 700;
    font-size: 14px;
  }}
  .case-head h2 {{
    margin: 0;
    font-size: 18px;
    line-height: 1.35;
    word-break: break-word;
  }}
  .case-meta {{
    display: flex;
    gap: 10px;
    align-items: center;
    color: var(--muted);
    font-size: 13px;
    white-space: nowrap;
  }}
  .case-body[hidden] {{ display: none !important; }}
  .steps {{
    list-style: none;
    margin: 0;
    padding: 8px 0 12px;
  }}
  .step {{
    padding: 14px 20px;
    border-bottom: 1px dashed var(--line);
  }}
  .step:last-child {{ border-bottom: none; }}
  .step-head {{
    display: flex;
    gap: 10px;
    align-items: baseline;
    flex-wrap: wrap;
  }}
  .badge {{
    font-size: 11px;
    font-weight: 700;
    padding: 2px 8px;
    border-radius: 999px;
  }}
  .step.ok .badge {{ background: var(--ok-soft); color: var(--ok); }}
  .step.fail .badge {{ background: var(--fail-soft); color: var(--fail); }}
  .step-no {{ color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; }}
  .step-desc {{ font-weight: 600; }}
  .step-meta {{
    margin-top: 6px;
    color: var(--muted);
    font-size: 12px;
  }}
  .step-meta code {{
    font-family: "IBM Plex Mono", "Cascadia Code", Consolas, monospace;
    background: #f3f6f8;
    padding: 1px 6px;
    border-radius: 6px;
  }}
  .step-error {{
    margin-top: 8px;
    padding: 10px 12px;
    border-radius: 10px;
    background: var(--fail-soft);
    color: var(--fail);
    font-size: 13px;
    white-space: pre-wrap;
    word-break: break-word;
  }}
  .step-note {{
    margin-top: 8px;
    color: var(--muted);
    font-size: 13px;
  }}
  .shot {{
    margin: 12px 0 0;
    border-radius: 12px;
    overflow: hidden;
    border: 1px solid var(--line);
    background: #0b1218;
  }}
  .shot img {{
    display: block;
    width: 100%;
    max-height: 520px;
    object-fit: contain;
    background: #0b1218;
  }}
  .empty {{
    margin: 0;
    padding: 18px 20px;
    color: var(--muted);
  }}
  .pager {{
    display: flex;
    justify-content: center;
    align-items: center;
    gap: 10px;
    margin-top: 20px;
    flex-wrap: wrap;
  }}
  .pager button {{
    appearance: none;
    border: 1px solid var(--line);
    background: var(--paper);
    color: var(--ink);
    border-radius: 10px;
    padding: 8px 14px;
    font: inherit;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
  }}
  .pager button:hover:not(:disabled) {{
    border-color: var(--brand);
    color: var(--brand);
  }}
  .pager button:disabled {{
    opacity: 0.45;
    cursor: not-allowed;
  }}
  .pager .page-info {{
    color: var(--muted);
    font-size: 13px;
    min-width: 120px;
    text-align: center;
  }}
  footer.site {{
    margin-top: 36px;
    color: var(--muted);
    font-size: 12px;
    text-align: center;
  }}
  @media print {{
    body {{ background: #fff; }}
    .wrap {{ width: 100%; padding: 0; }}
    .rate-panel, .stats, .case, .toc {{ box-shadow: none; }}
    .shot img {{ max-height: none; }}
    .pager, .list-toolbar {{ display: none !important; }}
    .case {{ display: block !important; }}
    .case-body {{ display: block !important; }}
    .case-body[hidden] {{ display: block !important; }}
  }}
</style>
</head>
<body>
  <div class="wrap">
    <header class="hero">
      <div class="brand">VoyanTest Report</div>
      <h1>{_esc(name)}</h1>
      <p class="subtitle">项目 {_esc(project)} · 批次状态 {_esc(status)}</p>
      <div class="summary">
        <section class="rate-panel">
          <div>
            <div class="rate-num">{rate}%</div>
            <div class="rate-label">通过率 · {passed}/{total} 用例通过</div>
          </div>
          <div class="bar"><span></span></div>
        </section>
        <section class="stats">
          <div class="stat"><div class="k">总用例</div><div class="v">{total}</div></div>
          <div class="stat"><div class="k">通过</div><div class="v ok">{passed}</div></div>
          <div class="stat"><div class="k">失败</div><div class="v fail">{failed}</div></div>
          <div class="stat"><div class="k">开始时间</div><div class="v" style="font-size:16px">{_esc(_fmt_time(detail.get('started_at') or detail.get('created_at')))}</div></div>
        </section>
      </div>
      <div class="meta-row">
        <span>结束：{_esc(_fmt_time(detail.get('finished_at')))}</span>
        <span>批次 ID：{_esc(detail.get('id'))}</span>
        <span>导出为离线 HTML（截图已内嵌）</span>
      </div>
    </header>

    <nav class="toc">
      <h3>用例目录</h3>
      <ol>
        {toc_html}
      </ol>
    </nav>

    <div class="list-toolbar">
      <span>默认折叠步骤与截图，点击用例行展开</span>
      <div class="list-toolbar-right">
        <label class="page-size-label">
          每页
          <select id="page-size" aria-label="每页显示数量">
            <option value="10">10</option>
            <option value="20" selected>20</option>
            <option value="50">50</option>
            <option value="100">100</option>
          </select>
          条
        </label>
        <span id="page-summary"></span>
      </div>
    </div>

    <main id="case-list" data-page-size="20" data-total="{total_runs}">
      {cases_html}
    </main>

    <nav class="pager" id="pager" hidden>
      <button type="button" id="page-prev">上一页</button>
      <span class="page-info" id="page-info">1 / 1</span>
      <button type="button" id="page-next">下一页</button>
    </nav>

    <footer class="site">Generated by VoyanTest · 静态执行报告</footer>
  </div>
<script>
(function () {{
  var list = document.getElementById('case-list');
  if (!list) return;
  var cases = Array.prototype.slice.call(list.querySelectorAll('.case'));
  var allowedSizes = {{ '10': 1, '20': 1, '50': 1, '100': 1 }};
  var pageSizeSelect = document.getElementById('page-size');
  var pageSize = 20;
  if (pageSizeSelect && allowedSizes[pageSizeSelect.value]) {{
    pageSize = parseInt(pageSizeSelect.value, 10);
  }} else {{
    var raw = parseInt(list.getAttribute('data-page-size') || '20', 10);
    pageSize = allowedSizes[String(raw)] ? raw : 20;
    if (pageSizeSelect) pageSizeSelect.value = String(pageSize);
  }}
  var total = cases.length;
  var totalPages = Math.max(1, Math.ceil(total / pageSize) || 1);
  var page = 1;
  var pager = document.getElementById('pager');
  var pageInfo = document.getElementById('page-info');
  var pageSummary = document.getElementById('page-summary');
  var prevBtn = document.getElementById('page-prev');
  var nextBtn = document.getElementById('page-next');

  function setExpanded(article, open) {{
    var head = article.querySelector('.case-head');
    var body = article.querySelector('.case-body');
    if (!head || !body) return;
    article.classList.toggle('open', open);
    head.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) body.removeAttribute('hidden');
    else body.setAttribute('hidden', '');
  }}

  function recalcPages() {{
    totalPages = Math.max(1, Math.ceil(total / pageSize) || 1);
    if (page > totalPages) page = totalPages;
  }}

  function renderPage() {{
    var start = (page - 1) * pageSize;
    var end = start + pageSize;
    cases.forEach(function (el, idx) {{
      var onPage = idx >= start && idx < end;
      el.style.display = onPage ? '' : 'none';
      if (!onPage) setExpanded(el, false);
    }});
    if (pager) {{
      if (totalPages > 1) pager.removeAttribute('hidden');
      else pager.setAttribute('hidden', '');
    }}
    if (pageInfo) pageInfo.textContent = page + ' / ' + totalPages;
    if (pageSummary) {{
      var from = total ? start + 1 : 0;
      var to = Math.min(end, total);
      pageSummary.textContent = total
        ? ('第 ' + from + '–' + to + ' 条，共 ' + total + ' 条用例')
        : '无用例';
    }}
    if (prevBtn) prevBtn.disabled = page <= 1;
    if (nextBtn) nextBtn.disabled = page >= totalPages;
  }}

  cases.forEach(function (article) {{
    var head = article.querySelector('.case-head');
    if (!head) return;
    head.addEventListener('click', function () {{
      var open = !article.classList.contains('open');
      setExpanded(article, open);
    }});
    head.addEventListener('keydown', function (ev) {{
      if (ev.key === 'Enter' || ev.key === ' ') {{
        ev.preventDefault();
        head.click();
      }}
    }});
  }});

  if (pageSizeSelect) {{
    pageSizeSelect.addEventListener('change', function () {{
      var next = parseInt(pageSizeSelect.value, 10);
      if (!allowedSizes[String(next)]) return;
      pageSize = next;
      list.setAttribute('data-page-size', String(pageSize));
      page = 1;
      recalcPages();
      renderPage();
    }});
  }}

  if (prevBtn) prevBtn.addEventListener('click', function () {{
    if (page > 1) {{ page -= 1; renderPage(); window.scrollTo({{ top: list.offsetTop - 24, behavior: 'smooth' }}); }}
  }});
  if (nextBtn) nextBtn.addEventListener('click', function () {{
    if (page < totalPages) {{ page += 1; renderPage(); window.scrollTo({{ top: list.offsetTop - 24, behavior: 'smooth' }}); }}
  }});

  document.querySelectorAll('[data-jump-case]').forEach(function (a) {{
    a.addEventListener('click', function (ev) {{
      var idx = parseInt(a.getAttribute('data-jump-case') || '0', 10);
      if (!idx) return;
      ev.preventDefault();
      var targetPage = Math.ceil(idx / pageSize);
      page = targetPage;
      renderPage();
      var el = document.getElementById('case-' + idx);
      if (el) {{
        setExpanded(el, true);
        el.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
      }}
    }});
  }});

  recalcPages();
  renderPage();
}})();
</script>
</body>
</html>
"""
