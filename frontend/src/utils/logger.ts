// 前端统一日志工具。
// 在生产构建（import.meta.env.DEV === false）下，error/warn/info 退化为 no-op，
// 避免 console.error 在生产环境泄露错误信息或污染用户控制台。
// 仅在开发环境下保留真实的 console 输出，便于本地调试。
const noop = (..._args: unknown[]) => undefined;

const logger = {
  error: import.meta.env.DEV ? console.error : noop,
  warn: import.meta.env.DEV ? console.warn : noop,
  info: import.meta.env.DEV ? console.info : noop,
};

export default logger;
