import {
  Form, Input, Button, Space,
} from '@arco-design/web-react';
import { FormInstance } from '@arco-design/web-react/es/Form';
import { IconLock, IconUser } from '@arco-design/web-react/icon';
import React, { useEffect, useRef, useState } from 'react';
import axios from 'axios';
import useStorage from '@/utils/useStorage';
import useLocale from '@/utils/useLocale';
import locale from './locale';
import styles from './style/index.module.less';

export default function LoginForm() {
  const formRef = useRef<FormInstance>();
  const [errorMessage, setErrorMessage] = useState('');
  const [loading, setLoading] = useState(false);
  const [loginParams, setLoginParams, removeLoginParams] =
    useStorage('loginParams');
  const t = useLocale(locale);
  const [rememberPassword, setRememberPassword] = useState(!!loginParams);

  function afterLoginSuccess(params: { userName: string; password: string }, mustChangePassword: boolean) {
    if (rememberPassword) {
      setLoginParams(JSON.stringify(params));
    } else {
      removeLoginParams();
    }
    localStorage.setItem('userStatus', 'login');
    if (mustChangePassword) {
      window.location.href = '/change-password';
    } else {
      window.location.href = '/';
    }
  }

  function login(params: { userName: string; password: string }) {
    setErrorMessage('');
    setLoading(true);
    axios
      .post('/api/auth/login', {
        username: params.userName,
        password: params.password,
      })
      .then((res) => {
        afterLoginSuccess(params, res.data.must_change_password);
      })
      .catch((err) => {
        const msg = err.response?.data?.detail || t['login.form.login.errMsg'];
        setErrorMessage(msg);
      })
      .finally(() => {
        setLoading(false);
      });
  }

  function onSubmitClick() {
    formRef.current?.validate().then((values) => {
      login(values);
    });
  }

  useEffect(() => {
    const rememberPassword = !!loginParams;
    setRememberPassword(rememberPassword);
    if (formRef.current && rememberPassword) {
      const parseParams = JSON.parse(loginParams);
      formRef.current.setFieldsValue(parseParams);
    }
  }, [loginParams]);

  return (
    <div className={styles['login-form-wrapper']}>
      <div className={styles['login-form-title']}>
        {t['login.form.title']}
      </div>
      <div className={styles['login-form-error-msg']}>{errorMessage}</div>
      <Form
        className={styles['login-form']}
        layout="vertical"
        ref={formRef}
        initialValues={{ userName: '', password: '' }}
      >
        <Form.Item
          field="userName"
          rules={[
            { required: true, message: t['login.form.userName.errMsg'] },
          ]}
        >
          <Input
            prefix={<IconUser />}
            placeholder={t['login.form.userName.placeholder']}
            onPressEnter={onSubmitClick}
          />
        </Form.Item>
        <Form.Item
          field="password"
          rules={[
            { required: true, message: t['login.form.password.errMsg'] },
          ]}
        >
          <Input.Password
            prefix={<IconLock />}
            placeholder={t['login.form.password.placeholder']}
            onPressEnter={onSubmitClick}
          />
        </Form.Item>
        <Space size={16} direction="vertical">
          <Button type="primary" long onClick={onSubmitClick} loading={loading}>
            {t['login.form.login']}
          </Button>
        </Space>
      </Form>
    </div>
  );
}
