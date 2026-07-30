/**
 * Numbered step / expected-result text helpers (aligned with app.gen.adapter).
 */

/** Parse ``1. xxx`` text into slot-aligned bodies (keeps empty slots by index). */
export function splitNumberedItems(text: string): string[] {
  if (!text || !String(text).trim()) return [];
  const src = String(text).trim();

  // Collapsed junk: "1.2.3.4." or "1. 2. 3."
  if (/^(?:\d+[\.、]\s*)+$/.test(src)) return [];

  // Line-oriented parse (also handles single-line ``9. only this``)
  const indexed = new Map<number, string>();
  const sequential: string[] = [];
  let sawMarker = false;
  for (const ln of src.split('\n')) {
    const m = ln.match(/^\s*(\d+)[\.、]\s*(.*)$/);
    if (m) {
      sawMarker = true;
      const idx = Number.parseInt(m[1], 10);
      const body = (m[2] || '').trim();
      indexed.set(idx, body);
      sequential.push(body);
    } else if (ln.trim()) {
      sequential.push(ln.trim());
    }
  }
  if (sawMarker && indexed.size > 0) {
    const values = [...indexed.values()];
    if (!values.some((v) => v)) return [];
    const maxI = Math.max(...indexed.keys());
    // Expand by explicit index so ``9. only last`` aligns to step 9 (cap at 100).
    if (maxI <= 100) {
      return Array.from({ length: maxI }, (_, i) => indexed.get(i + 1) || '');
    }
    return sequential;
  }

  const inlineRe = /(?:^|\s)(\d+)[\.、]\s+([\s\S]+?)(?=\s+\d+[\.、]\s+|$)/g;
  const inlineItems: string[] = [];
  let m2: RegExpExecArray | null;
  while ((m2 = inlineRe.exec(src)) !== null) {
    inlineItems.push(m2[2].replace(/\s+/g, ' ').trim());
  }
  if (inlineItems.length) {
    const cleaned = inlineItems.filter(Boolean);
    if (cleaned.length === 1 && /^(?:\d+[\.、]\s*)+$/.test(cleaned[0])) return [];
    return cleaned;
  }

  return src
    .split('\n')
    .map((p) => p.trim())
    .filter(Boolean);
}

/** Non-empty bodies only (for display lists). */
export function splitNumberedItemsNonEmpty(text: string): string[] {
  return splitNumberedItems(text).filter((p) => p.trim());
}

/**
 * Serialize expected results: omit empty slots (no bare ``1.\\n2.\\n3.`` junk).
 * Keeps 1-based step index so import/edit can right-align / index-map.
 */
export function formatNumberedExpected(
  steps: Array<{ parsed_result?: string | null }>,
): string {
  const lines: string[] = [];
  steps.forEach((s, i) => {
    const body = (s.parsed_result || '').trim();
    if (!body) return;
    // Drop items that are still only numbering
    if (/^(?:\d+[\.、]\s*)+$/.test(body)) return;
    lines.push(`${i + 1}. ${body}`);
  });
  return lines.join('\n');
}

/** Serialize steps as dense ``1. desc`` lines (empty descriptions kept for order). */
export function formatNumberedSteps(
  steps: Array<{ description?: string | null }>,
): string {
  return steps.map((s, i) => `${i + 1}. ${(s.description || '').trim()}`).join('\n');
}
