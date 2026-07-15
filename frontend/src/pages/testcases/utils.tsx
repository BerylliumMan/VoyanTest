import React from 'react';
import {
  Button, Popover, Space,
} from '@arco-design/web-react';
import { Module } from './types';
import styles from './style/components.module.less';

interface TreeNodeData {
  key: string;
  title: React.ReactNode;
  children?: TreeNodeData[];
}

export function findModule(tree: Module[], id: number): Module | null {
  for (const m of tree) {
    if (m.id === id) return m;
    if (m.children) {
      const found = findModule(m.children, id);
      if (found) return found;
    }
  }
  return null;
}

export function findModuleInTree(tree: Module[], id: number): Module | null {
  return findModule(tree, id);
}

export function renderTree(
  mods: Module[],
  selectedModuleId: number | null,
  moduleTree: Module[],
  t: Record<string, string>,
  onEditModule: (mod: Module) => void,
  onDeleteModule: (id: number, name: string) => void,
  onRunModule: (id: number) => void,
): TreeNodeData[] {
  return mods.map((m) => ({
    key: String(m.id),
    title: (
      <span className="module-node-title">
        <span>{m.name}</span>
        {selectedModuleId === m.id && (
          <Popover
            trigger="click"
            position="right"
            content={
              <Space>
                <Button size="mini" type="secondary" onClick={() => {
                  const mod = findModule(moduleTree, m.id);
                  if (mod) onEditModule(mod);
                }}>{t['edit']}</Button>
                <Button size="mini" status="danger" onClick={() => {
                  if (window.confirm(t['confirm.delete.module'].replace('{name}', m.name))) {
                    onDeleteModule(m.id, m.name);
                  }
                }}>{t['delete']}</Button>
                <Button size="mini" type="primary" onClick={() => onRunModule(m.id)}>{t['run.all']}</Button>
              </Space>
            }
          >
            <Button size="mini" type="text" className={styles['tree-trigger']}>▼</Button>
          </Popover>
        )}
      </span>
    ),
    children: m.children ? renderTree(m.children, selectedModuleId, moduleTree, t, onEditModule, onDeleteModule, onRunModule) : undefined,
  }));
}
