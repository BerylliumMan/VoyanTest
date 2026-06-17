import React from 'react';
import loadable, { LoadableComponent } from '@loadable/component';
import { Spin } from '@arco-design/web-react';
import styles from '../style/layout.module.less';

// https://github.com/gregberge/loadable-components/pull/226
type LoadableFn = (() => Promise<unknown>) & {
  requireAsync?: () => Promise<unknown>;
};

type LoadableReturn = React.ComponentType<unknown> & {
  preload: () => Promise<unknown>;
};

function load(fn: LoadableFn, options: { fallback?: unknown }): LoadableReturn {
  const Component = (loadable as unknown as <P>(
    loadFn: () => Promise<P>,
    opts: { fallback?: unknown }
  ) => LoadableComponent<P>)(fn as () => Promise<unknown>, options) as unknown as LoadableReturn;

  Component.preload = fn.requireAsync || fn;

  return Component;
}

function LoadingComponent(props: {
  error: boolean;
  timedOut: boolean;
  pastDelay: boolean;
}) {
  if (props.error) {
    console.error(props.error);
    return null;
  }
  return (
    <div className={styles.spin}>
      <Spin />
    </div>
  );
}

export default (loader: LoadableFn) =>
  load(loader, {
    fallback: LoadingComponent({
      pastDelay: true,
      error: false,
      timedOut: false,
    }),
  });
