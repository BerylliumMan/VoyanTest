import React from 'react';
import {
  Card, Select, Button, Tree, Space, Popover,
} from '@arco-design/web-react';
import { IconFolder, IconSettings, IconPlus, IconEdit, IconFolderAdd } from '@arco-design/web-react/icon';
import { Module, Environment, Project } from '../types';
import { renderTree } from '../utils';
import styles from '../style/components.module.less';

interface ModuleTreeProps {
  projects: Project[];
  selectedProject: number | null;
  onProjectChange: (val: number) => void;
  selectedEnvironment: number | null;
  environments: Environment[];
  onEnvironmentChange: (val: number) => void;
  modules: Module[];
  moduleTree: Module[];
  selectedModuleId: number | null;
  onSelectModule: (id: number | null, resetPage?: boolean) => void;
  onCreateModule: () => void;
  onEditModule: (mod: Module) => void;
  onDeleteModule: (id: number, name: string) => void;
  onRunModule: (id: number) => void;
  onRunAll: () => void;
  t: Record<string, string>;
  openCreateEnv: () => void;
  openEnvManage: () => void;
}

const ModuleTree: React.FC<ModuleTreeProps> = ({
  projects,
  selectedProject,
  onProjectChange,
  selectedEnvironment,
  environments,
  onEnvironmentChange,
  modules,
  moduleTree,
  selectedModuleId,
  onSelectModule,
  onCreateModule,
  onEditModule,
  onDeleteModule,
  onRunModule,
  onRunAll,
  t,
  openCreateEnv,
  openEnvManage,
}) => {
  return (
    <div className={styles.sidebar}>
      <div className={styles['icon-row']}>
        <IconFolder className={styles['row-icon']} />
        <Select
          placeholder={t['select.project']}
          className={`testcase-select ${styles['flex-select']}`}
          value={selectedProject ?? undefined}
          onChange={onProjectChange}
          options={projects.map((p) => ({ label: p.name, value: p.id }))}
          showSearch
        />
      </div>
      {selectedProject && (
        <div className={styles['icon-row']}>
          <IconSettings className={styles['row-icon']} />
          <Select
            placeholder={t['environment.select_placeholder']}
            className={`testcase-select ${styles['flex-select']}`}
            value={selectedEnvironment ?? undefined}
            onChange={onEnvironmentChange}
            options={environments.map((e: Environment) => ({ label: e.name, value: e.id }))}
          />
          <Button size="mini" type="secondary" icon={<IconPlus />} onClick={openCreateEnv} aria-label="新建环境" />
          <Button size="mini" type="secondary" icon={<IconEdit />} onClick={openEnvManage} aria-label="管理环境" />
        </div>
      )}
      <Card className={styles['module-card']} title={t['module']} extra={
        <Space>
          <Button size="mini" type="secondary" icon={<IconFolderAdd />} onClick={onCreateModule} aria-label="新建模块" />
        </Space>
      }>
        {selectedProject ? (
          <>
            <div className={`${styles['all-modules-row']} ${selectedModuleId === null ? styles['all-modules-row-active'] : styles['all-modules-row-inactive']}`}
              onClick={() => onSelectModule(null, false)}
              role="button" tabIndex={0}
              onKeyDown={(e) => e.key === 'Enter' && onSelectModule(null, false)}
            >
              <span>{t['all.modules']}</span>
              {selectedModuleId === null && (
                <Popover
                  trigger="click"
                  position="right"
                  content={
                    <Space>
                      <Button size="mini" type="primary" onClick={onRunAll}>{t['run.project.all']}</Button>
                    </Space>
                  }
                >
                  <Button size="mini" type="text" className={styles['dropdown-btn']}>▼</Button>
                </Popover>
              )}
            </div>
            <Tree
              treeData={renderTree(moduleTree, selectedModuleId, moduleTree, t, onEditModule, onDeleteModule, onRunModule)}
              selectedKeys={selectedModuleId ? [String(selectedModuleId)] : []}
              onSelect={(keys) => { onSelectModule(keys[0] ? Number(keys[0]) : null, true); }}
              actionOnClick="select"
            />
          </>
        ) : <div className={styles['empty-hint']}>{t['select.project.first']}</div>}
      </Card>
    </div>
  );
};

export default ModuleTree;
