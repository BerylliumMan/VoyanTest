import auth, { AuthParams, UserPermission } from '@/utils/authentication';
import { useEffect, useMemo, useState } from 'react';

export type IRoute = AuthParams & {
  name: string;
  key: string;
  breadcrumb?: boolean;
  children?: IRoute[];
  ignore?: boolean;
  component?: React.ComponentType<any> & { preload(): Promise<void> };
  path?: string;
};

export const routes: IRoute[] = [
  {
    name: 'menu.dashboard',
    key: 'dashboard',
  },
  {
    name: 'menu.projects',
    key: 'projects',
  },
  {
    name: 'menu.gen',
    key: 'gen',
    children: [
      {
        name: 'menu.gen.analysis',
        key: 'gen',
      },
      {
        name: 'menu.gen.history',
        key: 'gen-history',
      },
    ],
  },
  {
    name: '分析详情',
    key: 'gen-history-detail',
    path: '/gen-history-detail/:id',
    ignore: true,
  },
  {
    name: '运行调试',
    key: 'run-debug',
    path: '/run-debug/:runId',
    ignore: true,
  },
  {
    name: '调试运行',
    key: 'run-debug',
    path: '/run-debug',
    ignore: true,
  },
  {
    name: 'menu.testcases',
    key: 'testcases',
  },
  {
    name: 'menu.reports',
    key: 'reports',
  },
  {
    name: 'menu.agents',
    key: 'agents',
  },
  {
    name: 'Agent 详情',
    key: 'agent-detail',
    path: '/agents/:id',
    ignore: true,
  },
  {
    name: 'menu.audit_logs',
    key: 'audit-logs',
  },
  {
    name: 'menu.recordings',
    key: 'recordings',
  },
  {
    name: 'menu.settings',
    key: 'settings',
  },
];

export const getName = (path: string, routeList: IRoute[]): string | undefined => {
  for (const item of routeList) {
    const itemPath = `/${item.key}`;
    if (path === itemPath) {
      return item.name;
    } else if (item.children) {
      const childName = getName(path, item.children);
      if (childName) {
        return childName;
      }
    }
  }
  return undefined;
};

export const generatePermission = (role: string): UserPermission => {
  const actions = role === 'admin' ? ['*'] : ['read'];
  const result: UserPermission = {};
  routes.forEach((item) => {
    if (item.children) {
      item.children.forEach((child) => {
        result[child.name] = actions;
      });
    } else {
      result[item.name] = actions;
    }
  });
  return result;
};

const useRoute = (userPermission: UserPermission): [IRoute[], string] => {
  const filterRoute = (sourceRoutes: IRoute[], arr: IRoute[] = []): IRoute[] => {
    if (!sourceRoutes.length) {
      return [];
    }
    for (const route of sourceRoutes) {
      const { requiredPermissions, oneOfPerm } = route;
      let visible = true;
      if (requiredPermissions) {
        visible = auth({ requiredPermissions, oneOfPerm }, userPermission);
      }
      if (!visible) {
        continue;
      }
      if (route.children && route.children.length) {
        const newChildren: IRoute[] = [];
        const newRoute: IRoute = { ...route, children: newChildren };
        filterRoute(route.children, newChildren);
        if (newChildren.length) {
          arr.push(newRoute);
        }
      } else {
        arr.push({ ...route });
      }
    }
    return arr;
  };

  const [permissionRoute, setPermissionRoute] = useState(routes);

  useEffect(() => {
    const newRoutes = filterRoute(routes);
    setPermissionRoute(newRoutes);
  }, [JSON.stringify(userPermission)]);

  const defaultRoute = useMemo(() => {
    const first = permissionRoute[0];
    if (first) {
      return first.children?.[0]?.key || first.key;
    }
    return '';
  }, [permissionRoute]);

  return [permissionRoute, defaultRoute];
};

export default useRoute;
