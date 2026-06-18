import React, { useState, useMemo, useRef, useEffect } from 'react';
import { Switch, Route, Redirect, useHistory, useLocation } from 'react-router-dom';
import { TransitionGroup, CSSTransition } from 'react-transition-group';
import { Layout, Menu, Breadcrumb, Spin, Button } from '@arco-design/web-react';
import cs from 'classnames';
import {
  IconMenuFold,
  IconMenuUnfold,
} from '@arco-design/web-react/icon';
import { useSelector } from 'react-redux';
import qs from 'query-string';
import NProgress from 'nprogress';
import Navbar from './components/NavBar';
import Footer from './components/Footer';
import useRoute, { IRoute } from '@/routes';
import { getIconFromKey, getFlattenRoutes } from './layout-utils';
import useLocale from './utils/useLocale';
import getUrlParams from './utils/getUrlParams';
import { GlobalState } from './store';
import styles from './style/layout.module.less';
import './style/page-transition.less';

const MenuItem = Menu.Item;
const SubMenu = Menu.SubMenu;

const Sider = Layout.Sider;
const Content = Layout.Content;

function PageLayout() {
  const urlParams = getUrlParams();
  const history = useHistory();
  const location = useLocation();
  const pathname = history.location.pathname;
  const currentComponent = qs.parseUrl(pathname).url.slice(1);
  const locale = useLocale();
  const { settings, userLoading, userInfo } = useSelector(
    (state: GlobalState) => state
  );

  useEffect(() => {
    if (userInfo?.must_change_password && window.location.pathname !== '/change-password') {
      window.location.pathname = '/change-password';
    }
  }, [userInfo]);

  const [routes, defaultRoute] = useRoute(userInfo?.permissions ?? {});
  const defaultSelectedKeys = [currentComponent || defaultRoute];
  const paths = (currentComponent || defaultRoute).split('/');
  const defaultOpenKeys = paths.slice(0, paths.length - 1);

  const [breadcrumb, setBreadCrumb] = useState<string[]>([]);
  const [collapsed, setCollapsed] = useState<boolean>(false);
  const [selectedKeys, setSelectedKeys] =
    useState<string[]>(defaultSelectedKeys);
  const [openKeys, setOpenKeys] = useState<string[]>(defaultOpenKeys);

  const routeMap = useRef<Map<string, string[]>>(new Map());
  const menuMap = useRef<
    Map<string, { menuItem?: boolean; subMenu?: boolean }>
  >(new Map());

  const navbarHeight = 60;
  const menuWidth = collapsed ? 48 : settings?.menuWidth ?? 48;

  const showNavbar = (settings?.navbar ?? true) && urlParams.navbar !== false;
  const showMenu = (settings?.menu ?? true) && urlParams.menu !== false;
  const showFooter = (settings?.footer ?? true) && urlParams.footer !== false;

  const flattenRoutes = useMemo(() => getFlattenRoutes(routes) || [], [routes]);

  function onClickMenuItem(key: string) {
    const currentRoute = flattenRoutes.find((r) => r.key === key);
    if (!currentRoute?.component) {
      return;
    }
    const component = currentRoute.component;
    const preload = component.preload();
    NProgress.start();
    preload.then(() => {
      history.push(currentRoute.path ? currentRoute.path : `/${key}`);
      NProgress.done();
    });
  }

  function toggleCollapse() {
    setCollapsed((collapsed) => !collapsed);
  }

  const paddingLeft = showMenu ? { paddingLeft: menuWidth } : {};
  const paddingTop = showNavbar ? { paddingTop: navbarHeight } : {};
  const paddingStyle = { ...paddingLeft, ...paddingTop };

  function renderRoutes(locale: Record<string, string>) {
    routeMap.current.clear();
    return function travel(_routes: IRoute[], level: number, parentNode: string[] = []) {
      return _routes.map((route) => {
        const { breadcrumb = true, ignore } = route;
        const iconDom = getIconFromKey(route.key);
        const titleDom = (
          <>
            {iconDom} {locale[route.name] || route.name}
          </>
        );

        routeMap.current.set(
          `/${route.key}`,
          breadcrumb ? [...parentNode, route.name] : []
        );

        const visibleChildren = (route.children || []).filter((child) => {
          const { ignore, breadcrumb = true } = child;
          if (ignore || route.ignore) {
            routeMap.current.set(
              `/${child.key}`,
              breadcrumb ? [...parentNode, route.name, child.name] : []
            );
          }

          return !ignore;
        });

        if (ignore) {
          return '';
        }
        if (visibleChildren.length) {
          menuMap.current.set(route.key, { subMenu: true });
          return (
            <SubMenu key={route.key} title={titleDom}>
              {travel(visibleChildren, level + 1, [...parentNode, route.name])}
            </SubMenu>
          );
        }
        menuMap.current.set(route.key, { menuItem: true });
        return <MenuItem key={route.key}>{titleDom}</MenuItem>;
      });
    };
  }

  function updateMenuStatus() {
    const pathKeys = pathname.split('/');
    const newSelectedKeys: string[] = [];
    const newOpenKeys: string[] = [...openKeys];
    while (pathKeys.length > 0) {
      const currentRouteKey = pathKeys.join('/');
      const menuKey = currentRouteKey.replace(/^\//, '');
      const menuType = menuMap.current.get(menuKey);
      if (menuType && menuType.menuItem) {
        newSelectedKeys.push(menuKey);
      }
      if (menuType && menuType.subMenu && !openKeys.includes(menuKey)) {
        newOpenKeys.push(menuKey);
      }
      pathKeys.pop();
    }
    setSelectedKeys(newSelectedKeys);
    setOpenKeys(newOpenKeys);
  }

  useEffect(() => {
    const routeConfig = routeMap.current.get(pathname);
    setBreadCrumb(routeConfig || []);
    updateMenuStatus();
    // updateMenuStatus 内部已用 ref 持有 menuMap/openKeys，避免依赖陈旧闭包；
    // 将其列入依赖数组会导致 pathname 未变时仍频繁触发
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pathname]);

  if (userInfo?.must_change_password) {
    return (
      <Layout className={styles.layout}>
        <div className={cs(styles['layout-navbar'], { [styles['layout-navbar-hidden']]: !showNavbar })}>
          <Navbar show={showNavbar} />
        </div>
        <Spin className={styles['spin']} />
      </Layout>
    );
  }

  return (
    <Layout className={styles.layout}>
      <div
        className={cs(styles['layout-navbar'], {
          [styles['layout-navbar-hidden']]: !showNavbar,
        })}
      >
        <Navbar show={showNavbar} />
      </div>
      {userLoading ? (
        <Spin className={styles['spin']} />
      ) : (
        <Layout>
          {showMenu && (
            <Sider
              className={styles['layout-sider']}
              width={menuWidth}
              collapsed={collapsed}
              onCollapse={setCollapsed}
              trigger={null}
              collapsible
              breakpoint="xl"
              style={paddingTop}
            >
              <div className={styles['menu-wrapper']}>
                <Menu
                  collapse={collapsed}
                  onClickMenuItem={onClickMenuItem}
                  selectedKeys={selectedKeys}
                  openKeys={openKeys}
                  onClickSubMenu={(_: string, openKeys: string[]) => {
                    setOpenKeys(openKeys);
                  }}
                >
                  {renderRoutes(locale)(routes, 1)}
                </Menu>
              </div>
              <Button
                className={styles['collapse-btn']}
                onClick={toggleCollapse}
                aria-label={collapsed ? '展开侧栏' : '折叠侧栏'}
                icon={collapsed ? <IconMenuUnfold /> : <IconMenuFold />}
              />
            </Sider>
          )}
          <Layout className={styles['layout-content']} style={paddingStyle}>
            <div className={styles['layout-content-wrapper']}>
              {!!breadcrumb.length && (
                <div className={styles['layout-breadcrumb']}>
                  <Breadcrumb>
                    {breadcrumb.map((node, index) => (
                      <Breadcrumb.Item key={index}>
                        {typeof node === 'string' ? locale[node] || node : node}
                      </Breadcrumb.Item>
                    ))}
                  </Breadcrumb>
                </div>
              )}
              <Content>
                <TransitionGroup className="page-transition-group">
                  <CSSTransition
                    key={location.key || location.pathname}
                    classNames="page-slide"
                    timeout={300}
                  >
                    <Switch location={location}>
                      {flattenRoutes.map((route, index) => {
                        return (
                          <Route
                            key={index}
                            path={route.path || `/${route.key}`}
                            component={route.component}
                          />
                        );
                      })}
                      <Route exact path="/">
                        <Redirect to={`/${defaultRoute}`} />
                      </Route>
                      <Route path="*">
                        <Redirect to={`/${defaultRoute}`} />
                      </Route>
                    </Switch>
                  </CSSTransition>
                </TransitionGroup>
              </Content>
            </div>
            {showFooter && <Footer />}
          </Layout>
        </Layout>
      )}
    </Layout>
  );
}

export default PageLayout;
