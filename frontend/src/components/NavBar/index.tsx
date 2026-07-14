import React, { useContext, useEffect, useState } from 'react';
import {
  Tooltip, Avatar, Select, Dropdown, Menu, Message, Button, Badge, List, Space, Tag,
} from '@arco-design/web-react';
import {
  IconLanguage, IconSunFill, IconMoonFill,
  IconUser, IconSettings, IconPoweroff, IconLoading, IconNotification,
} from '@arco-design/web-react/icon';
import { useSelector } from 'react-redux';
import axios from 'axios';
import { GlobalState } from '@/store';
import { GlobalContext } from '@/context';
import useLocale from '@/utils/useLocale';
import logger from '@/utils/logger';
import Logo from '@/assets/logo.svg';
import IconButton from './IconButton';
import Settings from '../Settings';
import styles from './style/index.module.less';
import defaultLocale from '@/locale';

function Navbar({ show }: { show: boolean }) {
  const t = useLocale();
  const { userInfo, userLoading } = useSelector((state: GlobalState) => state);
  const { setLang, lang, theme, setTheme } = useContext(GlobalContext);
  const [notifCount, setNotifCount] = useState(0);
  const [notifs, setNotifs] = useState<any[]>([]);
  const [notifVisible, setNotifVisible] = useState(false);

  useEffect(() => {
    if (!userInfo) return;
    axios.get('/api/notifications/unread-count').then((r) => setNotifCount(r.data?.count || 0)).catch(() => {});
  }, [userInfo]);

  const loadNotifs = async () => {
    try {
      const r = await axios.get('/api/notifications/?size=10');
      setNotifs(r.data?.items || []);
      setNotifVisible(true);
    } catch { /* silent */ }
  };

  const markRead = async (id: number) => {
    await axios.put(`/api/notifications/${id}/read`);
    setNotifCount((c) => Math.max(0, c - 1));
    setNotifs((prev) => prev.map((n) => n.id === id ? { ...n, read: true } : n));
  };

  const markAllRead = async () => {
    await axios.put('/api/notifications/read-all');
    setNotifCount(0);
    setNotifs((prev) => prev.map((n) => ({ ...n, read: true })));
  };

  async function logout() {
    try { await axios.post('/api/auth/logout'); } catch (e: unknown) { logger.error('Logout failed', e); }
    localStorage.removeItem('userStatus');
    window.location.href = '/login';
  }

  function onMenuItemClick(key: string) {
    if (key === 'logout') {
      logout();
    } else {
      Message.info(`You clicked ${key}`);
    }
  }

  if (!show) {
    return (
      <div className={styles['fixed-settings']}>
        <Settings
          trigger={
            <Button icon={<IconSettings />} type="primary" size="large" />
          }
        />
      </div>
    );
  }

  const droplist = (
    <Menu onClickMenuItem={onMenuItemClick}>
      <Menu.Item key="logout">
        <IconPoweroff className={styles['dropdown-icon']} />
        {t['navbar.logout']}
      </Menu.Item>
    </Menu>
  );

  return (
    <div className={styles.navbar}>
      <div className={styles.left}>
        <div className={styles.logo}>
          <Logo width={28} height={28} />
          <div className={styles['logo-name']}>{t['navbar.appName']}</div>
        </div>
      </div>
      <ul className={styles.right}>
        <li>
          <Select
            triggerElement={<IconButton icon={<IconLanguage />} aria-label="切换语言" />}
            options={[
              { label: '中文', value: 'zh-CN' },
              { label: 'English', value: 'en-US' },
            ]}
            value={lang}
            triggerProps={{
              autoAlignPopupWidth: false,
              autoAlignPopupMinWidth: true,
              position: 'br',
            }}
            trigger="hover"
            onChange={(value: string) => {
              if (setLang) {
                setLang(value);
              }
              const nextLang = defaultLocale[value as keyof typeof defaultLocale];
              Message.info(`${nextLang['message.lang.tips']}${value}`);
            }}
          />
        </li>
        <li>
          <Tooltip
            content={
              theme === 'light'
                ? t['settings.navbar.theme.toDark']
                : t['settings.navbar.theme.toLight']
            }
          >
            <IconButton
              icon={theme !== 'dark' ? <IconMoonFill /> : <IconSunFill />}
              onClick={() =>
                setTheme && setTheme(theme === 'light' ? 'dark' : 'light')
              }
              aria-label="切换主题"
            />
          </Tooltip>
        </li>
        {userInfo && (
          <li>
            <Dropdown
              droplist={
                <Menu onClickMenuItem={(key) => key === 'clear' && markAllRead()}>
                  {notifs.length === 0 ? (
                    <Menu.Item key="empty" disabled>暂无通知</Menu.Item>
                  ) : notifs.slice(0, 5).map((n) => (
                    <Menu.Item key={n.id} onClick={() => !n.read && markRead(n.id)}>
                      <Space>
                        <Tag color={n.type === 'error' ? 'red' : n.type === 'success' ? 'green' : 'blue'} size="small">{n.type}</Tag>
                        <span style={{ fontWeight: n.read ? 'normal' : 'bold' }}>{n.title}</span>
                      </Space>
                    </Menu.Item>
                  ))}
                  {notifs.length > 0 && <Menu.Item key="clear">全部标为已读</Menu.Item>}
                </Menu>
              }
              position="br"
              trigger="click"
              onVisibleChange={(v) => { if (v) loadNotifs(); else setNotifVisible(false); }}
            >
              <Badge count={notifCount} dot={notifCount > 0}>
                <IconButton icon={<IconNotification />} aria-label="通知" />
              </Badge>
            </Dropdown>
          </li>
        )}
        {userInfo && (
          <li>
            <Dropdown droplist={droplist} position="br" disabled={userLoading}>
              <Avatar size={32} style={{ cursor: 'pointer' }}>
                {userLoading ? (
                  <IconLoading />
                ) : (
                  <IconUser />
                )}
              </Avatar>
            </Dropdown>
          </li>
        )}
      </ul>
    </div>
  );
}

export default Navbar;
