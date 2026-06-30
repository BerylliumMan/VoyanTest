import React, { useEffect } from 'react';
import Footer from '@/components/Footer';
import Logo from '@/assets/logo.svg';
import LoginForm from './form';
import LoginBanner from './banner';
import useLocale from '@/utils/useLocale';
import styles from './style/index.module.less';

function Login() {
  const t = useLocale();
  useEffect(() => {
    document.body.setAttribute('arco-theme', 'light');
  }, []);

  // 进入登录页时检查数据库是否已初始化，未初始化则跳转配置页
  useEffect(() => {
    fetch('/api/setup/status')
      .then((r) => r.json())
      .then((data) => {
        if (!data.configured) {
          window.location.href = '/setup';
        }
      })
      .catch(() => {});
  }, []);

  return (
    <div className={styles.container}>
      <div className={styles.logo}>
        <Logo width={36} height={36} />
        <div className={styles['logo-text']}>{t['navbar.appName']}</div>
      </div>
      <div className={styles.banner}>
        <div className={styles['banner-inner']}>
          <LoginBanner />
        </div>
      </div>
      <div className={styles.content}>
        <div className={styles['content-inner']}>
          <LoginForm />
        </div>
        <div className={styles.footer}>
          <Footer />
        </div>
      </div>
    </div>
  );
}
Login.displayName = 'LoginPage';

export default Login;
