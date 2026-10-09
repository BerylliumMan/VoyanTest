/** Best-effort Instant NL → structured_step (align with core/step_normalize.py). */

export type StructuredStep = {
  action: string;
  target_name?: string | null;
  target_role?: string | null;
  value?: string | null;
  disambiguation?: string | null;
  icon_hint?: string | null;
  note?: string | null;
  /** Recorded Playwright/CSS selector solidified from CDP events */
  selector?: string | null;
};

const CTRL_TYPE =
  '(?:下拉框|下拉菜单|下拉列表|选择器|输入框|文本框|文本域|编辑框|组合框|按钮|控件|弹窗|对话框|提示框|模块|菜单|页签|选项卡|链接|区域|图标|图片|图像|箭头|符号|徽标)';

function stripEllipsis(inner: string): string {
  let s = (inner || '').trim();
  if (!s) return s;
  s = s.replace(/[（(][^）)]*(?:…+|\.{2,}|。{2,})[^）)]*[）)]\s*$/, '').trim();
  s = s.replace(/(?:…+|\.{2,}|。{2,})\s*$/, '').trim();
  return s || inner.trim();
}

function stripCtrlSuffix(name: string): string {
  const re = new RegExp(`${CTRL_TYPE}$`, 'i');
  const cleaned = name.replace(re, '').trim();
  return cleaned || name;
}

/** 去掉值外层成对的括号/引号（与后端 _strip_wrappers 对齐）。 */
function stripWrappers(value: string): string {
  let v = (value || '').trim();
  const pairs: Array<[string, string]> = [
    ['【', '】'], ['「', '」'], ['『', '』'], ['"', '"'], ["'", "'"], ['（', '）'], ['(', ')'],
  ];
  let changed = true;
  while (changed && v) {
    changed = false;
    for (const [a, b] of pairs) {
      if (v.startsWith(a) && v.endsWith(b) && v.length >= a.length + b.length) {
        v = v.slice(a.length, v.length - b.length).trim();
        changed = true;
      }
    }
  }
  return v;
}

/** 上传/拖拽路径：中文分隔符归一为逗号（引擎按逗号拆多文件）。 */
function normalizePaths(value: string): string {
  const v = (value || '').trim();
  if (!v) return '';
  return v
    .replace(/[，、；;]+/g, ',')
    .split(',')
    .map((p) => p.trim())
    .filter(Boolean)
    .join(',');
}

function isCloseAllDialogs(s: string): boolean {
  const closeAll =
    /(?:把|将)?所有(?:的)?(?:对话框|弹窗|提示框)/.test(s) ||
    /(?:关闭|关掉)所有(?:的)?(?:对话框|弹窗|提示框)/.test(s);
  const mentionsClose =
    /点击【(?:关闭|X|×)】|点击.*(?:关闭|【X】)|关闭标志|关闭按钮|【X】|形状的关闭/.test(s);
  return !!(closeAll && (mentionsClose || s.includes('关闭') || s.includes('【X】')));
}

function parseCloseAllDialogs(step: string): StructuredStep | null {
  if (!isCloseAllDialogs(step)) return null;
  return {
    action: 'click',
    target_name: '关闭',
    target_role: 'button',
    disambiguation: '所有对话框',
    icon_hint: '【X】形状的关闭标志',
    note: '先等待页面中间对话框出现，再关闭全部（关闭按钮或X）；禁止点「去查看」/消息列表/通知正文',
  };
}

function expandCompound(s: string): string[] {
  if (isCloseAllDialogs(s)) {
    return ['等待弹窗或对话框出现', '点击【关闭】'];
  }
  return [s];
}

function sanitize(step: string): string {
  let s = (step || '').trim();
  if (!s) return s;
  let m: RegExpMatchArray | null;
  let prev = '';
  while (prev !== s) {
    prev = s;
    s = s.replace(/【\s*在\s*【([^】]+)】\s*】/g, '【$1】');
    s = s.replace(/【\s*【([^】]+)】\s*】/g, '【$1】');
  }
  if (
    /^(?:等待(?:页面)?(?:加载)?完成|等待加载完成|等待页面稳定|等待系统(?:自动)?(?:处理|验证|响应)|等待系统处理)$/.test(
      s,
    )
  ) {
    return '等待页面稳定';
  }
  // 点击【产品授权】模块
  m = s.match(new RegExp(`^点击\\s*【([^】]+)】\\s*${CTRL_TYPE}\\s*$`));
  if (m) {
    const label = m[1].trim();
    if (label) return `点击【${label}】`;
  }
  m = s.match(new RegExp(`^点击\\s*(.+?)${CTRL_TYPE}\\s*$`));
  if (m && !s.includes('【')) {
    const label = stripCtrlSuffix(m[1].replace(/^[：:的]+|[：:的]+$/g, '').trim());
    if (label) return `点击【${label}】`;
  }
  m = s.match(/^(?:查看|检查|观察)\s*【([^】]+)】\s*(?:区域|面板|页面|内容|栏)?\s*$/);
  if (m) return `等待【${m[1].trim()}】出现`;
  m = s.match(/^(?:查看|检查|观察)\s*(.+?)\s*(?:区域|面板|页面|内容|栏)\s*$/);
  if (m && !s.includes('【')) {
    const label = m[1].replace(/^[：:的]+|[：:的]+$/g, '').trim();
    if (label) return `等待【${label}】出现`;
  }
  m = s.match(/^设置\s*【([^】]+)】\s*(?:为|成)\s*【([^】]+)】\s*$/);
  if (m) return `在【${m[1].trim()}】中选择【${m[2].trim()}】`;
  m = s.match(/^清空\s*【([^】]+)】/);
  if (m) return `在【${m[1].trim()}】输入 `;
  m = s.match(/^(?:登录系统)?(?:并)?进入\s*【([^】]+)】/);
  if (m) return `打开【${m[1].trim()}】`;
  m = s.match(/^(?:登录系统)?(?:并)?进入\s*(.+?)(?:页面|页)?\s*$/);
  if (m && !s.includes('【')) {
    const label = m[1].replace(/^[：:的]+|[：:的]+$/g, '').trim();
    if (label && label.length <= 40) return `打开【${label}】`;
  }
  if (/^登录系统$/.test(s)) return '点击【登录】';
  // 点击搜索结果中的京州市院
  m = s.match(
    /^点击\s*(?:【)?(?<ctx>搜索结果|筛选结果|列表|下拉|弹窗|菜单)中的?】?\s*【(?<label>[^】]+)】\s*$/,
  );
  if (m?.groups?.label) {
    return `点击【${m.groups.label.trim()}】（${m.groups.ctx}中的）`;
  }
  m = s.match(
    /^点击\s*【(?<ctx>搜索结果|筛选结果|列表|下拉|弹窗|菜单)中的?】\s*(?<label>[^【】\s]+)\s*$/,
  );
  if (m?.groups?.label) {
    return `点击【${m.groups.label.trim()}】（${m.groups.ctx}中的）`;
  }
  m = s.match(
    /^点击\s*(?<ctx>搜索结果|筛选结果|列表|下拉|弹窗|菜单)中的?\s*(?<label>.+?)\s*$/,
  );
  if (m?.groups?.label && !s.includes('【')) {
    const label = m.groups.label.replace(/^[：:的]+|[：:的]+$/g, '').trim();
    if (label) return `点击【${label}】（${m.groups.ctx}中的）`;
  }
  return s;
}

export function parseInstantToStructured(step: string): StructuredStep | null {
  const raw = (step || '').trim();
  if (!raw) return null;

  const closeAll = parseCloseAllDialogs(raw);
  if (closeAll) return closeAll;

  const parts = expandCompound(raw);
  if (parts.length > 1) {
    const parsedParts: StructuredStep[] = [];
    for (const part of parts) {
      const parsed = parseInstantToStructured(part);
      if (parsed) parsedParts.push(parsed);
    }
    if (parsedParts.length) {
      for (let i = parsedParts.length - 1; i >= 0; i -= 1) {
        if (parsedParts[i].action !== 'wait') return parsedParts[i];
      }
      return parsedParts[parsedParts.length - 1];
    }
  }

  const s = sanitize(raw);
  if (!s) return null;

  let m: RegExpMatchArray | null;

  m = s.match(
    /^(?:在\s*)?(?:弹出的)?["'「『]?([^"'」』【】]+)["'」』]?\s*(?:中)?\s*(?:输入|填写|填入)\s*【([^】]+)】\s*$/,
  );
  if (m) {
    const field = stripCtrlSuffix(m[1].replace(/^[：:的]+|[：:的]+$/g, '').trim());
    if (field) {
      return {
        action: 'fill',
        target_name: stripEllipsis(field),
        target_role: 'textbox',
        value: m[2].trim(),
      };
    }
  }

  if (/点击\s*(?:页面)?(?:空白|外侧|遮罩)/.test(s)) {
    return { action: 'click_blank' };
  }

  m = s.match(/^(?:打开|进入)\s*【([^】]+)】/);
  if (m) return { action: 'goto', target_name: stripEllipsis(m[1]) };

  m = s.match(/^等待\s*【([^】]+)】\s*出现/);
  if (m) {
    const label = stripEllipsis(m[1]);
    return { action: 'wait', target_name: label, value: label };
  }
  if (/^等待(?:页面稳定|弹窗或对话框出现|加载完成)$/.test(s)) {
    return { action: 'wait', value: null, target_name: null, note: s };
  }

  m = s.match(/^断言页面包含\s*【([^】]+)】/);
  if (m) return { action: 'assert_text', value: stripEllipsis(m[1]) };
  m = s.match(/^断言\s*【([^】]+)】\s*可见/);
  if (m) return { action: 'assert_visible', target_name: stripEllipsis(m[1]) };

  m = s.match(/^(?:在\s*)?【([^】]+)】\s*(?:中)?\s*(?:输入|填写|填入)\s*(.+)$/);
  if (m) {
    return {
      action: 'fill',
      target_name: stripEllipsis(m[1]),
      target_role: 'textbox',
      value: m[2].trim(),
    };
  }

  m = s.match(/^(?:在\s*)?【([^】]+)】\s*(?:中)?\s*选择\s*【([^】]+)】\s*$/);
  if (m) {
    return {
      action: 'select',
      target_name: stripEllipsis(m[1]),
      target_role: 'combobox',
      value: stripEllipsis(m[2]),
    };
  }

  m = s.match(/^选择\s*【([^】]+)】\s*$/);
  if (m) {
    const label = stripEllipsis(m[1]);
    return { action: 'select', target_name: label, target_role: 'option', value: label };
  }

  m = s.match(/^取消勾选\s*【([^】]+)】/);
  if (m) {
    return { action: 'uncheck', target_name: stripEllipsis(m[1]), target_role: 'checkbox' };
  }
  m = s.match(/^勾选\s*【([^】]+)】/);
  if (m) {
    return { action: 'check', target_name: stripEllipsis(m[1]), target_role: 'checkbox' };
  }

  m = s.match(/^悬停\s*【([^】]+)】/);
  if (m) return { action: 'hover', target_name: stripEllipsis(m[1]) };

  // 双击 / 右键点击（描述优先编辑新增）
  m = s.match(
    new RegExp(`^(?:双击|连续点击)\\s*[【「『"']([^】」』"']+)[】」』"']\\s*(?:[（(]([^）)]+)[）)])?\\s*$`),
  );
  if (m) {
    const out: StructuredStep = {
      action: 'double_click',
      target_name: stripEllipsis(m[1]),
      target_role: 'button',
    };
    if (m[2]) out.disambiguation = m[2].trim();
    return out;
  }
  m = s.match(
    new RegExp(`^(?:右键点击|右键|右击)\\s*[【「『"']([^】」』"']+)[】」』"']\\s*(?:[（(]([^）)]+)[）)])?\\s*$`),
  );
  if (m) {
    const out: StructuredStep = {
      action: 'right_click',
      target_name: stripEllipsis(m[1]),
      target_role: 'button',
    };
    if (m[2]) out.disambiguation = m[2].trim();
    return out;
  }

  m = s.match(
    new RegExp(`^点击\\s*【([^】]+)】\\s*(?:[（(]([^）)]+)[）)])?\\s*(?:${CTRL_TYPE})?\\s*$`),
  );
  if (m) {
    const out: StructuredStep = {
      action: 'click',
      target_name: stripEllipsis(m[1]),
      target_role: 'button',
    };
    if (m[2]) out.disambiguation = m[2].trim();
    return out;
  }

  m = s.match(/^(?:查看|检查|观察)\s*【([^】]+)】/);
  if (m) {
    const label = stripEllipsis(m[1]);
    return { action: 'wait', target_name: label, value: label };
  }
  m = s.match(/^(?:查看|检查|观察)\s*(.+?)\s*(?:区域|面板|页面|内容|栏)\s*$/);
  if (m && !s.includes('【')) {
    const label = stripEllipsis(m[1].replace(/^[：:的]+|[：:的]+$/g, '').trim());
    if (label) return { action: 'wait', target_name: label, value: label };
  }

  m = s.match(new RegExp(`^点击.+?【([^】]+)】\\s*(?:${CTRL_TYPE})?\\s*$`));
  if (m) {
    return { action: 'click', target_name: stripEllipsis(m[1]), target_role: 'button' };
  }

  m = s.match(/^(?:在\s*)?【([^】]+)】\s*(?:中)?\s*选择\s*(.+)$/);
  if (m) {
    return {
      action: 'select',
      target_name: stripEllipsis(m[1]),
      target_role: 'combobox',
      value: m[2].trim(),
    };
  }

  // 上传文件 / 拖拽文件 / 原生弹窗（描述优先编辑新增）
  m = s.match(/^在【([^】]+)】\s*(?:中)?\s*上传\s*(?:文件|附件)\s*[：:]?\s*(.+)$/);
  if (m) {
    const paths = normalizePaths(stripWrappers(m[2].trim()));
    if (paths) {
      return { action: 'upload', target_name: stripEllipsis(m[1]), value: paths };
    }
  }
  m = s.match(/^上传\s*(?:文件|附件)\s*[：:]?\s*(.+)$/);
  if (m) {
    const paths = normalizePaths(stripWrappers(m[1].trim()));
    if (paths) {
      return { action: 'upload', target_name: '文件', value: paths };
    }
  }
  m = s.match(/^上传\s*(.+)$/);
  if (m) {
    const rest = m[1].trim();
    let tail = rest.replace(/^(?:文件|附件|图片|头像|截图|证书|文档|视频|压缩包)/, '').trim();
    tail = tail.replace(/^[：:]+/, '').trim();
    tail = stripWrappers(tail);
    const looksLikePath = /[/\\]/.test(tail) || /\.\w{1,8}$/.test(tail);
    if (looksLikePath) {
      return { action: 'upload', target_name: '文件', value: normalizePaths(tail) };
    }
    return { action: 'upload', target_name: '文件', value: null, note: s };
  }

  m = s.match(/^(?:拖拽|拖动|拖放)\s*(?:文件\s*)?(.+?)\s*到\s*[【「『"']([^】」』"']+)[】」』"']\s*$/);
  if (m) {
    const paths = normalizePaths(stripWrappers(m[1].trim()));
    if (paths) {
      return { action: 'drop', target_name: stripEllipsis(m[2]), value: paths };
    }
  }
  m = s.match(/^(?:拖拽|拖动|拖放)\s*(?:文件\s*)?(.+?)\s*到\s*(.+?)\s*$/);
  if (m) {
    const paths = normalizePaths(stripWrappers(m[1].trim()));
    const target = m[2].trim().replace(/^[：:的]+|[：:的]+$/g, '').trim();
    if (paths && target && !m[2].includes('【')) {
      return { action: 'drop', target_name: stripEllipsis(target), value: paths };
    }
  }

  // 原生弹窗：接受弹窗 / 拒绝弹窗 / 接受弹窗：输入文本
  m = s.match(/^接受\s*(?:原生|浏览器|系统)?\s*(?:弹窗|对话框|提示框)\s*(?:[：:]\s*(.+))?$/);
  if (m) {
    const text = (m[1] || '').trim();
    return { action: 'dialog', value: text ? `accept:${text}` : 'accept' };
  }
  m = s.match(/^拒绝\s*(?:原生|浏览器|系统)?\s*(?:弹窗|对话框|提示框)\s*$/);
  if (m) {
    return { action: 'dialog', value: 'dismiss' };
  }
  m = s.match(/^(?:关闭|取消)\s*(?:原生|浏览器|系统)\s*(?:弹窗|对话框|提示框)\s*$/);
  if (m) {
    return { action: 'dialog', value: 'dismiss' };
  }

  // 选择 …文件（下拉选文件类选项；上传请用「上传文件 路径」）
  m = s.match(/^选择\s*(.+?文件.*?)\s*$/);
  if (m) {
    return { action: 'select', target_name: '文件', value: m[1].trim(), note: s };
  }

  // Last resort: extract first 【label】 as click target
  m = s.match(/【([^】]+)】/);
  if (m) {
    return { action: 'click', target_name: stripEllipsis(m[1]), target_role: 'button' };
  }

  return null;
}

export function structuredStepIsComplete(step: StructuredStep | null | undefined): boolean {
  if (!step || !step.action) return false;
  const action = String(step.action);
  if (action === 'click_blank') return true;
  if (action === 'wait' && (step.value || step.target_name || step.note)) return true;
  if (action === 'icon_click') return !!(step.icon_hint || step.target_name);
  if (action === 'double_click' || action === 'right_click') return !!step.target_name;
  if (action === 'upload') return !!step.value;
  if (action === 'drop') return !!(step.target_name && step.value);
  if (action === 'dialog') return !!step.value;
  if (['fill', 'select', 'press_key', 'assert_text'].includes(action)) {
    return !!(step.target_name || step.value);
  }
  return !!step.target_name;
}

/** Fill empty structured fields from Instant description for UI editing. */
export function hydrateStructuredFromDescription(
  description: string,
  structured: Record<string, unknown> | null | undefined,
): Record<string, unknown> | null {
  const desc = (description || '').trim();
  let current = structured && typeof structured === 'object' ? { ...structured } : null;

  // 目标=搜索结果中的 + 值=京州市院 → 纠正为可点击文案
  if (current) {
    const action = String(current.action || '');
    const name = String(current.target_name || '').trim();
    const value = current.value == null ? '' : String(current.value).trim();
    const isContext =
      /(?:搜索结果|筛选结果|下拉|列表|弹窗|对话框|菜单|树|表格|选项).{0,6}中的?$/.test(name) ||
      /(?:中的|里的)$/.test(name);
    if (action === 'click' && name && value && isContext) {
      current = {
        ...current,
        target_name: value,
        value: null,
        disambiguation: current.disambiguation || name,
      };
    }
    if (action === 'fill' && name) {
      let cleaned = name.replace(/\\/g, '').replace(/^(?:在\s*)?(?:弹出的)?/, '').trim();
      cleaned = cleaned.replace(/^["'「『]+|["'」』]+$/g, '').replace(/中\s*$/, '').trim();
      if (cleaned) current = { ...current, target_name: cleaned };
    }
  }

  // Close-all Instant kept as click「关闭」only — backfill 消歧/图标/备注
  if (desc && isCloseAllDialogs(desc)) {
    const rich = parseCloseAllDialogs(desc)!;
    if (!current) return { ...rich };
    const mergedClose: Record<string, unknown> = { ...rich, ...current };
    for (const [k, v] of Object.entries(rich)) {
      const cur = mergedClose[k];
      if (cur == null || cur === '') mergedClose[k] = v;
    }
    if (!mergedClose.action) mergedClose.action = rich.action;
    return mergedClose;
  }

  if (structuredStepIsComplete(current as StructuredStep)) {
    return current;
  }
  if (!desc) {
    return current || { action: 'click' };
  }
  const parsed = parseInstantToStructured(desc);
  if (!parsed) {
    // 描述优先：无法结构化时不再伪造 click 目标，交给引擎按描述执行
    return current;
  }
  // Prefer existing non-empty fields; fill gaps from parse.
  const merged: Record<string, unknown> = { ...parsed, ...(current || {}) };
  for (const [k, v] of Object.entries(parsed)) {
    const cur = merged[k];
    if (cur == null || cur === '') merged[k] = v;
  }
  if (!merged.action) merged.action = parsed.action || 'click';
  return merged;
}

/**
 * 描述优先编辑：按描述重新派生结构化提示（严格模式）。
 *
 * 与 hydrate 的差别：用户改写描述后不再保留旧的动作语义；仅当动作 + 目标
 * 与旧结构化一致时，保留固化 selector 与旧补充字段（消歧/图标/备注等）。
 * 无法解析时返回 null —— 执行时由引擎按描述走 LLM 意图解析（绝不伪造动作）。
 */
export function deriveStructuredFromDescription(
  description: string,
  prev: Record<string, unknown> | null | undefined,
): Record<string, unknown> | null {
  const desc = (description || '').trim();
  if (!desc) return null;
  const parsed = parseInstantToStructured(desc);
  if (!parsed) return null;
  const out: Record<string, unknown> = { ...parsed };
  const prevAction = String(prev?.action || '');
  const prevName = String(prev?.target_name || '');
  const sameTarget =
    !!prev && prevAction === String(parsed.action) && prevName === String(parsed.target_name || '');
  if (sameTarget) {
    for (const k of ['selector', 'disambiguation', 'icon_hint', 'note', 'target_role', 'frame_hint', 'wait_mode'] as const) {
      const cur = out[k];
      const old = prev ? prev[k] : undefined;
      if ((cur == null || cur === '') && old != null && old !== '') out[k] = old;
    }
  }
  return out;
}
