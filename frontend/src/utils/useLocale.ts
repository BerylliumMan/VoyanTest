import { useContext } from 'react';
import { GlobalContext } from '../context';
import defaultLocale from '../locale';

type LocaleRecord = Record<string, string>;

function useLocale(locale: Record<string, LocaleRecord> | null = null): LocaleRecord {
  const { lang } = useContext(GlobalContext);

  const source = locale || defaultLocale;
  return (lang && source[lang]) || {};
}

export default useLocale;
