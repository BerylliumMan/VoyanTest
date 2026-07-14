import React from 'react';
import loadable, { LoadableComponent } from '@loadable/component';
import { Spin } from '@arco-design/web-react';
import logger from './logger';
import styles from '../style/layout.module.less';

// https://github.com/gregberge/loadable-components/pull/226
type LoadableFn = (() => Promise<unknown>) & {
  requireAsync?: () => Promise<unknown>;
};

type LoadableReturn = React.ComponentType<unknown> & {
  preload: () => Promise<unknown>;
};

// Widens the strict generic signature to accept a loader returning an
// unknown component shape.
const loadableTyped = loadable as <P>(
  loadFn: () => Promise<P>,
  opts: { fallback?: unknown }
) => LoadableComponent<P>;

function load(fn: LoadableFn, options: { fallback?: unknown }): LoadableReturn {
  const Component = loadableTyped(fn, options);
  const customPreload: () => Promise<unknown> = fn.requireAsync || fn;
  return Object.assign(Component as React.ComponentType<unknown>, { preload: customPreload });
}

function LoadingComponent(props: {
  error: boolean;
  timedOut: boolean;
  pastDelay: boolean;
}) {
  if (props.error) {
    logger.error(props.error);
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
