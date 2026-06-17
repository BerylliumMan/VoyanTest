import React, { useRef, useState, useEffect } from 'react';
import {
  Form, Input, Button, Message,
} from '@arco-design/web-react';
import { FormInstance } from '@arco-design/web-react/es/Form';
import { IconLock } from '@arco-design/web-react/icon';
import { useSelector, useDispatch } from 'react-redux';
import axios from 'axios';
import { GlobalState } from '@/store';
import styles from './style/index.module.less';

const ChangePassword: React.FC = () => {
  const formRef = useRef<FormInstance | null>(null);
  const [errorMessage, setErrorMessage] = useState('');
  const [loading, setLoading] = useState(false);
  const dispatch = useDispatch();
  const userInfo = useSelector((state: GlobalState) => state.userInfo);

  useEffect(() => {
    document.body.setAttribute('arco-theme', 'light');
  }, []);

  useEffect(() => {
    if (userInfo && userInfo.must_change_password === false) {
      window.location.href = '/';
    }
  }, [userInfo]);

  const handleSubmit = () => {
    formRef.current?.validate().then((values: {
      oldPassword: string;
      newPassword: string;
      confirmPassword: string;
    }) => {
      if (values.newPassword !== values.confirmPassword) {
        setErrorMessage('两次输入的新密码不一致');
        return;
      }
      setErrorMessage('');
      setLoading(true);
      axios
        .post('/api/auth/change-password', {
          old_password: values.oldPassword,
          new_password: values.newPassword,
        })
        .then(() => {
          Message.success('密码修改成功');
          dispatch({
            type: 'update-userInfo',
            payload: {
              userInfo: { ...userInfo, must_change_password: false },
              userLoading: false,
            },
          });
          window.location.href = '/';
        })
        .catch((err) => {
          const msg = err.response?.data?.detail || '密码修改失败，请重试';
          setErrorMessage(msg);
        })
        .finally(() => {
          setLoading(false);
        });
    });
  };

  return (
    <div className={styles.container}>
      <div className={styles['form-wrapper']}>
        <div className={styles.title}>修改默认密码</div>
        <div className={styles.subtitle}>
          首次登录必须修改默认密码后才能使用平台功能
        </div>
        <div className={styles['error-msg']}>{errorMessage}</div>
        <Form
          layout="vertical"
          ref={formRef}
          initialValues={{ oldPassword: '', newPassword: '', confirmPassword: '' }}
        >
          <Form.Item
            field="oldPassword"
            rules={[{ required: true, message: '请输入当前密码' }]}
          >
            <Input.Password
              prefix={<IconLock />}
              placeholder="当前密码"
              onPressEnter={handleSubmit}
            />
          </Form.Item>
          <Form.Item
            field="newPassword"
            rules={[
              { required: true, message: '请输入新密码' },
              { minLength: 8, message: '密码至少 8 位' },
              {
                validator: (value, callback) => {
                  if (!/[a-zA-Z]/.test(value) || !/\d/.test(value)) {
                    callback('密码需包含字母和数字');
                  } else {
                    callback();
                  }
                },
              },
            ]}
          >
            <Input.Password
              prefix={<IconLock />}
              placeholder="新密码（至少8位，含字母和数字）"
              onPressEnter={handleSubmit}
            />
          </Form.Item>
          <Form.Item
            field="confirmPassword"
            rules={[{ required: true, message: '请确认新密码' }]}
          >
            <Input.Password
              prefix={<IconLock />}
              placeholder="确认新密码"
              onPressEnter={handleSubmit}
            />
          </Form.Item>
          <Button type="primary" long onClick={handleSubmit} loading={loading}>
            确认修改
          </Button>
        </Form>
      </div>
    </div>
  );
};

export default ChangePassword;
