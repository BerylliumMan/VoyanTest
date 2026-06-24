import React from 'react';
import { Tabs } from '@arco-design/web-react';
import { IconSettings, IconUser, IconClockCircle } from '@arco-design/web-react/icon';
import useLocale from '@/utils/useLocale';
import AiConfig from './AiConfig';
import UserManagement from './UserManagement';
import ScheduleManagement from './ScheduleManagement';
import styles from './index.module.less';

const { TabPane } = Tabs;

function Settings() {
  const t = useLocale();

  return (
    <div>
      <Tabs defaultActiveTab="ai">
        <TabPane key="ai" title={<><IconSettings className={styles.tabIcon} />{t['ai.config']}</>}>
          <AiConfig />
        </TabPane>
        <TabPane key="users" title={<><IconUser className={styles.tabIcon} />{t['user.mgmt']}</>}>
          <UserManagement />
        </TabPane>
        <TabPane key="schedules" title={<><IconClockCircle className={styles.tabIcon} />{t['schedule.mgmt']}</>}>
          <ScheduleManagement />
        </TabPane>
      </Tabs>
    </div>
  );
}

export default Settings;
