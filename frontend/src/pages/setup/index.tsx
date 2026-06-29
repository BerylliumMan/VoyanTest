import React, { useState, useEffect } from 'react';
import { Card, Form, Input, InputNumber, Button, Message, Steps, Result, Spin, Typography } from '@arco-design/web-react';
import { IconCheckCircle, IconSafe } from '@arco-design/web-react/icon';
import { apiGet, apiPost } from '@/utils/apiRequest';
import styles from './style/index.module.less';

const { Title, Paragraph } = Typography;

function Setup() {
  const [loading, setLoading] = useState(true);
  const [checking, setChecking] = useState(false);
  const [done, setDone] = useState(false);
  const [step, setStep] = useState(0);

  useEffect(() => {
    apiGet('/api/setup/status')
      .then((data) => {
        if (data.configured) {
          setDone(true);
          window.location.href = '/login';
        }
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const handleSubmit = async (values: any) => {
    setChecking(true);
    try {
      const data = await apiPost('/api/setup/database', {
        host: values.host,
        port: values.port,
        user: values.user,
        password: values.password,
        database: values.database,
      }, '数据库配置成功');
      setDone(true);
      setStep(2);
      setTimeout(() => { window.location.href = '/login'; }, 2000);
    } catch (e: any) {
      Message.error('配置失败: ' + (e?.message || '未知错误'));
    } finally {
      setChecking(false);
    }
  };

  if (loading) {
    return <Spin loading className="spin-center" />;
  }

  if (done) {
    return (
      <div className={styles.container}>
        <Card className={styles.card}>
          <Result
            status="success"
            title="数据库配置完成"
            subTitle="即将跳转到登录页..."
          />
        </Card>
      </div>
    );
  }

  return (
    <div className={styles.container}>
      <Card className={styles.card}>
        <div style={{ textAlign: 'center', marginBottom: 24 }}>
          <IconSafe style={{ fontSize: 48, color: 'rgb(var(--arcoblue-6))' }} />
          <Title heading={4}>VoyanTest 初始化配置</Title>
          <Paragraph type="secondary">配置 PostgreSQL 数据库连接以完成首次启动设置</Paragraph>
        </div>

        <Steps current={step} style={{ marginBottom: 32 }}>
          <Steps.Step title="数据库配置" description="填写 PG 连接信息" />
          <Steps.Step title="连接测试" description="验证并初始化" />
          <Steps.Step title="完成" description="跳转登录" />
        </Steps>

        <Form
          layout="vertical"
          onOk={handleSubmit}
          initialValues={{
            host: 'localhost',
            port: 5432,
            user: 'voyantest',
            password: '',
            database: 'voyantest',
          }}
        >
          <Form.Item label="数据库主机" field="host" rules={[{ required: true, message: '请输入主机地址' }]}>
            <Input placeholder="localhost" />
          </Form.Item>
          <Form.Item label="端口" field="port" rules={[{ required: true, message: '请输入端口' }]}>
            <InputNumber min={1} max={65535} placeholder="5432" style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item label="用户名" field="user" rules={[{ required: true, message: '请输入用户名' }]}>
            <Input placeholder="voyantest" />
          </Form.Item>
          <Form.Item label="密码" field="password">
            <Input.Password placeholder="数据库密码" />
          </Form.Item>
          <Form.Item label="数据库名" field="database" rules={[{ required: true, message: '请输入数据库名' }]}>
            <Input placeholder="voyantest" />
          </Form.Item>
          <Form.Item>
            <Button type="primary" long htmlType="submit" loading={checking}>
              {checking ? '正在测试连接并初始化...' : '测试连接并初始化'}
            </Button>
          </Form.Item>
        </Form>
      </Card>
    </div>
  );
}

export default Setup;
