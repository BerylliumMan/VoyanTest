import React from 'react';
import { IconCheck, IconClose, IconLoading, IconArrowRight } from '@arco-design/web-react/icon';
import { StepStatus } from './types';
import styles from './style/index.module.less';

/** 步骤状态图标 */
const StepStatusIcon: React.FC<{ status: StepStatus }> = ({ status }) => {
  switch (status) {
    case 'pending':
      return <span className={`${styles['step-status-icon']} ${styles.pending}`} />;
    case 'running':
      return (
        <span className={`${styles['step-status-icon']} ${styles.running}`}>
          <IconLoading spin />
        </span>
      );
    case 'passed':
      return (
        <span className={`${styles['step-status-icon']} ${styles.passed}`}>
          <IconCheck />
        </span>
      );
    case 'failed':
      return (
        <span className={`${styles['step-status-icon']} ${styles.failed}`}>
          <IconClose />
        </span>
      );
    case 'skipped':
      return (
        <span className={`${styles['step-status-icon']} ${styles.skipped}`}>
          <IconArrowRight />
        </span>
      );
    default:
      return <span className={`${styles['step-status-icon']} ${styles.pending}`} />;
  }
};

export default StepStatusIcon;
