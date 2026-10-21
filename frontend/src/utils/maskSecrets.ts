// 038 US4：对话显示层密码打码（与 core/chat/mask.py 同规则；不改 API/DB）。
const PASSWORD_RE = /(密码|口令|password|passwd|pwd)(\s*[:：=]\s*|\s+)([^\s，,。;；、"'）)】]+)/gi;

export function maskSecrets(text: string | null | undefined): string {
  if (!text) return text || '';
  return String(text).replace(PASSWORD_RE, (_m, lead: string, sep: string) => `${lead}${sep}******`);
}
