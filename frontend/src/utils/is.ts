export function isArray(val: unknown): boolean {
  return Object.prototype.toString.call(val) === '[object Array]';
}
export function isObject(val: unknown): boolean {
  return Object.prototype.toString.call(val) === '[object Object]';
}
export function isString(val: unknown): boolean {
  return Object.prototype.toString.call(val) === '[object String]';
}

export const isSSR = (function () {
  try {
    return !(typeof window !== 'undefined' && document !== undefined);
  } catch (e) {
    return true;
  }
})();
