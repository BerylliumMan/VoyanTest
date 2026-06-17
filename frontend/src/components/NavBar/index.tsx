import React, { useContext } from 'react';
import {
  Tooltip, Avatar, Select, Dropdown, Menu, Message, Button,
} from '@arco-design/web-react';
import {
  IconLanguage, IconSunFill, IconMoonFill,
  IconUser, IconSettings, IconPoweroff, IconLoading,
} from '@arco-design/web-react/icon';
import { useSelector } from 'react-redux';
import axios from 'axios';
import { GlobalState } from '@/store';
import { GlobalContext } from '@/context';
import useLocale from '@/utils/useLocale';
import Logo from '@/assets/logo.svg';
import IconButton from './IconButton';
import Settings from '../Settings';
import styles from './style/index.module.less';
import defaultLocale from '@/locale';

function Navbar({ show }: { show: boolean }) {
  const t = useLocale();
  const { userInfo, userLoading } = useSelector((state: GlobalState) => state);
  const { setLang, lang, theme, setTheme } = useContext(GlobalContext);

  async function logout() {
    try { await axios.post('/api/auth/logout'); } catch (e: unknown) { console.error('Logout failed', e); }
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
