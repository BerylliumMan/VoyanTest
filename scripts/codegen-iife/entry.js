/**
 * Browser entry: expose Playwright codegen locator helpers on window.__vtCodegen.
 */
import {
  generateLocator,
  generateSelectors,
  selectorToLocator,
} from "playwright-selector-generator";

function installCapture() {
  if (typeof window === "undefined" || window.__vtCaptureInstalled) return;
  const mark = (ev) => {
    const t = ev && ev.target;
    if (t && t.nodeType === 1) window.__vtLastTarget = t;
  };
  for (const type of ["click", "mousedown", "input", "change", "keydown", "focusin"]) {
    window.addEventListener(type, mark, true);
  }
  window.__vtCaptureInstalled = true;
}

function isVisible(el) {
  if (!el || el.nodeType !== 1) return false;
  const r = el.getBoundingClientRect();
  if (r.width <= 0 || r.height <= 0) return false;
  const st = window.getComputedStyle(el);
  return st.visibility !== "hidden" && st.display !== "none";
}

function resolveTarget() {
  const t = window.__vtLastTarget;
  if (t && t.isConnected && isVisible(t)) return t;
  const a = document.activeElement;
  if (a && a !== document.body && a !== document.documentElement && isVisible(a)) {
    return a;
  }
  return null;
}

function findByPlaceholder(ph) {
  if (!ph) return null;
  const inputs = Array.from(
    document.querySelectorAll(`input[placeholder="${ph}"], textarea[placeholder="${ph}"]`)
  ).filter((i) => !i.disabled && isVisible(i));
  return inputs.length ? inputs[inputs.length - 1] : null;
}

function findByExactText(text) {
  if (!text) return null;
  const want = String(text).trim();
  if (!want) return null;
  const candidates = Array.from(
    document.querySelectorAll(
      "button, a, label, span, div, li, td, th, [role='treeitem'], [role='option'], [role='button']"
    )
  ).filter((el) => isVisible(el) && (el.textContent || "").trim() === want);
  if (!candidates.length) return null;
  // Prefer deepest / smallest text node container
  candidates.sort((a, b) => {
    const da = (a.textContent || "").length;
    const db = (b.textContent || "").length;
    return da - db;
  });
  return candidates[0];
}

function stripPagePrefix(loc) {
  if (!loc || typeof loc !== "string") return loc;
  return loc.replace(/^page\./, "").trim();
}

/**
 * @param {object} [hint] optional { placeholder, exact_text, name }
 */
function resolvePlaywrightLocator(hint) {
  installCapture();
  hint = hint || {};
  const out = {
    ok: false,
    playwright_locator: null,
    locator_candidates: [],
    active: null,
    error: null,
    source: null,
  };

  let el = null;
  if (hint.placeholder) {
    el = findByPlaceholder(hint.placeholder);
    if (el) out.source = "placeholder";
  }
  if (!el && (hint.exact_text || hint.name)) {
    el = findByExactText(hint.exact_text || hint.name);
    if (el) out.source = "exact_text";
  }
  if (!el) {
    el = resolveTarget();
    if (el) out.source = "capture";
  }

  if (!el) {
    out.error = "no_target";
    return out;
  }
  try {
    const r = el.getBoundingClientRect();
    out.active = {
      tag: (el.tagName || "").toLowerCase(),
      type: el.getAttribute("type") || "",
      placeholder: el.getAttribute("placeholder") || "",
      role: el.getAttribute("role") || "",
      name: el.getAttribute("name") || "",
      text: (el.innerText || el.textContent || "").trim().slice(0, 80),
      valueLen: (el.value || "").length,
      className: (el.className || "").toString().slice(0, 120),
      visible: r.width > 0 && r.height > 0,
    };
    const loc = generateLocator(el, { lang: "python" });
    out.playwright_locator = stripPagePrefix(loc);
    const sels = generateSelectors(el) || [];
    out.locator_candidates = sels
      .slice(0, 6)
      .map((s) => stripPagePrefix(selectorToLocator(s, "python")))
      .filter(Boolean);
    out.ok = Boolean(out.playwright_locator);
  } catch (e) {
    out.error = String((e && e.message) || e);
  }
  return out;
}

installCapture();

window.__vtCodegen = {
  generateLocator,
  generateSelectors,
  selectorToLocator,
  installCapture,
  resolvePlaywrightLocator,
};
