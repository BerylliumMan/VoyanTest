import './style/global.less';
import React, { useEffect } from 'react';
import ReactDOM from 'react-dom';
import { createStore } from 'redux';
import { Provider } from 'react-redux';
import { ConfigProvider } from '@arco-design/web-react';
import zhCN from '@arco-design/web-react/es/locale/zh-CN';
import enUS from '@arco-design/web-react/es/locale/en-US';
import { BrowserRouter, Switch, Route } from 'react-router-dom';
import axios from 'axios';
import rootReducer, { GlobalState } from './store';

// 全局 axios 请求拦截器：must_change_password 时阻止非白名单 API
const PASSWORD_CHANGE_ALLOWED = [
  '/api/auth/me',
  '/api/auth/login',
  '/api/auth/login-form',
  '/api/auth/logout',
  '/api/auth/change-password',
];
axios.interceptors.request.use(
  (config) => {
    const state = store.getState() as GlobalState;
    if (
      state.userInfo?.must_change_password &&
      config.url &&
      !PASSWORD_CHANGE_ALLOWED.some((p) => config.url!.startsWith(p))
    ) {
      return Promise.reject(new Error('请先修改默认密码'));
    }
    return config;
  },
  (error) => Promise.reject(error)
);

// 全局 axios 响应拦截器：401 自动跳转登录页
axios.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      localStorage.removeItem('userStatus');
      store.dispatch({
        type: 'update-userInfo',
        payload: { userInfo: undefined, userLoading: false },
      });
      if (window.location.pathname.replace(/\//g, '') !== 'login') {
        window.location.pathname = '/login';
      }
    }
    return Promise.reject(error);
  }
);
import PageLayout from './layout';
import { GlobalContext } from './context';
import Login from './pages/login';
import ChangePassword from './pages/change-password';
import checkLogin from './utils/checkLogin';
import changeTheme from './utils/changeTheme';
import useStorage from './utils/useStorage';
const store = createStore(rootReducer);

function Index() {
  const [lang, setLang] = useStorage('arco-lang', 'zh-CN');
  const [theme, setTheme] = useStorage('arco-theme', 'light');

  function getArcoLocale() {
    switch (lang) {
      case 'zh-CN':
        return zhCN;
      case 'en-US':
        return enUS;
      default:
        return zhCN;
    }
  }

  function fetchUserInfo() {
    store.dispatch({
      type: 'update-userInfo',
      payload: { userLoading: true },
    });
    axios
      .get('/api/auth/me')
      .then((res) => {
        const userData = res.data;
        store.dispatch({
          type: 'update-userInfo',
          payload: {
            userInfo: {
              name: userData.username,
              role: userData.role,
              permissions: {},
              must_change_password: userData.must_change_password,
            },
            userLoading: false,
          },
        });
        if (userData.must_change_password && window.location.pathname !== '/change-password') {
          window.location.pathname = '/change-password';
        }
      })
      .catch(() => {
        store.dispatch({
          type: 'update-userInfo',
          payload: { userInfo: undefined, userLoading: false },
        });
        localStorage.removeItem('userStatus');
        if (window.location.pathname.replace(/\//g, '') !== 'login') {
          window.location.pathname = '/login';
        }
      });
  }

  useEffect(() => {
    if (checkLogin()) {
      fetchUserInfo();
    } else if (window.location.pathname.replace(/\//g, '') !== 'login') {
      window.location.pathname = '/login';
    }
  }, []);

  useEffect(() => {
    if (theme) {
      changeTheme(theme);
    }
  }, [theme]);

  const contextValue = {
    lang,
    setLang,
    theme,
    setTheme,
  };

  return (
    <BrowserRouter>
      <ConfigProvider
        locale={getArcoLocale()}
        componentConfig={{
          Card: {},
          List: {},
          Table: {},
        }}
      >
        <Provider store={store}>
          <GlobalContext.Provider value={contextValue}>
            <Switch>
              <Route path="/login" component={Login} />
              <Route path="/change-password" component={ChangePassword} />
              <Route path="/" component={PageLayout} />
            </Switch>
          </GlobalContext.Provider>
        </Provider>
      </ConfigProvider>
    </BrowserRouter>
  );
}

ReactDOM.render(<Index />, document.getElementById('root'));
