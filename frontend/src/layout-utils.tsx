import React from 'react';
import {
  IconDashboard,
  IconList,
  IconSettings,
  IconFile,
  IconApps,
  IconRobot,
  IconSafe,
  IconThunderbolt,
  IconHistory,
} from '@arco-design/web-react/icon';
import lazyload from '@/utils/lazyload';
import { IRoute } from '@/routes';
import { isArray } from './utils/is';
import styles from './style/layout.module.less';

export function getIconFromKey(key: string): JSX.Element {
  switch (key) {
    case 'dashboard':
      return <IconDashboard className={styles.icon} />;
    case 'projects':
      return <IconApps className={styles.icon} />;
    case 'testcases':
      return <IconList className={styles.icon} />;
    case 'gen':
      return <IconThunderbolt className={styles.icon} />;
    case 'gen-history':
      return <IconHistory className={styles.icon} />;
    case 'reports':
      return <IconFile className={styles.icon} />;
    case 'agents':
      return <IconRobot className={styles.icon} />;
    case 'audit-logs':
      return <IconSafe className={styles.icon} />;
    case 'settings':
      return <IconSettings className={styles.icon} />;
    default:
      return <div className={styles['icon-empty']} />;
  }
}

export function getFlattenRoutes(routes: IRoute[]): IRoute[] {
  const mod = import.meta.glob('./pages/**/[a-z[]*.tsx');
  const res: IRoute[] = [];
  function travel(_routes: IRoute[]) {
    _routes.forEach((route) => {
      const visibleChildren = (route.children || []).filter(
        (child) => !child.ignore
      );
      if (route.key && (!route.children || !visibleChildren.length)) {
        try {
          route.component = lazyload(mod[`./pages/${route.key}/index.tsx`]);
          res.push(route);
        } catch (e) {
          console.error(e);
        }
      }

      if (isArray(route.children) && route.children.length) {
        travel(route.children);
      }
    });
  }
  travel(routes);
  return res;
}
