import React, { useCallback } from 'react';
import {
  Button, Tag, Modal, Input, Spin, Space, Typography, Empty, Form,
} from '@arco-design/web-react';
import {
  IconPlayArrow, IconPause, IconCheck, IconClose, IconLoading,
  IconSync, IconSkipNext, IconEdit, IconStop, IconInfoCircle,
  IconTool,
} from '@arco-design/web-react/icon';
import { useParams, useLocation } from 'react-router-dom';
import useLocale from '@/utils/useLocale';
import styles from './style/index.module.less';
import { StepStatus } from './types';
import StepStatusIcon from './StepStatusIcon';
import { useRunDebug } from './useRunDebug';

const { Text } = Typography;

/* ========== 常量 & 辅助函数 ========== */

const STATUS_COLORS: Record<StepStatus, string> = {
  pending: 'gray',
  running: 'blue',
  passed: 'green',
  failed: 'red',
  skipped: 'orange',
};

const getStatusLabel = (status: StepStatus, t: Record<string, string>): string => {
  switch (status) {
    case 'pending': return t['step.waiting'];
    case 'running': return t['running'];
    case 'passed': return t['passed'];
    case 'failed': return t['failed'];
    case 'skipped': return t['debug.skipped'];
    default: return status;
  }
};

/* ========== 主页面组件 ========== */

const RunDebugPage: React.FC = () => {
  const t = useLocale();

  /* --- URL 参数解析（同时支持路径参数 /:runId 和查询参数 ?runId=） --- */
  const { runId: routeRunId } = useParams<{ runId: string }>();
  const location = useLocation();

  const getUrlParam = useCallback(
    (key: string): string | null => {
      if (key === 'runId' && routeRunId) return routeRunId;
      const params = new URLSearchParams(location.search);
      return params.get(key);
    },
    [routeRunId, location.search],
  );

  const runId = getUrlParam('runId');
  const caseId = getUrlParam('caseId');

  /* --- 业务逻辑（状态/WS/事件/计算属性）由 hook 提供 --- */
  const {
    caseData, steps, phase, wsConnected, wsStatus,
    selectedStepIdx, setSelectedStepIdx,
    loading, pauseReason, pauseStepDesc,
    editVisible, setEditVisible, editForm,
    currentStepIdx, stats, selectedStep,
    handleRetry, handleSkip, handleAbort,
    handleEditOpen, handleEditSubmit,
  } = useRunDebug(runId, caseId, t);

  /* --- 渲染 --- */
  if (loading) {
    return (
      <div className={styles.container}>
          <Spin
            loading
            tip={t['debug.load_case']}
            className={styles['spin-center']}
          />
      </div>
    );
  }

  if (!runId) {
    return (
      <div className={styles.container}>
        <Empty
          icon={<IconInfoCircle className={styles['empty-icon-large']} />}
          description={t['debug.no_runId']}
        />
      </div>
    );
  }

  return (
    <div className={styles.container}>
      {/* ===== 顶部信息栏 ===== */}
      <div className={styles['header-bar']}>
        <div className={styles['header-left']}>
          <span className={styles['case-name']}>
            {caseData?.name || (caseId ? t['debug.case_loading'] : t['debug.live_monitor_title'])}
          </span>
          <span className={styles['run-id']}>run#{runId}</span>
        </div>
        <div className={styles['header-right']}>
          <div className={styles['ws-indicator']}>
            <span
              className={`${styles['ws-dot']} ${
                wsStatus === 'connected' ? styles.connected
                  : wsStatus === 'reconnecting' ? styles.reconnecting
                  : styles.disconnected
              }`}
            />
            {wsStatus === 'connected' ? t['debug.connected']
              : wsStatus === 'reconnecting' ? '重连中…'
              : t['debug.disconnected']}
          </div>
          <Tag
            color={
              phase === 'running'
                ? 'blue'
                : phase === 'paused'
                  ? 'orange'
                  : phase === 'completed'
                    ? 'green'
                    : 'gray'
            }
          >
            {phase === 'running' && <IconLoading spin className={styles['icon-inline']} />}
            {phase === 'running'
              ? t['running']
              : phase === 'paused'
                ? t['debug.paused']
                : phase === 'completed'
                  ? t['debug.completed']
                  : t['debug.idle']}
          </Tag>
        </div>
      </div>

      {/* ===== 主内容区（左右两栏） ===== */}
      <div className={styles['main-content']}>
        {/* --- 左侧：步骤列表 --- */}
        <div className={styles['step-panel']}>
          <div className={styles['step-panel-header']}>
            {t['debug.steps_count'].replace('{count}', String(stats.total))}
          </div>
          <div className={styles['step-list']}>
            {steps.length === 0 && !caseId && (
              <Empty
                icon={<IconInfoCircle className={styles['empty-icon-medium']} />}
                description={t['debug.waiting_data']}
              />
            )}
            {steps.map((step, idx) => {
              const isCurrent = step.status === 'running';
              const isSelected = selectedStepIdx === idx;
              return (
                <div
                  key={step.id || step.step_order}
                  className={`${styles['step-item']} ${
                    isCurrent ? styles.active : ''
                  } ${isSelected ? styles.selected : ''}`}
                  onClick={() => setSelectedStepIdx(idx)}
                  role="button"
                  tabIndex={0}
                  onKeyDown={(e) => e.key === 'Enter' && setSelectedStepIdx(idx)}
                >
                  <StepStatusIcon status={step.status} />
                  <div className={styles['step-content']}>
                    <div className={styles['step-order']}>
                      {t['debug.step_label'].replace('{order}', String(step.step_order))}
                    </div>
                    <div className={styles['step-desc']}>{step.description}</div>
                    {step.healed_selector && (
                      <div className={styles['healed-hint']}>
                        <IconTool /> {t['debug.healed_hint'].replace('{selector}', step.healed_selector)}
                      </div>
                    )}
                  </div>
                  {step.duration != null && (
                    <span className={styles['step-duration']}>
                      {step.duration.toFixed(1)}s
                    </span>
                  )}
                </div>
              );
            })}
          </div>
        </div>

        {/* --- 右侧：详情与日志区 --- */}
        <div className={styles['detail-panel']}>
          {/* 暂停时的控制按钮 */}
          {phase === 'paused' && (
            <div className={styles['control-bar']}>
              <div className={styles['pause-reason']}>
                {t['debug.pause_reason'].replace('{reason}', pauseReason)}
                {pauseStepDesc && ` ${t['debug.pause_step'].replace('{desc}', pauseStepDesc)}`}
              </div>
              <Button
                type="primary"
                className={styles['control-btn']}
                icon={<IconSync />}
                onClick={handleRetry}
              >
                {t['debug.retry']}
              </Button>
              <Button
                type="primary"
                status="warning"
                className={styles['control-btn']}
                icon={<IconSkipNext />}
                onClick={handleSkip}
              >
                {t['debug.skip']}
              </Button>
              <Button
                type="primary"
                status="danger"
                className={styles['control-btn']}
                icon={<IconStop />}
                onClick={handleAbort}
              >
                {t['debug.abort']}
              </Button>
              <Button
                type="outline"
                className={styles['control-btn']}
                icon={<IconEdit />}
                onClick={handleEditOpen}
              >
                {t['edit']}
              </Button>
            </div>
          )}

          {/* 详情头部 */}
          <div className={styles['detail-header']}>
            {selectedStep ? (
              <>
                <StepStatusIcon status={selectedStep.status} />
                <span>{t['debug.step_detail_title'].replace('{order}', String(selectedStep.step_order))}</span>
                {selectedStep.duration != null && (
                  <Tag size="small" color={STATUS_COLORS[selectedStep.status]}>
                    {getStatusLabel(selectedStep.status, t)} ·{' '}
                    {selectedStep.duration.toFixed(1)}s
                  </Tag>
                )}
              </>
            ) : (
              <Space>
                {phase === 'running' && (
                  <IconPlayArrow className={styles['phase-icon-running']} />
                )}
                {phase === 'paused' && (
                  <IconPause className={styles['phase-icon-paused']} />
                )}
                {phase === 'completed' && (
                  <IconCheck className={styles['phase-icon-completed']} />
                )}
                {phase === 'running'
                  ? t['debug.live_monitor']
                  : phase === 'paused'
                    ? t['debug.paused_phase']
                    : phase === 'completed'
                      ? t['debug.result_summary']
                      : t['debug.select_step_prompt']}
              </Space>
            )}
          </div>

          {/* 详情内容 */}
          <div className={styles['detail-body']}>
            {/* 运行中状态 */}
            {phase === 'running' && !selectedStep && (
              <div className={styles['running-state']}>
                <div className={styles['running-spinner']}>
                  <IconLoading spin />
                </div>
                <div className={styles['running-text']}>{t['debug.running_text']}</div>
                {currentStepIdx >= 0 && (
                  <div className={styles['current-step-hint']}>
                    {t['debug.current_step_hint']
                      .replace('{order}', String(steps[currentStepIdx]?.step_order ?? ''))
                      .replace('{desc}', steps[currentStepIdx]?.description ?? '')}
                  </div>
                )}
                {steps.length > 0 && (
                  <div className={styles['summary-stats']}>
                    <div className={styles['stat-item']}>
                      <div className={`${styles['stat-value']} ${styles['stat-passed']}`}>
                        {stats.passed}
                      </div>
                      <div className={styles['stat-label']}>{t['passed']}</div>
                    </div>
                    <div className={styles['stat-item']}>
                      <div className={`${styles['stat-value']} ${styles['stat-failed']}`}>
                        {stats.failed}
                      </div>
                      <div className={styles['stat-label']}>{t['failed']}</div>
                    </div>
                    <div className={styles['stat-item']}>
                      <div className={`${styles['stat-value']} ${styles['stat-pending']}`}>
                        {stats.pending}
                      </div>
                      <div className={styles['stat-label']}>{t['debug.remaining']}</div>
                    </div>
                  </div>
                )}
              </div>
            )}

            {/* 完成状态 */}
            {phase === 'completed' && !selectedStep && (
              <div className={styles['completed-state']}>
                <div className={styles['completed-icon']}>
                  {stats.failed === 0 ? (
                    <IconCheck className={styles['phase-icon-completed']} />
                  ) : (
                    <IconClose className={styles['phase-icon-failed']} />
                  )}
                </div>
                <Text className={styles['completed-state-text']}>
                  {stats.failed === 0 ? `${t['all.passed']}!` : t['debug.completed_with_failure']}
                </Text>
                <div className={styles['summary-stats']}>
                  <div className={styles['stat-item']}>
                    <div className={`${styles['stat-value']} ${styles['stat-passed']}`}>
                      {stats.passed}
                    </div>
                    <div className={styles['stat-label']}>{t['passed']}</div>
                  </div>
                  <div className={styles['stat-item']}>
                    <div className={`${styles['stat-value']} ${styles['stat-failed']}`}>
                      {stats.failed}
                    </div>
                    <div className={styles['stat-label']}>{t['failed']}</div>
                  </div>
                  <div className={styles['stat-item']}>
                    <div className={`${styles['stat-value']} ${styles['stat-pending']}`}>
                      {stats.total}
                    </div>
                    <div className={styles['stat-label']}>{t['debug.total']}</div>
                  </div>
                </div>
              </div>
            )}

            {/* 暂停时无选中步骤 */}
            {phase === 'paused' && !selectedStep && (
              <div className={styles['running-state']}>
                <IconPause className={styles['running-state-icon']} />
                <Text className={styles['pause-hint']}>
                  {t['debug.paused_phase']}{pauseReason}
                </Text>
                <Text className={styles['pause-subhint']}>
                  {t['debug.pause_action_hint']}
                </Text>
              </div>
            )}

            {/* 已选中步骤的详情 */}
            {selectedStep && (
              <>
                <div className={styles['detail-section']}>
                  <Text className={styles['detail-title']}>
                    {t['debug.step_label'].replace('{order}', String(selectedStep.step_order))}
                  </Text>
                  <Text className={styles['detail-desc']}>
                    {selectedStep.description}
                  </Text>
                  {selectedStep.healed_selector && (
                    <div className={styles['healed-hint']}>
                      <IconTool /> {t['debug.healed_hint'].replace('{selector}', selectedStep.healed_selector)}
                    </div>
                  )}
                </div>

                {selectedStep.error && (
                  <div className={styles['detail-subsection']}>
                    <Text className={styles['detail-label-danger']}>
                      {t['debug.error_info']}
                    </Text>
                    <div className={styles['log-error']}>{selectedStep.error}</div>
                  </div>
                )}

                {selectedStep.screenshot_path ? (
                  <div className={styles['screenshot-section']}>
                    <Text className={styles['detail-label']}>
                      {t['debug.screenshot_label']}
                    </Text>
                    <img
                      src={`/${selectedStep.screenshot_path}`}
                      alt={t['debug.screenshot_alt'].replace('{order}', String(selectedStep.step_order))}
                      className={styles['screenshot-img']}
                    />
                  </div>
                ) : (
                  <div className={styles['screenshot-placeholder']}>
                    <IconInfoCircle />
                    <span>{t['debug.no_screenshot']}</span>
                  </div>
                )}

                {selectedStep.logs.length > 0 && (
                  <div className={styles['log-section']}>
                    <Text className={styles['detail-label']}>
                      {t['debug.log_label']}
                    </Text>
                    <div className={styles['log-area']}>
                      {selectedStep.logs.map((log, i) => (
                        <div key={i} className={styles['log-entry']}>
                          {log}
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </>
            )}
          </div>
        </div>
      </div>

      {/* ===== 编辑步骤描述弹窗 ===== */}
      <Modal
        title={t['debug.edit_step_title']}
        visible={editVisible}
        onOk={handleEditSubmit}
        onCancel={() => setEditVisible(false)}
        okText={t['debug.confirm_edit']}
        cancelText={t['cancel']}
        unmountOnExit
        className={styles['edit-modal']}
      >
        <Form form={editForm} layout="vertical">
          <div className={styles['edit-modal-text']}>
            {t['debug.edit_modal_hint']}
          </div>
          <Form.Item
            field="new_description"
            rules={[
              { required: true, message: t['debug.step_desc_required'] },
            ]}
          >
            <Input.TextArea
              placeholder={t['debug.edit_placeholder']}
              autoSize={{ minRows: 3, maxRows: 8 }}
            />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
};

export default RunDebugPage;
