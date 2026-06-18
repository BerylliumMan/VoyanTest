import '@testing-library/jest-dom';

/* -------------------------------------------------------------------------- */
/*  jsdom polyfills                                                            */
/* -------------------------------------------------------------------------- */

/* Arco Design 的 Grid (Row/Col) 在挂载时会调用 window.matchMedia，
 * jsdom 默认不提供该 API，会导致未捕获错误。
 * 这里给一个最小可用的 mock 即可，覆盖所有媒体查询。 */
if (typeof window !== 'undefined' && typeof window.matchMedia !== 'function') {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }),
  });
}
