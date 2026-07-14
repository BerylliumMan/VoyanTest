import React from 'react';
import { Typography } from '@arco-design/web-react';
import { useSelector } from 'react-redux';
import { GlobalState } from '../../store';
import useLocale from '@/utils/useLocale';
import styles from './style/color-panel.module.less';

function ColorPanel() {
  const settings = useSelector((state: GlobalState) => state.settings);
  const locale = useLocale();
  const themeColor = settings?.themeColor ?? '#165DFF';

  return (
    <div>
      <div className={styles.input}>
        <div
          className={styles.color}
          style={{ backgroundColor: themeColor }}
        />
        <span>{themeColor}</span>
      </div>
      <Typography.Paragraph style={{ fontSize: 12 }}>
        {locale['settings.color.tooltip']}
      </Typography.Paragraph>
    </div>
  );
}

export default ColorPanel;
