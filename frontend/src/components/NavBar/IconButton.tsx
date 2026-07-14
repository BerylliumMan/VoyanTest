import React, { forwardRef, Ref } from 'react';
import { Button } from '@arco-design/web-react';
import styles from './style/icon-button.module.less';
import cs from 'classnames';

interface IconButtonProps {
  icon?: React.ReactNode;
  className?: string;
  onClick?: React.MouseEventHandler<HTMLButtonElement>;
  'aria-label'?: string;
}

function IconButton(props: IconButtonProps, ref: Ref<unknown>) {
  const { icon, className, onClick, 'aria-label': ariaLabel } = props;

  return (
    <Button
      ref={ref as React.Ref<HTMLButtonElement>}
      icon={icon}
      shape="circle"
      type="secondary"
      className={cs(styles['icon-button'], className)}
      onClick={onClick as ((e: Event) => void) | undefined}
      aria-label={ariaLabel}
    />
  );
}

export default forwardRef(IconButton);
