import React, {useState} from 'react';
import {CheckCircle2, Clock, GitBranch, Pause, Play, AlertTriangle, XCircle, SkipForward} from 'lucide-react';
import {actOrchestration} from './api.js';

const STATUS_LABELS = {
  planning: '规划拆解中',
  running: '协同执行中',
  verifying: '独立交叉审查',
  pausing: '正在暂停',
  paused: '已暂停',
  waiting_approval: '等待审批',
  waiting_quota: '等待配额',
  needs_attention: '需要处理',
  cancelling: '正在停止',
  cancelled: '已停止',
  completed: '协同完成',
  failed: '执行失败',
};

const ROLE_LABELS = {
  planner: '架构规划',
  coder_backend: '后端实现',
  coder_frontend: '前端 UI',
  coder_fast: '轻量编码',
  verifier: '验证测试',
  reviewer: '交叉审查',
};

const STEP_STATUS_ICON = {
  succeeded: CheckCircle2,
  failed: XCircle,
  cancelled: XCircle,
  running: Clock,
  dispatched: Clock,
};

export default function OrchestrationCard({run, onRunUpdated}) {
  const [busyAction, setBusyAction] = useState('');
  const [actionError, setActionError] = useState('');

  if (!run) return null;

  const isTerminal = ['completed', 'failed', 'cancelled'].includes(run.status);
  const canPause = ['planning', 'running', 'verifying'].includes(run.status);
  const canResume = ['paused', 'needs_attention'].includes(run.status);
  const budget = run.budget || {};
  const maxAttempts = budget.max_attempts || 20;
  const maxFix = budget.max_fix_rounds || 2;
  const steps = Array.isArray(run.steps) ? run.steps : [];

  async function triggerAction(action, extra = {}) {
    if (busyAction) return;
    setBusyAction(action);
    setActionError('');
    try {
      const updated = await actOrchestration(run.id, {
        action,
        expected_revision: run.revision || 1,
        request_id: `act_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
        ...extra,
      });
      if (onRunUpdated) onRunUpdated(updated);
    } catch (err) {
      setActionError(err?.message || '操作失败，请刷新状态后重试');
    } finally {
      setBusyAction('');
    }
  }

  return (
    <section className={`orch-card orch-status-${run.status}`} aria-label="多模型协同编排进度">
      <header className="orch-card-head">
        <div className="orch-card-title-row">
          <span className="orch-badge-strategy">
            <GitBranch size={13} aria-hidden="true"/>
            <span>{run.strategy === 'parallel' ? '并行协同' : '串行接力'}</span>
          </span>
          <span className={`orch-badge-status orch-st-${run.status}`}>
            {STATUS_LABELS[run.status] || run.status}
          </span>
          <span className="orch-budget-meta">
            调用 {run.total_attempts || 0}/{maxAttempts} · 返修 {run.fix_rounds || 0}/{maxFix}
          </span>
        </div>

        <div className="orch-card-actions">
          {canPause && (
            <button
              type="button"
              className="orch-act-btn"
              disabled={!!busyAction}
              onClick={() => triggerAction('pause')}
              title="暂停后续步骤派发（当前步骤完成后停下）"
            >
              <Pause size={13} aria-hidden="true"/>
              <span>暂停</span>
            </button>
          )}
          {canResume && (
            <button
              type="button"
              className="orch-act-btn orch-act-primary"
              disabled={!!busyAction}
              onClick={() => triggerAction('resume')}
              title="继续执行剩余步骤"
            >
              <Play size={13} aria-hidden="true"/>
              <span>继续</span>
            </button>
          )}
          {!isTerminal && (
            <button
              type="button"
              className="orch-act-btn orch-act-danger"
              disabled={!!busyAction}
              onClick={() => triggerAction('cancel')}
              title="立即停止整个协同任务及所有子任务"
            >
              <XCircle size={13} aria-hidden="true"/>
              <span>停止</span>
            </button>
          )}
        </div>
      </header>

      {(run.wait_reason || run.terminal_reason) && (
        <div className={`orch-notice ${run.status === 'needs_attention' ? 'is-warn' : ''}`}>
          <AlertTriangle size={14} aria-hidden="true"/>
          <span>{run.wait_reason || run.terminal_reason}</span>
        </div>
      )}

      {actionError && (
        <div className="orch-notice is-warn" role="alert">
          <span>{actionError}</span>
        </div>
      )}

      {steps.length > 0 && (
        <ol className="orch-step-list">
          {steps.map(step => {
            const att = step.latest_attempt;
            const IconComp = STEP_STATUS_ICON[step.status] || Clock;
            const canSkip = !!step.optional && (step.status === 'pending' || step.status === 'ready');
            return (
              <li key={step.id} className={`orch-step-item orch-step-${step.status}`}>
                <div className="orch-step-main">
                  <IconComp size={14} className="orch-step-icon" aria-hidden="true"/>
                  <span className="orch-step-role">{ROLE_LABELS[step.role] || step.role}</span>
                  <span className="orch-step-title">{step.title || step.logical_key}</span>
                </div>
                <div className="orch-step-meta">
                  {att && (
                    <span className="orch-step-model" title={att.route_reason || ''}>
                      {att.engine}{att.model ? ` · ${att.model}` : ''}
                    </span>
                  )}
                  <span className={`orch-step-state st-${step.status}`}>{step.status}</span>
                  {canSkip && (
                    <button
                      type="button"
                      className="orch-skip-btn"
                      disabled={!!busyAction}
                      onClick={() => triggerAction('skip_optional', {step_id: step.id})}
                      title="跳过此可选步骤"
                    >
                      <SkipForward size={12} aria-hidden="true"/>
                      <span>跳过</span>
                    </button>
                  )}
                </div>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}
