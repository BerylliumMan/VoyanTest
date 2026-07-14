// https://stackoverflow.com/questions/68424114/next-js-how-to-fetch-localstorage-data-before-client-side-rendering
// 解决 nextJS 无法获取初始localstorage问题

import { useEffect, useState } from 'react';
import { isSSR } from '@/utils/is';

const getDefaultStorage = (key: string): string | undefined => {
  if (!isSSR) {
    return localStorage.getItem(key) ?? undefined;
  } else {
    return undefined;
  }
};

function useStorage(
  key: string,
  defaultValue?: string
): [string | undefined, (value: string) => void, () => void] {
  const [storedValue, setStoredValue] = useState<string | undefined>(
    getDefaultStorage(key) || defaultValue
  );

  const setStorageValue = (value: string) => {
    if (!isSSR) {
      localStorage.setItem(key, value);
      if (value !== storedValue) {
        setStoredValue(value);
      }
    }
  };

  const removeStorage = () => {
    if (!isSSR) {
      localStorage.removeItem(key);
    }
  };

  useEffect(() => {
    const storageValue = localStorage.getItem(key);
    if (storageValue) {
      setStoredValue(storageValue);
    }
    // 仅在挂载时读取 localStorage 初始值；故意不把 key 加入 deps：
    //  - 调用方约定通过组件 key / 显式 remount 切换 key，因此本 effect 没必要随 key 变化重跑。
    //  - 若把 key 加入 deps，key 变化时会用 localStorage 的旧值覆盖 setStorageValue 已写入的最新值，
    //    导致同 key 下的后续写丢失。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return [storedValue, setStorageValue, removeStorage];
}

export default useStorage;
